#!/usr/bin/env bash
set -euo pipefail

# Instagram SuperPower -- Analyze Account via Loore REST (https://loore.ai)
# Usage: bash analyze.sh <username> [top_N] [days]
# Example: bash analyze.sh garyvee 5 14
# Key: LOORE_API_KEY env var, or ~/.secrets/loore/api-key

USERNAME="${1:?Usage: analyze.sh <username> [top_N] [days]}"
TOP_N="${2:-5}"
DAYS="${3:-14}"

LOORE_KEY_FILE="$HOME/.secrets/loore/api-key"

if [ -z "${LOORE_API_KEY:-}" ]; then
    if [ ! -f "$LOORE_KEY_FILE" ]; then
        echo "ERROR: LOORE_API_KEY is not set and no key file at $LOORE_KEY_FILE"
        exit 1
    fi
    LOORE_API_KEY=$(cat "$LOORE_KEY_FILE")
fi
export LOORE_API_KEY
export LOORE_API_BASE="${LOORE_API_BASE:-https://mcp.loore.ai/v1}"

echo "Analyzing @${USERNAME} -- top ${TOP_N} reels, last ${DAYS} days..."
echo ""

python3 - "$USERNAME" "$TOP_N" "$DAYS" << 'PYEOF'
import json, os, sys, urllib.error, urllib.request
from datetime import datetime, timezone, timedelta

username = sys.argv[1]
top_n = int(sys.argv[2])
days = int(sys.argv[3])
loore_key = os.environ["LOORE_API_KEY"]
base = os.environ["LOORE_API_BASE"].rstrip("/")

def api_post(method, body=None):
    """POST https://mcp.loore.ai/v1/<method>; returns the `result` of the envelope."""
    req = urllib.request.Request(
        f"{base}/{method}",
        data=json.dumps(body or {}).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {loore_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            envelope = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        if e.code == 428:
            # Paid call needs spend authorization: approve it in Loore, then retry
            # with spend_authorization_id in the request body.
            raise RuntimeError(f"HTTP 428 spend authorization required: {detail}")
        raise RuntimeError(f"HTTP {e.code}: {detail}")
    if envelope.get("status") != "ok":
        raise RuntimeError(f"Loore error: {envelope.get('error', envelope)}")
    return envelope.get("result", {})

def parse_ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

# 1. Profile
print(f"Looking up @{username}...")
try:
    user = api_post("ig-profile", {"username": username}).get("profile", {})
    user_id = user.get("id")
    print(f"Account: @{user.get('username', username)}")
    print(f"Full name: {user.get('fullName', '?')}")
    print(f"Followers: {user.get('followers', 0):,}")
    print(f"Following: {user.get('following', 0):,}")
    print(f"Posts: {user.get('posts', 0):,}")
    print(f"Verified: {user.get('isVerified', '?')}")
    print(f"Category: {user.get('category', '?')}")
    print(f"Bio: {(user.get('biography') or '')[:100]}")
    print()
except Exception as e:
    print(f"Profile lookup failed: {e}")
    sys.exit(1)

# 2. Reels
print("Fetching reels...")
cutoff = datetime.now(timezone.utc) - timedelta(days=days)
all_reels = []
cursor = None

for page in range(5):  # max 5 pages (~250 reels)
    body = {"limit": 50}
    if user_id:
        body["user_id"] = str(user_id)
    else:
        body["username"] = username
    if cursor:
        body["cursor"] = cursor

    try:
        data = api_post("ig-user-reels", body)
    except Exception as e:
        print(f"Reels fetch error: {e}")
        break

    items = data.get("items", [])
    if not items:
        break

    for item in items:
        taken_dt = parse_ts(item["takenAt"]) if item.get("takenAt") else None
        if taken_dt is None or taken_dt < cutoff:
            continue

        views = item.get("views") or 0
        likes = item.get("likes") or 0  # absent when the author hid likes
        comments = item.get("comments") or 0
        caption = (item.get("caption") or "")[:200]

        all_reels.append({
            "code": item.get("code", ""),
            "url": item.get("url", ""),
            "date": taken_dt.strftime("%Y-%m-%d"),
            "views": views,
            "likes": likes,
            "comments": comments,
            "caption": caption.replace("\n", " "),
            "engagement": views + likes * 10,
        })

    cursor = data.get("nextCursor")
    if not data.get("hasMore") or not cursor:
        break
    last = items[-1].get("takenAt")
    if last and parse_ts(last) < cutoff:
        break

# 3. Sort and display
all_reels.sort(key=lambda r: r["engagement"], reverse=True)
top = all_reels[:top_n]

print(f"\nFound {len(all_reels)} reels in last {days} days")
print(f"TOP {len(top)} by engagement:\n")
print("=" * 70)

for i, r in enumerate(top, 1):
    print(f"\n#{i}")
    print(f"  URL: {r['url']}")
    print(f"  Date: {r['date']}")
    print(f"  Views: {r['views']:,}")
    print(f"  Likes: {r['likes']:,}")
    print(f"  Comments: {r['comments']:,}")
    print(f"  Caption: {r['caption'][:120]}")
    print(f"  Engagement: {r['engagement']:,}")

print("\n" + "=" * 70)
print("JSON:")
print(json.dumps(top, ensure_ascii=False, indent=2))

# 4. Balance (free call)
try:
    balance = api_post("billing-balance")
    print(f"\nLoore credits left: {balance['creditsRemaining']:,}")
except Exception:
    pass
PYEOF

echo ""
echo "Done."
