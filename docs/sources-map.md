# Sources Map

Every block in this stack has at least one good free option and a paid alternative
that buys you convenience. This document maps each component to a recommendation,
an alternative or two, the real cost, and the trade-off you accept.

Read this before you start procurement. The defaults below are deliberately boring:
mature, documented, self-hostable. The alternatives are listed because sometimes
boring is wrong for your situation (you have no ops time, you need managed scale,
you are already locked into a vendor).

Cost legend used throughout:

- `Free` — no fee, self-hosted on hardware you already pay for.
- `Free tier + paid` — usable indefinitely below a threshold, costs money above it.
- `Paid` — has no usable free tier.
- `Self-host` — software is free; you pay for the server.

## Database

**Recommended:** [Postgres 16](https://www.postgresql.org/docs/16/) with [Alembic](https://alembic.sqlalchemy.org/en/latest/) for schema management — mature, predictable, runs anywhere; Alembic if you live in Python.

| Component  | Alternative          | Cost              | Notes                                  |
|------------|----------------------|-------------------|----------------------------------------|
| Postgres   | [Supabase](https://supabase.com/docs) | Free tier + paid | Managed Postgres with auth + storage.  |
| Postgres   | [Neon](https://neon.tech/docs)        | Free tier + paid | Serverless branches, cold-start cost.  |
| Schema mgmt| [Atlas](https://atlasgo.io/docs)      | Free              | Language-agnostic migrations.          |

Pick self-hosted Postgres if you already have a Linux VM and want zero vendor
lock-in. The yearly cost of a $20/month VM that runs Postgres + everything else
beats any managed plan, and you keep raw `psql` access. Pick Supabase or Neon
when you want a real ops team to handle backups and failover for you, and you
accept that pgvector availability and version pinning are at the provider's
mercy. Neon's serverless model adds cold-start latency on idle databases —
fine for dashboards, painful for chat workloads.

```bash
# Self-host on Ubuntu 22.04
sudo apt install postgresql-16 postgresql-16-pgvector
sudo -u postgres createdb agentos
```

## Vector Search

**Recommended:** [pgvector](https://github.com/pgvector/pgvector) — in-Postgres, no second daemon, joins against relational tables in one query.

| Component | Alternative                                  | Cost              | Notes                                |
|-----------|----------------------------------------------|-------------------|--------------------------------------|
| Vector    | [Qdrant](https://qdrant.tech/documentation/) | Free / Self-host  | Fast at scale, nicer filtering DSL.  |
| Vector    | [Weaviate](https://weaviate.io/developers/weaviate) | Free / Self-host | Strong if you want hybrid search.    |
| Vector    | [Chroma](https://docs.trychroma.com/)        | Free              | Good for local dev, not for prod.    |

pgvector wins for one reason: you already run Postgres. Adding a vector column
costs you one extension, one index, and zero new daemons to monitor. You can
join vector results against relational tables in a single query. Qdrant and
Weaviate are faster at scale (10M+ vectors) and have nicer filtering DSLs,
but you pay in ops cost: another service to run, secure, back up, and patch.
Choose them only if you have already proven you need them.

```sql
CREATE EXTENSION vector;
CREATE INDEX ON notes USING ivfflat (embedding vector_cosine_ops);
SELECT id FROM notes ORDER BY embedding <=> $1 LIMIT 5;
```

## Embeddings

**Recommended:** [FastEmbed](https://github.com/qdrant/fastembed) — local, ONNX runtime, no API key, no per-token bill.

| Component  | Alternative                                       | Cost                          | Notes                                |
|------------|---------------------------------------------------|-------------------------------|--------------------------------------|
| Embeddings | [OpenRouter](https://openrouter.ai/docs)          | varies, ~$0.02 per million tokens | One key, many embedding providers.   |
| Embeddings | [Cohere Embed v3](https://docs.cohere.com/docs/embeddings) | Paid, ~$0.10 per million tokens | Multilingual, strong recall.        |
| Embeddings | [Voyage AI](https://docs.voyageai.com/)           | Paid, ~$0.05 per million tokens | Domain-specific embed families.     |

FastEmbed runs the embedding model in-process on CPU using ONNX runtime — no
external API, no rate limits, no per-token bill. It uses well-known open models
(`bge-small-en-v1.5`, `multilingual-e5-large`) that are competitive for retrieval
within a small corpus (< 1M chunks). Pick a paid API when you have a polyglot
corpus and want best-in-class multilingual recall (Cohere), or when you have a
specific vertical and Voyage has a tuned model for it. Costs at "writing time"
are indicative — embedding pricing has been falling roughly 30% per year, so
re-check before committing.

```bash
pip install fastembed
python -c "from fastembed import TextEmbedding; \
  m = TextEmbedding('BAAI/bge-small-en-v1.5'); \
  print(next(m.embed(['hello world']))[:5])"
```

## Chat LLM

**Recommended:** [Anthropic Claude](https://docs.anthropic.com/) (Sonnet or Opus tier) — best balance of long-context handling and instruction following for a personal second brain.

| Component | Alternative                                 | Cost                | Notes                              |
|-----------|---------------------------------------------|---------------------|------------------------------------|
| LLM       | [OpenAI](https://platform.openai.com/docs/) | Paid                | Near-equivalent, often a few percent cheaper. |
| LLM       | [OpenRouter](https://openrouter.ai/docs)    | Paid                | One key, many models, easy A/B.    |
| LLM       | [Ollama](https://github.com/ollama/ollama)  | Self-host           | Local inference, privacy-first.    |

For a personal second-brain that reads private notes and runs writing tasks,
Claude (Sonnet or Opus tier) gives the best balance of long-context handling
and instruction following at writing time. OpenAI is a near-equivalent and
often a few percent cheaper per call. OpenRouter is the right pick when you
want to switch providers without code changes, or when you need a model that
one provider hosts and the other does not. Ollama is the answer when private
data must never leave your machine — accept that local 8B–13B models are not
yet at the level of frontier hosted models for nuanced reasoning.

```python
from anthropic import Anthropic
client = Anthropic()
msg = client.messages.create(model="claude-sonnet-4-5", max_tokens=512,
    messages=[{"role": "user", "content": "summarize my last 7 days"}])
```

## Reverse Proxy

| Component | Recommended                                  | Alternative                                                          | Cost | Notes                              |
|-----------|----------------------------------------------|----------------------------------------------------------------------|------|------------------------------------|
| Proxy     | [Caddy 2](https://caddyserver.com/docs/)     | [Nginx](https://nginx.org/en/docs/) + [Certbot](https://eff-certbot.readthedocs.io/) | Free | Automatic HTTPS via Let's Encrypt. |
| Proxy     | [Caddy 2](https://caddyserver.com/docs/)     | [Traefik](https://doc.traefik.io/traefik/)                           | Free | Strong if you live in Docker.      |

Caddy is the right default because TLS certificates renew themselves with zero
configuration and the config file syntax fits on a postcard. Nginx is the
incumbent and has the broadest tutorial coverage on the public internet, but
you'll write more lines of config and run Certbot separately. Traefik shines
inside a Docker Swarm or Kubernetes cluster where service discovery matters;
for a single VM running a handful of services, it's overkill.

```caddy
brain.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

## Process Supervisor

| Component  | Recommended                                                                                         | Alternative                                    | Cost      | Notes                            |
|------------|-----------------------------------------------------------------------------------------------------|------------------------------------------------|-----------|----------------------------------|
| Supervisor | [systemd](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)             | [PM2](https://pm2.keymetrics.io/docs/usage/quick-start/) | Free      | Built-in on every Linux distro.  |
| Supervisor | [systemd](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)             | [supervisord](http://supervisord.org/)         | Free      | Pre-systemd legacy, still works. |

systemd ships with your OS. It restarts crashed processes, captures logs to
journald, enforces resource limits, and orders dependencies — all without any
extra package. PM2 makes sense only if your team already speaks Node and wants
its cluster-mode load balancing. supervisord is a fine fallback if you are on
a non-systemd distro (Alpine), but otherwise it duplicates work the OS already
does.

```ini
# /etc/systemd/system/brain-api.service
[Service]
ExecStart=/opt/brain/.venv/bin/python -m brain.api
Restart=always
```

## Backup

| Component | Recommended                                | Alternative                                                                | Cost          | Notes                                  |
|-----------|--------------------------------------------|----------------------------------------------------------------------------|---------------|----------------------------------------|
| Backup    | [restic](https://restic.readthedocs.io/)   | [Backblaze B2](https://www.backblaze.com/cloud-storage)                    | Paid, ~$6/TB/month | Cheapest object storage at writing time. |
| Backup    | [restic](https://restic.readthedocs.io/)   | [Wasabi](https://wasabi.com/cloud-object-storage)                          | Paid, ~$7/TB/month | No egress fees, simpler pricing.       |
| Backup    | [restic](https://restic.readthedocs.io/)   | [Cloudflare R2](https://developers.cloudflare.com/r2/)                     | Paid, ~$15/TB/month | Free egress, useful for restore tests. |
| Backup    | [restic](https://restic.readthedocs.io/)   | [DigitalOcean Spaces](https://docs.digitalocean.com/products/spaces/)      | Paid, $5/250GB     | Flat fee, predictable for small data.  |

restic does encrypted, deduplicated, incremental snapshots against any
S3-compatible target. The repository format is documented and stable, so you
can move providers without re-uploading from scratch. Storage choice depends on
egress: if you expect to restore frequently (DR drills, dev clones), pay R2's
slightly higher storage fee to dodge egress charges. For pure cold archive,
Backblaze B2 is hard to beat. Schedule backups via systemd timer or cron; test
restore quarterly.

```bash
restic -r b2:mybucket:/brain init
restic -r b2:mybucket:/brain backup /var/lib/brain
restic -r b2:mybucket:/brain forget --keep-daily 7 --keep-weekly 4
```

## Telegram Integration

| Component | Recommended                                          | Alternative                                                       | Cost | Notes                                 |
|-----------|------------------------------------------------------|-------------------------------------------------------------------|------|---------------------------------------|
| Bot API   | [aiogram 3](https://docs.aiogram.dev/)               | [python-telegram-bot](https://docs.python-telegram-bot.org/)      | Free | Simple, official Bot API surface.     |
| MTProto   | [Telethon](https://docs.telethon.dev/en/stable/)     | [Pyrogram](https://docs.pyrogram.org/)                            | Free | Full user account, reads any chat.    |

Two completely different paths. **Bot API** (via `api.telegram.org`) is the
simple, supported route: register a bot with BotFather, talk to your bot from
any user account. Use this when you want a personal assistant you talk to in
DMs. **MTProto** runs as if it were a user — it can read all chats you are a
member of, including channels you only subscribe to. Use this when you want
your second brain to passively ingest everything you already read.

For MTProto, you need an `API_ID` and `API_HASH` from
[my.telegram.org/apps](https://my.telegram.org/apps). Authorization can fail
from data-center IP ranges; if the apps page does not load, retry from a
residential connection. Once you have the credentials, store them outside
your repo.

```python
from telethon import TelegramClient
api_id, api_hash = 12345, "abc..."
client = TelegramClient("session_name", api_id, api_hash)
client.start()
```

## Skills Bundle

| Component   | Recommended                                            | Alternative              | Cost | Notes                                  |
|-------------|--------------------------------------------------------|--------------------------|------|----------------------------------------|
| Skill base  | This repo's `skills/` folder                           | Write your own from spec | Free | Reference implementations included.    |
| Skill spec  | [Claude skill format](https://docs.anthropic.com/)     | Custom YAML schema       | Free | Folder + `SKILL.md` + scripts.         |

The `skills/` directory in this repo is the canonical bundle: each subfolder
has a `SKILL.md` describing trigger phrases and inputs, plus a `scripts/`
directory with executable helpers. To extend, fork the repo and either edit a
skill in-place or copy one as a template and rename. Skills are designed to be
swappable: replace the embedding skill with one that calls Cohere instead of
FastEmbed, and the rest of the system keeps working as long as the interface
contract (input args, output schema) is preserved.

```bash
cp -r skills/example skills/my-new-skill
$EDITOR skills/my-new-skill/SKILL.md
```

## Optional Extras

| Component   | Recommended                                                 | Alternative                                       | Cost      | Notes                              |
|-------------|-------------------------------------------------------------|---------------------------------------------------|-----------|------------------------------------|
| Metrics     | [Prometheus](https://prometheus.io/docs/) + [Grafana](https://grafana.com/docs/grafana/latest/) | [Netdata](https://learn.netdata.cloud/) | Free      | Standard ops dashboard.            |
| Logs        | [Loki](https://grafana.com/docs/loki/latest/)               | journald + grep                                   | Free      | Loki only if you have multiple VMs.|
| LLM tracing | [Langfuse](https://langfuse.com/docs)                       | [Phoenix](https://docs.arize.com/phoenix)         | Self-host / Free tier + paid | Track every prompt + cost. |

You do not need any of this on day one. Add Prometheus + Grafana the first
time you wonder "is the embedding job actually running?" Add Loki when you
have more than one VM and `ssh + grep` stops scaling. Add Langfuse the first
time you get a surprise $200 bill from an LLM provider and want to know which
prompt did it.

```yaml
# docker-compose for the lazy
services:
  prometheus: { image: prom/prometheus, ports: ["9090:9090"] }
  grafana:    { image: grafana/grafana, ports: ["3000:3000"] }
```

## Cost Math Worked Through

A rough monthly bill for a working personal second brain at small scale,
assuming you self-host on a $20 VM and use a paid LLM:

| Line item                      | Monthly cost |
|--------------------------------|--------------|
| VM (4 vCPU, 8 GB RAM, 80 GB)   | $20          |
| Postgres + pgvector            | $0 (on VM)   |
| FastEmbed                      | $0 (on VM)   |
| Claude API (~5M tokens)        | ~$15         |
| restic + Backblaze B2 (50 GB)  | ~$0.30       |
| Domain name + DNS              | ~$1          |
| **Total**                      | **~$36/mo**  |

Move embeddings to a paid API and the bill climbs by another $5–$20 depending
on volume. Move the database to managed Postgres and add $25–$50. Each managed
service buys ops time back; the question is whether your ops time costs more
than the difference.

## Recommended starter stack

| Concern          | Pick                              |
|------------------|-----------------------------------|
| OS               | Ubuntu 22.04 LTS on a $20/mo VM   |
| Database         | Postgres 16 + pgvector            |
| Embeddings       | FastEmbed (`bge-small-en-v1.5`)   |
| LLM              | Anthropic Claude API              |
| Reverse proxy    | Caddy                             |
| Supervisor       | systemd                           |
| Backup           | restic to Backblaze B2            |
| Telegram         | Bot API via aiogram first; add Telethon later if you want passive ingestion |
| Metrics          | Skip until you need it            |

This is the boring stack. None of it is fashionable. All of it has been in
production for years, is documented exhaustively, and will still be the right
answer in two years. Start here. Replace components only when you can describe
the specific pain that pushed you out — "Postgres is slow" is not a reason;
"we have 50M vectors and pgvector recall is 60% at p95 200ms" is.

The fastest path from zero to working: provision the VM, install Postgres,
install Caddy, point a domain at the VM, get an Anthropic API key, clone this
repo, run the setup script. Skip every optional extra. Add complexity only
when the system tells you it needs it.
