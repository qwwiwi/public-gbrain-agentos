"""gbrain doctor — operator diagnostic CLI for self-hosted gbrain installs.

Single-file, stdlib-first (plus asyncpg + httpx already runtime deps).
Runs 10 checks against the local install and reports pass/warn/fail.

Required environment variables (typically loaded from /etc/gbrain/secrets.env):
    PG_HOST, PG_PORT, PG_DATABASE, PG_USER, PG_PASSWORD
    VAULT_ROOT (default /opt/gbrain/vault)
    FASTEMBED_MODEL (default intfloat/multilingual-e5-large)
    TOKEN_HASH_SALT (optional, used to hash Bearer tokens)

Exit codes:
    0 — no fail-status checks (only pass/warn).
    1 — at least one fail-status check.
    2 — invocation error (bad CLI flag, missing required env, malformed input).

Safety:
    * --fix only applies whitelisted, non-destructive autofixes:
        - CREATE EXTENSION IF NOT EXISTS vector (when DB role has privilege).
        - systemctl restart gbrain-* (only if running as root; otherwise prints
          a sudo hint without executing).
    * Never runs DROP / DELETE / file removal.
    * Bearer tokens are NEVER printed raw. Always masked via `_mask_token` to
      show first 8 + last 4 characters only.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Literal, Sequence

# These imports are runtime deps in pyproject.toml.
import asyncpg
import httpx

# Repo root on sys.path so `services.shared.config` resolves when running as a
# script (python scripts/gbrain_doctor.py) and not just as installed entry point.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

Status = Literal["pass", "warn", "fail", "skip"]


@dataclass
class CheckResult:
    """Outcome of a single doctor check."""

    name: str
    status: Status
    message: str
    remediation: str | None = None
    auto_fix: Callable[[], Awaitable[bool]] | None = None

    def to_serializable(self) -> dict[str, Any]:
        """Return a JSON-safe dict (drops the autofix callable)."""
        data = asdict(self)
        data.pop("auto_fix", None)
        return data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GREEN = 92
_YELLOW = 93
_RED = 91
_BOLD = 1
_DIM = 2


def _color(text: str, code: int, *, use_color: bool = True) -> str:
    """Wrap text in an ANSI escape sequence, or return plain when disabled."""
    if not use_color:
        return text
    return f"\033[{code}m{text}\033[0m"


def _status_tag(status: Status, *, use_color: bool = True) -> str:
    """Format the bracketed status tag for table output."""
    mapping = {
        "pass": ("[PASS]", _GREEN),
        "warn": ("[WARN]", _YELLOW),
        "fail": ("[FAIL]", _RED),
        "skip": ("[SKIP]", _DIM),
    }
    label, code = mapping[status]
    return _color(label, code, use_color=use_color)


def _mask_token(token: str) -> str:
    """Return ``<first8>...<last4>`` for the token; ``***`` when too short.

    Bearer values are sensitive — never print raw. Anything shorter than 13
    chars (8 + ellipsis + 4 = 13 min input we accept for partial reveal)
    collapses to ``***``.
    """
    if not isinstance(token, str) or len(token) < 13:
        return "***"
    return f"{token[:8]}...{token[-4:]}"


def _hash_token(raw: str, salt: str) -> str:
    """Compute sha256(salt + raw) lowercase hex — matches agent_tokens schema."""
    return hashlib.sha256((salt + raw).encode("utf-8")).hexdigest()


def _expected_cron_entries() -> list[str]:
    """Canonical expected cron substrings (single source of truth)."""
    return [
        "gbrain-ingest-worker",  # services.ingest_worker
        "inbox-agent/scripts/compile.sh",
        "inbox-agent/scripts/daily-digest.sh",
    ]


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

async def check_postgres_reachable(pg_kwargs: dict[str, Any]) -> tuple[CheckResult, asyncpg.Connection | None]:
    """Open a one-shot connection. Returns the connection for re-use, or None."""
    try:
        conn = await asyncpg.connect(**pg_kwargs)
    except Exception as exc:  # noqa: BLE001 — surface every connect failure
        return (
            CheckResult(
                name="postgres_reachable",
                status="fail",
                message=f"cannot connect: {exc}",
                remediation="Verify PG_HOST/PG_PORT/PG_USER/PG_PASSWORD in /etc/gbrain/secrets.env, then rerun scripts/install.sh.",
            ),
            None,
        )
    return (
        CheckResult(
            name="postgres_reachable",
            status="pass",
            message=f"connected to {pg_kwargs.get('database')}",
        ),
        conn,
    )


async def check_pgvector_extension(conn: asyncpg.Connection) -> CheckResult:
    """Ensure the `vector` extension is installed in the current DB."""
    row = await conn.fetchrow(
        "SELECT extname FROM pg_extension WHERE extname = $1", "vector"
    )
    if row:
        return CheckResult(
            name="pgvector_extension",
            status="pass",
            message="extension 'vector' installed",
        )

    async def _autofix() -> bool:
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            return True
        except Exception:  # noqa: BLE001
            return False

    return CheckResult(
        name="pgvector_extension",
        status="fail",
        message="extension 'vector' missing",
        remediation="Run `CREATE EXTENSION vector;` as a superuser, or pass --fix.",
        auto_fix=_autofix,
    )


async def check_schema_tables(conn: asyncpg.Connection) -> CheckResult:
    """Ensure all 7 expected tables exist in the public schema."""
    expected = {
        "agent_tokens",
        "documents",
        "chunks",
        "slots",
        "delivery_outbox",
        "audit_log",
        "embedding_jobs",
    }
    rows = await conn.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    )
    present = {r["tablename"] for r in rows}
    missing = expected - present
    if missing:
        return CheckResult(
            name="schema_tables",
            status="fail",
            message=f"missing tables: {sorted(missing)}",
            remediation="Run scripts/install.sh to apply migrations.",
        )
    return CheckResult(
        name="schema_tables",
        status="pass",
        message=f"all {len(expected)} expected tables present",
    )


async def check_agent_tokens(conn: asyncpg.Connection) -> CheckResult:
    """At least one non-revoked agent_tokens row must exist."""
    count = await conn.fetchval(
        "SELECT count(*) FROM agent_tokens WHERE revoked_at IS NULL"
    )
    if not count:
        return CheckResult(
            name="agent_tokens",
            status="fail",
            message="no active agent tokens",
            remediation="Run `python scripts/issue-agent-token.py <agent>` to mint one.",
        )
    return CheckResult(
        name="agent_tokens",
        status="pass",
        message=f"count={count}",
    )


async def check_bearer_mapping(
    conn: asyncpg.Connection,
    mcp_json_path: Path,
    token_hash_salt: str,
) -> CheckResult:
    """Map each Bearer in `~/.mcp.json` to an agent_tokens row.

    Output NEVER includes the raw token — only `_mask_token(...)` prefix and
    matched agent name (or `<unmatched>`).
    """
    if not mcp_json_path.exists():
        return CheckResult(
            name="bearer_mapping",
            status="warn",
            message=f"{mcp_json_path} not found — skipping",
            remediation="Place a client `.mcp.json` here or pass --mcp-json PATH.",
        )

    try:
        raw = mcp_json_path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult(
            name="bearer_mapping",
            status="fail",
            message=f"cannot read/parse {mcp_json_path}: {exc}",
            remediation="Validate the JSON syntax.",
        )

    bearers: list[str] = []
    servers = parsed.get("mcpServers") or parsed.get("servers") or {}
    if isinstance(servers, dict):
        for _name, spec in servers.items():
            if not isinstance(spec, dict):
                continue
            headers = spec.get("headers") or {}
            if not isinstance(headers, dict):
                continue
            auth = headers.get("Authorization") or headers.get("authorization")
            if isinstance(auth, str) and auth.lower().startswith("bearer "):
                bearers.append(auth.split(" ", 1)[1].strip())

    if not bearers:
        return CheckResult(
            name="bearer_mapping",
            status="warn",
            message="no Bearer tokens in .mcp.json",
            remediation="Add a Bearer header for at least one MCP server.",
        )

    unmatched: list[str] = []
    matched: list[tuple[str, str]] = []
    for token in bearers:
        digest = _hash_token(token, token_hash_salt)
        row = await conn.fetchrow(
            "SELECT agent FROM agent_tokens WHERE token_sha256 = $1 AND revoked_at IS NULL",
            digest,
        )
        if row:
            matched.append((_mask_token(token), row["agent"]))
        else:
            unmatched.append(_mask_token(token))

    detail_parts: list[str] = [f"{mask}->{agent}" for mask, agent in matched]
    detail_parts.extend(f"{mask}->[unmatched]" for mask in unmatched)
    detail = ", ".join(detail_parts)

    if unmatched:
        return CheckResult(
            name="bearer_mapping",
            status="warn",
            message=f"{len(unmatched)} unmatched / {len(bearers)} total: {detail}",
            remediation="Reissue tokens via scripts/issue-agent-token.py or rotate stale entries.",
        )
    return CheckResult(
        name="bearer_mapping",
        status="pass",
        message=detail,
    )


def check_vault_root_writable(vault_root: Path) -> CheckResult:
    """Ensure VAULT_ROOT exists and is writable (probe touch + unlink)."""
    if not vault_root.exists():
        return CheckResult(
            name="vault_root_writable",
            status="fail",
            message=f"{vault_root} does not exist",
            remediation="Create the directory or fix VAULT_ROOT env.",
        )
    probe = vault_root / ".gbrain-doctor-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return CheckResult(
            name="vault_root_writable",
            status="fail",
            message=f"cannot write to {vault_root}: {exc}",
            remediation="chown / chmod the directory to the gbrain service user.",
        )
    return CheckResult(
        name="vault_root_writable",
        status="pass",
        message=f"{vault_root} writable",
    )


def _default_mcp_ports() -> tuple[int, int, int]:
    """Read MCP ports from env vars with .env.example defaults.

    Mirrors the install / smoke-test scripts so custom installs don't
    false-fail when MCP_MEMORY_PORT / MCP_RECALL_PORT / MCP_SWARM_PORT
    are overridden (H3).
    """
    def _port(name: str, default: int) -> int:
        raw = os.environ.get(name)
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    return (
        _port("MCP_MEMORY_PORT", 8767),
        _port("MCP_RECALL_PORT", 8768),
        _port("MCP_SWARM_PORT", 8766),
    )


async def check_mcp_livez(
    ports: Sequence[int] | None = None,
) -> CheckResult:
    """Probe local MCP /livez endpoints on the documented ports.

    H3: reads ports from MCP_MEMORY_PORT / MCP_RECALL_PORT / MCP_SWARM_PORT
    env vars (default 8767/8768/8766). M8: realizes ``ports`` into a tuple
    on entry so the "responding" success message never sees an exhausted
    iterator.
    """
    if ports is None:
        ports = _default_mcp_ports()
    # M8: materialize the sequence so re-iteration in the success message
    # always shows the real count.
    ports = tuple(ports)
    failed: list[str] = []
    async with httpx.AsyncClient(timeout=2.0) as client:
        for port in ports:
            url = f"http://127.0.0.1:{port}/livez"
            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    failed.append(f"{port}:HTTP{resp.status_code}")
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{port}:{type(exc).__name__}")

    if failed:
        async def _autofix() -> bool:
            if os.geteuid() != 0:
                return False
            if not shutil.which("systemctl"):
                return False
            try:
                subprocess.run(
                    [
                        "systemctl",
                        "restart",
                        "gbrain-memory-mcp",
                        "gbrain-recall-mcp",
                        "gbrain-swarm-mcp",
                    ],
                    check=True,
                    timeout=30,
                )
                return True
            except Exception:  # noqa: BLE001
                return False

        return CheckResult(
            name="mcp_livez",
            status="fail",
            message=f"unreachable: {', '.join(failed)}",
            remediation="sudo systemctl restart gbrain-memory-mcp gbrain-recall-mcp gbrain-swarm-mcp",
            auto_fix=_autofix,
        )
    return CheckResult(
        name="mcp_livez",
        status="pass",
        message=f"all {len(ports)} MCP servers responding",
    )


def check_fastembed_cache(model_name: str) -> CheckResult:
    """Warn-only: probe ~/.cache/fastembed/<model>/ for at least one .onnx."""
    cache_dir = Path.home() / ".cache" / "fastembed"
    if not cache_dir.exists():
        return CheckResult(
            name="fastembed_cache",
            status="warn",
            message=f"{cache_dir} missing; first call will download {model_name}",
        )
    # Best-effort: scan for any subdir matching the safe model slug.
    safe_slug = model_name.replace("/", "_")
    candidates = [
        cache_dir / model_name,
        cache_dir / safe_slug,
    ]
    for cand in candidates:
        if cand.exists():
            onnx = list(cand.glob("**/*.onnx"))
            if onnx:
                return CheckResult(
                    name="fastembed_cache",
                    status="pass",
                    message=f"{len(onnx)} onnx file(s) under {cand}",
                )
    return CheckResult(
        name="fastembed_cache",
        status="warn",
        message=f"no onnx artefacts under {cache_dir} for {model_name}",
    )


def check_cron(expected: Iterable[str] | None = None) -> CheckResult:
    """Warn-only: verify expected substrings appear in `crontab -l` output."""
    expected_list = list(expected or _expected_cron_entries())
    if not shutil.which("crontab"):
        return CheckResult(
            name="cron",
            status="skip",
            message="crontab binary not on PATH",
        )
    try:
        result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=5
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="cron",
            status="warn",
            message=f"crontab -l failed: {exc}",
        )
    if result.returncode != 0:
        return CheckResult(
            name="cron",
            status="warn",
            message="no crontab for current user",
            remediation="Run `crontab -e` and add gbrain ingest + inbox-agent entries.",
        )
    missing = [pat for pat in expected_list if pat not in result.stdout]
    if missing:
        return CheckResult(
            name="cron",
            status="warn",
            message=f"missing entries: {missing}",
            remediation="Add the missing lines via `crontab -e`.",
        )
    return CheckResult(
        name="cron",
        status="pass",
        message="all expected cron entries present",
    )


async def check_hmac_secret_health(
    conn: asyncpg.Connection,
    secrets_json: str | None,
) -> CheckResult:
    """Verify every DB-registered HMAC agent has a matching raw secret in env.

    For each ``agent_tokens`` row with ``hmac_secret_sha256 IS NOT NULL`` and
    ``revoked_at IS NULL``, we look up the agent in the env-mounted
    ``GBRAIN_HMAC_SECRETS_JSON={"agent":"raw_secret"}`` map and compare
    ``sha256(raw_secret)`` against the stored hash.

    Status semantics:
      * ``skip`` — no HMAC rows in the DB (HMAC auth is opt-in, this is fine).
      * ``pass`` — every DB HMAC agent has a matching env secret.
      * ``warn`` — at least one DB HMAC agent is missing in env (HMAC auth
        will fail closed for that agent; Bearer continues to work).
      * ``fail`` — at least one env secret hashes to something other than the
        stored value (DB and env are out of sync — likely a rotation gap).

    The raw env secret is NEVER printed in any output path. Only agent names
    and 12-char sha256 prefixes appear in the message.
    """
    try:
        rows = await conn.fetch(
            "SELECT agent, hmac_secret_sha256 FROM agent_tokens "
            "WHERE hmac_secret_sha256 IS NOT NULL AND revoked_at IS NULL"
        )
    except Exception as exc:  # noqa: BLE001
        # H5: DB query failure is fail, not warn — operator must fix
        # connectivity before trusting any HMAC posture.
        return CheckResult(
            name="hmac_secret_health",
            status="fail",
            message=f"query failed: {exc}",
            remediation="Verify migration 004_hmac_secrets.sql has been applied.",
        )

    # H5: parse env JSON BEFORE the no-rows short-circuit so a typo in
    # GBRAIN_HMAC_SECRETS_JSON that orphans an env-only agent surfaces
    # even when the DB has zero HMAC rows yet.
    env_map: dict[str, str] = {}
    parse_error: str | None = None
    if secrets_json and secrets_json.strip() and secrets_json.strip() != "{}":
        try:
            parsed = json.loads(secrets_json)
            if not isinstance(parsed, dict):
                parse_error = "GBRAIN_HMAC_SECRETS_JSON must be a JSON object"
            else:
                # Coerce to str/str; reject non-string values silently (they
                # cannot hash anyway). NEVER store raw values past this loop.
                for k, v in parsed.items():
                    if isinstance(k, str) and isinstance(v, str):
                        env_map[k] = v
        except json.JSONDecodeError as exc:
            parse_error = f"GBRAIN_HMAC_SECRETS_JSON parse error: {exc.msg}"

    if parse_error:
        # H5: JSON parse failure is fail, not warn — an unparseable env
        # means HMAC auth is silently broken for every agent.
        return CheckResult(
            name="hmac_secret_health",
            status="fail",
            message=parse_error,
            remediation="Set GBRAIN_HMAC_SECRETS_JSON to a JSON object "
            "mapping agent → raw secret.",
        )

    db_agents = {row["agent"] for row in rows}
    env_agents = set(env_map)

    if not rows:
        # No DB rows. If env carries agents that have no DB row, surface
        # that — a typo in the env key would otherwise go unnoticed.
        unknown_in_env = sorted(env_agents)
        if unknown_in_env:
            return CheckResult(
                name="hmac_secret_health",
                status="warn",
                message=(
                    f"GBRAIN_HMAC_SECRETS_JSON carries {len(unknown_in_env)} "
                    f"agent(s) with no DB row: {unknown_in_env}"
                ),
                remediation=(
                    "Either run scripts/issue-hmac-secret.py for each agent, "
                    "or remove the orphaned key(s) from GBRAIN_HMAC_SECRETS_JSON."
                ),
            )
        return CheckResult(
            name="hmac_secret_health",
            status="skip",
            message="no HMAC-enabled agents in agent_tokens",
        )

    missing: list[str] = []
    mismatched: list[tuple[str, str]] = []  # (agent, stored_prefix)
    matched: list[str] = []

    for row in rows:
        agent = row["agent"]
        stored_hash = row["hmac_secret_sha256"]
        raw = env_map.get(agent)
        if raw is None:
            missing.append(agent)
            continue
        # Compute sha256 of the env raw secret; NEVER log `raw` past here.
        computed = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if computed == stored_hash:
            matched.append(agent)
        else:
            mismatched.append((agent, stored_hash[:12]))

    if mismatched:
        detail = ", ".join(
            f"{agent}(db={prefix}...)" for agent, prefix in mismatched
        )
        return CheckResult(
            name="hmac_secret_health",
            status="fail",
            message=(
                f"{len(mismatched)} agent(s) with hash mismatch: {detail}"
            ),
            remediation=(
                "DB and env disagree — re-run "
                "`scripts/issue-hmac-secret.py --agent <name> --rotate` "
                "and update GBRAIN_HMAC_SECRETS_JSON, or restore the previous "
                "env value if rotation was unintentional."
            ),
        )

    # H5: detect orphans in BOTH directions:
    #   - missing  = DB has agent but env does not (existing check)
    #   - unknown_in_env = env has agent but DB does not (NEW)
    unknown_in_env = sorted(env_agents - db_agents)

    if missing or unknown_in_env:
        parts: list[str] = []
        if missing:
            parts.append(f"missing in env: {sorted(missing)}")
        if unknown_in_env:
            parts.append(f"unknown agent(s) in env (no DB row): {unknown_in_env}")
        return CheckResult(
            name="hmac_secret_health",
            status="warn",
            message=(
                f"matched={len(matched)}/{len(rows)}; " + "; ".join(parts)
            ),
            remediation=(
                "Add missing agent(s) to GBRAIN_HMAC_SECRETS_JSON or remove "
                "orphan key(s). Re-run `scripts/issue-hmac-secret.py --agent "
                "<name> --rotate` if the raw secret was lost."
            ),
        )

    return CheckResult(
        name="hmac_secret_health",
        status="pass",
        message=f"{len(matched)} HMAC agent(s) healthy: {sorted(matched)}",
    )


async def check_embedding_queue_depth(conn: asyncpg.Connection) -> CheckResult:
    """Warn-only: count pending embedding_jobs rows."""
    try:
        depth = await conn.fetchval(
            "SELECT count(*) FROM embedding_jobs WHERE processed_at IS NULL"
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="embedding_queue_depth",
            status="warn",
            message=f"query failed: {exc}",
        )
    if depth and depth > 1000:
        return CheckResult(
            name="embedding_queue_depth",
            status="fail",
            message=f"pending={depth} (>1000)",
            remediation="Inspect services.ingest_worker logs; FastEmbed may be stuck.",
        )
    if depth and depth > 100:
        return CheckResult(
            name="embedding_queue_depth",
            status="warn",
            message=f"pending={depth} (>100)",
        )
    return CheckResult(
        name="embedding_queue_depth",
        status="pass",
        message=f"pending={depth or 0}",
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def run_all_checks(args: argparse.Namespace) -> list[CheckResult]:
    """Execute all checks in declared order; return aggregated results."""
    results: list[CheckResult] = []

    pg_kwargs: dict[str, Any] = {
        "host": os.environ.get("PG_HOST", "/var/run/postgresql"),
        "database": os.environ.get("PG_DATABASE", "gbrain"),
        "user": os.environ.get("PG_USER", "gbrain"),
        "password": os.environ.get("PG_PASSWORD", ""),
    }
    if not pg_kwargs["host"].startswith("/"):
        port_raw = os.environ.get("PG_PORT", "5432")
        try:
            pg_kwargs["port"] = int(port_raw)
        except (TypeError, ValueError) as exc:
            # M4: defensive parsing -- bad PG_PORT becomes a controlled
            # RuntimeError (exit 2 via main()) instead of a traceback.
            raise RuntimeError(
                f"PG_PORT must be an integer, got: {port_raw!r}"
            ) from exc

    pg_result, conn = await check_postgres_reachable(pg_kwargs)
    results.append(pg_result)

    if conn is None:
        # Downstream DB checks are skipped — emit explicit skip rows so the
        # JSON contract stays predictable for operators.
        for name in (
            "pgvector_extension",
            "schema_tables",
            "agent_tokens",
            "bearer_mapping",
            "hmac_secret_health",
            "embedding_queue_depth",
        ):
            results.append(
                CheckResult(
                    name=name,
                    status="skip",
                    message="postgres unavailable",
                )
            )
    else:
        try:
            results.append(await check_pgvector_extension(conn))
            results.append(await check_schema_tables(conn))
            results.append(await check_agent_tokens(conn))
            results.append(
                await check_bearer_mapping(
                    conn,
                    Path(args.mcp_json).expanduser(),
                    os.environ.get("TOKEN_HASH_SALT", ""),
                )
            )
            results.append(
                await check_hmac_secret_health(
                    conn,
                    os.environ.get("GBRAIN_HMAC_SECRETS_JSON"),
                )
            )
        finally:
            # embedding queue is the last DB-touching check before close.
            try:
                results.append(await check_embedding_queue_depth(conn))
            finally:
                await conn.close()

    vault_root = Path(os.environ.get("VAULT_ROOT", "/opt/gbrain/vault")).expanduser()
    results.append(check_vault_root_writable(vault_root))
    results.append(await check_mcp_livez())
    results.append(
        check_fastembed_cache(
            os.environ.get("FASTEMBED_MODEL", "intfloat/multilingual-e5-large")
        )
    )
    results.append(check_cron())
    return results


def render_table(results: list[CheckResult], *, use_color: bool) -> None:
    """Pretty-print results as a left-aligned table."""
    name_width = max((len(r.name) for r in results), default=10)
    for r in results:
        tag = _status_tag(r.status, use_color=use_color)
        print(f"{tag} {r.name.ljust(name_width)}  {r.message}")
        if r.status in ("fail", "warn") and r.remediation:
            hint = _color(f"        ↳ {r.remediation}", _DIM, use_color=use_color)
            print(hint)


def render_json(results: list[CheckResult]) -> None:
    """Emit results as a JSON array. Excludes the autofix callable."""
    print(json.dumps([r.to_serializable() for r in results], indent=2))


def _confirm_autofix(prompt: str) -> bool:
    """Interactive y/N prompt to stderr (default no).

    H2: ``--fix`` may restart services. Without ``--yes``, the operator
    must explicitly confirm. Returns False when stdin is non-interactive
    or input is anything other than "y" / "yes" (case-insensitive).
    """
    if not sys.stdin.isatty():
        return False
    print(prompt, file=sys.stderr, end="", flush=True)
    try:
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("", file=sys.stderr)
        return False
    return answer in ("y", "yes")


async def maybe_autofix(
    results: list[CheckResult],
    args: argparse.Namespace,
) -> None:
    """Apply safe autofixes for fail-status checks when --fix is passed.

    H1: autofix log lines always go to stderr so ``--json`` stdout
    remains machine-readable. M7: autofix events always emit regardless
    of ``--quiet`` (audit trail). H2: when an autofix triggers a service
    restart, require interactive confirmation unless ``--yes`` is set.
    """
    if not args.fix:
        return
    fixed = 0
    failed = 0
    for r in results:
        if r.status != "fail" or r.auto_fix is None:
            continue

        # H2: mcp_livez autofix restarts services -- require confirmation.
        if r.name == "mcp_livez" and not getattr(args, "yes", False):
            if not _confirm_autofix(
                "[fix] mcp_livez autofix will run "
                "`systemctl restart gbrain-{memory,recall,swarm}-mcp`. "
                "Continue? [y/N] "
            ):
                print(
                    f"[fix] {r.name}: skipped (operator declined)",
                    file=sys.stderr,
                )
                continue

        print(f"[fix] running autofix for {r.name}...", file=sys.stderr)
        ok = False
        try:
            ok = await r.auto_fix()
        except Exception as exc:  # noqa: BLE001
            print(f"[fix] {r.name} raised {exc!r}", file=sys.stderr)
        print(
            f"[fix] {r.name}: {'ok' if ok else 'failed'}",
            file=sys.stderr,
        )
        if ok:
            fixed += 1
        else:
            failed += 1

    if fixed or failed:
        print(
            f"[fix] summary: {fixed} fixed, {failed} failed",
            file=sys.stderr,
        )


def compute_exit_code(results: list[CheckResult]) -> int:
    """1 if any fail; otherwise 0 (warn + pass + skip are all acceptable)."""
    return 1 if any(r.status == "fail" for r in results) else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser. `doctor` subcommand is positional-optional
    so both `gbrain-doctor` and `gbrain doctor` invocations work."""
    parser = argparse.ArgumentParser(
        prog="gbrain-doctor",
        description="Diagnostic CLI for a self-hosted gbrain install.",
    )
    parser.add_argument(
        "subcommand",
        nargs="?",
        default="doctor",
        choices=["doctor"],
        help="Subcommand (currently only `doctor`).",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Apply whitelisted safe autofixes for failing checks.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit results as a JSON array.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Silence stdout; rely on exit code only.",
    )
    parser.add_argument(
        "--mcp-json",
        default=str(Path.home() / ".mcp.json"),
        help="Path to a client .mcp.json (default: ~/.mcp.json).",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colors in default table output.",
    )
    parser.add_argument(
        "--yes",
        "--noninteractive",
        action="store_true",
        dest="yes",
        help="Skip --fix interactive confirmation prompts (for CI).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for `gbrain-doctor` console script."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        results = asyncio.run(run_all_checks(args))
    except RuntimeError as exc:
        print(f"gbrain-doctor: config error: {exc}", file=sys.stderr)
        return 2

    asyncio.run(maybe_autofix(results, args))

    if args.quiet:
        return compute_exit_code(results)
    if args.json:
        render_json(results)
    else:
        # M6: respect NO_COLOR env (https://no-color.org) + skip color when
        # stdout is not a tty (avoids ANSI noise when piping to a file).
        use_color = (
            (not args.no_color)
            and "NO_COLOR" not in os.environ
            and sys.stdout.isatty()
        )
        render_table(results, use_color=use_color)
    return compute_exit_code(results)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
