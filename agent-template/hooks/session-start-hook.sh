#!/usr/bin/env bash
set -euo pipefail

# sdk-guard: skip when running as Agent SDK child to prevent recursion (issue #143)
if [ "${CLAUDE_SDK_CHILD:-0}" = "1" ]; then
    exit 0
fi

# SessionStart hook -- runs once at the start of a Claude Code session.
# 1) Logs that a session started.
# 2) If gbrain MCP credentials are present, calls gbrain-recall-on-start.sh to
#    prepend a "relevant gbrain recalls" block to core/hot/recent.md.
#
# Wire via templates/settings.json.template (SessionStart hook).
# Non-blocking: any failure exits 0.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS="${AGENT_WORKSPACE:-$(cd "$SCRIPT_DIR/.." && pwd)}"
AGENT_ID="${AGENT_ID:-$(basename "$(dirname "$WS")")}"
LOGDIR="$WS/logs"
HOOK_LOG="$LOGDIR/hooks.log"
HANDOFF="$WS/core/hot/handoff.md"

mkdir -p "$LOGDIR"
touch "$HOOK_LOG"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [session-start] $1" >> "$HOOK_LOG"; }

log "session started (agent=${AGENT_ID})"

# Surface handoff to the user via stderr (visible in hook output)
if [ -f "$HANDOFF" ] && [ -s "$HANDOFF" ]; then
    log "handoff present: $(wc -l <"$HANDOFF") lines"
fi

# Optional gbrain recall (only if env is set; install.sh writes these to a
# per-agent rc file you can `source` before launching Claude Code).
RECALL_SCRIPT="$WS/scripts/gbrain-recall-on-start.sh"
if [ -x "$RECALL_SCRIPT" ]; then
    if [ -n "${MCP_HOST:-}" ] && [ -n "${AGENT_BEARER:-}" ]; then
        log "running gbrain recall"
        bash "$RECALL_SCRIPT" >>"$HOOK_LOG" 2>&1 || log "gbrain-recall returned non-zero"
    else
        log "MCP_HOST or AGENT_BEARER unset; skipping recall"
    fi
fi

exit 0
