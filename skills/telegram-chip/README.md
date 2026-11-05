# telegram-chip

> **Если диалога нет – не пишем. Первым пишет только человек.**
> Hard rule (ban risk): the account owner's account NEVER writes first. `POST /messages/send`,
> `/messages/send-file` and `/messages/forward` to a private chat are refused with HTTP 403
> `{"success": false, "data": null, "error": "no dialog: contact never wrote first – sending is forbidden (ban risk)"}`
> unless that user has at least one incoming message in the chat. Groups/channels and Saved Messages are unaffected.
> Do not work around it (other clients, drafts-as-send, invites). The legacy `main.py` MCP server does NOT enforce it – keep it disabled.

Single-client Telegram integration built on Telethon.

## What it provides

- HTTP API (FastAPI, default `:8080`)
- Optional MCP stdio server
- Optional sales webhook hooks
- Chat export (`tgdl`-style JSON)

## Architecture

One Telethon client is the source of truth. All operations should go through that single process.

## Configuration

Use `.env` in this folder.

Required:

```env
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
TELEGRAM_SESSION_STRING=...
```

Optional:

```env
TELEGRAM_SESSION_NAME=telegram_session
SALES_WEBHOOK_URL=http://localhost:18789/api/cron/wake
SALES_MONITORED_USERS=12345,67890
```

## Recommended authorization flow (important)

1. Obtain `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` at `https://my.telegram.org/apps`.
   - Prefer mobile internet without Wi‑Fi/VPN, or use a fresh private/incognito browser session.
2. Put API credentials into `.env`.
3. Run:

```bash
./generate-session.sh
```

4. Complete prompts:
   - phone number (`+...`),
   - login code from Telegram,
   - 2FA password (if enabled).
5. Verify session string:

```bash
grep '^TELEGRAM_SESSION_STRING=' .env
```

## Security rules

- Treat `TELEGRAM_SESSION_STRING` like a password.
- Never send it in chats/issues/logs.
- If leaked, revoke/regenerate immediately.
- Do not run multiple Telethon processes with the same session string.
- Keep Telethon isolated to `skills/telegram-chip-ws/.venv` and access Telegram from other code only via HTTP API.

## Run API

```bash
.venv/bin/python api.py
```

Health check:

```bash
curl http://127.0.0.1:8080/health
```

## Main endpoints

- `GET /health`
- `GET /chats/list?limit=50&chat_type=user|group|channel&unread_only=false` — chat metadata. Each item includes `unread_count`, `is_pinned`, `read_outbox_max_id`, `read_inbox_max_id`, `top_message_id`.
- `GET /chats/{chat_id}/read_status` — read receipts for one chat: `{read_outbox_max_id, read_inbox_max_id, top_message_id, unread_count}`. Lighter than `/chats/list` (single peer via `GetPeerDialogsRequest`).
- `GET /chats/{chat_id}/messages?page=1&page_size=20` — text lines `ID: n | sender | Date: … | Message: <text>`; media messages get a marker (see «Media»)
- `GET /chats/{chat_id}/messages/{message_id}` — single message; media fields `media_kind`, `media_marker`, `file_name`, `mime_type`, `size`, `duration` (null when not applicable); if `media_type=MessageMediaPoll`, response includes `poll{ question, answers, results, total_voters, multiple_choice, public_voters, closed }`
- `GET /chats/{chat_id}/messages/{message_id}/media` — download media (see «Media» below)
- `GET /chats/{chat_id}/messages/{message_id}/media?transcribe=1` — download + transcribe voice/video-note/audio/video
- `GET /chats/{chat_id}/messages/{message_id}/media/file` — raw bytes of the (cached) media file
- `POST /messages/send` — refused with 403 if the private contact never wrote first (see the rule at the top)
- `GET /folders`, `POST /folders/upsert` — chat folders (see «Chat folders»)
- `POST /messages/edit`
- `POST /messages/delete`
- `POST /tgdl/export?chat_id=<id>&limit=500`

## Endpoint reference (matches `api.py`)

Read (safe, no write risk):
- `GET /health`, `GET /me`, `GET /resolve/{username}`, `GET /users/{user_id}/status`
- `GET /chats`, `GET /chats/list`, `GET /chats/{id}`, `GET /chats/{id}/messages`, `GET /chats/{id}/messages/{msg}`
- `GET /chats/{id}/messages/{msg}/media` (download to cache, `v=2`, `transcribe=1`) and `GET /chats/{id}/messages/{msg}/media/file` (the bytes)
- `GET /chats/{id}/participants`, `/admins`, `/invite-link`, `/read_status`
- `GET /contacts`, `GET /contacts/search`, `GET /poll/new-dms`, `GET /folders` (chat folders)

Write (rate-limited; private chats obey the never-write-first rule):
- `POST /messages/send`, `/messages/send-file`, `/messages/forward`, `PUT /messages/edit`, `DELETE /messages/delete`, `POST /messages/search`
- `POST /folders/upsert` (create/update one folder by exact title; no delete endpoint)
- `POST /supergroups` (create a megagroup with invites and visible history), `POST /chats/{id}/history-visible`
- `POST /groups` (basic group), `POST /groups/invite`, `POST /admin/promote`, `/admin/ban`, `/admin/unban`
- `POST /chats/{id}/archive|unarchive|mute|unmute|leave`, `POST /contacts`, `DELETE /contacts/{id}`, `POST /drafts/save`, `DELETE /drafts/{id}`
- `POST /tgdl/export`

## Chat folders (lead tags)

- `GET /folders?include_chats=true` — list folders: `id, title, type (filter|chatlist|default), color, emoticon, pinned_count, include_count, exclude_count, pinned_peer_ids, include_peer_ids` (+ `chats` when `include_chats=true`; use `include_chats=false` for a cheap call).
- `POST /folders/upsert` — body `{"title": "Горячие", "color": 0-6|null, "emoticon": "🔥", "include_chat_ids": [123, "-100..."], "mode": "replace"|"add"|"remove"}`.
  Finds the folder by EXACT title (creates it with the lowest free id 2..255 if absent) and sets its chats. Only that folder is touched – other folders are never modified; there is no delete endpoint.
  `color`/`emoticon` change only when sent (`color: null` clears). Response `data`: `{action: created|updated|unchanged, id, title, mode, color, emoticon, pinned_count, include_count, added, removed, include_peer_ids}`.
  Errors: 422 validation / title >12 chars / >200 chats (premium limit) / unresolvable chat ids (nothing written) / result would be empty; 409 title is a shared chat list or no free id; 429 FloodWait; 502 upstream.
  All Telegram calls go through the shared rate limiter. Unknown ids trigger one `get_dialogs` cache warm-up per request.

## Media (files, photos, voice, video notes)

**Empty `Message:` text does not mean an empty message — check for media.** Since 2026-10-16 the messages list prefixes media messages with a marker, followed by the caption if any:

```
ID: 812 | Ivan | Date: ... | Message: [файл: dogovor.pdf, 1.2 MB] посмотри до вечера
ID: 813 | Ivan | Date: ... | Message: [голосовое 0:10]
```

Markers: `[фото]`, `[файл: name, size]`, `[голосовое m:ss]`, `[кружок m:ss]`, `[видео m:ss]`, `[аудио: title, m:ss]`, `[GIF]`, `[стикер]`, `[опрос: question]`, `[геолокация]`, `[контакт: name]`, `[вложение: Type]`. Link previews get no marker.

Download:

```bash
curl 'http://127.0.0.1:8080/chats/123456789/messages/813/media?v=2'
# data: {"success": true, "path": "./media-cache/123456789/813.ogg",
#        "file_name": "813.ogg", "mime_type": "audio/ogg", "size": 40213,
#        "kind": "voice", "duration": 10, "cached": false}
```

- Files land in `media-cache/<chat>/<msg>.<ext>` inside the skill dir on the chip host (dir 700, files 600). Repeat calls are served from cache with no Telegram request (`cached: true`).
- The chip may run on a different host than its caller (for example reached through an SSH tunnel on `127.0.0.1:8080`). `path` is a path on the chip host — from another machine fetch the bytes with `/media/file`:
  `curl -o doc.pdf http://127.0.0.1:8080/chats/123456789/messages/812/media/file`
- **Two error contracts.** Default `v=1` (legacy, unchanged for old callers): always HTTP 200, failure = `data` `{"success": false, "error": "..."}` (e.g. `"Message has no media"`), unexpected Telegram errors = `success: false` in the envelope. Opt in with `?v=2` (implied by `transcribe=1` and by `/media/file`) to get HTTP codes: 404 no message/media, 413 over `CHIP_MEDIA_MAX_BYTES` (default 50 MB), 422 poll/geo/link-preview without file, 429 Telegram FloodWait, 502 upstream error, 504 timeout.
- Downloadable: photos, documents, voice, video, video notes, audio, stickers, GIFs, contacts (`.vcf`), and the document/photo inside a link preview.
- `output_path=` still works as before (parent dir created; a trailing `/` means «save inside this directory» with a Telegram-generated name). Not cached.
- Rate limits: every Telegram read goes through the global rate limit + FloodWait retry (`RATE_LIMIT_PROTECTION.md`); at most 2 concurrent downloads, and a FloodWait backoff releases its slot while sleeping. The global limiter serializes permits (concurrent callers are spaced 0.2 s apart). Telethon itself auto-sleeps FloodWaits ≤ 60 s (`flood_sleep_threshold`) inside a download, so one slot may be held up to ~60 s; other downloads just queue, they do not fail. Concurrent requests for the same attachment are serialized by a per-message lock (one Telegram download, the rest hit the cache); files are written to a temp name and atomically renamed.

Transcription (`?transcribe=1`): Groq Whisper (`whisper-large-v3-turbo`, language `ru`; override via `CHIP_TRANSCRIBE_MODEL` / `CHIP_TRANSCRIBE_LANGUAGE`). Adds `transcript` and `transcript_cached` to the payload; cached in `media-cache/<chat>/<msg>.transcript.txt`. Limit 25 MB (413). At most 2 concurrent Groq requests; the file is streamed, not loaded into memory. Key lookup at call time: env `GROQ_API_KEY` → file from env `GROQ_API_KEY_FILE` → `<skill>/secrets/groq-api-key` → `~/.secrets/groq-api-key`. Under systemd with `ProtectHome=true` home dirs are invisible, so put the key in the service `.env` or in `<skill>/secrets/groq-api-key`. No key → HTTP 503 (download still works). Groq errors → 502 with the key redacted.

Tests (offline, fake client + fake Groq, no network): `.venv/bin/python -m unittest test_media_info test_media_fetch -v`

## Read receipts

To check whether a peer has read a message you sent:

```
sent_message_id <= read_outbox_max_id  ⇒  peer прочитал
```

`read_outbox_max_id` is the id of the latest **outgoing** message the peer has acknowledged as read. `read_inbox_max_id` is the symmetric value for **incoming** messages you've read. `top_message_id` is the id of the latest message in the chat — handy for "has peer read everything I sent" comparisons (`read_outbox_max_id == top_message_id` ⇒ all read).

Example:

```bash
curl http://127.0.0.1:8080/chats/123456789/read_status
# {"chat_id": 123456789, "read_outbox_max_id": 65367, "read_inbox_max_id": 65279, "top_message_id": 65367, "unread_count": 0}
```

## Reading polls (important caveat)

For **anonymous multi-choice polls** (`public_voters: false` AND `multiple_choice: true`) Telegram returns per-option vote counts ONLY to accounts that have already voted in that poll. The session user account behind this skill must vote first — otherwise `results[]` is empty and only `total_voters` is visible.

There is no `vote` endpoint in this API by design (one session = one user account; programmatic votes pollute the data). Correct flow:

1. The session-owner human votes in the poll manually from Telegram (mobile/desktop client).
2. Then call `GET /chats/{chat_id}/messages/{message_id}` — `poll.results` will contain `voters` per option.

For single-choice or non-anonymous polls (`public_voters: true`) results are visible without voting.

## MCP stdio (optional)

Use `main.py` only when MCP tool integration is needed. If HTTP API is enough, keep MCP disabled.

## Troubleshooting quick hits

- `AuthKeyDuplicatedError` → stop all parallel clients, generate new session string.
- API not starting → check `.env`, port 8080 conflict, stale lock file.
- Auth problems on my.telegram.org → retry via mobile internet or private/incognito session.
