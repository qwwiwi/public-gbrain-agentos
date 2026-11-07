---
name: instagram-superpower
description: >
  Instagram analytics via Loore: top reels per account, watchlist of accounts
  to scan periodically, engagement scoring. Use when: (1) you need to analyze
  an Instagram account (followers, posts, top reels), (2) build a watchlist of
  competitors, (3) find the most-engaging reels of an account over a window of
  N days.
  Triggers: «analyze instagram account», «top reels», «watchlist», «engagement»,
  Instagram profile URLs.
  NOT for: YouTube, Twitter, mass spam, auto-likes, auto-follows.
---

# instagram-superpower

Analytics for Instagram accounts using [Loore](https://loore.ai) (REST API, CLI and
MCP server; paid per call). Docs: [docs.loore.ai](https://docs.loore.ai).

The **download** half of this skill (media files from Instagram CDN via a
self-hosted Cobalt instance) was tightly coupled to a private VPS layout in
the original codebase. It has been removed from this public distro. If you
want it back, deploy your own [Cobalt](https://github.com/imputnet/cobalt) and
wire its URL into your own script.

## Setup

```sh
export LOORE_API_KEY=<your-key>
# or keep it in a file (read by analyze.sh when the env var is unset):
mkdir -p ~/.secrets/loore
echo "<YOUR_LOORE_API_KEY>" > ~/.secrets/loore/api-key
chmod 600 ~/.secrets/loore/api-key
```

Get a key at [loore.ai](https://loore.ai) (Settings → API and MCP). One key works for REST
and MCP (`https://mcp.loore.ai/mcp`).

## Usage

### Analyze a single account

```sh
bash scripts/analyze.sh <username> [top_N] [days]
# Example:
bash scripts/analyze.sh someuser 5 14
```

Output: profile stats + top-N reels by engagement (views + likes*10) for the
last N days, both human-readable and JSON.

### Watchlist

```sh
bash scripts/watchlist.sh list
bash scripts/watchlist.sh add <username>
bash scripts/watchlist.sh remove <username>
bash scripts/watchlist.sh scan [top_N] [days]
```

`scan` walks every account in `~/.secrets/cobalt/watchlist.json` and calls
`analyze.sh` on each with a 10-second pause between requests.

## API calls & balance

`analyze.sh` calls the Loore REST API: `POST https://mcp.loore.ai/v1/<method>` with
`Authorization: Bearer $LOORE_API_KEY` (methods `ig-profile`, `ig-user-reels`; the same
tools are available over MCP as `ig_profile`, `ig_user_reels`). Other `ig_*` tools cover
posts, comments, stories and more; `analyze_reel` returns a reel transcript.

```sh
curl -X POST https://mcp.loore.ai/v1/ig-profile \
  -H "Authorization: Bearer $LOORE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"username":"@someuser"}'
```

Paid calls may return HTTP 428 with `spend_authorization`: approve the spend, then retry
with the returned authorization id. The credit balance is printed at the end of
`analyze.sh` output (`billing-balance` is a free call).

## Omitted scripts

- `scripts/download.sh` — required a private Cobalt VPS.
- `scripts/check-cookies.sh` — same.
- `scripts/deploy-cookies.sh` — same.
- `references/cookie-refresh.md` — credential-rotation runbook for a private setup.

These are kept as stubs that exit 1, so existing references do not crash; replace
with your own implementation if you need media download.
