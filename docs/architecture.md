# Architecture

How the system works, in enough detail that you can debug it and extend it without guessing.

---

## Overview

Three MCP services on a VPS, a local Telegram bot on your workstation, a markdown vault as the source of truth, Postgres + pgvector as a recomputable index, and an ingest worker that keeps the index in sync with the vault.

```
                              local                      VPS
                  +---------------------+   +-----------------------------+
                  |  inbox-agent        |   |  Caddy (TLS, optional)      |
  Telegram -----> |  Telegram bot       |-->|     /memory/mcp -> :8767    |
  forwards        |  dual-write hook    |   |     /recall/mcp -> :8768    |
                  |  cron: compile,     |   |     /swarm/mcp  -> :8766    |
                  |        daily-digest |   |                             |
                  |  raw/  (local fs)   |   |  memory_mcp   recall_mcp    |
                  +---------------------+   |  swarm_mcp    ingest-worker |
                                            |                             |
                                            |  Postgres 16 + pgvector     |
                                            |    agent_tokens             |
                                            |    audit_log                |
                                            |    delivery_outbox          |
                                            |    documents (body + tsv)   |
                                            |    chunks (content + vec)   |
                                            |    embedding_jobs           |
                                            |                             |
                                            |  /opt/gbrain/vault/  (md)   |
                                            +-----------------------------+
```

Markdown stays canonical. Everything else is rebuildable from markdown + the auth table.

---

## The three MCP services

All three speak MCP over HTTP using FastMCP's `streamable-http` transport. They each have their own systemd unit, their own port, and their own scope of behaviour. They share `services/shared/` for auth, DB, and audit logging.

### `memory_mcp` (port 8767)

**Purpose:** the only path through which new markdown files are written into the vault and registered in Postgres.

**Tools exposed (write only):**

| Tool | Scope | What it writes |
|---|---|---|
| `create_decision_note` | `30-decisions` | Decision records (context, decision, consequences, alternatives) |
| `create_runbook_note` | `70-runbooks` | Operational runbooks (incident response, deploy procedures) |
| `create_error_pattern_note` | `80-error-patterns` | Recurring failure modes (symptom, root cause, fix) |
| `create_external_note` | `50-external`, `50-knowledge` | External content: URLs, transcripts, screenshots, voice-to-text |
| `create_handoff` | `90-inbox` | Inter-session handoff notes |
| `append_daily_log` | `20-daily` | Daily journal entry append |
| `supersede_decision` | `30-decisions` | Mark an old decision as superseded, link to the new one |
| `update_document` | (caller's scope) | Patch an existing file by path |
| `update_index` | `90-inbox` | Rebuild an index page from its children |

**Auth contract:** every call requires `Authorization: Bearer <token>`. The token's sha256 is looked up in `agent_tokens`; the `agent` and `can_write_scopes` columns determine identity and what folders the call may write into. Attempting to write outside your scopes returns 403.

**AuthCaptureMiddleware:** see "Auth model" below. The middleware reads the header per-request and stashes it in a `ContextVar` that the tool handler retrieves. Do not "simplify" this by reading the header inside `_extract_token` — under FastMCP's stateless-HTTP mode the headers do not propagate to the tool context.

### `recall_mcp` (port 8768)

**Purpose:** the only path agents use to search and read from the vault.

**Tools exposed (read only):**

| Tool | Behaviour |
|---|---|
| `recall(query, scope?, limit=10)` | Hybrid search — vector + FTS + RRF + temporal decay + source_weights |
| `recent(scope?, limit=20)` | Recently created/modified vault entries, optionally scoped |
| `related(path_or_id, limit=10)` | Find entries similar to a known one (vector-only) |
| `get(path_or_id)` | Fetch one file's full body + metadata |
| `reindex_check()` | Sanity check — count of files in vault vs rows in `documents`, drift report |
| `stats()` | Per-scope counts, last-updated timestamps, vault size |

**No writes ever.** A compromised recall token leaks data (bad) but cannot corrupt the vault. A compromised memory token can write garbage (worse). Issue recall-only tokens to research agents that should not be allowed to write.

### `swarm_mcp` (port 8766)

**Purpose:** an event bus for inter-agent messaging. One agent can dispatch a task to another, ack it, broadcast to many, or escalate.

**Tools exposed:**

| Tool | Behaviour |
|---|---|
| `notify(to_agent, payload)` | Enqueue a task in `outbox` table for the recipient |
| `ack(task_id, result?)` | Mark a task as completed |
| `escalate(task_id, reason)` | Bump priority; re-route to a designated escalation agent |
| `broadcast(to_agents[], payload)` | Fan-out notify |
| `list_my_pending()` | Caller's pending tasks (filtered by `to_agent = me`) |
| `list_recent_deliveries(limit=20)` | Recent outbox rows the caller can see |
| `get_delivery(task_id)` | One row by id |
| `stats()` | Per-agent counts of pending/acked/escalated |

Tasks are at-least-once: a worker re-delivers if no ack arrives within `next_retry_at`. Idempotency is the recipient's responsibility (use the `task_id`).

---

## The vault

Plain markdown on a filesystem, mounted at `${VAULT_ROOT}` (default `/opt/gbrain/vault/`). 12 folders, each with its own purpose:

| Folder | Purpose |
|---|---|
| `10-strategy/` | Long-horizon plans, OKRs, north-star definitions |
| `10-system/` | How the system itself is configured (your own ops notes) |
| `20-daily/` | One file per day, append-only |
| `20-metrics/` | Numeric snapshots (subscriber counts, MRR, conversion rates) |
| `30-decisions/` | One file per architectural or product decision |
| `40-projects/` | Per-project working notes (status, blockers, deliverables) |
| `50-external/` | Content forwarded into the inbox (URLs, voice notes, screenshots) |
| `50-knowledge/` | Curated explanations of topics (vs. raw external content) |
| `60-tasks/` | Task tracking, kanban-style notes |
| `70-runbooks/` | Step-by-step procedures (deploy, restore, on-call) |
| `80-error-patterns/` | Recurring problems with their resolutions |
| `90-inbox/` | Triage area for items not yet classified |

**Why markdown?** Three reasons. (1) Human-editable in any text editor without depending on this stack. (2) Re-indexable — if Postgres dies, you re-run the ingest worker and the index rebuilds from markdown. (3) Diffable — `git` works natively, you can review your agents' writes the same way you review code.

**File naming:** `YYYY-MM-DD-<slug>.md` is the convention but not enforced. The agent name and write timestamp are also in YAML frontmatter at the top of each file.

**Frontmatter:**

```yaml
---
title: "Decision: pin embedding model to multilingual-e5-large"
agent: coordinator-agent
scope: 30-decisions
created_at: 2026-05-16T12:34:56Z
tags: [embeddings, vector-search]
---
```

The ingest worker reads frontmatter; recall returns it in result metadata.

---

## Auth model

**`agent_tokens` table:**

```sql
agent_tokens (
  agent              text primary key,
  token_sha256       text not null unique,
  can_write_scopes   text[] not null default '{}',
  can_read_scopes    text[] not null default '{*}',
  rate_limit_per_min integer not null default 60,
  created_at         timestamptz not null default now(),
  last_rotated       timestamptz not null default now(),
  revoked_at         timestamptz
)
```

**Token lifecycle:**

1. `python scripts/issue-agent-token.py --agent coordinator-agent --scopes '30-decisions,50-external,...'` generates a random 32-byte token, stores its sha256 in `agent_tokens`, and prints the raw token to stdout once.
2. The user stores the raw token in a password manager.
3. The agent's `.mcp.json` includes `Authorization: Bearer <token>` in every MCP server config.
4. To revoke, set `revoked_at = now()` on the row; the auth check excludes revoked tokens.

**Request flow:**

```
HTTP request --> AuthCaptureMiddleware --> FastMCP HTTP app --> tool handler
                  |                                                |
                  | reads Authorization header                     | calls _extract_token()
                  | writes to _REQUEST_AUTH ContextVar             | reads ContextVar
                  v                                                v
                  (ContextVar lives for the duration of the request)
```

**Why the middleware?** Under FastMCP's stateless-HTTP transport, the `ctx.request_context.request.headers` mapping is empty inside the tool handler. The headers are visible only at the ASGI layer. The middleware captures them at the ASGI layer and puts them in a `ContextVar` that tool handlers can read.

**Why no env fallback?** Earlier versions had an `MCP-FALLBACK-TOKEN` env variable used when no Authorization header was present. This silently misattributed every unauthenticated request to a single fallback agent — every other agent looked like the fallback. Identity is a security boundary. A missing header now returns 401 explicitly. No silent attribution.

---

## Recall — hybrid search, RRF, decay, source_weights

`recall(query, scope?, limit=10)` executes the following pipeline:

### Step 1: two parallel candidate sets

**Vector candidates:** embed the query with the same FastEmbed model used for ingestion (`multilingual-e5-large`, 1024 dims). Cosine search against `chunks.embedding` (one row per chunk, vector(1024)) using the HNSW index, joined back to `documents` by `chunks.doc_id`. Take top 50 documents (deduped on doc_id, keeping the best-scoring chunk).

**Lexical candidates:** convert the query to a `tsquery` (`websearch_to_tsquery`). Match against `documents.body_tsv` (and/or `chunks.content_tsv`) using the GIN index. Rank by `ts_rank_cd`. Take top 50.

### Step 2: Reciprocal Rank Fusion

For each candidate present in either set, compute:

```
rrf_score = sum over each list:  1 / (k + rank_in_list)
```

with `k = 60`. RRF rewards items that appear in both lists; lone vector hits or lone lexical hits both contribute but at lower weight.

### Step 3: temporal decay

Multiply the RRF score by an exponential decay based on `updated_at`:

```
decay = exp(-age_days / half_life_days)
```

Default `half_life_days = 180`. Older notes are not erased but ranked lower for ties.

### Step 4: source_weights

Multiply by a per-scope weight from `services/recall_mcp/source_weights.py`:

| Scope | Default weight |
|---|---|
| `80-error-patterns` | 3.0 |
| `70-runbooks` | 2.0 |
| `30-decisions` | 1.5 |
| `50-knowledge` | 1.2 |
| `40-projects` | 1.0 |
| `10-strategy`, `10-system` | 1.0 |
| `20-daily`, `20-metrics` | 0.8 |
| `50-external` | 0.7 |
| `60-tasks` | 0.6 |
| `90-inbox` | 0.5 |

Rationale: a query like "how do we redeploy the recall service" should preferentially surface a runbook over a daily-log mention. A query about a past failure mode should bias toward error-patterns. The weights are a default — tune them in `source_weights.py` for your usage.

### Step 5: scope filter and limit

If `scope=` was passed, drop everything outside the requested scopes. Return the top `limit` rows with their frontmatter, body excerpt, and final score.

---

## Ingest worker

A separate process (`gbrain-ingest-worker.service`) that consumes the `embedding_jobs` queue and keeps `chunks.embedding` in sync with `documents.body`.

**Loop:**

1. Poll `embedding_jobs` where `status = 'pending'` using `FOR UPDATE SKIP LOCKED`.
2. For each job, look up the matching `documents` row (`doc_id`).
3. Parse frontmatter (`agent`, `scope`, `title`, `created_at`) — already stored as `documents.frontmatter` JSONB.
4. Chunk the body — current implementation splits the body into word-windows (defaults: `WINDOW_SIZE_DEFAULT=500` words, `OVERLAP_DEFAULT=50`). Token-level chunking via FastEmbed's tokenizer is a known follow-up.
5. Embed each chunk with FastEmbed `multilingual-e5-large` (1024 dims).
6. Upsert into `chunks` keyed by `(doc_id, position)`; store `chunk_hash` to make re-runs idempotent.
7. Mark the job `status = 'done'` and sleep until the next poll.

**Where do jobs come from?** Today, only `memory_mcp` writes them: every successful `create_*_note` / `update_document` call performs `_queue_embedding(doc_id)`. Files dropped directly into `${VAULT_ROOT}` on the filesystem are **not** automatically picked up — there is no inotify watcher in this distro. If you want to index a file you wrote by hand, call `memory_mcp.update_document(path=...)` or insert an `embedding_jobs` row manually.

**Idempotency:** the sha256 check makes the worker safe to restart, safe to run on a freshly restored vault, and safe to point at an existing populated DB.

**Failure handling:** embedding failures (e.g. model not downloaded, OOM) are logged and the file is skipped — the next loop retries. Postgres failures retry with exponential backoff.

**FastEmbed model:** `intfloat/multilingual-e5-large`, 1024 dims, runs on CPU. First start downloads ~1GB into the FastEmbed cache. Subsequent starts are instant.

---

## Inbox-agent — dual-write pattern

The local Telegram bot daemon (`inbox-agent/bot.py`, python-telegram-bot polling) is the only ingestion path you actively touch. On each inbound message it calls the hook (`inbox-agent/hooks/save-to-raw.sh`) synchronously, replies with a short ack, and exits the handler. The hook executes:

1. Write the raw message (text + metadata) to a local file under `${INBOX_AGENT_HOME}/raw/YYYY/MM/DD/<timestamp>-<source>.md`.
2. Immediately attempt to call `memory_mcp.create_external_note` with the same content, scope `50-external`, agent `inbox-agent`.

**Both writes attempt in parallel.** If the network is down or the brain is unreachable, the local raw write still succeeds. The compile cron job (`*/15 * * * *`) re-tries any raw files that still have `compiled: false` in their frontmatter.

**Why dual-write?**

- Resilience: a single network blip does not lose a forward.
- Reviewability: the raw folder is a chronological audit trail you can read independently of the brain.
- Migration safety: you can reset the brain DB and re-ingest from raw.

**Compile cycle:** every 15 minutes, `inbox-agent/scripts/compile.sh` walks `raw/` for files with `compiled: false`, classifies them by content type (URL → fetch + extract; voice → Whisper transcribe; image → describe), enriches the markdown, writes a structured note via memory MCP, and flips the flag to `compiled: true`. The original raw file stays in place as an audit trail.

**Daily digest:** at 09:00 UTC, `inbox-agent/scripts/daily-digest.sh` recalls the last ~26h of `50-external` writes, asks Sonnet for an HTML digest using `inbox-agent/prompts/digest.prompt.md` + `inbox-agent/config/digest-template.html`, and sends it to the user's Telegram via the Bot API.

---

## Skill bundle

The `skills/` folder is a vendored set of optional ingestion skills your agents can invoke per content type. Each skill is self-contained:

| Skill | Triggers on | What it does |
|---|---|---|
| `transcript/` | YouTube URLs | Fetches transcript via Loore (`yt_transcript`) |
| `markdown-new/` | Generic web URLs | Clean markdown extraction via readability |
| `groq-voice/` | Audio attachments | Whisper transcription via Groq |
| `twitter/` | x.com / twitter.com URLs | Thread reading via SocialData |
| `instagram-superpower/` | instagram.com URLs | Instagram analytics via Loore |
| `perplexity-research/` | Explicit `/research` command | Sonar web research |
| `agent-browser/` | Explicit invocation | Headless Chrome via CDP for sites the others miss |
| `mcp-builder/` | Explicit invocation | Helper for building new MCP servers |
| `telegram-chip/` | Telegram user-account access | MTProto bridge for reading personal Telegram |

**Adding a skill:** create a directory under `skills/<your-skill>/` with a `SKILL.md` (the prompt the agent reads to understand when and how to use it), an optional `scripts/` folder for shell helpers, and reference the skill from `inbox-agent/config/classifier.yaml` so the bot routes the right content to it. See `skills/README.md` for the contract details.

---

## Agent workspace layer (Path B)

This section covers the **optional** per-agent workspace layer produced by `agent-template/install.sh`. If you installed Path A only (brain + inbox-agent), you can skip this section. If you installed Path B (brain + N personal Claude Code agents), this is how each of those agents is structured and how they talk to the brain.

### What an agent workspace is

A workspace is a self-contained Claude Code agent home directory at `~/.claude-lab/<agent-id>/.claude/`. It contains the agent's SOUL (its identity prompt), its operational rules, its layered memory, its hooks, and its `.mcp.json` wired to the shared brain. One workspace = one agent identity. Multiple workspaces share **one** brain — that is the design.

The skeleton:

```
~/.claude-lab/<agent-id>/.claude/
├── CLAUDE.md              # SOUL — identity, role, style, boundaries (always loaded)
├── settings.json          # env vars, hooks registration, permissions
├── .mcp.json              # MCP server config — brain memory/recall/swarm + Bearer
├── core/
│   ├── USER.md            # Owner profile (always loaded via @include)
│   ├── rules.md           # Operational rules (always loaded via @include)
│   ├── AGENTS.md          # Agent directory (Read tool, NOT loaded at startup)
│   ├── MEMORY.md          # Cold memory, lessons index (Read on demand)
│   ├── LEARNINGS.md       # Lesson archive from mistakes (Read on demand)
│   ├── warm/
│   │   └── decisions.md   # Key decisions, last ~14 days (always loaded)
│   └── hot/
│       ├── handoff.md     # Last ~10 entries from conversation log (always loaded)
│       ├── recent.md      # Full conversation log (NOT loaded into session)
│       └── archive/       # Old logs rotated out by date
├── tools/
│   └── TOOLS.md           # Tool/service directory (Read on demand)
├── skills/                # Skills bundle (subset of repo `skills/`)
├── hooks/                 # Shell scripts triggered by Claude Code events
│   ├── stop-hook.sh
│   ├── session-start-hook.sh
│   └── precompact-hook.sh
├── scripts/               # Memory-rotation scripts (run by cron)
│   ├── memory-rotate.sh
│   ├── trim-hot.sh
│   ├── rotate-warm.sh
│   ├── compress-warm.sh
│   └── gbrain-recall-on-start.sh
└── logs/                  # Per-script log files (chmod 600)
```

Every file in `templates/` is rendered with the user's answers at install time (agent id, role, owner name, MCP host, bearer, model). The hooks and scripts are copied verbatim and `chmod +x`ed.

### The 4-layer local memory model

Within a workspace, memory lives in four tiers from fastest to slowest. The split matters because Claude Code's working window is bounded and not every memory file should occupy permanent context space.

```
IDENTITY ───── always in context  (CLAUDE.md + core/USER.md + core/rules.md)
WARM 14d ───── always in context  (core/warm/decisions.md)
HOT ────────── handoff at startup (core/hot/handoff.md, last ~10 entries)
COLD ───────── Read tool on demand (core/MEMORY.md, core/LEARNINGS.md, tools/TOOLS.md)
SHARED BRAIN ─ MCP on demand     (gbrain via .mcp.json — recall.*, memory.*, swarm.*)
```

- **IDENTITY** — who the agent is, who its owner is, what its operational rules are. Small (~6–8 KB total), loaded via `@include` at every session start.
- **WARM (decisions.md)** — material decisions made in the last ~14 days. Compact (~2–4 KB). Always loaded so the agent never "forgets" what was decided last week.
- **HOT (handoff.md)** — the last ~10 entries from `recent.md` (the full conversation log). Loaded at session start by the SessionStart hook. The full `recent.md` is **never** loaded — only the slice the hook extracts.
- **COLD** — `MEMORY.md`, `LEARNINGS.md`, `TOOLS.md`, `AGENTS.md`. Not loaded automatically. The agent reads them with the `Read` tool when relevant. This keeps the startup payload under ~3% of a typical 400k working window.
- **SHARED BRAIN** — everything in the gbrain vault. Accessed via the 3 MCP servers in `.mcp.json`. The agent calls `recall.recall("query")` to find anything across history, or `memory.create_decision_note(...)` to write a new entry into the shared brain alongside its local `decisions.md`.

The agent does not "choose" which layer to use — the layers are wired so the right one is in front of it at the right time. CLAUDE.md is always in context. `handoff.md` is in context after SessionStart. `decisions.md` is in context always. `MEMORY.md` is one Read away. Gbrain is one `recall` call away.

### How workspaces consume the shared brain (MCP)

The `.mcp.json` rendered at install time registers three MCP servers — one each for memory, recall, swarm:

```json
{
  "mcpServers": {
    "gbrain-memory": {
      "url": "https://<MCP_HOST>/memory/mcp",
      "headers": { "Authorization": "Bearer <AGENT_BEARER>" }
    },
    "gbrain-recall": {
      "url": "https://<MCP_HOST>/recall/mcp",
      "headers": { "Authorization": "Bearer <AGENT_BEARER>" }
    },
    "gbrain-swarm": {
      "url": "https://<MCP_HOST>/swarm/mcp",
      "headers": { "Authorization": "Bearer <AGENT_BEARER>" }
    }
  }
}
```

The bearer is per-agent (issued by `scripts/issue-agent-token.py --agent <agent-id> --scopes '...'`). The scope set the token has determines what the agent can write. A `coordinator-agent` token typically holds the full write set; a `researcher-agent` token might have an empty write set (recall only).

Inside Claude Code, this surfaces as three groups of tools the agent can call: `gbrain-memory.create_decision_note(...)`, `gbrain-recall.recall(...)`, `gbrain-swarm.notify(...)`, etc. No HTTP plumbing — Claude Code handles the JSON-RPC and the Bearer.

### How hooks glue local memory to the shared brain

Three hooks live in each workspace's `hooks/` and are registered in `settings.json`. They fire on Claude Code lifecycle events:

| Hook | Trigger | What it does |
|---|---|---|
| `session-start-hook.sh` | Claude Code session opens | Reads the last ~10 entries from `core/hot/recent.md` and writes them to `core/hot/handoff.md` so the next session starts with continuity. May also call `gbrain-recall.recent(scope='90-inbox')` and prepend the latest inbox items so the agent sees anything new since last session. |
| `stop-hook.sh` | Session ends (Claude Code emits Stop event) | Appends the latest assistant/user turns to `core/hot/recent.md` (the full chronological log). This is **the** capture step — without it, no memory ever rotates. |
| `precompact-hook.sh` | Claude Code is about to auto-compact context | Snapshots the current session into a more compressed form so nothing material is lost when auto-compact discards old turns. Typically writes a one-line entry to `core/hot/recent.md` and (if a decision was made) optionally calls `gbrain-memory.create_decision_note(...)` to dual-write into the brain. |

The Stop hook is the critical one. If it fails silently (a known antipattern in some headless `claude -p` modes), nothing rotates and the agent's memory drifts. Verify Stop hook output lands in `core/hot/recent.md` after every session by tailing the file or watching its `mtime`.

The memory-rotation scripts in `scripts/` are run by cron, not by hooks. They are the "slow loop":

- `trim-hot.sh` (hourly) — when `recent.md` exceeds a size threshold, summarise the oldest section with a Sonnet subagent call and append the summary to `core/warm/decisions.md`; move the original chunk to `core/hot/archive/<date>.md`.
- `rotate-warm.sh` (daily, 03:30) — when an entry in `decisions.md` is older than ~14 days, move it to `core/MEMORY.md` under the relevant section header.
- `compress-warm.sh` (weekly, Sunday 04:00) — re-summarise overgrown `decisions.md` sections to keep the always-loaded warm tier compact.
- `gbrain-recall-on-start.sh` (called by SessionStart) — query the brain for any new entries since last session in scopes the agent cares about.

### Multi-agent scenarios

The whole point of Path B is that you can run multiple workspaces against one brain. The pattern:

- Each personal agent has its own `~/.claude-lab/<agent-id>/.claude/`.
- Each has its own Bearer in `agent_tokens`. `audit_log` attributes every write to the correct agent — never to a shared identity.
- Each has its own write scopes. `coordinator-agent` writes decisions; `marketer-agent` writes external notes; `researcher-agent` writes nothing.
- All read the **same** vault. A decision written by `coordinator-agent` shows up in `researcher-agent`'s recall the next time it queries.
- All can use `swarm_mcp` to notify each other: `coordinator-agent` calls `gbrain-swarm.notify(to_agent='coder-agent', payload={...})`, `coder-agent` polls `list_my_pending()` and acks when done.

What multiple workspaces do **not** share locally: their own hot/warm/cold tiers. `coordinator-agent`'s `handoff.md` is private to its workspace. If two agents need to share a piece of state, it goes into the brain via `gbrain-memory`, not via the local memory layer.

The global `~/.claude/CLAUDE.md` (user-level conventions) is read by **all** workspaces — it is the "house style" shared by every agent the user runs. Per-agent specifics live in the workspace's own `CLAUDE.md` (the SOUL).

For deeper coverage of multi-agent patterns (per-agent Telegram bots, role boundaries, escalation paths), see `agent-template/docs/MULTI-AGENT.md`.

---

## Data flow — end to end example

You forward a YouTube URL to your Telegram bot at 14:00:

1. **14:00:00** — Bot receives the message. `save-to-raw.sh` runs.
2. **14:00:00.1** — Raw file written to `${INBOX_AGENT_HOME}/raw/2026/05/16/1716470400-telegram-fwd.md`.
3. **14:00:00.2** — Hook calls `memory_mcp.create_external_note` with the URL + the forwarded text. Brain returns `gbrain_id`.
4. **14:00:00.3** — Hook writes `gbrain_id` back into the raw file's frontmatter.
5. **14:05:00** — Compile cron runs. Sees the raw file is compiled (`gbrain_id` present), skips.
6. **14:05:01** — Another raw file from a different forward (a voice note) is found. Classifier routes it to `groq-voice`. Transcript is generated and written as a fresh `50-external` note via memory MCP.
7. **VPS, async** — `memory_mcp.create_external_note` inserts the new file row into `documents` and enqueues an `embedding_jobs` row. The ingest worker picks the job up within a few seconds, chunks the body, embeds each chunk, and upserts rows into `chunks`. The note is now recallable.
8. **15:00** — From your coordinator agent's MCP context, you call `recall.recent(scope='50-external', limit=10)`. The YouTube URL appears with `agent: inbox-agent`, score reflecting recency and scope weight.
9. **Next morning 07:00** — Daily digest cron runs. Builds yesterday's recap. Bot sends it to you on Telegram.

Total moving parts the user touches: forward a Telegram message. Everything else is automated.
