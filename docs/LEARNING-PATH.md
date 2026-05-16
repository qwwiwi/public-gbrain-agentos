# Learning Path

Self-paced. About 6 to 8 hours of focused work, spread across as many evenings as you like. Five milestones, in order. Each milestone has a concept, a short list of docs to read, a hands-on exercise you can finish in 10 to 20 minutes, and a three-question self-check.

Treat this as a study guide, not a tutorial. The actual content lives in the existing repo docs — this page tells you what to read, in what order, and how to know you understood it.

> **Note:** All answers to the self-checks are collected at the bottom under "Self-Quiz Answers". Try to write your own answers first.

If you only have one evening, do Milestones 1 and 2. They give you the mental model. The other three are about turning that model into running software.

A word on pacing. The milestones are sized so you can fit one into a weekday evening and two into a weekend morning. Do not rush. The exercises matter more than the reading — they are how you find the gaps in your own understanding before they become bugs in your deploy.

How to use this page: open the doc linked under "Read", finish it, then come back here and do the hands-on. The self-check is your gate to the next milestone. If you cannot answer two of the three questions in your own words, you are not ready to move on — re-read the doc, not the answer key.

**What you will have at the end:** after Milestone 5 you will have a running shared brain on a VPS you control, one agent (the inbox agent) writing curated notes to that brain on a schedule, a `CLAUDE.md` you wrote yourself describing how the agent behaves, and the mental model to add a second agent without breaking the first. That is the minimum viable second brain. Everything else — multi-agent swarms, monitoring, fancy recall — is iteration on top of that base.

### What this path is not

This is not a course. There is no certificate, no instructor, no community channel. You move at your own speed and you debug your own deploy. The repo docs are the source of truth; this page is a sequence over them. If you find a gap, open an issue against the repo — the next iteration of this path will fill it in.

---

## Milestone 1: Theory — Why Shared Memory

**Concept.** An agent without memory is a goldfish. The context window is not memory — it dies at session end. Local markdown files alone fail the moment you spin up a second agent, or a second machine, or the same agent in a new session. You need one shared brain behind an API that any number of agents can read and write, with identity attached to every write.

Three claims to hold in your head:

- The agent process is short-lived. Memory has to outlive it.
- Multiple agents collaborating means one source of truth, not N copies that drift.
- Every write must carry an identity so you can audit and recover later.

The hardest concept here is tiering. Not every fact deserves the same shelf. Today's debug log is hot — useful for an hour, noise tomorrow. A decision about your stack is warm — surfaces for weeks. A retrospective from last quarter is cold — searchable but not in your face. Pretending all three live at the same level is the fastest way to drown a new agent in context.

If you have ever opened a session and watched the agent re-derive a decision you already made last Tuesday, you have felt the goldfish problem firsthand. The fix is not "give the agent a bigger context window" — it is "give the agent the right slice of memory at the right moment". Tiering is how you do that.

A subtle corollary: shared memory is also shared accountability. The moment two agents write to the same store, you need to know who wrote what. That is why identity-at-the-boundary appears in Milestone 2 and not later — it is part of the foundation, not an add-on.

### Read

- [`walkthrough.md`](./walkthrough.md) — sections "The Problem: Agents Without Memory" and "The Solution: Shared Brain Behind an API".
- [`README.md`](../README.md) — top section only, for the one-paragraph framing.
- [`agent-template/docs/MEMORY.md`](../agent-template/docs/MEMORY.md) — optional, if the tiering idea feels abstract.

### Hands-on

Open a blank file. Without looking at the diagram, sketch the four memory layers (hot, warm, cold, long-term) and write one sentence under each describing what kind of fact lives there and how long it lives. Then open `walkthrough.md` and compare. Where your sketch disagrees with the doc is exactly where to focus next time you re-read. Bonus: pick one real fact from your week (a decision, a bug, a name you keep forgetting) and place it on the correct tier.

### Check yourself

1. Why does adding a second agent break "just keep notes in local markdown files"?
2. What is the difference between hot memory and warm memory, in one sentence each?
3. If two agents both want to record a decision at the same time, what mechanism prevents one from silently overwriting the other?

---

## Milestone 2: Architecture — How the Stack Fits

**Concept.** The shared brain is three small MCP services (memory, recall, swarm) in front of one Postgres database with the `pgvector` extension. Every agent talks to those services over HTTP using the Model Context Protocol. A bearer token in the request header identifies the agent. A middleware layer reads the header at the transport boundary and pins the identity for the entire request. Without that pin, every call would look like it came from the same caller — which is exactly the bug that you do not want to ship.

You do not need to understand the SQL. You do need to understand the request path: agent → MCP transport → service handler → database → response.

Two ideas worth slowing down on. First, MCP is just a transport contract — your agent does not care which language the server is written in, only that the tool names and shapes match the manifest. That decoupling is why you can swap out the recall service without touching any agent code. Second, identity at the boundary is not paranoia, it is what makes audit, rate-limiting, and per-agent permissions possible later without rewriting any handler.

The three services split by verb, not by data. Memory writes facts. Recall reads facts. Swarm moves messages between agents. They all share the same Postgres instance and the same auth model — splitting them by responsibility makes each one small enough to reason about and replace independently.

### Read

- [`architecture.md`](./architecture.md) — the canonical stack overview.
- [`agent-template/docs/ARCHITECTURE.md`](../agent-template/docs/ARCHITECTURE.md) — same picture from the agent side: what an agent sees, what it sends, what it expects back.
- [`security.md`](./security.md) — optional, the auth section makes the middleware story concrete.

### Hands-on

On paper or in a whiteboard tool, draw the request path for a single `recall` call. Start from "agent decides to ask a question" and end at "agent receives a ranked list of memory hits". Include: which service handles it, where the embedding is computed, what hits Postgres, and what comes back. Aim for five to seven boxes. Compare to the diagram in `architecture.md`. If you missed the embedding step or the identity check, re-read those sections. Then repeat the exercise for a `memory.write_decision` call — the path is shorter, but the identity stamp matters more.

### Check yourself

1. Why is `pgvector` better than a separate vector database for a small team running this stack?
2. What does the auth middleware do that the tool handler cannot do safely on its own?
3. Name the three MCP services and one verb each one is responsible for.

---

## Milestone 3: Inbox Agent — Your First Real Agent

This milestone is design-only — you will wire the inbox agent up after deploying the brain in Milestone 5.

**Concept.** The inbox agent is the cleanest first agent to build because the problem is small and the value is immediate. Something feeds raw content in (a Telegram saved-message channel, an email forwarder, an RSS reader, a webhook from your note app). A capture hook drops the raw input into a staging folder. A scheduled compile step turns raw scraps into curated notes with structure, tags, and links. The curated notes get written to the shared brain so any other agent can recall them.

The pattern is dual-write: keep the local markdown file as the canonical human-readable form, and push a copy to the brain so it joins the searchable corpus. Local wins for editing. Brain wins for recall across agents.

Why start here? Because the inbox agent is a closed loop you can verify in one afternoon. You forward yourself a note, you wait for the next compile tick, you read the curated output, you query the brain from another agent and watch the note surface. That round-trip is the smallest possible proof that the whole stack works end-to-end.

The compile step is where the real design choices live. A naive compile is "take the raw text, push it to the brain". A useful compile extracts structure — dates, names, decisions, action items, links — and tags the result so future recalls find it. You can start naive and iterate; the brain will not punish you for first-draft notes.

### Read

- [`inbox-agent/README.md`](../inbox-agent/README.md) — the reference implementation.
- [`agent-template/docs/FIRST-AGENT.md`](../agent-template/docs/FIRST-AGENT.md) — generic shape of any first agent you write.
- [`agent-template/docs/HOOKS.md`](../agent-template/docs/HOOKS.md) — optional, only if you want to wire custom hooks before compile.

### Hands-on

Pick one inbox source you actually use and write it down. Telegram saved messages? Personal email? A shared note folder? A Slack channel you control? Just decide. Then in one paragraph, describe what counts as a "useful note" coming from that source — meeting transcripts, voice memos, links with commentary, raw quotes? You are choosing the shape of your future curated corpus. Do not deploy anything yet. Sketch the YAML frontmatter your curated notes will use (source, captured_at, tags, type) before you write a single line of code — the schema is the agent.

### Check yourself

1. Why does the inbox agent write to both a local file and the shared brain, instead of just one?
2. What runs on a cron schedule and why is that better than compiling on every new raw entry?
3. If your raw source produces noise (ads, autoreplies), where in the pipeline should you filter it out, and why there?

---

## Milestone 4: CLAUDE.md — Giving Your Agent a Soul

**Concept.** `CLAUDE.md` is the file Claude Code reads at the start of every session in a workspace. It is the agent's identity, its rules of engagement, and the index to the rest of its memory. A good `CLAUDE.md` is short — usually under 300 lines — and ruthlessly specific. It tells the agent who it is, who it serves, what it must never do, where to find the rest of its memory, and how to behave when it is unsure. Everything else (warm decisions, learnings, tool inventory) lives in separate files that `CLAUDE.md` points at.

The biggest mistake people make is cargo-culting someone else's `CLAUDE.md`. Yours should reflect your actual workflow, your actual tools, your actual constraints. Copy the structure, not the content.

A second mistake is treating `CLAUDE.md` as static. It is a living artifact. Every time you correct the agent ("no, do not commit that, you forgot the review step"), you have a choice: explain it once and forget, or capture it as a rule. The agents that get reliable over time are the ones whose `CLAUDE.md` grows from corrections, not from imagination.

A useful test: imagine you handed your `CLAUDE.md` to a new contractor and asked them to act as the agent for a day. Could they? If the answer requires a 30-minute call to fill in context, the file is too short. If the answer is "yes but they would be slow", the file is right-sized.

### Read

- [`agent-template/templates/CLAUDE.md.template`](../agent-template/templates/CLAUDE.md.template) — the skeleton.
- [`agent-template/docs/AGENT-LAWS.md`](../agent-template/docs/AGENT-LAWS.md) — the patterns that make an agent reliable over many sessions.
- [`agent-template/docs/MEMORY.md`](../agent-template/docs/MEMORY.md) — how the memory tiers connect back to the file.
- [`agent-template/docs/MULTI-AGENT.md`](../agent-template/docs/MULTI-AGENT.md) — optional, only if you plan a second agent in the next month.

### Hands-on

Write a 150 to 250 line `CLAUDE.md` for the inbox agent you scoped in Milestone 3. Use the template as your skeleton. Fill in: the agent's role in one paragraph, who it reports to, three to five hard rules ("never delete raw inbox entries", "always tag curated notes with source"), and pointers to the memory files it will use. Do not deploy. Just write the file and read it back as if you were the agent — would you know what to do on Monday morning? If the answer is "not sure", that is the gap to fix.

### Check yourself

1. Why should hard rules live in `CLAUDE.md` itself and not in a separate file the agent might forget to load?
2. What is the difference between a rule and a learning, and why are they stored separately?
3. If two rules in your `CLAUDE.md` contradict each other, what should the agent do?

---

## Milestone 5: Deploy — Make It Real

**Concept.** The shared brain runs on a small VPS. Your agents run wherever you do your work — usually a laptop. Secrets stay on the laptop side and are sent to the VPS over an encrypted channel only when needed. The deploy is boring on purpose: clone, install dependencies, configure environment, run migrations, start systemd units, smoke-test. If a step needs a creative decision, the script asks. If it does not, the script just runs.

You have two ways to deploy. The fast way: run [`scripts/install-local.sh`](../scripts/install-local.sh) from your laptop against a fresh Ubuntu VPS — it walks you through prompts, copies files, sets up systemd, and verifies. The boring way: hand [`AGENT.md`](../AGENT.md) to a Claude Code agent in this repo and let it run the deploy end-to-end, asking you for confirmation on destructive steps.

Either way, the deploy is a one-time event. After that, your day-to-day is just writing to the brain and recalling from it. Resist the urge to keep tweaking the infrastructure once smoke is green — the value is in the notes you accumulate, not in the pipeline that stores them.

One real-world note: a clean install on a fresh VPS is almost always easier than reviving a half-broken one. If your first deploy goes sideways and you cannot diagnose it in 30 minutes, destroy the VPS, provision a new one, and re-run. The scripts are designed for that — repeatability beats heroics.

### Read

- [`setup.md`](./setup.md) — step-by-step install and config.
- [`security.md`](./security.md) — what the threat model is, what tokens to rotate, what to put on a firewall.
- [`troubleshooting.md`](./troubleshooting.md) — skim it before you deploy, not after.
- [`agent-template/docs/SETUP-GUIDE.md`](../agent-template/docs/SETUP-GUIDE.md) — agent-side install, after the brain is up.

### Hands-on

Provision a fresh Ubuntu VPS (any provider with one CPU and 2 GB RAM will do). Run [`scripts/install-local.sh`](../scripts/install-local.sh) from your laptop against it. When it finishes, run [`scripts/smoke-test.sh`](../scripts/smoke-test.sh) and read every line of the output. A green smoke does not mean done — it means the services answered. Now go back to Milestone 3 and actually wire up your inbox source. If smoke fails, read the failing line, open [`troubleshooting.md`](./troubleshooting.md), and resist the urge to ssh in and patch by hand — fix the script, re-run, keep it reproducible.

### Check yourself

1. Why do agent tokens live on your laptop and not in the VPS repo checkout?
2. What does the smoke test prove, and what does it not prove?
3. If the deploy fails halfway through, what makes it safe to re-run from the start?

---

## Self-Quiz Answers

**Milestone 1.**

1. A second agent has its own filesystem and its own session — local files on agent A are invisible to agent B. You end up with two separate corpora that drift, with no way to merge cleanly.
2. Hot memory is the current session and the last few hours of activity — fast, lossy, dies on compaction. Warm memory is recent decisions and learnings — surfaces automatically at session start, lives weeks to months.
3. The memory service serializes writes through the database and stamps each write with the calling agent's identity. Two simultaneous writes both succeed and both are visible in the audit log; nothing is silently overwritten.

**Milestone 2.**

1. One database to operate, one backup story, one place to run queries that join structured data with vector search. A separate vector DB doubles the ops surface for no win at small scale.
2. The middleware reads the bearer token from the HTTP layer and pins the agent identity in a request-scoped context before any tool handler runs. The handler itself sees a clean "who am I serving" value and cannot accidentally fall back to a default identity if the header is missing — the request just fails authentication.
3. Memory (write notes), recall (search notes), swarm (notify another agent).

**Milestone 3.**

1. The local file is canonical for humans editing in their note app or text editor. The brain copy is searchable across all agents and survives even if the laptop is wiped. Dual-write means you keep both properties.
2. Cron decouples capture from compile. Raw entries can arrive in bursts; compile runs on a steady cadence and can batch, dedupe, and apply heavier processing. Compile-on-every-raw makes the system fragile under load.
3. As early as possible, ideally in the capture hook. Filtering at compile time means the noise sits in your raw store forever taking up space and showing up in any unfiltered query.

**Milestone 4.**

1. `CLAUDE.md` is loaded at the start of every session, before the agent does anything. A separate file the agent has to discover and read might not get loaded until the agent has already made a mistake.
2. A rule is a constraint the agent must never violate (set by you). A learning is a scored observation about how to do things better (often promoted from a correction). Rules are absolute, learnings are weighted — mixing them makes both worse.
3. Stop and ask. Two contradicting rules means the rule set is broken; the agent should not pick one and proceed silently.

**Milestone 5.**

1. Tokens grant access to the brain. Keeping them on the laptop means a compromised VPS cannot impersonate every agent — it can only impersonate the services on that VPS.
2. The smoke test proves the services are running, the database is reachable, auth is configured, and round-trip write-then-read works. It does not prove your inbox source is wired up, your agents have the right tokens, or your backups are running.
3. The install scripts are idempotent — every step checks current state before acting. Re-running picks up where the previous attempt left off and only re-applies steps that have not yet succeeded.

---

When you finish all five milestones, go back to [`walkthrough.md`](./walkthrough.md) and re-read it. It will read completely differently the second time. That is the signal you understood the stack.

Coming in the next iteration: longer-form lessons under `docs/lessons/`, an inbox-flow narrative walkthrough with screenshots, and a contributor guide. Those are not yet covered here. If something in this path felt thin, that is probably where the depth will go.

One last piece of advice. Build the smallest version of every piece first. Smallest brain, smallest agent, smallest inbox. Get it round-tripping end-to-end before you add a second agent, a second source, or a second memory tier. Every shortcut you take at the start is a debt you pay later — but every premature feature is a debt you may never need to take on at all.
