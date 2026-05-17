#!/usr/bin/env bash
# shellcheck disable=SC2012
set -euo pipefail

# sdk-guard: skip when running as Agent SDK child to prevent recursion (issue #143)
if [ "${CLAUDE_SDK_CHILD:-0}" = "1" ]; then
    exit 0
fi

# PreCompact hook -- snapshot recent.md before Claude Code auto-compacts context.
# Keeps the last N pre-compact snapshots so you can recover state if compaction
# loses information you cared about.
#
# Wire via templates/settings.json.template (PreCompact hook).
# Non-blocking: any failure exits 0.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS="${AGENT_WORKSPACE:-$(cd "$SCRIPT_DIR/.." && pwd)}"
AGENT_ID="${AGENT_ID:-$(basename "$(dirname "$WS")")}"
HOT="$WS/core/hot/recent.md"
SNAP_DIR="$WS/core/hot/pre-compact"
LOGDIR="$WS/logs"
HOOK_LOG="$LOGDIR/hooks.log"
KEEP_SNAPSHOTS="${KEEP_SNAPSHOTS:-10}"

mkdir -p "$SNAP_DIR" "$LOGDIR"
touch "$HOOK_LOG"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [precompact] $1" >> "$HOOK_LOG"; }

if [ ! -f "$HOT" ] || [ ! -s "$HOT" ]; then
    log "no recent.md to snapshot"
    exit 0
fi

TS=$(date -u +%Y%m%d-%H%M%S)
SNAP="$SNAP_DIR/recent-${TS}.md"
cp "$HOT" "$SNAP" || { log "snapshot copy failed"; exit 0; }
log "snapshot saved: $SNAP ($(wc -c <"$SNAP") bytes)"

# Rotate: keep newest N
COUNT=$(ls -1 "$SNAP_DIR"/recent-*.md 2>/dev/null | wc -l | tr -d ' ')
if [ "$COUNT" -gt "$KEEP_SNAPSHOTS" ]; then
    REMOVE=$((COUNT - KEEP_SNAPSHOTS))
    ls -1t "$SNAP_DIR"/recent-*.md | tail -n "$REMOVE" | while read -r old; do
        rm -f "$old"
        log "rotated out: $old"
    done
fi

exit 0
