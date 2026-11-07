# Skills bundle

A small bundle of Claude Code skills that pair well with this gbrain distro.
Most are independent — you can install one, several, or none of them.

## How to install

Pick one of these per skill:

```sh
# 1. Symlink (recommended — survives skill updates via git pull on this repo):
ln -s "$PWD/skills/<name>" ~/.claude/skills/<name>

# 2. Or per-agent:
ln -s "$PWD/skills/<name>" ~/.claude-lab/<your-agent>/.claude/skills/<name>

# 3. Or copy:
cp -R skills/<name> ~/.claude/skills/<name>
```

After install, Claude Code picks up the skill on the next session and routes to
it when the description matches.

## Skills in this bundle

| Skill | What it does | Needs |
|---|---|---|
| `agent-browser` | Browser automation CLI (Chrome/Chromium via CDP). Navigate, click, fill forms, screenshot. | `agent-browser` npm/cargo/brew binary |
| `markdown-new` | Clean Markdown extraction from any URL via `markdown.new`. Drop-in replacement for noisy `web_fetch`. | none |
| `groq-voice` | Transcribe voice messages (`.ogg`) via Groq Whisper. | `GROQ_API_KEY` |
| `transcript` | YouTube transcripts via Loore (`yt_transcript`). | `LOORE_API_KEY` (https://loore.ai) |
| `twitter` | Read tweets, threads, profiles, articles via FxTwitter (free) + SocialData (paid fallback). | `SOCIALDATA_API_KEY` for non-free endpoints |
| `perplexity-research` | Web research with citations via Perplexity Sonar. | `PERPLEXITY_API_KEY` |
| `mcp-builder` | Anthropic-authored skill that helps you build new MCP servers from scratch. | none |
| `telegram-chip` | Telegram **user-account** (MTProto/Telethon) wrapper, exposes a local HTTP API. | `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, interactive login. **Use only if you actually need a user account**, not for bots. |
| `instagram-superpower` | Instagram analytics via Loore (top reels, watchlist). Media-download half was private; only the analytics scripts ship. | `LOORE_API_KEY` (https://loore.ai). |

## What was removed during sanitization

- `instagram-superpower/scripts/{download,check-cookies,deploy-cookies}.sh` —
  required a private self-hosted Cobalt VPS and SSH key layout. Stub scripts
  remain in place that exit 1 with a "omitted from public distro" message.
- `instagram-superpower/references/cookie-refresh.md` — credential-rotation
  runbook for that same private setup.
- Various hardcoded Tailscale IPs, internal user-ids, and personal home paths
  across all skills were replaced with placeholders.

## Skill independence

These skills do not depend on each other and do not depend on gbrain. You
can run any one of them standalone in a vanilla Claude Code workspace. Some
of them (e.g. `transcript`, `markdown-new`) pair nicely with `inbox-agent`'s
`compile.sh` step, but you have to wire that yourself — the bundled
`compile.sh` does not call skills by default.
