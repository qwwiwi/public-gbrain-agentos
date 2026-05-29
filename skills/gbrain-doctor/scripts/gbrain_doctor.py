#!/usr/bin/env python3
"""gbrain-doctor — agent-facing diagnostic CLI for an Orgrimmar agent's gbrain setup.

Runs grouped read-only checks (MCP connectivity/identity, swarm, recall,
memory, hooks parity, webhooks, topology/security, GitHub repo, skill install)
against the agent's own machine and reports pass/warn/fail/skip.

Distinct from the server-side ``scripts/gbrain_doctor.py`` (which inspects the
VPS install). This one runs from an agent workspace and talks to the gbrain
MCP endpoints over streamable-http.

Exit codes:
    0 — no ``fail`` results (warn/skip allowed).
    1 — at least one ``fail`` result.
    2 — invocation/config error (Python <3.12, bad ``--group``, unreadable
        explicit ``--mcp-json`` / ``--expected-hooks``).

Safety:
    * Default run is read-only. No mutating MCP tools are called.
    * ``--fix`` only invokes whitelisted local autofix callbacks attached by
      the check modules (chmod +x, symlink, gh repo create), each behind a
      stderr confirmation prompt unless ``--yes``.
    * Every message and every diagnostic line passes through ``redact()``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# Ensure sibling modules resolve whether this file is run directly
# (``python3 .../gbrain_doctor.py``) or imported as part of a package. We add
# the script's own directory to sys.path so flat ``import result`` works.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import redact  # noqa: E402  (path shim must run first)
import result as result_mod  # noqa: E402
from result import CheckResult  # noqa: E402

logger = logging.getLogger("gbrain_doctor")

# Repo root: skills/gbrain-doctor/scripts/ -> up 3 = repo checkout.
_REPO_ROOT = _SCRIPT_DIR.parent.parent.parent
_SKILL_ROOT = _SCRIPT_DIR.parent

VALID_GROUPS = {f"G{i}" for i in range(1, 11)}

# Map a URL path segment to the canonical service name. gbrain uses ``/task/``
# in the URL but the service is conceptually ``tasks``.
_PATH_TO_SERVICE = {
    "memory": "memory",
    "recall": "recall",
    "swarm": "swarm",
    "task": "tasks",
    "tasks": "tasks",
}


# ---------------------------------------------------------------------------
# Config model
# ---------------------------------------------------------------------------


@dataclass
class McpServer:
    """A single gbrain MCP server entry resolved from ``.mcp.json``."""

    key: str
    service: str  # "memory" | "recall" | "swarm" | "tasks"
    url: str
    token: str  # raw; kept in memory only, never printed un-masked
    type: str  # expected "http"


def _infer_service(url: str) -> str | None:
    """Infer the canonical service name from an MCP URL path.

    Args:
        url: The server URL, e.g. ``https://host/swarm/mcp``.

    Returns:
        ``"memory"`` / ``"recall"`` / ``"swarm"`` / ``"tasks"`` or ``None``
        when no known segment is present.
    """
    # Split path into segments and look for a known service token. We check
    # the segment immediately before a trailing ``mcp`` first, then any match.
    try:
        from urllib.parse import urlparse

        path = urlparse(url).path
    except Exception:  # noqa: BLE001 — malformed URL handled by caller
        path = url
    segments = [s for s in path.split("/") if s]
    for seg in segments:
        if seg in _PATH_TO_SERVICE:
            return _PATH_TO_SERVICE[seg]
    return None


def _infer_service_from_key(key: str) -> str | None:
    """Infer the canonical service name from an MCP server key (config name).

    Fallback for port-based topologies where every server shares the same
    ``/mcp`` path and the service is encoded only in the config key, e.g.
    ``gbrain-memory`` / ``gbrain-recall`` / ``gbrain-swarm`` / ``gbrain-task``.

    Args:
        key: The mcpServers entry name from ``.mcp.json``.

    Returns:
        ``"memory"`` / ``"recall"`` / ``"swarm"`` / ``"tasks"`` or ``None``
        when no known service token is present in the key.
    """
    for token in re.split(r"[^a-z0-9]+", key.lower()):
        if token in _PATH_TO_SERVICE:
            return _PATH_TO_SERVICE[token]
    return None


def load_mcp_config(
    mcp_json_path: str | None, agent: str | None
) -> tuple[list[McpServer], Path]:
    """Load and parse the agent's ``.mcp.json`` into ``McpServer`` records.

    Resolution order:
        1. ``--mcp-json`` (explicit; unreadable -> hard config error)
        2. ``~/.claude-lab/<agent>/.claude/.mcp.json`` (when ``agent`` set)
        3. ``./.mcp.json``
        4. ``~/.mcp.json``

    Args:
        mcp_json_path: Explicit override path, or ``None``.
        agent: Canonical agent id, or ``None``.

    Returns:
        A tuple ``(servers, resolved_path)``. ``servers`` may be empty when no
        config exists at the resolved fallback paths (the connectivity checks
        then report this). ``resolved_path`` is the path that was used (or the
        first candidate when none existed).

    Raises:
        SystemExit: With code 2 when an explicit ``--mcp-json`` path is
            missing or unparseable (per CLI contract). Fallback paths that do
            not exist are NOT fatal — they yield an empty server list.
    """
    candidates: list[Path] = []
    explicit = mcp_json_path is not None
    if mcp_json_path:
        candidates.append(Path(mcp_json_path).expanduser())
    else:
        if agent:
            candidates.append(
                Path.home() / ".claude-lab" / agent / ".claude" / ".mcp.json"
            )
        candidates.append(Path.cwd() / ".mcp.json")
        candidates.append(Path.home() / ".mcp.json")

    resolved: Path | None = None
    raw: str | None = None
    for cand in candidates:
        if cand.is_file():
            try:
                raw = cand.read_text(encoding="utf-8")
            except OSError as exc:
                if explicit:
                    _die_config(f"cannot read --mcp-json {cand}: {exc}")
                continue
            resolved = cand
            break

    if resolved is None:
        if explicit:
            _die_config(f"--mcp-json path not found: {candidates[0]}")
        # No config found at any fallback — return empty; checks report it.
        return [], candidates[0]

    try:
        parsed = json.loads(raw or "")
    except json.JSONDecodeError as exc:
        if explicit:
            _die_config(f"malformed JSON in --mcp-json {resolved}: {exc}")
        # A malformed fallback config is surfaced as empty + the path; the
        # connectivity checks will flag absence. We do not crash the CLI for
        # an implicit, possibly-foreign ~/.mcp.json.
        logger.warning("malformed .mcp.json at %s: %s", resolved, redact.redact(str(exc)))
        return [], resolved

    servers: list[McpServer] = []
    raw_servers = parsed.get("mcpServers") or parsed.get("servers") or {}
    if isinstance(raw_servers, dict):
        for key, spec in raw_servers.items():
            if not isinstance(spec, dict):
                continue
            url = spec.get("url") or ""
            if not isinstance(url, str) or not url:
                continue
            # Path-based topology (``/memory/mcp``) first; fall back to the
            # config key (``gbrain-memory``) for port-based topologies where
            # every server shares the same ``/mcp`` path.
            service = _infer_service(url) or _infer_service_from_key(key)
            if service is None:
                # Not a gbrain server (or unknown path) — skip silently; the
                # doctor only cares about gbrain memory/recall/swarm/tasks.
                continue
            headers = spec.get("headers") or {}
            token = ""
            if isinstance(headers, dict):
                auth = headers.get("Authorization") or headers.get("authorization")
                if isinstance(auth, str) and auth.lower().startswith("bearer "):
                    token = auth.split(" ", 1)[1].strip()
            servers.append(
                McpServer(
                    key=key,
                    service=service,
                    url=url,
                    token=token,
                    type=str(spec.get("type") or "http"),
                )
            )

    return servers, resolved


# ---------------------------------------------------------------------------
# Doctor context passed to every check module
# ---------------------------------------------------------------------------


@dataclass
class DoctorContext:
    """Shared state and helpers handed to each check module's ``run_checks``."""

    agent: str | None
    servers: list[McpServer]
    mcp_json_path: Path
    expected_hooks_path: Path
    groups: set[str] | None  # None = all groups; else subset of G1..G10
    skill_root: Path
    workspace_root: Path | None
    repo_root: Path
    do_fix: bool
    assume_yes: bool
    probe_scope: str | None = None  # optional scope for recall.recent smoke

    def server(self, service: str) -> McpServer | None:
        """Return the configured server for a canonical service, or ``None``.

        Args:
            service: ``"memory"`` / ``"recall"`` / ``"swarm"`` / ``"tasks"``.

        Returns:
            The matching :class:`McpServer`, or ``None`` when absent.
        """
        for srv in self.servers:
            if srv.service == service:
                return srv
        return None

    def want(self, group: str) -> bool:
        """Whether a given group should run under the current ``--group`` filter.

        Args:
            group: A group id like ``"G6"``.

        Returns:
            ``True`` when no filter is set or ``group`` is in the filter.
        """
        return self.groups is None or group in self.groups


# ---------------------------------------------------------------------------
# Check-module registry
# ---------------------------------------------------------------------------

# Ordered list of (module_name, group_ids) the registry attempts to import.
# Group ids are informational — used to emit a precise skip when a module is
# absent during integration so the CLI still runs end-to-end.
_CHECK_MODULES: list[tuple[str, tuple[str, ...]]] = [
    ("checks_mcp", ("G1", "G2", "G3", "G4", "G5")),
    ("checks_hooks", ("G6",)),
    ("checks_local", ("G7", "G8", "G9", "G10")),
]


def _load_check_modules() -> list[tuple[str, Callable, tuple[str, ...]]]:
    """Import each sibling check module, guarding against absence.

    Returns:
        A list of ``(module_name, run_checks_callable_or_None, group_ids)``.
        When a module is missing or lacks ``run_checks``, the callable slot is
        ``None`` so the caller can emit a skip result.
    """
    loaded: list[tuple[str, Callable | None, tuple[str, ...]]] = []
    for mod_name, groups in _CHECK_MODULES:
        try:
            module = __import__(mod_name)
        except Exception as exc:  # noqa: BLE001 — any import failure -> skip
            logger.warning(
                "check module %s unavailable: %s",
                mod_name,
                redact.redact(str(exc)),
            )
            loaded.append((mod_name, None, groups))
            continue
        run = getattr(module, "run_checks", None)
        if not callable(run):
            loaded.append((mod_name, None, groups))
            continue
        loaded.append((mod_name, run, groups))
    return loaded


def _run_all_checks(ctx: DoctorContext) -> list[CheckResult]:
    """Run every available check module and concatenate redacted results.

    Modules that are absent yield a single ``skip`` row naming the module.
    Any exception escaping a module's ``run_checks`` is converted into a
    redacted ``fail`` row rather than crashing the CLI.
    """
    results: list[CheckResult] = []
    for mod_name, run, groups in _load_check_modules():
        # Skip the whole module early when none of its groups are wanted.
        if ctx.groups is not None and not any(g in ctx.groups for g in groups):
            continue
        if run is None:
            grp = groups[0] if groups else "G?"
            results.append(
                CheckResult(
                    name=f"{grp}.module_{mod_name}",
                    status="skip",
                    message=redact.redact(
                        f"check module '{mod_name}' not present yet "
                        f"(covers {', '.join(groups)})"
                    ),
                )
            )
            continue
        try:
            mod_results = run(ctx)
        except Exception as exc:  # noqa: BLE001 — isolate module failures
            grp = groups[0] if groups else "G?"
            results.append(
                CheckResult(
                    name=f"{grp}.module_{mod_name}_error",
                    status="fail",
                    message=redact.redact(
                        f"check module '{mod_name}' raised: {exc}"
                    ),
                    remediation="Inspect the check module; this is a doctor bug.",
                )
            )
            continue
        if isinstance(mod_results, list):
            results.extend(mod_results)
    return results


def _filter_by_group(
    results: list[CheckResult], groups: set[str] | None
) -> list[CheckResult]:
    """Keep only results whose name prefix is in ``groups`` (None = keep all).

    Result names are ``G<n>.<check>``. A result lacking a recognized group
    prefix is always kept (e.g. module skip rows already carry a group).
    """
    if groups is None:
        return results
    kept: list[CheckResult] = []
    for r in results:
        prefix = r.name.split(".", 1)[0]
        if prefix in groups or prefix not in VALID_GROUPS:
            kept.append(r)
    return kept


# ---------------------------------------------------------------------------
# Autofix orchestration
# ---------------------------------------------------------------------------


def _confirm(prompt: str) -> bool:
    """Prompt yes/no on stderr (default no). Returns ``False`` if non-TTY."""
    if not sys.stdin.isatty():
        return False
    print(prompt, file=sys.stderr, end="", flush=True)
    try:
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("", file=sys.stderr)
        return False
    return answer in ("y", "yes")


def _apply_fixes(results: list[CheckResult], ctx: DoctorContext) -> None:
    """Apply whitelisted autofixes for failing checks under ``--fix``.

    Each failing result that carries an ``auto_fix`` callback is offered. In
    interactive mode the operator confirms per fix on stderr; with
    ``--yes`` confirmation is skipped. All log lines go to stderr, redacted,
    and are emitted regardless of ``--quiet`` (audit trail).
    """
    fixed = 0
    failed = 0
    skipped = 0
    for r in results:
        if r.status != "fail" or r.auto_fix is None:
            continue
        if not ctx.assume_yes:
            ok_to_run = _confirm(
                f"[fix] apply safe fix for {r.name}? [y/N] "
            )
            if not ok_to_run:
                print(
                    f"[fix] {r.name}: skipped (declined or non-interactive)",
                    file=sys.stderr,
                )
                skipped += 1
                continue
        print(f"[fix] running autofix for {r.name}...", file=sys.stderr)
        ok = False
        try:
            ok = bool(r.auto_fix())
        except Exception as exc:  # noqa: BLE001 — never crash on a fix
            print(
                f"[fix] {r.name} raised {redact.redact(repr(exc))}",
                file=sys.stderr,
            )
        print(
            f"[fix] {r.name}: {'ok' if ok else 'failed'}",
            file=sys.stderr,
        )
        if ok:
            fixed += 1
        else:
            failed += 1

    if fixed or failed or skipped:
        print(
            f"[fix] summary: {fixed} fixed, {failed} failed, {skipped} skipped. "
            "Re-run the same command to verify.",
            file=sys.stderr,
        )
    else:
        print(
            "[fix] no failing checks had a safe autofix available.",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Identity / workspace resolution
# ---------------------------------------------------------------------------


def _resolve_workspace_root(agent: str | None) -> Path | None:
    """Resolve ``~/.claude-lab/<agent>/.claude`` when it exists."""
    if not agent:
        return None
    ws = Path.home() / ".claude-lab" / agent / ".claude"
    return ws if ws.is_dir() else None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _die_config(message: str) -> None:
    """Print a redacted config error to stderr and exit with code 2."""
    print(f"gbrain-doctor: config error: {redact.redact(message)}", file=sys.stderr)
    raise SystemExit(2)


def _parse_groups(values: list[str] | None) -> set[str] | None:
    """Parse ``--group`` values (repeatable + comma-separated) into a set.

    Args:
        values: The raw ``--group`` argument list, or ``None``.

    Returns:
        A set of group ids, or ``None`` when no filter was supplied.

    Raises:
        SystemExit: Code 2 on any value outside ``G1..G10``.
    """
    if not values:
        return None
    groups: set[str] = set()
    for raw in values:
        for piece in raw.split(","):
            piece = piece.strip()
            if not piece:
                continue
            up = piece.upper()
            if up not in VALID_GROUPS:
                _die_config(
                    f"invalid --group value {piece!r}; valid: G1..G10"
                )
            groups.add(up)
    return groups or None


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser for all documented flags."""
    parser = argparse.ArgumentParser(
        prog="gbrain-doctor",
        description=(
            "Agent-facing diagnostic for an Orgrimmar agent's gbrain MCP "
            "setup: connectivity, identity, recall, swarm, hooks parity, "
            "webhooks, topology, GitHub repo, and skill install."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON array of check results to stdout (no raw secrets).",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Offer whitelisted safe autofixes for failing checks "
        "(chmod +x, symlink, gh repo create). Confirms on stderr.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress normal stdout; rely on exit code. Autofix logs still "
        "go to stderr.",
    )
    parser.add_argument(
        "--agent",
        default=os.environ.get("AGENT_ID") or None,
        help="Canonical agent id for identity, workspace, webhook, GitHub, "
        "and task-registry checks.",
    )
    parser.add_argument(
        "--mcp-json",
        default=None,
        help="Override .mcp.json path. Default resolution: --mcp-json > "
        "~/.claude-lab/<agent>/.claude/.mcp.json > ./.mcp.json > ~/.mcp.json.",
    )
    parser.add_argument(
        "--expected-hooks",
        default=None,
        help="Override hook manifest path. Default: "
        "references/expected-hooks.json under the skill root.",
    )
    parser.add_argument(
        "--group",
        action="append",
        metavar="G1[,G6]",
        help="Run only selected groups. Repeatable and comma-separated. "
        "Valid values G1..G10.",
    )
    parser.add_argument(
        "--probe-scope",
        default=os.environ.get("GBRAIN_PROBE_SCOPE") or None,
        metavar="SCOPE",
        help="Optional scope passed to the recall.recent smoke check "
        "(falls back to env GBRAIN_PROBE_SCOPE).",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI color in table output (also off when NO_COLOR is "
        "set or stdout is not a TTY).",
    )
    parser.add_argument(
        "--yes",
        "--noninteractive",
        action="store_true",
        dest="yes",
        help="Skip confirmation prompts for safe --fix actions only.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code (0/1/2)."""
    logging.basicConfig(
        level=logging.WARNING,
        format="gbrain-doctor: %(message)s",
        stream=sys.stderr,
    )

    # Python version gate (contract: <3.12 -> exit 2 with a clear message).
    if sys.version_info < (3, 12):
        print(
            "gbrain-doctor: requires Python 3.12+, found "
            f"{sys.version_info.major}.{sys.version_info.minor}",
            file=sys.stderr,
        )
        return 2

    parser = _build_parser()
    args = parser.parse_args(argv)

    # --group validation (raises SystemExit(2) on bad value).
    groups = _parse_groups(args.group)

    # Load .mcp.json (raises SystemExit(2) on unreadable explicit override).
    servers, mcp_path = load_mcp_config(args.mcp_json, args.agent)

    # Context-aware redaction: register every loaded Bearer token plus any
    # secret-bearing env values so even opaque, prefix-less tokens echoed back
    # by a server are masked in every downstream check's output (H5).
    redact.register_secrets(s.token for s in servers)
    for env_name, env_val in os.environ.items():
        if env_name == "AGENT_BEARER" or (
            env_name.startswith("GBRAIN_") and "TOKEN" in env_name
        ):
            if env_val:
                redact.register_secrets([env_val])

    # Expected-hooks manifest path resolution. An explicit unreadable path is
    # a hard config error; the default is allowed to be absent (G6 reports it).
    if args.expected_hooks:
        expected_hooks_path = Path(args.expected_hooks).expanduser()
        if not expected_hooks_path.is_file():
            _die_config(
                f"--expected-hooks path not found: {expected_hooks_path}"
            )
        try:
            expected_hooks_path.read_text(encoding="utf-8")
        except OSError as exc:
            _die_config(
                f"cannot read --expected-hooks {expected_hooks_path}: {exc}"
            )
    else:
        expected_hooks_path = _SKILL_ROOT / "references" / "expected-hooks.json"

    ctx = DoctorContext(
        agent=args.agent,
        servers=servers,
        mcp_json_path=mcp_path,
        expected_hooks_path=expected_hooks_path,
        groups=groups,
        skill_root=_SKILL_ROOT,
        workspace_root=_resolve_workspace_root(args.agent),
        repo_root=_REPO_ROOT,
        do_fix=bool(args.fix),
        assume_yes=bool(args.yes),
        probe_scope=args.probe_scope,
    )

    results = _run_all_checks(ctx)
    results = _filter_by_group(results, groups)

    if args.fix:
        _apply_fixes(results, ctx)

    if not args.quiet:
        if args.json:
            print(result_mod.render_json(results))
        else:
            no_color = (
                args.no_color
                or "NO_COLOR" in os.environ
                or not sys.stdout.isatty()
            )
            print(result_mod.render_table(results, no_color=no_color))

    _, exit_code = result_mod.verdict(results)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
