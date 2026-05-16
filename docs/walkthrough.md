# Welcome to Your Second Brain

If you read only one page in this repository, read this one. Everything
else — the migrations, the systemd units, the MCP server code, the agent
template — is implementation. This page is the why.

This repo is the open-source skeleton of a shared memory system for AI
agents. It is built around Claude Code as the primary runtime, but the
architectural ideas apply to any agent that can call an HTTP tool. You
install it on one small VPS, point your local agents at it, and your
agents stop being amnesiac strangers to each other. They start to act
like a team that remembers.

This document is for you if you have already played with Claude Code, hit
the wall where your agent forgets everything between sessions, and
decided that wall is worth knocking down. It is not a setup script —
see [docs/setup.md](setup.md) for the commands. It is not a component
reference — see [docs/architecture.md](architecture.md) for the
diagrams. It is the narrative spine that makes both of those documents
make sense.

By the end of this page you will know what you are building, why each
piece exists, and what to read next. The repository root
[README.md](../README.md) gives you the elevator pitch; this walkthrough
gives you the long version.

## The Problem: Agents Without Memory

An AI agent without persistent memory is a goldfish in a suit. It looks
competent for the length of one conversation. The moment the context
window resets — at compact, at session end, at a crash — every nuance
of the last six hours is gone. You start over. Then you start over
again tomorrow.

Imagine you have three agents on your laptop: a code reviewer, a
marketing writer, and a research assistant. Each is sharp inside a
session. Each forgets everything when you close the terminal. Now
imagine the code reviewer learns on Monday that you switched from REST
to gRPC. On Wednesday, the marketing writer asks the code reviewer
about your API style, gets nothing useful back, and writes a blog post
about your "RESTful endpoints." You read it, sigh, fix it by hand, and
the cycle repeats.

This is not a hypothetical. This is the default state of every
multi-agent setup that uses the context window as memory. You can scale
the window to a million tokens and the problem only shifts: now your
agent forgets less inside a session, but still loses everything when
the session ends or compacts. The window is working memory, not
storage.

The first instinct is to dump everything into local markdown files.
Make a `notes/` folder, write things down, point each agent at it via
`CLAUDE.md`. This works for about a week. Then the problems start. The
files grow past the context budget, so you cannot include them all.
Agents on different machines have different copies. One agent edits a
note, another does not see the edit. You build a sync layer. The sync
layer breaks. You add cron jobs. The cron jobs race each other.

By month two you have built a worse, slower, less searchable version of
a database, and you still do not have semantic recall. The next thing
you reach for is a vector store. That gets you closer, but if you wire
each agent to its own store you are back to silos. If you wire all
agents to one store with no identity boundary, you cannot tell who
wrote what, and one misbehaving agent can poison everyone else.

> Note: The context window is not memory. A million-token window is
> impressive working memory, but it is wiped between sessions and
> pruned at compact. Treating it as storage is a category error.

The real fix is to separate three things that markdown files conflate:
write, read, and search. Writes need an idempotent contract so the
same fact does not get stored twice with slightly different wording.
Reads need a query interface that finds things by meaning, not by
filename. Search needs an index that survives across machines. Once
you have those three, the agent's memory stops being a pile of files
and starts being something you can reason about.

## The Solution: Shared Brain Behind an API

The architectural move is small but decisive: put the memory behind an
HTTP API, give every agent a bearer token, and let the API enforce
identity. One brain, many agents, one contract.

This is the same trick that turned filesystem-shared databases into
networked databases in the 1980s. When everyone reads and writes the
same files, eventually someone corrupts a record. When everyone goes
through an API, the API enforces invariants — schema, permissions,
audit trail. You trade a little latency for a lot of correctness, and
the latency is measured in tens of milliseconds.

In this repo the brain runs three services on one VPS. Each service is
a small Python process speaking the Model Context Protocol (MCP) over
streamable HTTP, so any MCP-aware client can talk to it. (MCP is a
JSON-RPC tool-calling protocol — function calls over HTTP that any
MCP-aware client, including Claude Code, knows how to invoke.)

The first service is **memory** — the write side. Agents call tools
like `create_decision_note` and `create_runbook_note`. The service
deduplicates by content hash, so retrying a write is free. It writes a
canonical markdown file into the vault and enqueues it for indexing.
Decisions, runbooks, error patterns, handoffs — each has its own tool
with its own schema, and each lands in a predictable folder.

The second service is **recall** — the read side. Agents send a
query like *"why did we switch from REST to gRPC?"* and get back
ranked snippets. The ranking is hybrid: vector similarity for meaning,
full-text for exact terms, and a small temporal-decay boost so recent
notes outrank ancient ones of equal relevance. Recall is read-only;
it cannot mutate state, which makes it safe to grant broadly.

The third service is **swarm** — agent-to-agent communication. When
agent A wants agent B to draft something, it calls
`swarm.notify(to_agent="B", payload={...})`. The swarm service
persists the message in an outbox table, retries on failure, and
triggers B's gateway over a webhook. B can be offline and pick up the
work when it comes back. The outbox gives you at-least-once delivery
without making each caller responsible for retries.

Each request carries a bearer token in its `Authorization` header. The
server resolves the token to an agent identity *at the API boundary*,
before any tool runs. In practice this means: a small piece of code
(an ASGI middleware) reads the `Authorization` header before the tool
function runs, looks up the agent's identity, and stores it where the
tool function will see it. That identity ends up stamped on every
audit-log row, every vault note's frontmatter, and every outbox entry.

There is no way for the `coder-agent` to write a note that looks like
it came from the `marketer-agent`, because the agent name is not a
parameter — it is derived from the token. This sounds obvious. In
practice almost every multi-agent system gets this wrong on the first
try, because it is easy to pass an `agent_id` as a function argument
and never notice that the argument is forgeable.

> Note: Identity at the boundary is the single security property that
> makes multi-agent systems debuggable. Without it, a misbehaving
> agent can blame any other agent, and you cannot tell what really
> happened. If you implement nothing else from this repo, implement
> this.

The three services share one Postgres database and one filesystem
vault. The vault is plain markdown organized into semantic folders —
`30-decisions/`, `70-runbooks/`, `80-error-patterns/`, and so on.
Markdown is the canonical store. The Postgres index is derived: if you
blow it away, a reindex job rebuilds it from the vault. Your data is
human-readable, grep-able, version-controllable in git, and survives
any database catastrophe. The database is fast; the markdown is
permanent.

You can read the full service breakdown in
[services/memory_mcp/](../services/memory_mcp/),
[services/recall_mcp/](../services/recall_mcp/), and
[services/swarm_mcp/](../services/swarm_mcp/).

## Memory Layers

One bucket for everything does not work. You want recent activity to
surface fast, archived knowledge to stay searchable, and noise to fade
out. The trick is to tier memory by temperature.

There are four tiers, and each one has a clear job. They map onto
different files and different access patterns, and the lifetime
shortens as the heat rises.

**Hot** is the last day or two of activity. It lives in a file the
agent reads at startup — `recent.md` in the agent's workspace. Every
turn the agent takes appends a one-line snippet here: the user prompt,
a short summary of what the agent did, the timestamp. Hot memory is
small on purpose. Ten to twenty entries is enough to remind the agent
what it was doing yesterday, not enough to crowd the context window.
Hot is where "what was I working on?" gets answered without a recall
call.

**Warm** is decided knowledge — `decisions.md` and `LEARNINGS.md`.
These files do not grow with every turn; they grow when something is
worth remembering. A decision goes in when you choose between two
paths and want to remember why. A learning goes in when you fix a bug
and want the next agent to know the pattern. Warm is read on demand:
the agent loads it when the current task touches a topic the warm
layer covers. The agent template includes both files with an explicit
schema so writes are uniform.

**Cold** is the long tail — every note ever written, indexed for
semantic search. The agent does not load cold memory at startup. It
queries cold memory via the `recall` service when a question demands
deep context. Cold is where six months of project history lives,
ready to be found by meaning rather than by filename. The cold layer
is the only tier that does not appear in any file the agent reads
unprompted; it is reached through a tool call, on intent.

**Shared** is the cross-agent layer — the vault on the VPS, accessible
to every agent through the MCP API. When the `coder-agent` writes a
decision about your deploy pipeline, it dual-writes: one copy goes
into its local `decisions.md` (warm, personal), one goes into the
shared vault via `memory.create_decision_note` (cold, team). The next
time the `marketer-agent` asks "how do we deploy?", the shared vault
answers. Each agent gets the benefit of every other agent's writing
without coordinating anything.

> Note: Tiering matters because cost and signal-to-noise both scale
> with what you load into context. Hot memory is free to read but
> useless after two days. Cold memory is expensive to search but
> precious for old questions. Get the layers right and your agents
> feel fast and remember everything. Get them wrong and you either
> burn tokens or forget yesterday.

The rotation between tiers is automatic. A small cron job moves
entries older than fourteen days from warm to cold. Hot is pruned to
the last twenty turns at session start. You do not curate this by
hand — you set the rules once and the system maintains itself. When
you decide the rules are wrong, you change one config value and the
next cron cycle adopts the new policy.

## The Inbox Pattern

Most of what you want your agents to remember does not arrive as a
conversation. It arrives as a forwarded link, a voice memo to
yourself, a screenshot, a long email you skimmed. The inbox pattern
handles this stream.

The pattern is simple. One agent — call it the `inbox-agent` — owns a
single channel that you dump everything into. In this repo that
channel is a Telegram bot, but it could be an email address, an RSS
feed, or a folder you drop files into. The inbox agent does three
things in order: it captures raw input, it dual-writes a backup, and
it asks a slower agent to summarize.

Capture is dumb on purpose. The bot receives your message, writes the
raw payload to `raw/YYYY-MM/{type}/{slug}.md` on disk, and
simultaneously calls `memory.create_external_note` to mirror it into
the shared vault under `50-external/`. Dual-writing buys you a
guarantee: if the VPS is down, the local copy survives; if the local
disk dies, the shared copy survives. Either alone is a single point
of failure, and outages always come on the day you most needed the
note you forwarded yesterday.

The summarization runs out-of-band. A cron every six hours scans for
raw notes with `compiled: false`, asks a cheaper model to produce a
structured summary with decisions, action items, and tags, and writes
the result alongside the raw note with `compiled: true`. The raw note
never gets deleted — you can always go back to it — but the compiled
version is what the rest of your agents will actually find in recall.

This separation is the point. You should never have to decide, at
capture time, whether something is a decision or a learning or a
knowledge base entry. You forward, the inbox stores, the summarizer
classifies, and the recall layer finds it later by meaning. The
cognitive load on you is one button: forward.

The reference implementation lives in
[inbox-agent/](../inbox-agent/README.md). It is roughly one hundred
and fifty lines of bash and Python — small enough to read in one
sitting and modify for your own ingestion channel.

## What You'll Build

The deployment is two-sided. One side is a VPS that hosts the shared
brain. The other side is your local machine — laptop, workstation, or
wherever your agents actually run.

On the VPS you will have **Postgres 15 or newer** with the `pgvector`
extension, holding the search index and metadata tables. You will
have **three MCP services** — memory, recall, swarm — each a small
Python process listening on its own port. An **ingest worker**
processes the embedding queue: when memory writes a note, the worker
generates the embedding and indexes it. A **reverse proxy** (Caddy is
the recommended default) gives all three services one TLS-protected
hostname. If you want the inbox pattern, an **optional Telegram bot**
runs as a separate process on the same VPS.

On your local machine you will have **Claude Code** with a workspace
per agent. An "agent" here is just a directory with its own
`CLAUDE.md`, its own skills, and its own bearer token. You will have
an **`.mcp.json` file** in each workspace pointing the agent at the
three MCP services. This is the integration surface: once it is set,
the agent can call `memory.create_decision_note(...)` like a local
function. Each workspace also keeps **local memory files** —
`recent.md`, `decisions.md`, `LEARNINGS.md` — populated by the hooks
that fire after each turn.

The split matters. Secrets stay on your laptop. Your agents run with
full local filesystem access, which is what you want for real work.
The VPS is small and mostly stateless — it holds the search index but
the canonical markdown lives in a git-tracked vault. You can rebuild
the VPS from scratch in an hour without losing knowledge.

For the build itself, see
[agent-template/docs/FIRST-AGENT.md](../agent-template/docs/FIRST-AGENT.md)
for the local agent setup and [docs/setup.md](setup.md) for the VPS
side.

## How the Pieces Talk

Now that you know what runs where, follow one request end-to-end — it
makes the whole stack click.

A single recall call is the cleanest way to see the whole loop in
motion. Walk through it once and the architecture clicks.

Your agent, running inside Claude Code, calls
`recall.recall(query="why did we switch from REST to gRPC?")`. Under
the hood, Claude Code sends an MCP `tools/call` request over a
streamable-HTTP transport to
`https://mcp.your-domain.example/recall/mcp`, with the agent's bearer
token in the `Authorization` header. Caddy terminates TLS and routes
the request to the local recall process on its private port.

Recall does four things in sequence. First, it resolves the bearer to
an agent identity at the API boundary (the same middleware that
protects every other call). Second, it computes a query embedding
using a local embeddings model — `FastEmbed` with
`multilingual-e5-large` is the default, no external API needed.
Third, it runs a hybrid search in Postgres: vector similarity via
`pgvector`, full-text via Postgres's built-in tsvector, and a fusion
step that combines the two with reciprocal-rank fusion plus a small
temporal-decay multiplier. Fourth, it returns the top N snippets as a
JSON response.

The agent receives the snippets, integrates them into its context,
and answers your question with citations to actual decisions you made
months ago. The whole round trip — embed, search, fuse, respond —
typically completes in 50 to 200 milliseconds on a small VPS.

Writes are symmetric. Your agent calls
`memory.create_decision_note(title=..., body=..., why=...)`. The
memory service hashes the body, checks if that hash already exists,
writes the markdown file to the vault if it does not, enqueues the
file for indexing, and stamps the audit log with the resolved agent
identity. The ingest worker picks up the queue entry, generates
embeddings, and inserts them into Postgres. From the agent's
perspective the write returns immediately; the index becomes
searchable a second or two later.

Swarm is the third corner. Agent A calls
`swarm.notify(to_agent="B", payload={...})`. The swarm service
persists the message in an outbox table and hits a webhook on agent
B's local gateway. Agent B's gateway picks it up, formats a prompt,
and feeds it into a Claude Code session for B. The outbox provides
retries and at-least-once delivery, so B can be offline and pick up
the work when it comes back.

For the full diagrams — request paths, identity flow, the
auth-capture middleware, the audit log schema — see
[docs/architecture.md](architecture.md).

## Where to Go Next

The order in which you read the rest of the docs matters less than
the order in which you actually do things. Here is the path I
recommend.

If you want to *understand* the system before touching it, read
[docs/architecture.md](architecture.md) next — it has the diagrams
this walkthrough deliberately skipped. Then skim
[agent-template/docs/AGENT-LAWS.md](../agent-template/docs/AGENT-LAWS.md)
to see the rule system that keeps agents from misbehaving.

If you want to *deploy*, the fastest path is to hand
[AGENT.md](../AGENT.md) to your Claude Code agent and let it run the
install. That file is written for an agent reader: it is a contract
that takes a fresh VPS and turns it into a working brain. If you
prefer to drive the install yourself,
[docs/setup.md](setup.md) has the same steps in human form.

If you want to *build your first agent on top* of an already-deployed
brain, start with
[agent-template/docs/SETUP-GUIDE.md](../agent-template/docs/SETUP-GUIDE.md)
and then
[agent-template/docs/FIRST-AGENT.md](../agent-template/docs/FIRST-AGENT.md).
Together they walk you from empty directory to a Claude Code
workspace that can write and read shared memory.

If you want to run *multiple coordinated agents*,
[agent-template/docs/MULTI-AGENT.md](../agent-template/docs/MULTI-AGENT.md)
covers the patterns: bearer scopes, swarm conventions, when to use
shared vault versus per-agent local memory, and how to keep agents
from stepping on each other.

The inbox pattern is its own track. Read
[inbox-agent/README.md](../inbox-agent/README.md) and adapt the
capture surface to whatever channel you actually dump information
into.

## What This Is Not

The honest framing matters more than the marketing.

This is not a managed service. There is no hosted version. You run a
VPS, you run the install, you watch the logs. If you are not
comfortable doing that, this repo is the wrong starting point — try a
managed memory product first and come back when you outgrow it.

This is not a retrieval-augmented-generation toolkit. It is not
optimized for ingesting a thousand-page PDF and answering questions
over it. The data model is "small notes, written by agents and humans
during work, retrieved by other agents during work." If you want to
chat with documents, use a different stack.

This is not a Notion or Obsidian replacement. Humans are not the
primary readers. The vault is grep-friendly because grep is useful
for debugging, but the daily reader is your agent, not you. If you
find yourself opening the vault in a text editor every day, something
has gone wrong in the agent workflow.

This is not magic. The brain only knows what was written to it. If
your agents never call `memory.create_decision_note`, the recall
layer will return nothing. The investment is in writing — in building
the discipline (and the hooks) that capture decisions as they happen.
The recall side is automatic; the memory side is not.

> Warning: The most common failure mode is to install the stack,
> forget to wire up the write hooks, and conclude six weeks later
> that recall "doesn't work." Recall works fine. There was nothing to
> find. Wire the writes first; the reads will take care of
> themselves.

Build the writes. Build the discipline. Then watch your agents start
to remember.
