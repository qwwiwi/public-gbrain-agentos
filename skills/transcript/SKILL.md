---
name: transcript
description: >
  Транскрипт YouTube роликов через Loore (yt_transcript). ОБЯЗАТЕЛЬНО используй этот скилл когда:
  (1) пользователь присылает YouTube ссылку (youtube.com, youtu.be),
  (2) просит рассказать о чём видео, (3) просит транскрипт/субтитры,
  (4) говорит «что в этом видео», «о чём ролик», «перескажи видео»,
  (5) любой запрос связанный с содержимым YouTube видео.
  Триггеры: YouTube ссылка, «транскрипт», «о чём видео», «что говорят в видео»,
  «перескажи ролик», «summarize video», «get transcript».
  Данные YouTube -- только через Loore или официальный YouTube Data API v3.
  yt-dlp и скрейпинг со своего IP -- только для скачивания видеофайлов.
homepage: https://loore.ai
user-invocable: true
---

# Transcript

Fetch YouTube video transcripts via [Loore](https://loore.ai) (REST, CLI and MCP server).
Docs: [docs.loore.ai](https://docs.loore.ai).

**YouTube rule:** YouTube data (transcripts, metadata, comments, search) comes only from
Loore (`yt_*` tools) or the official YouTube Data API v3. `yt-dlp` or scraping from your own
IP is allowed only for downloading video files, never for data.

## Setup

Create an API key in the Loore dashboard (Settings → API and MCP) at [loore.ai](https://loore.ai)
and export it:

```bash
export LOORE_API_KEY=<your-key>
```

One key works for both REST and MCP. MCP endpoint: `https://mcp.loore.ai/mcp`.

## POST https://mcp.loore.ai/v1/yt-transcript

```bash
curl -s -X POST https://mcp.loore.ai/v1/yt-transcript \
  -H "Authorization: Bearer $LOORE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url": "VIDEO_URL", "include_timestamps": true}'
```

| Param                | Required | Default | Values                                          |
| -------------------- | -------- | ------- | ----------------------------------------------- |
| `url`                | yes      | —       | YouTube video or Shorts URL, or 11-char video ID |
| `language`           | no       | —       | Caption language, e.g. `ru`, `en`, `en-US`      |
| `original_audio`     | no       | `false` | `true` = only the original spoken language      |
| `include_timestamps` | no       | `false` | `true` = timed segments instead of plain text   |

The answer comes in one envelope: `{ "status": "ok", "result": { ... } }`. Errors use the same
envelope: `{ "status": "error", "error": "...", "details": { } }`. Long transcripts are paged
(up to 40000 characters per answer); continue with the cursor/offset from the answer.

Related `yt_*` tools: `yt_search`, `yt_channel`, `yt_channel_videos`, `yt_video`
(lists available `captionLanguages`), `yt_comments`.

## Errors

| Code | Meaning                  | Action                                                           |
| ---- | ------------------------ | ---------------------------------------------------------------- |
| 401  | Bad API key              | Check `LOORE_API_KEY`                                            |
| 428  | Spend authorization      | Paid call: approve the spend (`spend_authorization`), then retry |
| 429  | Rate limited             | Wait and retry                                                   |

## Tips

- For long videos, summarize key points first, offer full transcript on request.
- Use `include_timestamps: true` when you need precise timestamps for quoting specific moments.
- Use plain text (default) for clean text suitable for translation or analysis.
- Check available caption languages with `yt_video` before asking for a specific `language`.
