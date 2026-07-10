"""swarm-mcp worker — polls delivery_outbox and POSTs to agent gateways.

Transport: HTTP POST to URL from AGENT_GATEWAYS env (JSON map {agent: url}).
HTTP 200/2xx → mark_acked. 5xx/timeout/network → mark_retry with backoff.
4xx (except 429) → mark as failed (permanent client error).
Missing gateway URL for an agent → mark_retry (operator can configure later).

Per-agent outbound auth (extension 2026-05-17, Hermes integration):
- ``AGENT_GATEWAYS`` remains the existing JSON map ``{agent: url}``.
- Optional ``AGENT_GATEWAY_AUTH`` JSON map selects auth mode per agent:
  ``{"tyrande": "hmac:env:TYRANDE_WEBHOOK_HMAC",
     "claude":  "bearer:env:GATEWAY_WEBHOOK_TOKEN"}``.
  Spec ``<mode>:env:<ENV_VAR_NAME>`` resolves the secret from the named env var
  at load time. Raw secrets must never be embedded in ``AGENT_GATEWAY_AUTH``
  literally.
- Agents without an explicit ``AGENT_GATEWAY_AUTH`` entry keep the legacy
  behavior: use ``GATEWAY_WEBHOOK_TOKEN`` as a Bearer token if set, otherwise
  send no auth header. Bearer is therefore the default for backward
  compatibility.
- ``GBRAIN_HMAC_OUTBOUND_ENABLED=0`` disables HMAC signing globally; targets
  configured as HMAC are then returned as ``retry`` so they re-deliver after
  the operator re-enables outbound HMAC.
"""
import asyncio
import dataclasses
import json
import logging
import os
import signal
import sys
from typing import Literal

import httpx

os.environ.setdefault("MCP_PORT", "0")  # worker doesn't need MCP port

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.shared.config import Config
from services.shared.db import close_pool, get_pool
from services.shared.hmac_sign import sign_request

from . import outbox

logger = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 5
HTTP_TIMEOUT_SEC = 10
BATCH_SIZE = 20

# Bridge to a Telegram/HTTP gateway which expects POST {agentId, message, chatId}.
# Configure with env: OWNER_CHAT_ID (Telegram chat for forwarded prompts),
# COORDINATOR_AGENT (agent name used as the loop-prevention sink),
# GATEWAY_WEBHOOK_TOKEN (Bearer token if your gateway enforces auth).
OWNER_CHAT_ID = int(os.environ.get("OWNER_CHAT_ID", "0"))
GATEWAY_TOKEN = os.environ.get("GATEWAY_WEBHOOK_TOKEN", "")
COORDINATOR_AGENT = os.environ.get("COORDINATOR_AGENT", "coordinator-agent")


@dataclasses.dataclass(frozen=True)
class GatewayAuth:
    """Resolved per-agent gateway auth.

    Attributes:
        mode: Auth scheme. ``"bearer"`` adds ``Authorization: Bearer <value>``,
            ``"hmac"`` signs the body with the value as the raw secret bytes,
            ``"none"`` sends no auth header.
        value: Raw token (bearer) or raw secret (hmac). Empty string for
            ``none``. Treat as sensitive — never log or include in errors.
            ``repr=False`` so the secret does not leak via stray repr/log.
    """

    mode: Literal["bearer", "hmac", "none"]
    value: str = dataclasses.field(repr=False)


def _resolve_auth_spec(spec: str) -> GatewayAuth:
    """Resolve a single ``AGENT_GATEWAY_AUTH`` value.

    Supported forms:
        ``bearer:env:VAR_NAME``  -> Bearer with value from env var
        ``hmac:env:VAR_NAME``    -> HMAC with secret from env var
        ``none``                 -> no auth

    Unknown / empty / unresolvable specs degrade to ``GatewayAuth("none","")``.
    The literal raw token form is intentionally NOT supported here to keep raw
    secrets out of process arg lists / docker inspect output.
    """
    if not spec or not isinstance(spec, str):
        return GatewayAuth("none", "")
    spec = spec.strip()
    if spec == "none":
        return GatewayAuth("none", "")
    parts = spec.split(":", 2)
    if len(parts) != 3:
        return GatewayAuth("none", "")
    mode, source, name = parts[0].lower(), parts[1].lower(), parts[2]
    if mode not in ("bearer", "hmac"):
        return GatewayAuth("none", "")
    if source != "env":
        return GatewayAuth("none", "")
    value = os.environ.get(name, "")
    if not value:
        return GatewayAuth("none", "")
    return GatewayAuth(mode, value)  # type: ignore[arg-type]


def _load_gateway_auth() -> dict[str, GatewayAuth]:
    """Parse ``AGENT_GATEWAY_AUTH`` env JSON into a per-agent auth map.

    Returns an empty dict if the env var is unset or malformed. Each value is
    resolved via :func:`_resolve_auth_spec`; the returned map never carries an
    env var NAME, only the resolved raw secret/token.
    """
    raw = os.environ.get("AGENT_GATEWAY_AUTH", "")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception as exc:
        logger.error("AGENT_GATEWAY_AUTH parse failed: %s", exc)
        return {}
    if not isinstance(parsed, dict):
        logger.error("AGENT_GATEWAY_AUTH is not a JSON object, ignoring")
        return {}
    out: dict[str, GatewayAuth] = {}
    for agent, spec in parsed.items():
        out[str(agent)] = _resolve_auth_spec(str(spec))
    return out


def _gateway_auth_for(agent: str, auth_map: dict[str, GatewayAuth]) -> GatewayAuth:
    """Return the GatewayAuth to use for ``agent``.

    Priority:
        1. Explicit ``AGENT_GATEWAY_AUTH`` entry for the agent.
        2. Legacy fallback: ``GATEWAY_WEBHOOK_TOKEN`` env as Bearer.
        3. ``GatewayAuth("none", "")``.
    """
    if agent in auth_map:
        return auth_map[agent]
    if GATEWAY_TOKEN:
        return GatewayAuth("bearer", GATEWAY_TOKEN)
    return GatewayAuth("none", "")


def _serialize_gateway_body(body: dict) -> bytes:
    """Serialize a gateway webhook body to bytes exactly once.

    The returned bytes are what we sign AND what we POST — using the same
    bytes for both guarantees the verifier sees identical content. ``httpx``
    is invoked with ``content=<bytes>`` (not ``json=``) so it does not
    re-serialize the dict.
    """
    return json.dumps(body, ensure_ascii=False, sort_keys=False, separators=(",", ":")).encode("utf-8")


def _hmac_outbound_enabled() -> bool:
    """Whether outbound HMAC signing is globally enabled.

    Default: enabled. Set ``GBRAIN_HMAC_OUTBOUND_ENABLED=0`` for emergency
    rollback — HMAC targets then defer to retry until re-enabled.
    """
    return os.environ.get("GBRAIN_HMAC_OUTBOUND_ENABLED", "1") != "0"


# Fields surfaced explicitly in the prompt header; every OTHER substantive
# payload key (question/answers/deliverable/context/subject…) is rendered by
# _render_payload_content so the agent sees the actual task inline. Fixes the
# "(no title) + empty body" bug: the prompt used to show only title/body and
# silently dropped every field a sender put elsewhere, so recipients saw an
# "empty" task. Internal (_-prefixed) routing keys stay hidden.
_PROMPT_HIDDEN_KEYS = frozenset({"title", "body", "urgency"})
# Chat gateways may relay this to a real chat; clamp below the 4096-char limit.
_PROMPT_MAX = 3800


def _render_payload_content(payload: dict) -> str:
    """Render the substantive payload fields (beyond title/body) as text."""
    parts: list[str] = []
    for k, v in payload.items():
        if k in _PROMPT_HIDDEN_KEYS or k.startswith("_"):
            continue
        if v is None or v == "" or (isinstance(v, (list, dict)) and not v):
            continue
        rendered = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=2)
        parts.append(f"{k}: {rendered}")
    return "\n".join(parts)


def _finalize_prompt(text: str, task_id: str) -> str:
    """Clamp the whole prompt below the chat length limit with a get_delivery
    hint (reserving the hint's length so it is never truncated)."""
    if len(text) <= _PROMPT_MAX:
        return text
    hint = f"\n… (truncated; full payload: swarm.get_delivery(\"{task_id[:128]}\"))"
    return text[: _PROMPT_MAX - len(hint)] + hint


def _format_virtual_prompt(from_agent: str, to_agent: str, task_id: str, payload: dict) -> str:
    """Pack inter-agent payload into a chat-style prompt the agent will see.

    The receiving agent sees this as if it came from the owner via the
    chat gateway (synthetic update). Agent must call swarm.ack(task_id) when done.

    Loop-prevention gates:
    - Ack-only fast path for: (a) reports back to the coordinator (COORDINATOR_AGENT),
      detected by title prefix "Report from " or `_origin_task` field; (b) explicit
      smoke pings via `_smoke=true`. These skip the full report + dual-notify flow
      which would otherwise cause infinite recursion (coordinator → coordinator).
    """
    title = payload.get("title") or "(no title)"
    body = payload.get("body") or ""
    urgency = payload.get("urgency") or payload.get("_priority") or "normal"
    reason = payload.get("_escalation_reason") or ""
    extra = ""
    if reason:
        extra = f"\nEscalation reason: {reason}"
    content = _render_payload_content(payload)
    content_block = f"\n{content}" if content else ""

    # Hard loop gate: COORDINATOR_AGENT is the coordinator; it never needs a
    # dual-report back to itself. Any swarm.notify(coordinator, ...) → ack-only.
    # Plus explicit smoke pings (`_smoke=true`) for any target.
    is_to_coordinator = to_agent == COORDINATOR_AGENT
    is_smoke = bool(payload.get("_smoke"))

    if is_to_coordinator or is_smoke:
        if is_to_coordinator:
            kind_hint = (
                "retro-summary"
                if str(title).startswith("Report from") or payload.get("_origin_task")
                else "request to coordinator"
            )
        else:
            kind_hint = "smoke ping"
        return _finalize_prompt(
            f"[Inter-agent from {from_agent} -> {to_agent}] urgency={urgency} ({kind_hint})\n"
            f"Task: {title}\n"
            f"{body}{extra}{content_block}\n"
            f"---\n"
            f"ACTIONS (ack-only fast path, no dual-report):\n"
            f"1. Inspect payload and decide if action is needed. Coordinator targets "
            f"and smoke pings do not require a full chat report.\n"
            f"2. If meaningful, send the owner a 1-3 line note via the chat gateway. "
            f"Otherwise skip.\n"
            f"3. DO NOT swarm.notify back (loop risk). Go straight to swarm.ack.\n"
            f"4. swarm.ack(task_id=\"{task_id}\").",
            task_id,
        )

    return _finalize_prompt(
        f"[Inter-agent from {from_agent} -> {to_agent}] urgency={urgency}\n"
        f"Task: {title}\n"
        f"{body}{extra}{content_block}\n"
        f"---\n"
        f"ACTIONS:\n"
        f"1. Execute the task.\n"
        f"2. Send the owner a detailed chat report. Format:\n"
        f"\n"
        f"   Task from {from_agent}: <short name>\n"
        f"\n"
        f"   What I did:\n"
        f"   - concrete step 1 (paths/commands/numbers)\n"
        f"   - concrete step 2\n"
        f"   - ...\n"
        f"\n"
        f"   Result:\n"
        f"   - what worked, what failed, gaps found\n"
        f"   - links to files/commits/PRs if applicable\n"
        f"\n"
        f"   Time spent: <minutes or mm:ss>\n"
        f"\n"
        f"   Avoid one-liner 'done, acked' reports. The owner wants substance, "
        f"at least 5-10 lines.\n"
        f"3. ALSO SEND A SHORT SUMMARY TO THE COORDINATOR via swarm.notify:\n"
        f"   swarm.notify(to_agent=\"{COORDINATOR_AGENT}\", payload={{"
        f"\"title\": \"Report from {to_agent}: <task name>\", "
        f"\"body\": \"<2-4 bullets: what done + commit/path + gaps>\", "
        f"\"_origin_task\": \"{task_id}\"}})\n"
        f"   Without this step the coordinator cannot see your work or schedule "
        f"follow-ups. Chat report = owner, swarm.notify = coordinator. Two recipients.\n"
        f"4. Call swarm.ack(task_id=\"{task_id}\") at the very end.",
        task_id,
    )


class _ShutdownFlag:
    def __init__(self) -> None:
        self.requested = False

    def set(self) -> None:
        self.requested = True


_shutdown = _ShutdownFlag()


def _handle_signal(sig: int, _frame: object) -> None:
    logger.info("Received signal %d, requesting shutdown", sig)
    _shutdown.set()


def _load_gateways() -> dict[str, str]:
    """Parse AGENT_GATEWAYS env JSON: {"agent_name": "http://...", ...}."""
    raw = os.environ.get("AGENT_GATEWAYS", "{}")
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            logger.error("AGENT_GATEWAYS is not a JSON object, ignoring")
            return {}
        return {str(k): str(v) for k, v in parsed.items()}
    except Exception as exc:
        logger.error("AGENT_GATEWAYS parse failed: %s", exc)
        return {}


async def _deliver_one(
    client: httpx.AsyncClient,
    gateways: dict[str, str],
    row: object,
    auth_map: dict[str, GatewayAuth] | None = None,
) -> tuple[str, str]:
    """Try to deliver one row. Returns (status, last_error).

    Selects per-agent auth via ``auth_map`` (resolved from ``AGENT_GATEWAY_AUTH``)
    with a legacy ``GATEWAY_WEBHOOK_TOKEN`` Bearer fallback. The request body
    bytes are serialized exactly once and shared between signature computation
    (when HMAC) and the POST itself — this is the integrity invariant.
    """
    to_agent = row["to_agent"]
    url = gateways.get(to_agent)
    if not url:
        return "retry", f"no gateway URL for agent={to_agent}"

    payload = json.loads(row["payload_json"])
    # Repackage into gateway webhook schema.
    body = {
        "agentId": to_agent,
        "message": _format_virtual_prompt(row["from_agent"], to_agent, row["task_id"], payload),
        "chatId": OWNER_CHAT_ID,
    }

    auth = _gateway_auth_for(to_agent, auth_map or {})
    headers: dict[str, str] = {"Content-Type": "application/json"}
    body_bytes = _serialize_gateway_body(body)

    if auth.mode == "hmac":
        if not _hmac_outbound_enabled():
            return "retry", f"hmac_outbound_disabled for agent={to_agent}"
        sig_headers = sign_request(auth.value.encode("utf-8"), body_bytes)
        headers.update(sig_headers)
    elif auth.mode == "bearer":
        headers["Authorization"] = f"Bearer {auth.value}"
    # mode == "none": no auth header.

    try:
        resp = await client.post(url, content=body_bytes, headers=headers, timeout=HTTP_TIMEOUT_SEC)
    except httpx.TimeoutException as exc:
        return "retry", f"timeout: {exc}"
    except httpx.HTTPError as exc:
        return "retry", f"http_error: {type(exc).__name__}: {exc}"

    if 200 <= resp.status_code < 300:
        return "acked", ""
    if resp.status_code == 429:
        return "retry", f"http_429"
    if 400 <= resp.status_code < 500:
        return "failed", f"http_{resp.status_code}: {resp.text[:200]}"
    return "retry", f"http_{resp.status_code}: {resp.text[:200]}"


async def run() -> None:
    config = Config(mcp_port=0)
    pool = await get_pool(config)
    n_recovered = await outbox.bootstrap_recovery(pool)
    gateways = _load_gateways()
    auth_map = _load_gateway_auth()
    logger.info(
        "swarm-worker started: gateways=%s auth_modes=%s recovered=%d poll=%ds",
        list(gateways.keys()),
        {a: v.mode for a, v in auth_map.items()},
        n_recovered,
        POLL_INTERVAL_SEC,
    )

    async with httpx.AsyncClient() as client:
        while not _shutdown.requested:
            try:
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        rows = await conn.fetch(
                            """
                            SELECT id, task_id, from_agent, to_agent, payload::text AS payload_json,
                                   attempts, max_attempts
                            FROM delivery_outbox
                            WHERE status = 'pending' AND next_retry_at <= now()
                            ORDER BY created_at
                            LIMIT $1
                            FOR UPDATE SKIP LOCKED
                            """,
                            BATCH_SIZE,
                        )
                        if rows:
                            logger.info("processing batch=%d", len(rows))
                        for row in rows:
                            status, last_error = await _deliver_one(client, gateways, row, auth_map)
                            if status == "acked":
                                await outbox.mark_acked(conn, row["task_id"])
                            elif status == "failed":
                                await conn.execute(
                                    """
                                    UPDATE delivery_outbox
                                    SET status='failed', attempts=$2, updated_at=now()
                                    WHERE id=$1
                                    """,
                                    row["id"], row["attempts"] + 1,
                                )
                                logger.warning("delivery failed permanently id=%d to=%s err=%s",
                                               row["id"], row["to_agent"], last_error[:120])
                            else:  # retry
                                await outbox.mark_retry(
                                    conn, row["id"], row["attempts"] + 1,
                                    row["max_attempts"], last_error,
                                )
            except Exception:
                logger.exception("worker loop error")

            for _ in range(POLL_INTERVAL_SEC):
                if _shutdown.requested:
                    break
                await asyncio.sleep(1)

    await close_pool()
    logger.info("swarm-worker stopped")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    asyncio.run(run())


if __name__ == "__main__":
    import services.swarm_mcp.worker as _self
    sys.exit(_self.main())
