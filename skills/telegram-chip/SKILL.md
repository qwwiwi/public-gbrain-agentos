---
name: telegram-chip
description: Unified Telegram core (HTTP API + MCP stdio + tgdl + sales hooks) with single-session Telethon architecture. Use when setting up Telegram automation, reading/sending Telegram messages via local API, exporting chat history, or troubleshooting Telethon auth/session conflicts.
metadata: {"clawdbot": {"always": false}}
---

# telegram-chip

> **Если диалога нет – не пишем. Первым пишет только человек.**
> Hard rule (ban risk): the account owner's account NEVER writes first. `POST /messages/send`,
> `/messages/send-file` and `/messages/forward` to a private chat are refused with HTTP 403
> `{"success": false, "data": null, "error": "no dialog: contact never wrote first – sending is forbidden (ban risk)"}`
> unless that user has at least one incoming message in the chat. Groups/channels and Saved Messages are unaffected.
> Do not work around it (other clients, drafts-as-send, invites). The legacy `main.py` MCP server does NOT enforce it – keep it disabled.

Unified Telegram core with a single Telethon client and optional sales hooks.

## Correct authorization procedure (important)

Use this exact order:

1. Prepare `.env` with `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`.
2. Run `./generate-session.sh` and complete interactive login:
   - phone number (`+7...`),
   - Telegram login code,
   - 2FA password (if enabled).
3. Verify `TELEGRAM_SESSION_STRING` is present in `.env`.
4. Start only one active Telethon process for this session string.

## Credential acquisition recommendations

When obtaining `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` at `my.telegram.org/apps`:
- Prefer phone mobile internet (without Wi‑Fi/VPN), **or**
- Use a clean private/incognito browser session.

This reduces auth friction and weird web-session/cache issues.

## Hard rules

- Never share `TELEGRAM_SESSION_STRING` in chats/logs.
- If session leaks, revoke/regenerate immediately.
- One session string must not be used by multiple concurrent Telethon processes (prevents `AuthKeyDuplicatedError`).
- Use HTTP API (`api.py`) as the only integration point.
- Do not use direct Telethon imports outside this skill runtime (`skills/telegram-chip/.venv`).

## Sending discipline

This skill drives the account owner's OWN account. Every message you send lands on a
real person as if the owner wrote it personally, so outbound cadence is part of the
contract, not a style preference.

- **Two short messages maximum, then stop and wait for a reply.** The counter
  resets only when the human answers.
- **Never split one thought across many sends.** If several instructions about
  the same person arrive while you work, accumulate them and send ONE message.
- Sending 5-10 messages in a row reads as bot spam and kills the personal
  contact these conversations exist to build.

Why: relaying every live instruction as its own send produces a burst of messages
to one person within minutes; the recipient reads it as spam.

A monitoring loop must READ ONLY (`GET /chats/...`). Reading is unlimited;
sending is not.

## Chat folders (lead tags)

- `GET /folders?include_chats=true` — list folders: `id, title, type (filter|chatlist|default), color, emoticon, pinned_count, include_count, exclude_count, pinned_peer_ids, include_peer_ids` (+ `chats` when `include_chats=true`; use `include_chats=false` for a cheap call).
- `POST /folders/upsert` — body `{"title": "Горячие", "color": 0-6|null, "emoticon": "🔥", "include_chat_ids": [123, "-100..."], "mode": "replace"|"add"|"remove"}`.
  Finds the folder by EXACT title (creates it with the lowest free id 2..255 if absent) and sets its chats. Only that folder is touched – other folders are never modified; there is no delete endpoint.
  `color`/`emoticon` change only when sent (`color: null` clears). Response `data`: `{action: created|updated|unchanged, id, title, mode, color, emoticon, pinned_count, include_count, added, removed, include_peer_ids}`.
  Errors: 422 validation / title >12 chars / >200 chats (premium limit) / unresolvable chat ids (nothing written) / result would be empty; 409 title is a shared chat list or no free id; 429 FloodWait; 502 upstream.
  All Telegram calls go through the shared rate limiter. Unknown ids trigger one `get_dialogs` cache warm-up per request.

## Client supergroups («Name | Support»)

- `POST /supergroups` — body `{"title": "Client | Support", "about": "…", "users": ["@bot1", 123]}`. Creates a megagroup (one attempt, never retried; FloodWait → error, nothing created), invites users one by one, makes history visible to new members. `data` = JSON `{created, chat_id (marked -100…), title, invites: [{user, status: invited|not_invited|error|skipped, error?}], history: {history_visible, changed}}`. `success:true` = the chat exists – do NOT retry even if an invite failed; fix invites via `POST /groups/invite`. Error «outcome unknown» (timeout/connection/Telegram 5xx on create) = the chat MAY exist – do NOT retry, check `/chats/list` for the title.
- `POST /chats/{id}/history-visible` — body `{"visible": true}` (default true). Supergroups/channels only; basic groups get a clear error.
- Then `POST /admin/promote` `{chat_id: "-100…", user_id: "@bot"}` per bot, and `GET /chats/{id}/invite-link` (each call exports a NEW additional link).
- `POST /groups` still creates a basic group (no history toggle, no admin promote) – prefer `/supergroups`.

## Media: empty text = check media

In `GET /chats/{id}/messages` a media message shows a marker instead of empty text: `[файл: a.pdf, 1.2 MB]`, `[голосовое 0:10]`, `[фото]`, `[кружок 0:12]`, `[видео 0:35]`, `[стикер]`, `[опрос: …]` + caption. Never treat a media message as «nothing was said».

- `GET /chats/{id}/messages/{msg}` — adds `media_kind`, `file_name`, `mime_type`, `size`, `duration`.
- `GET /chats/{id}/messages/{msg}/media?v=2` — download to the chip host cache, returns `{path, file_name, mime_type, size, kind, duration, cached}`. Without `v=2` the legacy contract applies (always HTTP 200, errors as `{success:false,error}` in `data`).
- `GET /chats/{id}/messages/{msg}/media/file` — the bytes (use this when the chip runs on another host: `path` is a path on the chip host). Save, then Read the file.
- `GET /chats/{id}/messages/{msg}/media?transcribe=1` — voice/кружок/audio transcript via Groq Whisper (`transcript` field). 503 = Groq key not configured on the chip host.

Errors (v=2 / transcribe / file): 404 no media, 413 over 50 MB (25 MB for transcription), 422 not downloadable, 429 FloodWait. Max 2 parallel downloads and 2 parallel transcriptions; global limit 5 req/s, permits serialized. A download slot can be held ≤60 s by Telethon's internal FloodWait sleep – extra requests queue, not fail. Details: `README.md` → «Media».

## Reading polls

Anonymous multi-choice polls hide per-option vote counts from non-participants. Ask the session-owner human to vote manually in Telegram first, then `GET /chats/{chat_id}/messages/{message_id}` returns `poll.results[].voters`. See `README.md` → "Reading polls" for details.

## Read receipts (read_outbox_max_id)

To check if a peer has read a message you sent: `sent_message_id <= read_outbox_max_id` ⇒ read.

- `GET /chats/list` returns `read_outbox_max_id`, `read_inbox_max_id`, `top_message_id` per chat (alongside `unread_count`).
- `GET /chats/{chat_id}/read_status` — single-peer endpoint, lighter than `/chats/list` (uses `GetPeerDialogsRequest`).

`read_outbox_max_id == top_message_id` ⇒ peer прочитал всё.

See `README.md` → "Read receipts" for details.

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

See `README.md` and `TROUBLESHOOTING.md` for full setup and failure recovery.
