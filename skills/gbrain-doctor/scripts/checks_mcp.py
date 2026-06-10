"""MCP-facing checks for gbrain-doctor (groups G1-G5).

Implements checks C001-C021 from the PLAN catalog against the configured
gbrain MCP streamable-http endpoints, using the verified stdlib JSON-RPC
client in :mod:`mcp_streamable`.

Group map:
    G1 connectivity  : C001-C006
    G2 identity/token: C007-C011
    G3 swarm         : C012-C015
    G4 recall        : C016-C018
    G5 memory (dry)  : C019-C021

Hard guarantees:
    * Default run is fully read-only — no mutating tool is ever called.
    * No exception escapes :func:`run_checks`; every unexpected error is
      converted into a redacted ``fail`` :class:`CheckResult`.
    * Every message passes through :func:`redact.redact`; tokens are only ever
      surfaced via :func:`redact.mask_token`.
    * Groups are honored via ``ctx.want("G1")`` .. ``ctx.want("G5")`` — a
      group's checks are not dispatched at all when filtered out.

Assumed gbrain tool names (verify against the live server during review):
    recall : ``stats``, ``recall``, ``recent``, ``reindex_check``
    swarm  : ``stats``, ``list_recent_deliveries``, ``list_my_pending``,
             ``get_delivery``
    tasks  : ``agent_list``, ``agent_status``
    memory : (tools/list only; expects ``create_*`` write tools registered)
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

import redact
from mcp_streamable import McpError, tool_call, tools_list
from result import CheckResult

# Substrings (lower-cased) that mark a "tool not registered / unknown method"
# style failure. Per PLAN these become ``warn`` for the optional smoke tools
# (C004 recall.stats, C005 swarm.stats) rather than ``fail``.
_NOT_REGISTERED_HINTS = (
    "not registered",
    "unknown tool",
    "tool not found",
    "no such tool",
    "method not found",
    "unknown method",
    "not implemented",
    "invalid tool name",
)

# Substrings (lower-cased) that mark an *argument validation* failure — i.e. the
# DOCTOR called a tool with a wrong/missing/extra argument (pydantic / FastMCP
# input validation), NOT a server fault. These map to ``warn`` (skill/server
# schema drift) rather than ``fail`` across every read-only tool-call check.
_ARG_VALIDATION_HINTS = (
    "validation error",
    "unexpected keyword",
    "missing required",
    "field required",
    "input should be",
    "extra_forbidden",
    "unexpected_keyword_argument",
    "missing_argument",
)

# Standard JSON-RPC "Invalid params" code. The streamable client folds the
# numeric code into the message text ("JSON-RPC error: ... -32602 ..."), so we
# also text-match this token when the structured code is unavailable.
_INVALID_PARAMS_CODE = -32602

# Shared remediation for a doctor/server tool-schema mismatch.
_ARG_MISMATCH_REMEDIATION = (
    "doctor/server tool-schema mismatch (skill may need updating for this "
    "gbrain version)"
)


def run_checks(ctx: "Any") -> list[CheckResult]:
    """Run all wanted MCP checks (G1-G5) and return redacted results.

    Args:
        ctx: The :class:`DoctorContext` built by the CLI core. Accessed via
            ``ctx.server(service)`` and ``ctx.want(group)``; never mutated.

    Returns:
        An ordered list of :class:`CheckResult`. Never raises — any unexpected
        error within a check is captured as a redacted ``fail`` row.
    """
    results: list[CheckResult] = []

    if ctx.want("G1"):
        _safe(results, "G1.mcp_config_servers_present", _c001_config_present, ctx)
        _safe(results, "G1.mcp_server_specs_well_formed", _c002_specs_well_formed, ctx)
        _safe(results, "G1.mcp_tools_list", _c003_tools_list, ctx)
        _safe(results, "G1.recall_stats", _c004_recall_stats, ctx)
        _safe(results, "G1.swarm_stats", _c005_swarm_stats, ctx)
        _safe(results, "G1.tasks_agent_list", _c006_tasks_agent_list, ctx)

    if ctx.want("G2"):
        _safe(results, "G2.auth_without_bearer_denied", _c007_auth_denied, ctx)
        _safe(results, "G2.agent_identity_resolved", _c008_identity, ctx)
        _safe(results, "G2.tasks_agent_status", _c009_agent_status, ctx)
        _safe(results, "G2.mcp_bearer_consistency", _c010_bearer_consistency, ctx)
        _safe(
            results,
            "G2.token_output_redaction_selftest",
            _c011_redaction_selftest,
            ctx,
        )

    if ctx.want("G3"):
        _safe(results, "G3.swarm_recent_acked_ratio", _c012_acked_ratio, ctx)
        _safe(results, "G3.swarm_pending_depth", _c013_pending_depth, ctx)
        _safe(results, "G3.swarm_delivery_lookup", _c014_delivery_lookup, ctx)
        _safe(
            results,
            "G3.swarm_self_notify_roundtrip",
            _c015_self_notify_roundtrip,
            ctx,
        )

    if ctx.want("G4"):
        _safe(results, "G4.recall_probe", _c016_recall_probe, ctx)
        _safe(results, "G4.recall_recent_probe", _c017_recall_recent, ctx)
        _safe(results, "G4.recall_reindex_check", _c018_reindex_check, ctx)

    if ctx.want("G5"):
        _safe(results, "G5.memory_tools_registered", _c019_memory_tools, ctx)
        _safe(results, "G5.memory_tool_schema_readable", _c020_memory_schema, ctx)
        _safe(results, "G5.memory_write_probe_dry", _c021_memory_write_dry, ctx)

    return results


# ---------------------------------------------------------------------------
# Dispatch wrapper — converts any escaped exception into a redacted fail row.
# ---------------------------------------------------------------------------


def _safe(results: list[CheckResult], name: str, fn, ctx) -> None:
    """Run one check fn, appending its result; never let an exception escape.

    Args:
        results: Accumulator list to append the produced result to.
        name: The group-prefixed check name to stamp on any error row.
        fn: A callable ``(ctx) -> CheckResult``.
        ctx: The doctor context to pass through.
    """
    try:
        results.append(fn(ctx))
    except Exception as exc:  # noqa: BLE001 — isolate every check
        results.append(
            CheckResult(
                name=name,
                status="fail",
                message=redact.redact(f"unexpected error: {exc!r}"),
                remediation="Inspect checks_mcp.py; this is a doctor bug.",
            )
        )


def _is_not_registered(err: McpError) -> bool:
    """Whether an MCP error looks like a missing/unregistered tool/method.

    Args:
        err: The raised :class:`McpError` (message already redacted).

    Returns:
        ``True`` when the (lower-cased) message matches a known
        "tool not registered" hint, or the HTTP status is 404.
    """
    if err.status_code == 404:
        return True
    msg = (err.message or "").lower()
    return any(hint in msg for hint in _NOT_REGISTERED_HINTS)


def _is_arg_validation_error(exc: McpError) -> bool:
    """Whether an MCP error means the doctor mis-called the tool's arguments.

    An argument-validation failure (wrong/missing/extra kwarg, type mismatch)
    indicates a doctor/server schema mismatch — the skill called the tool with
    the wrong shape, not that gbrain itself is broken. Such errors should be
    surfaced as ``warn`` rather than ``fail``.

    Detection order:
        1. If the McpError exposes a JSON-RPC ``code`` (``.code``/``.status_code``)
           equal to ``-32602`` (Invalid params), treat as a validation error.
        2. Otherwise text-match the (already-redacted) message against the
           known validation-error hints (case-insensitive), and also match the
           literal ``-32602`` token the server folds into the message.

    Args:
        exc: The raised :class:`McpError` (message already redacted).

    Returns:
        ``True`` when the failure looks like an argument-validation error.
    """
    # 1. Structured code, if the McpError variant exposes one.
    code = getattr(exc, "code", None)
    if code is None:
        # status_code is HTTP-level here, but a future variant may reuse it for
        # the JSON-RPC code; only honor it when it is exactly -32602.
        sc = getattr(exc, "status_code", None)
        if sc == _INVALID_PARAMS_CODE:
            return True
    elif code == _INVALID_PARAMS_CODE:
        return True

    # 2. Text fallback (the common path with the current streamable client).
    msg = (exc.message or "").lower()
    if str(_INVALID_PARAMS_CODE) in msg:
        return True
    return any(hint in msg for hint in _ARG_VALIDATION_HINTS)


def _resolve_probe_scope(ctx) -> str | None:
    """Resolve an optional recall scope to smoke ``recent`` with.

    Prefers a ``probe_scope`` attribute on the context (a sibling agent may add
    a ``--probe-scope`` CLI flag), then falls back to the ``GBRAIN_PROBE_SCOPE``
    environment variable. Tolerates the attribute's absence so no
    :class:`DoctorContext` change is required.

    Args:
        ctx: The doctor context.

    Returns:
        A non-empty scope string, or ``None`` when none is configured.
    """
    scope = getattr(ctx, "probe_scope", None)
    if isinstance(scope, str) and scope.strip():
        return scope.strip()
    env_scope = os.environ.get("GBRAIN_PROBE_SCOPE", "")
    if env_scope.strip():
        return env_scope.strip()
    return None


def _fmt_latency(ms: float) -> str:
    """Format a latency in ms as a compact integer-ms string."""
    return f"{int(round(ms))}ms"


# ---------------------------------------------------------------------------
# G1 — connectivity
# ---------------------------------------------------------------------------


def _is_non_claude_workspace(ctx) -> bool:
    """Whether the resolved workspace is clearly NOT a Claude Code agent.

    A Claude Code agent workspace carries a ``settings.json`` and/or a
    ``CLAUDE.md``. A non-Claude component (e.g. a Python ingestion bot) keeps
    only ``.mcp.json`` in its ``.claude/`` purely for MCP auth — it makes
    direct MCP calls and is not a swarm participant with lifecycle hooks.

    Conservative by design: returns ``True`` only when the workspace is
    resolved AND *both* markers are absent, so a merely-misconfigured real
    agent (e.g. a deleted settings.json) is not mistaken for a bot.
    """
    ws = getattr(ctx, "workspace_root", None)
    if ws is None:
        return False
    try:
        return not (ws / "settings.json").exists() and not (ws / "CLAUDE.md").exists()
    except OSError:  # pragma: no cover — defensive against odd FS errors
        return False


def _c001_config_present(ctx) -> CheckResult:
    """C001: required MCP servers (memory/recall/swarm) present; tasks optional.

    A non-Claude workspace (:func:`_is_non_claude_workspace`) legitimately omits
    ``swarm``/``task`` — those are downgraded to ``skip`` rather than failed.
    """
    name = "G1.mcp_config_servers_present"
    required = ("memory", "recall", "swarm")
    present = {s for s in required if ctx.server(s) is not None}
    missing = [s for s in required if s not in present]
    has_tasks = ctx.server("tasks") is not None
    tasks_note = "tasks present" if has_tasks else "tasks absent (optional)"

    if missing:
        # A non-Claude component (e.g. a Python ingestion bot) legitimately has
        # only memory+recall — it makes direct MCP calls and is not a swarm
        # participant. Downgrade to skip when ONLY swarm is missing and the
        # core read/write pair is present. Missing memory/recall stays a real
        # fault even for a bot.
        if (
            set(missing) == {"swarm"}
            and {"memory", "recall"}.issubset(present)
            and _is_non_claude_workspace(ctx)
        ):
            return CheckResult(
                name=name,
                status="skip",
                message=redact.redact(
                    "non-Claude workspace (no settings.json/CLAUDE.md): "
                    "swarm/task not expected; memory+recall present; "
                    f"{tasks_note}"
                ),
            )
        return CheckResult(
            name=name,
            status="fail",
            message=redact.redact(
                f"missing required gbrain servers: {', '.join(missing)}; "
                f"found: {', '.join(sorted(present)) or 'none'}; {tasks_note}"
            ),
            remediation=(
                "Add missing server entries from "
                "agent-template/templates/mcp.json.template; add tasks from "
                "docs/task-mcp-integration.md if needed."
            ),
        )
    return CheckResult(
        name=name,
        status="pass",
        message=redact.redact(
            f"memory/recall/swarm present; {tasks_note}"
        ),
    )


def _c002_specs_well_formed(ctx) -> CheckResult:
    """C002: each configured gbrain server is type http, valid URL, Bearer auth."""
    name = "G1.mcp_server_specs_well_formed"
    if not ctx.servers:
        return CheckResult(
            name=name,
            status="skip",
            message=redact.redact("no gbrain servers configured to validate"),
        )

    problems: list[str] = []
    for srv in ctx.servers:
        label = srv.service
        # type must be http
        if (srv.type or "").lower() != "http":
            problems.append(f"{label}: type={srv.type!r} (expected http)")
        # URL must parse with a scheme + host + a /<service>/mcp-ish path
        parsed = urlparse(srv.url or "")
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            problems.append(f"{label}: malformed URL")
        elif not parsed.path or "mcp" not in parsed.path:
            problems.append(f"{label}: URL path missing /mcp")
        # token present and not an unresolved placeholder
        tok = srv.token or ""
        if not tok:
            problems.append(f"{label}: missing Bearer token")
        elif _looks_like_placeholder(tok):
            problems.append(f"{label}: unresolved placeholder token")

    if problems:
        return CheckResult(
            name=name,
            status="fail",
            message=redact.redact("; ".join(problems)),
            remediation=(
                "Re-render .mcp.json via the installer or replace "
                "placeholders with real values outside chat/logs."
            ),
        )
    return CheckResult(
        name=name,
        status="pass",
        message=redact.redact(
            f"{len(ctx.servers)} server spec(s) valid (http + URL + Bearer)"
        ),
    )


def _looks_like_placeholder(tok: str) -> bool:
    """Whether a token value looks like an unresolved template placeholder."""
    t = tok.strip()
    if not t:
        return True
    # ${VAR}, {{VAR}}, <VAR>, or an obvious uppercase TOKEN sentinel.
    if t.startswith("${") or t.startswith("{{") or t.startswith("<"):
        return True
    upper = t.upper()
    sentinels = ("PLACEHOLDER", "CHANGEME", "YOUR_TOKEN", "AGENT_BEARER", "TODO")
    return any(s in upper for s in sentinels)


def _c003_tools_list(ctx) -> CheckResult:
    """C003: each present MCP endpoint accepts authed JSON-RPC and lists tools."""
    name = "G1.mcp_tools_list"
    order = ("recall", "swarm", "memory", "tasks")
    parts: list[str] = []
    any_fail = False
    any_present = False

    for service in order:
        srv = ctx.server(service)
        if srv is None:
            if service == "tasks":
                parts.append("tasks=skip")
            # non-tasks absence is covered by C001; don't double-fail here.
            continue
        any_present = True
        try:
            tools, latency_ms = tools_list(srv.url, srv.token)
        except McpError as exc:
            any_fail = True
            code = f" ({exc.status_code})" if exc.status_code else ""
            parts.append(f"{service}=ERR{code}:{exc.message}")
            continue
        parts.append(f"{service}={_fmt_latency(latency_ms)}({len(tools)} tools)")

    if not any_present:
        return CheckResult(
            name=name,
            status="skip",
            message=redact.redact("no reachable gbrain servers configured"),
        )
    if any_fail:
        return CheckResult(
            name=name,
            status="fail",
            message=redact.redact(" ".join(parts)),
            remediation="Verify URL, token, network path, and service health.",
        )
    return CheckResult(
        name=name,
        status="pass",
        message=redact.redact(" ".join(parts)),
    )


def _c004_recall_stats(ctx) -> CheckResult:
    """C004: recall service executes a read-only smoke tool (stats)."""
    return _smoke_stats(ctx, "recall", "G1.recall_stats")


def _c005_swarm_stats(ctx) -> CheckResult:
    """C005: swarm service executes a read-only smoke tool (stats)."""
    return _smoke_stats(ctx, "swarm", "G1.swarm_stats")


def _smoke_stats(ctx, service: str, name: str) -> CheckResult:
    """Shared C004/C005 body: call ``stats`` on a service, warn if absent tool."""
    srv = ctx.server(service)
    if srv is None:
        return CheckResult(
            name=name,
            status="skip",
            message=redact.redact(f"{service} server not configured"),
        )
    try:
        payload, latency_ms = tool_call(srv.url, srv.token, "stats", {})
    except McpError as exc:
        if _is_not_registered(exc):
            return CheckResult(
                name=name,
                status="warn",
                message=redact.redact(
                    f"{service}.stats not registered: {exc.message}"
                ),
                remediation=f"Verify {service} MCP exposes a read-only stats tool.",
            )
        if _is_arg_validation_error(exc):
            return CheckResult(
                name=name,
                status="warn",
                message=redact.redact(
                    f"{service}.stats arg-validation error: {exc.message}"
                ),
                remediation=_ARG_MISMATCH_REMEDIATION,
            )
        return CheckResult(
            name=name,
            status="fail",
            message=redact.redact(f"{service}.stats failed: {exc.message}"),
            remediation=(
                f"Restart/fix {service} service on host; ensure token has "
                f"{service} access."
            ),
        )
    return CheckResult(
        name=name,
        status="pass",
        message=redact.redact(
            f"{service}.stats ok ({_fmt_latency(latency_ms)}) "
            f"{_compact_obj(payload)}"
        ),
    )


def _c006_tasks_agent_list(ctx) -> CheckResult:
    """C006: optional tasks server reachable via agent_list when configured."""
    name = "G1.tasks_agent_list"
    srv = ctx.server("tasks")
    if srv is None:
        return CheckResult(
            name=name,
            status="skip",
            message=redact.redact("tasks server absent (optional)"),
        )
    try:
        payload, latency_ms = tool_call(srv.url, srv.token, "agent_list", {})
    except McpError as exc:
        if _is_arg_validation_error(exc):
            return CheckResult(
                name=name,
                status="warn",
                message=redact.redact(
                    f"tasks.agent_list arg-validation error: {exc.message}"
                ),
                remediation=_ARG_MISMATCH_REMEDIATION,
            )
        return CheckResult(
            name=name,
            status="fail",
            message=redact.redact(f"tasks.agent_list failed: {exc.message}"),
            remediation=(
                "Start task MCP and add the .mcp.json entry, or remove the "
                "broken optional entry."
            ),
        )
    count = _count_items(payload)
    suffix = f" ({count} agents)" if count is not None else ""
    return CheckResult(
        name=name,
        status="pass",
        message=redact.redact(
            f"tasks.agent_list ok ({_fmt_latency(latency_ms)}){suffix}"
        ),
    )


# ---------------------------------------------------------------------------
# G2 — identity / token
# ---------------------------------------------------------------------------


def _unauth_post(url: str, method: str, params: dict) -> tuple[int | None, str]:
    """Issue a JSON-RPC POST with NO Authorization header.

    A bespoke inline urllib POST is used because :mod:`mcp_streamable` always
    attaches a Bearer header. The token is never involved here by design.

    Args:
        url: The ``/<service>/mcp`` endpoint URL.
        method: JSON-RPC method (``"tools/list"`` or ``"tools/call"``).
        params: JSON-RPC params object.

    Returns:
        ``(http_status, body_text)``. ``http_status`` is the HTTP status code
        (from a normal response or an :class:`urllib.error.HTTPError`).

    Raises:
        (urllib.error.URLError, socket.timeout, TimeoutError, ssl.SSLError):
            on a transport-level failure; callers treat these as inconclusive.
    """
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            # Deliberately NO Authorization header.
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            status = resp.getcode()
            body = resp.read().decode("utf-8", errors="replace")
        return status, body
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001 — error-body read is best-effort
            body = ""
        return exc.code, body


def _unauth_call_succeeded(status: int | None, body: str) -> bool:
    """Whether an unauthenticated ``tools/call`` actually executed a tool.

    A real auth bypass means a 2xx HTTP status AND a JSON-RPC ``result`` with
    no top-level ``error`` and no ``result.isError`` flag. A 2xx that merely
    carries a JSON-RPC error (e.g. auth rejection encoded in-band, or unknown
    tool) is NOT a bypass.

    Args:
        status: HTTP status code (or ``None``).
        body: Decoded response body (plain JSON or SSE).

    Returns:
        ``True`` only when the call produced a genuine successful result.
    """
    if status is None or not (200 <= status < 300):
        return False
    try:
        envelope = _parse_unauth_body(body)
    except ValueError:
        return False
    if not isinstance(envelope, dict):
        return False
    if envelope.get("error"):
        return False
    result = envelope.get("result")
    if not isinstance(result, dict):
        return False
    if result.get("isError"):
        return False
    return True


def _parse_unauth_body(body: str) -> Any:
    """Parse a possibly-SSE JSON-RPC body without leaking on failure.

    Args:
        body: The decoded response body.

    Returns:
        The decoded JSON-RPC envelope.

    Raises:
        ValueError: when nothing parseable is found.
    """
    for line in body.splitlines():
        if line.startswith("data: "):
            frame = line[6:].strip()
            if frame:
                return json.loads(frame)
    text = body.strip()
    if not text:
        raise ValueError("empty body")
    return json.loads(text)


def _c007_auth_denied(ctx) -> CheckResult:
    """C007: tool *execution* must require auth (tools/call), not just tools/list.

    Live gbrain enforces auth only at ``tools/call`` — an unauthenticated
    ``tools/list`` returns 200 with the tool schema. That schema exposure is a
    server doctrine choice, not a true bypass, so it is downgraded to WARN.
    The real auth boundary is tested by an unauthenticated ``tools/call``:

        * unauth ``tools/call`` returns a genuine result  -> FAIL (real bypass)
        * unauth ``tools/list`` 200 but ``tools/call`` rejected -> WARN
        * both rejected -> PASS
        * transport error -> WARN (inconclusive)

    The ``tools/call`` probe targets a harmless read-only tool (``stats``); if
    that tool does not exist the server still rejects the unauthenticated call,
    which equally proves the execution boundary.
    """
    name = "G2.auth_without_bearer_denied"
    # Prefer a public-ish service; any present one proves the auth boundary.
    srv = None
    for service in ("recall", "swarm", "memory", "tasks"):
        srv = ctx.server(service)
        if srv is not None:
            break
    if srv is None:
        return CheckResult(
            name=name,
            status="skip",
            message=redact.redact("no gbrain server configured to probe"),
        )

    # 1. Probe unauthenticated tool EXECUTION — the real auth boundary.
    try:
        call_status, call_body = _unauth_post(
            srv.url, "tools/call", {"name": "stats", "arguments": {}}
        )
    except (
        urllib.error.URLError,
        socket.timeout,
        TimeoutError,
        ssl.SSLError,
    ) as exc:
        return CheckResult(
            name=name,
            status="warn",
            message=redact.redact(
                f"{srv.service}: no-auth probe inconclusive (network error): "
                f"{exc}"
            ),
            remediation="Re-run when the network path to the endpoint is up.",
        )

    if _unauth_call_succeeded(call_status, call_body):
        return CheckResult(
            name=name,
            status="fail",
            message=redact.redact(
                f"{srv.service}: unauthenticated tools/call returned a real "
                f"result (HTTP {call_status}) — auth bypass"
            ),
            remediation=(
                "Fix AuthCaptureMiddleware/proxy auth; tool execution must "
                "require a Bearer token."
            ),
        )

    # 2. tools/call is rejected. Check whether tools/list still leaks schema.
    try:
        list_status, _ = _unauth_post(srv.url, "tools/list", {})
    except (
        urllib.error.URLError,
        socket.timeout,
        TimeoutError,
        ssl.SSLError,
    ):
        # tools/call already proved the execution boundary holds.
        return CheckResult(
            name=name,
            status="pass",
            message=redact.redact(
                f"{srv.service}: unauthenticated tools/call rejected "
                f"(HTTP {call_status}); tools/list probe inconclusive"
            ),
        )

    list_exposed = list_status is not None and 200 <= list_status < 300
    if list_exposed:
        return CheckResult(
            name=name,
            status="warn",
            message=redact.redact(
                f"{srv.service}: unauthenticated tools/list exposes tool "
                f"schema only (HTTP {list_status}); tool execution is "
                f"auth-enforced (tools/call HTTP {call_status})"
            ),
            remediation=(
                "Server doctrine: tools/list is public, tools/call is "
                "auth-enforced. Add auth to tools/list if schema exposure is "
                "unacceptable."
            ),
        )

    return CheckResult(
        name=name,
        status="pass",
        message=redact.redact(
            f"{srv.service}: unauthenticated tools/call and tools/list both "
            f"rejected (HTTP {call_status}/{list_status})"
        ),
    )


def _c008_identity(ctx) -> CheckResult:
    """C008: the doctor knows which agent it is checking."""
    name = "G2.agent_identity_resolved"
    if ctx.agent:
        strength = "explicit/inferred"
        return CheckResult(
            name=name,
            status="pass",
            message=redact.redact(f"agent='{ctx.agent}' ({strength})"),
        )
    # Weak inference: workspace_root path may still hint at an agent.
    if ctx.workspace_root is not None:
        return CheckResult(
            name=name,
            status="warn",
            message=redact.redact(
                f"agent not given; weak hint from workspace "
                f"{ctx.workspace_root}"
            ),
            remediation="Run with --agent <id> from the agent workspace.",
        )
    return CheckResult(
        name=name,
        status="skip",
        message=redact.redact(
            "no agent id resolved; identity-only checks skipped"
        ),
        remediation="Run with --agent <id> from the agent workspace.",
    )


def _c009_agent_status(ctx) -> CheckResult:
    """C009: agent exists in task MCP registry when tasks is configured."""
    name = "G2.tasks_agent_status"
    srv = ctx.server("tasks")
    if srv is None:
        return CheckResult(
            name=name,
            status="skip",
            message=redact.redact("tasks server absent (optional)"),
        )
    if not ctx.agent:
        return CheckResult(
            name=name,
            status="skip",
            message=redact.redact("agent unknown; cannot query agent_status"),
            remediation="Run with --agent <id>.",
        )
    try:
        # Ground truth: agent_status requires ``agent_name`` (NOT ``agent``).
        payload, latency_ms = tool_call(
            srv.url, srv.token, "agent_status", {"agent_name": ctx.agent}
        )
    except McpError as exc:
        if _is_not_registered(exc):
            return CheckResult(
                name=name,
                status="warn",
                message=redact.redact(
                    f"tasks.agent_status unavailable: {exc.message}"
                ),
                remediation="Verify task MCP exposes agent_status.",
            )
        if _is_arg_validation_error(exc):
            return CheckResult(
                name=name,
                status="warn",
                message=redact.redact(
                    f"tasks.agent_status arg-validation error: {exc.message}"
                ),
                remediation=_ARG_MISMATCH_REMEDIATION,
            )
        return CheckResult(
            name=name,
            status="warn",
            message=redact.redact(
                f"agent '{ctx.agent}' status unknown/stale: {exc.message}"
            ),
            remediation="Register/heartbeat the agent through task MCP setup.",
        )
    # Look for a revoked/invalid flag in the returned status object.
    status_word = _status_word(payload)
    if status_word in ("revoked", "invalid", "disabled"):
        return CheckResult(
            name=name,
            status="warn",
            message=redact.redact(
                f"agent '{ctx.agent}' registry status: {status_word}"
            ),
            remediation="Re-register/heartbeat the agent through task MCP.",
        )
    return CheckResult(
        name=name,
        status="pass",
        message=redact.redact(
            f"agent '{ctx.agent}' found in task registry "
            f"({_fmt_latency(latency_ms)})"
        ),
    )


def _c010_bearer_consistency(ctx) -> CheckResult:
    """C010: gbrain servers use a non-placeholder Bearer; drift visible+masked."""
    name = "G2.mcp_bearer_consistency"
    if not ctx.servers:
        return CheckResult(
            name=name,
            status="skip",
            message=redact.redact("no gbrain servers configured"),
        )

    empties = [s.service for s in ctx.servers if not (s.token or "").strip()]
    placeholders = [
        s.service
        for s in ctx.servers
        if (s.token or "").strip() and _looks_like_placeholder(s.token)
    ]
    if empties or placeholders:
        bad = []
        if empties:
            bad.append(f"empty: {', '.join(empties)}")
        if placeholders:
            bad.append(f"placeholder: {', '.join(placeholders)}")
        return CheckResult(
            name=name,
            status="fail",
            message=redact.redact("; ".join(bad)),
            remediation="Reissue/rotate tokens and update .mcp.json carefully.",
        )

    distinct = {s.token for s in ctx.servers if (s.token or "").strip()}
    if len(distinct) <= 1:
        sample = next(iter(distinct))
        # Note: do not put the literal word "Bearer" before the masked token —
        # the redactor's `Bearer\s+\S+` rule would otherwise eat the mask.
        return CheckResult(
            name=name,
            status="pass",
            message=redact.redact(
                f"single shared auth token across {len(ctx.servers)} "
                f"server(s): {redact.mask_token(sample)}"
            ),
        )
    masked = ", ".join(
        f"{s.service}={redact.mask_token(s.token)}" for s in ctx.servers
    )
    return CheckResult(
        name=name,
        status="warn",
        message=redact.redact(
            f"{len(distinct)} distinct auth tokens "
            f"(intentional per-service?): {masked}"
        ),
        remediation="Confirm per-service tokens are intentional, not drift.",
    )


def _c011_redaction_selftest(ctx) -> CheckResult:
    """C011: feed sample secrets through redact() and assert all are masked."""
    name = "G2.token_output_redaction_selftest"
    samples = [
        "Authorization: Bearer sk-abcDEF1234567890token",
        "api key sk-proj-ABCdef0123456789-_",
        "secret hmac_deadBEEF0123456789",
        "login password=hunter2supersecret",
    ]
    leaks: list[str] = []
    for raw in samples:
        masked = redact.redact(raw)
        # If any obviously-secret token survives, the redactor failed.
        if "Bearer sk-" in masked or "sk-proj-" in masked or "sk-abc" in masked:
            leaks.append("sk/bearer leak")
        if "hmac_deadBEEF" in masked:
            leaks.append("hmac leak")
        if "password=hunter2" in masked:
            leaks.append("password leak")
    # mask_token/mask_hmac sanity (must not echo the full input).
    full_tok = "sk-abcDEF1234567890tokenXYZ"
    if full_tok in redact.mask_token(full_tok):
        leaks.append("mask_token echoes full token")
    full_h = "deadBEEF0123456789abcdef"
    if full_h in redact.mask_hmac(full_h):
        leaks.append("mask_hmac echoes full secret")

    if leaks:
        return CheckResult(
            name=name,
            status="fail",
            message=redact.redact(
                "redactor leaked sample secret(s): "
                + ", ".join(sorted(set(leaks)))
            ),
            remediation="Fix scripts/redact.py before trusting any output.",
        )
    return CheckResult(
        name=name,
        status="pass",
        message=redact.redact(
            f"{len(samples)} secret samples masked; mask_token/mask_hmac safe"
        ),
    )


# ---------------------------------------------------------------------------
# G3 — swarm
# ---------------------------------------------------------------------------


def _c012_acked_ratio(ctx) -> CheckResult:
    """C012: recent visible deliveries are mostly acked (fail if >20% failed)."""
    name = "G3.swarm_recent_acked_ratio"
    srv = ctx.server("swarm")
    if srv is None:
        return CheckResult(
            name=name,
            status="skip",
            message=redact.redact("swarm server not configured"),
        )
    try:
        payload, latency_ms = tool_call(
            srv.url, srv.token, "list_recent_deliveries", {"limit": 50}
        )
    except McpError as exc:
        if _is_not_registered(exc):
            return CheckResult(
                name=name,
                status="warn",
                message=redact.redact(
                    f"list_recent_deliveries unavailable: {exc.message}"
                ),
                remediation="Verify swarm MCP exposes list_recent_deliveries.",
            )
        if _is_arg_validation_error(exc):
            return CheckResult(
                name=name,
                status="warn",
                message=redact.redact(
                    f"list_recent_deliveries arg-validation error: "
                    f"{exc.message}"
                ),
                remediation=_ARG_MISMATCH_REMEDIATION,
            )
        return CheckResult(
            name=name,
            status="fail",
            message=redact.redact(
                f"list_recent_deliveries failed: {exc.message}"
            ),
            remediation=(
                "Inspect worker/listener using last_error mapping; fix webhook "
                "route/auth."
            ),
        )
    rows = _as_rows(payload)
    if not rows:
        return CheckResult(
            name=name,
            status="warn",
            message=redact.redact(
                f"no recent deliveries returned ({_fmt_latency(latency_ms)})"
            ),
            remediation="No recent swarm traffic to evaluate ack ratio.",
        )
    failed = sum(1 for r in rows if _is_failed_delivery(r))
    total = len(rows)
    ratio = failed / total if total else 0.0
    msg = (
        f"{failed}/{total} failed ({ratio:.0%}) over recent deliveries "
        f"({_fmt_latency(latency_ms)})"
    )
    if ratio > 0.20:
        return CheckResult(
            name=name,
            status="fail",
            message=redact.redact(msg),
            remediation=(
                "Inspect worker/listener using last_error mapping; fix webhook "
                "route/auth."
            ),
        )
    return CheckResult(name=name, status="pass", message=redact.redact(msg))


def _c013_pending_depth(ctx) -> CheckResult:
    """C013: this agent does not have a large stuck pending queue."""
    name = "G3.swarm_pending_depth"
    srv = ctx.server("swarm")
    if srv is None:
        return CheckResult(
            name=name,
            status="skip",
            message=redact.redact("swarm server not configured"),
        )
    # Ground truth: list_my_pending takes only ``limit`` (identity comes from the
    # Bearer token server-side). It has NO ``agent`` argument.
    args: dict[str, Any] = {"limit": 50}
    try:
        payload, latency_ms = tool_call(
            srv.url, srv.token, "list_my_pending", args
        )
    except McpError as exc:
        if _is_not_registered(exc):
            return CheckResult(
                name=name,
                status="warn",
                message=redact.redact(
                    f"list_my_pending unavailable: {exc.message}"
                ),
                remediation="Verify swarm MCP exposes list_my_pending.",
            )
        if _is_arg_validation_error(exc):
            return CheckResult(
                name=name,
                status="warn",
                message=redact.redact(
                    f"list_my_pending arg-validation error: {exc.message}"
                ),
                remediation=_ARG_MISMATCH_REMEDIATION,
            )
        return CheckResult(
            name=name,
            status="fail",
            message=redact.redact(f"list_my_pending failed: {exc.message}"),
            remediation="Process/ack pending tasks or debug worker delivery.",
        )
    rows = _as_rows(payload)
    depth = len(rows)
    msg = f"pending depth={depth} ({_fmt_latency(latency_ms)})"
    if depth >= 50:
        return CheckResult(
            name=name,
            status="fail",
            message=redact.redact(msg + " (queue likely stuck)"),
            remediation="Process/ack pending tasks or debug worker delivery.",
        )
    if depth > 20:
        return CheckResult(
            name=name,
            status="warn",
            message=redact.redact(msg),
            remediation="Process/ack pending tasks soon.",
        )
    return CheckResult(name=name, status="pass", message=redact.redact(msg))


def _c014_delivery_lookup(ctx) -> CheckResult:
    """C014: a visible recent delivery can be fetched by id via get_delivery."""
    name = "G3.swarm_delivery_lookup"
    srv = ctx.server("swarm")
    if srv is None:
        return CheckResult(
            name=name,
            status="skip",
            message=redact.redact("swarm server not configured"),
        )
    try:
        recent_payload, _ = tool_call(
            srv.url, srv.token, "list_recent_deliveries", {"limit": 5}
        )
    except McpError as exc:
        return CheckResult(
            name=name,
            status="skip",
            message=redact.redact(
                f"cannot list recent deliveries for lookup: {exc.message}"
            ),
        )
    rows = _as_rows(recent_payload)
    delivery_id = _first_delivery_id(rows)
    if delivery_id is None:
        return CheckResult(
            name=name,
            status="skip",
            message=redact.redact("no recent delivery id to look up"),
        )
    try:
        looked, latency_ms = tool_call(
            srv.url, srv.token, "get_delivery", {"task_id": delivery_id}
        )
    except McpError as exc:
        if _is_arg_validation_error(exc):
            return CheckResult(
                name=name,
                status="warn",
                message=redact.redact(
                    f"get_delivery arg-validation error: {exc.message}"
                ),
                remediation=_ARG_MISMATCH_REMEDIATION,
            )
        return CheckResult(
            name=name,
            status="fail",
            message=redact.redact(f"get_delivery failed: {exc.message}"),
            remediation="Check swarm DB/outbox consistency.",
        )
    returned_id = _extract_id(looked)
    if returned_id is not None and str(returned_id) != str(delivery_id):
        return CheckResult(
            name=name,
            status="fail",
            message=redact.redact(
                f"get_delivery returned mismatched id "
                f"(asked {delivery_id}, got {returned_id})"
            ),
            remediation="Check swarm DB/outbox consistency.",
        )
    return CheckResult(
        name=name,
        status="pass",
        message=redact.redact(
            f"fetched delivery {delivery_id} ({_fmt_latency(latency_ms)})"
        ),
    )


def _c015_self_notify_roundtrip(ctx) -> CheckResult:
    """C015: notify-to-self roundtrip is documented but NOT run by default."""
    return CheckResult(
        name="G3.swarm_self_notify_roundtrip",
        status="skip",
        message=redact.redact(
            "read-only default; see references/manual-write-probes.md"
        ),
        remediation=(
            "Follow the manual probe when the operator accepts an outbox "
            "mutation."
        ),
    )


# ---------------------------------------------------------------------------
# G4 — recall
# ---------------------------------------------------------------------------


def _c016_recall_probe(ctx) -> CheckResult:
    """C016: recall search executes without error even if no hits."""
    name = "G4.recall_probe"
    srv = ctx.server("recall")
    if srv is None:
        return CheckResult(
            name=name,
            status="skip",
            message=redact.redact("recall server not configured"),
        )
    try:
        payload, latency_ms = tool_call(
            srv.url,
            srv.token,
            "recall",
            {"query": "gbrain doctor smoke", "limit": 1},
        )
    except McpError as exc:
        if _is_arg_validation_error(exc):
            return CheckResult(
                name=name,
                status="warn",
                message=redact.redact(
                    f"recall.recall arg-validation error: {exc.message}"
                ),
                remediation=_ARG_MISMATCH_REMEDIATION,
            )
        return CheckResult(
            name=name,
            status="fail",
            message=redact.redact(f"recall.recall failed: {exc.message}"),
            remediation="Fix recall service, embeddings, or auth.",
        )
    hits = _count_items(payload)
    suffix = f" ({hits} hit(s))" if hits is not None else ""
    return CheckResult(
        name=name,
        status="pass",
        message=redact.redact(
            f"recall query ok ({_fmt_latency(latency_ms)}){suffix}"
        ),
    )


def _c017_recall_recent(ctx) -> CheckResult:
    """C017: recall recent read path works (warn on empty vault).

    Ground truth: ``recent`` REQUIRES a ``scope`` argument and a generic doctor
    cannot know the valid scope values for an arbitrary gbrain deployment. We
    therefore only smoke this check when a scope is provided (via a
    ``--probe-scope`` flag surfaced as ``ctx.probe_scope`` or the
    ``GBRAIN_PROBE_SCOPE`` env var); otherwise we ``skip`` cleanly. We never
    FAIL merely because the doctor omitted a required argument — C016
    (recall_probe) already exercises the read path.
    """
    name = "G4.recall_recent_probe"
    srv = ctx.server("recall")
    if srv is None:
        return CheckResult(
            name=name,
            status="skip",
            message=redact.redact("recall server not configured"),
        )
    scope = _resolve_probe_scope(ctx)
    if scope is None:
        return CheckResult(
            name=name,
            status="skip",
            message=redact.redact(
                "recall.recent requires a scope arg; set GBRAIN_PROBE_SCOPE to "
                "smoke it (C016 recall_probe covers read path)"
            ),
            remediation=(
                "Set GBRAIN_PROBE_SCOPE=<a valid recall scope> (or pass "
                "--probe-scope) to exercise recall.recent."
            ),
        )
    try:
        payload, latency_ms = tool_call(
            srv.url, srv.token, "recent", {"scope": scope, "limit": 1}
        )
    except McpError as exc:
        if _is_arg_validation_error(exc):
            return CheckResult(
                name=name,
                status="warn",
                message=redact.redact(
                    f"recall.recent arg-validation error (scope='{scope}'): "
                    f"{exc.message}"
                ),
                remediation=_ARG_MISMATCH_REMEDIATION,
            )
        return CheckResult(
            name=name,
            status="fail",
            message=redact.redact(f"recall.recent failed: {exc.message}"),
            remediation="Reindex vault or inspect recall service logs.",
        )
    count = _count_items(payload)
    if count == 0:
        return CheckResult(
            name=name,
            status="warn",
            message=redact.redact(
                f"recall.recent ok but vault empty ({_fmt_latency(latency_ms)})"
            ),
            remediation="Ingest notes; an empty vault yields no recall hits.",
        )
    suffix = f" ({count} item(s))" if count is not None else ""
    return CheckResult(
        name=name,
        status="pass",
        message=redact.redact(
            f"recall.recent ok ({_fmt_latency(latency_ms)}){suffix}"
        ),
    )


def _c018_reindex_check(ctx) -> CheckResult:
    """C018: recall reindex_check; tool exception (e.g. OperationalError) -> fail."""
    name = "G4.recall_reindex_check"
    srv = ctx.server("recall")
    if srv is None:
        return CheckResult(
            name=name,
            status="skip",
            message=redact.redact("recall server not configured"),
        )
    try:
        payload, latency_ms = tool_call(
            srv.url, srv.token, "reindex_check", {}
        )
    except McpError as exc:
        if _is_arg_validation_error(exc):
            return CheckResult(
                name=name,
                status="warn",
                message=redact.redact(
                    f"reindex_check arg-validation error: {exc.message}"
                ),
                remediation=_ARG_MISMATCH_REMEDIATION,
            )
        # A tool-level exception here is exactly the known aiosqlite/sqlite
        # OperationalError bug we want surfaced as a hard fail.
        return CheckResult(
            name=name,
            status="fail",
            message=redact.redact(f"reindex_check raised: {exc.message}"),
            remediation=(
                "Re-run ingest/reindex on the gbrain host; inspect recall "
                "dependencies (aiosqlite/sqlite)."
            ),
        )
    drift = _drift_count(payload)
    if drift:
        return CheckResult(
            name=name,
            status="warn",
            message=redact.redact(
                f"reindex_check reports {drift} drift row(s) "
                f"({_fmt_latency(latency_ms)})"
            ),
            remediation="Re-run ingest/reindex on the gbrain host.",
        )
    return CheckResult(
        name=name,
        status="pass",
        message=redact.redact(
            f"reindex_check ok, no drift ({_fmt_latency(latency_ms)})"
        ),
    )


# ---------------------------------------------------------------------------
# G5 — memory (dry / read-only)
# ---------------------------------------------------------------------------


def _c019_memory_tools(ctx) -> CheckResult:
    """C019: memory server exposes expected write tools (create_* etc.)."""
    name = "G5.memory_tools_registered"
    srv = ctx.server("memory")
    if srv is None:
        return CheckResult(
            name=name,
            status="fail",
            message=redact.redact("memory server not configured/unreachable"),
            remediation="Fix memory MCP service/tool gating.",
        )
    try:
        tools, latency_ms = tools_list(srv.url, srv.token)
    except McpError as exc:
        return CheckResult(
            name=name,
            status="fail",
            message=redact.redact(f"memory tools/list failed: {exc.message}"),
            remediation="Fix memory MCP service/tool gating.",
        )
    create_tools = [
        t.get("name", "")
        for t in tools
        if isinstance(t, dict) and str(t.get("name", "")).startswith("create_")
    ]
    if not create_tools:
        names = ", ".join(
            str(t.get("name", "?")) for t in tools if isinstance(t, dict)
        )
        return CheckResult(
            name=name,
            status="fail",
            message=redact.redact(
                f"no create_* tools on memory server (saw: {names or 'none'})"
            ),
            remediation="Fix memory MCP service/tool gating.",
        )
    return CheckResult(
        name=name,
        status="pass",
        message=redact.redact(
            f"memory exposes {len(create_tools)} create_* tool(s) "
            f"({_fmt_latency(latency_ms)})"
        ),
    )


def _c020_memory_schema(ctx) -> CheckResult:
    """C020: memory write tool schemas are readable without writing."""
    name = "G5.memory_tool_schema_readable"
    srv = ctx.server("memory")
    if srv is None:
        return CheckResult(
            name=name,
            status="skip",
            message=redact.redact("memory server not configured"),
        )
    try:
        tools, latency_ms = tools_list(srv.url, srv.token)
    except McpError as exc:
        return CheckResult(
            name=name,
            status="fail",
            message=redact.redact(
                f"memory tools/list malformed/failed: {exc.message}"
            ),
            remediation="Fix MCP server/tool registration.",
        )
    create_tools = [
        t
        for t in tools
        if isinstance(t, dict) and str(t.get("name", "")).startswith("create_")
    ]
    if not create_tools:
        return CheckResult(
            name=name,
            status="warn",
            message=redact.redact(
                "no create_* schemas to read (tools absent)"
            ),
            remediation="Fix MCP server/tool registration.",
        )
    with_schema = sum(
        1
        for t in create_tools
        if isinstance(t.get("inputSchema") or t.get("input_schema"), dict)
    )
    if with_schema == 0:
        return CheckResult(
            name=name,
            status="warn",
            message=redact.redact(
                f"{len(create_tools)} create_* tools but no readable inputSchema"
            ),
            remediation="Fix MCP server/tool registration.",
        )
    return CheckResult(
        name=name,
        status="pass",
        message=redact.redact(
            f"{with_schema}/{len(create_tools)} create_* schemas parse "
            f"({_fmt_latency(latency_ms)})"
        ),
    )


def _c021_memory_write_dry(ctx) -> CheckResult:
    """C021: memory write is NOT attempted in default doctor mode."""
    return CheckResult(
        name="G5.memory_write_probe_dry",
        status="skip",
        message=redact.redact("read-only default"),
        remediation=(
            "Use references/manual-write-probes.md for an operator-approved "
            "memory write smoke."
        ),
    )


# ---------------------------------------------------------------------------
# Payload-shape helpers — gbrain tool payloads vary; be defensive.
# ---------------------------------------------------------------------------


def _as_rows(payload: Any) -> list[dict]:
    """Best-effort extraction of a list of row dicts from a tool payload.

    Handles: a bare list, ``{"items": [...]}``, ``{"deliveries": [...]}``,
    ``{"pending": [...]}``, ``{"results": [...]}``, ``{"agents": [...]}``, or
    the mcp_streamable fallback ``{"content": [...]}``.
    """
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in (
            "items",
            "deliveries",
            "pending",
            "results",
            "agents",
            "rows",
            "data",
            "content",
        ):
            val = payload.get(key)
            if isinstance(val, list):
                # content[] may hold parsed dicts or strings; keep dicts.
                rows = [r for r in val if isinstance(r, dict)]
                if rows:
                    return rows
                # A content list of one parsed list/dict — recurse once.
                if key == "content" and len(val) == 1:
                    return _as_rows(val[0])
    return []


def _count_items(payload: Any) -> int | None:
    """Count list items in a tool payload, or ``None`` if not list-shaped."""
    if isinstance(payload, list):
        return len(payload)
    rows = _as_rows(payload)
    if rows:
        return len(rows)
    # An explicit count field is also acceptable.
    if isinstance(payload, dict):
        for key in ("count", "total"):
            val = payload.get(key)
            if isinstance(val, int):
                return val
    return None


def _is_failed_delivery(row: dict) -> bool:
    """Whether a delivery row represents a failed (non-acked) delivery."""
    status = str(row.get("status") or row.get("state") or "").lower()
    if status in ("failed", "error", "dead", "dlq", "rejected"):
        return True
    if row.get("last_error") or row.get("error"):
        return True
    # Explicit failure counter.
    fails = row.get("failures") or row.get("attempts_failed")
    if isinstance(fails, int) and fails > 0 and not row.get("acked"):
        return True
    return False


def _first_delivery_id(rows: list[dict]) -> Any:
    """Return the first usable delivery/task id from recent rows, else None."""
    for row in rows:
        for key in ("task_id", "id", "delivery_id", "message_id"):
            val = row.get(key)
            if val not in (None, ""):
                return val
    return None


def _extract_id(payload: Any) -> Any:
    """Extract an id from a get_delivery payload (dict or wrapped)."""
    if isinstance(payload, dict):
        for key in ("task_id", "id", "delivery_id", "message_id"):
            val = payload.get(key)
            if val not in (None, ""):
                return val
        # Wrapped row.
        rows = _as_rows(payload)
        if rows:
            return _first_delivery_id(rows)
    return None


def _status_word(payload: Any) -> str:
    """Extract a lower-cased status word from an agent_status payload."""
    if isinstance(payload, dict):
        for key in ("status", "state", "registry_status"):
            val = payload.get(key)
            if isinstance(val, str):
                return val.lower()
        # Wrapped row.
        rows = _as_rows(payload)
        if rows:
            return _status_word(rows[0])
    return ""


def _drift_count(payload: Any) -> int:
    """Count drift/mismatch rows reported by reindex_check (0 if none/unknown)."""
    if isinstance(payload, dict):
        for key in ("mismatches", "drift", "drift_rows", "out_of_sync"):
            val = payload.get(key)
            if isinstance(val, int):
                return val
            if isinstance(val, list):
                return len(val)
    return 0


def _compact_obj(payload: Any) -> str:
    """Render a tiny single-line summary of a stats-ish payload for messages."""
    try:
        if isinstance(payload, dict):
            # Keep only scalar values to avoid noisy/large messages.
            scalars = {
                k: v
                for k, v in payload.items()
                if isinstance(v, (int, float, str, bool))
            }
            if not scalars:
                return ""
            text = json.dumps(scalars, ensure_ascii=False, default=str)
            return text if len(text) <= 120 else text[:117] + "..."
    except Exception:  # noqa: BLE001 — message formatting must never raise
        return ""
    return ""
