# public-gbrain-agentos

> **Общий мозг для команды Claude Code агентов.** Self-hosted на одном VPS. Markdown vault, гибридный recall (semantic + lexical), Telegram inbox, слойная память для каждого агента, интеграция с Hermes Agent. Опционально — генератор самих агентов.

---

## TL;DR

Клонируй репо, передай файл `AGENT.md` свежему Claude Code агенту, выбери **Путь A** или **Путь B**, ответь на 8–12 вопросов — и через 30–90 минут у тебя:

- **(A)** Долговременная память для агентов, наполняемая через Telegram-бот.
- **(B)** То же самое плюс N персональных workspace'ов Claude Code агентов, каждый со своей слойной памятью и recall'ом в общий мозг.

---

## Что такое «общий мозг»?

**Общий мозг (Second Brain) — это структурированный слой долговременной памяти для AI-агентов.** Без него каждая сессия Claude Code забывает решения, ранбуки, ошибки и внешние источники. С ним — агенты пишут в общий markdown-vault и достают оттуда контекст вместо того чтобы раздувать промпт до 200k токенов.

**Плюс — генератор самих агентов (опционально).** Папка `agent-template/` в репо — полный генератор Claude Code workspace'ов со слойной памятью (hot/warm/cold), hooks (Stop / SessionStart / PreCompact) и `.mcp.json`, уже подключённым к твоему мозгу. Одна команда — один агент. Запускай столько раз, сколько агентов нужно.

**Для кого:** одиночные строители и небольшие команды, которые крутят 2+ Claude Code агентов (координатор, кодер, ревьюер, ресёрчер) и хотят чтобы они делили общую институциональную память.

- 1 агент, контекста 200k хватает → бери **Путь A** (только мозг).
- 3+ агента наступают друг другу на хвосты по решениям → **Путь B** (мозг + workspace'ы).

---

## Два пути установки

| | Путь A — минимальный | Путь B — полный стек |
|---|---|---|
| Что устанавливается | Общий мозг (VPS) + Telegram inbox-agent | Мозг + inbox-agent + N персональных workspace'ов |
| Время | ~30 мин | ~60–90 мин (+10 мин на каждого доп. агента) |
| Кому | Архив форвардов из Telegram, daily digest, recall API под ручное использование | Команды агентов с общей памятью, у каждого своя слойная память |
| Использует `agent-template/`? | Нет | Да |
| Результат | Markdown-vault с поиском, daily digest, recall через MCP | Всё от A + N workspace'ов `~/.claude-lab/<agent-id>/.claude/` с собственным SOUL, rules, decisions, hot handoff, hooks, cron-ротацией памяти и `.mcp.json` |

Не уверен → начни с **A**. Добавить B потом — одна команда на агента: `bash agent-template/install.sh`.

---

## Что ты получаешь после установки

| Компонент | Путь A | Путь B |
|---|---|---|
| Postgres 16 + pgvector на VPS | ✅ | ✅ |
| **4 MCP сервиса** (memory write, recall read, swarm event bus, tasks coordination) | ✅ | ✅ |
| Ingest worker (эмбеддит новые файлы vault'а) | ✅ | ✅ |
| Markdown vault (12 пронумерованных папок) | ✅ | ✅ |
| Telegram inbox-agent (dual-write в local raw/ + brain) | ✅ | ✅ |
| Daily digest cron (09:00) + compile cron (каждые 15 мин) | ✅ | ✅ |
| Опциональные ingestion skills (YouTube, IG, X, voice, web) | ✅ | ✅ |
| Bearer токен на каждую идентичность в `agent_tokens` | 2 по умолчанию | 2 + 1 на каждого персонального агента |
| **HMAC-аутентификация для Hermes Agent** (опционально) | ✅ | ✅ |
| Персональный Claude Code workspace `~/.claude-lab/<agent-id>/.claude/` | ❌ | ✅ (N штук) |
| Слойная память (CLAUDE.md / rules.md / decisions.md / handoff.md → MEMORY.md / LEARNINGS.md / TOOLS.md по запросу) | ❌ | ✅ |
| Stop / SessionStart / PreCompact hooks | ❌ | ✅ |
| Cron-ротация памяти (hot → warm → cold) | ❌ | ✅ |
| `.mcp.json` каждого workspace'а, уже подключённый к мозгу | ❌ | ✅ |

---

## Архитектура

```
   Ты форвардишь контент                    Твои агенты достают
   в Telegram-бот               <-->         и пишут решения
           |                                            |
           v                                            v
  +----------------+                          +-------------------+
  |  inbox-agent   |---------HTTPS / TS-------|       VPS         |
  |  (локально)    |                          |                   |
  |  dual-writes   |                          |  Caddy (TLS)      |
  |  в local raw/  |                          |  memory_mcp 8767  |
  |  И в remote    |                          |  recall_mcp 8768  |
  +----------------+                          |  swarm_mcp  8766  |
          ^                                   |  task_mcp   8769  |
          |                                   |  ingest-worker    |
  +-------+-------------+ (Путь B)            |                   |
  |  Персональные       |--------HTTPS/TS---->|  Postgres 16      |
  |  агенты             |                     |  + pgvector       |
  |  ~/.claude-lab/     |                     |  + FTS            |
  |  <agent-id>/.claude |                     |                   |
  |  + hot/warm/cold    |                     |  vault/ (12 dirs) |
  |  + Stop/Session/    |                     +-------------------+
  |    PreCompact hooks |                              ^
  |  + .mcp.json        |                              |
  +---------------------+                              |
                                       Hermes Agent ---+
                                       (опционально,
                                        HMAC-подпись)
```

**4 MCP сервиса:**

1. **`memory_mcp`** (порт 8767) — пишет в vault. Тулзы: создание decisions, runbooks, error-patterns, daily logs, external notes. Каждая запись через scoped Bearer (inbox-agent не может писать decisions).
2. **`recall_mcp`** (порт 8768) — гибридный поиск. Семантика (1024-dim FastEmbed multilingual embeddings) + лексика (Postgres FTS), слитые через Reciprocal Rank Fusion, re-weighted по типу источника и свежести.
3. **`swarm_mcp`** (порт 8766) — шина событий между агентами. Inbox, notify, ack, broadcast, escalate.
4. **`task_mcp`** (порт 8769) — координация задач. State machine `new → progress → review → done` (+ `blocked`), task_history, agent heartbeat с metadata.

**Идентичность:** у каждого агента собственный Bearer-токен в таблице `agent_tokens` со scope'ами read/write. AuthCaptureMiddleware (ASGI) кладёт identity в ContextVar — без silent fallback на «default agent» (исправлено в инцидентах identity-fix 2026-05-09/16).

**Vault — каноничен.** Plain markdown на файловой системе. Postgres — индекс, не источник. Потерял БД → переэмбеддил из markdown за 5 минут. Потерял vault → проблема — делай бэкап.

В Путь B каждый persональный workspace **тоже** plain markdown (SOUL, rules, decisions, handoff). Можно `tar`-нуть, перенести на другую машину, направить на тот же мозг и продолжить.

---

## Интеграция с Hermes Agent

Если ты крутишь [Hermes Agent](https://github.com/NousResearch/hermes-agent) (или подобный фреймворк, который подписывает запросы по HMAC-схеме `<timestamp>.<body>`), `public-gbrain-agentos` поддерживает обе схемы аутентификации одновременно — **Bearer и HMAC** — через общий middleware. Без правки самого Hermes.

### Как работает

- В таблице `agent_tokens` у агента есть оба поля: `token_sha256` (для Bearer) и `hmac_secret_sha256` (для HMAC). Любое можно `NULL` — один из двух обязателен.
- ASGI middleware `services/shared/asgi_auth.py` (`HermesAwareAuthMiddleware`) читает один из двух заголовков:
  - `Authorization: Bearer <token>` — стандартный путь
  - `X-Hermes-Signature: sha256=<hex>` + `X-Hermes-Timestamp: <unix>` — HMAC-путь
- HMAC проверяется constant-time (`hmac.compare_digest`), timestamp tolerance — 5 минут (настраивается через `HMAC_TIMESTAMP_TOLERANCE_SECONDS`).
- Identity-проверка одна и та же: scope-based RBAC через `agent_tokens.can_write_scopes` / `can_read_scopes`.

### Sidecar proxy для клиентов, которые не умеют HMAC

Hermes выпускает MCP tool-calls без HMAC-подписи. Чтобы не патчить Hermes, в репо есть `scripts/hermes_signed_proxy.py` (Starlette + httpx + uvicorn):

```
Hermes → http://localhost:9100/{memory,recall,swarm,task}/mcp
              ↓ proxy подписывает каждый запрос
              ↓ X-Hermes-Signature: sha256=...
              ↓ X-Hermes-Timestamp: <now>
              ↓
         https://gbrain.example.com/{memory,recall,swarm,task}/mcp
```

Запуск: `python scripts/hermes_signed_proxy.py --listen 0.0.0.0:9100 --upstream https://gbrain.example.com --secret-env GBRAIN_HMAC_SECRET --agent <agent-id>`.

### Выпуск HMAC-секрета

```bash
python scripts/issue-hmac-secret.py --agent <agent-id>
# Печатает raw HMAC secret ОДИН РАЗ в stdout, sha256 сохраняется в БД.
# Скопируй secret в безопасное хранилище — больше его получить нельзя.
```

Полный walkthrough + примеры подписания: `docs/hermes-integration.md`.

---

## Quick start

```bash
git clone https://github.com/<твой-форк>/public-gbrain-agentos.git
cd public-gbrain-agentos
# Открой Claude Code в этой папке, затем скажи агенту:
# «Прочитай AGENT.md. Путь A.» (или «Путь B с координатором и кодером»)
```

Готово. Агент читает `AGENT.md`, спрашивает у тебя VPS-доступ + несколько конфиг-параметров, запускает install-скрипты, выпускает токены, настраивает локального inbox-бота, и (Путь B) запускает `agent-template/install.sh` по разу на каждого персонального агента. В конце прогоняет end-to-end smoke test.

Хочешь руками — читай `docs/setup.md`, там те же шаги в человеческом виде.

---

## FAQ

**Зачем мне общий мозг, если у Claude Code есть memory tool?**
Memory tool — сессионная, теряется при `/clear` или reset контекста. Общий мозг — на диске, не зависит от сессии, делится между агентами, поддерживает поиск (semantic+lexical), backup, версионирование (vault — это git-friendly markdown).

**Можно без Telegram?**
Да. Telegram inbox — опциональный путь наполнения. Без него мозг работает как recall-API: агенты пишут decisions/runbooks через `memory_mcp` и достают через `recall_mcp`.

**Можно ли мозг без vault'а?**
Нет. Vault — источник правды. Postgres — только индекс.

**Что если я не знаю Postgres / Linux / VPS?**
Установка автоматизирована — Claude Code агент по `AGENT.md` сам выполнит SSH-команды, миграции, выпуск токенов. Тебе нужны: SSH-ключ, доступ к Ubuntu 22.04 VPS (например, Hetzner CPX42 — 4 vCPU / 8 GB RAM), и Claude Code локально.

**Hermes интеграция обязательна?**
Нет. Если не нужен Hermes — пропусти. Bearer-аутентификации хватит для Claude Code.

**Можно использовать с другими AI-фреймворками (LangChain, AutoGen)?**
Да, любой MCP-совместимый клиент может ходить в `memory_mcp` / `recall_mcp` / `swarm_mcp` / `task_mcp`. Для non-MCP клиентов есть HTTP JSON-RPC endpoint.

**Сколько стоит держать?**
VPS Hetzner CPX42 ~16€/мес. Telegram-бот бесплатный. Опциональные API (Groq Whisper, HikerAPI, Perplexity) — по своим тарифам, и нужны только если включишь соответствующий ingestion skill.

**Я могу форкать и менять под себя?**
Apache 2.0 license. Форкай, меняй, переделывай. Если найдёшь баг или сделаешь полезный feature — PR в upstream приветствуется.

---

## Что в репо

```
public-gbrain-agentos/
  AGENT.md                главный вход — кормишь Claude Code агенту
  README.md               этот файл
  LICENSE                 Apache 2.0
  .env.example            все env-переменные с комментариями
  .gitignore
  pyproject.toml          метаданные пакета + зависимости
  requirements.txt        зафиксированные версии

  docs/
    architecture.md       глубокое погружение в систему
    setup.md              ручная установка step-by-step (Путь A + Путь B)
    security.md           threat model, ротация токенов, exposure rules
    troubleshooting.md    FAQ по типичным ошибкам
    hermes-integration.md walkthrough Hermes Agent + HMAC + sidecar proxy
    task-mcp-integration.md tasks/agents heartbeat operator guide

  services/
    shared/               auth, db, audit, config, hmac_sign, asgi_auth
    memory_mcp/           write API (AuthCaptureMiddleware)
    recall_mcp/           read API (hybrid search)
    swarm_mcp/            event bus межагентных сообщений
    task_mcp/             координация задач (13 MCP tools)
    ingest_worker/        embeds новых chunks

  migrations/             SQL-схема (001-006)
  tests/                  pytest, smoke + unit

  vault-template/         12 папок + READMEs + шаблоны нот
                          (10-strategy, 20-daily, 30-decisions, ...)

  inbox-agent/            локальный бот + dual-write hook + cron'ы

  agent-template/         (только Путь B) генератор персональных workspace'ов
    install.sh            интерактивный: спрашивает agent id, role, owner,
                          MCP host, model — даёт готовый workspace
    templates/            *.template для CLAUDE.md, rules.md, mcp.json,
                          USER.md, decisions.md
    scripts/              memory-rotate.sh, trim-hot.sh, rotate-warm.sh,
                          compress-warm.sh, gbrain-recall-on-start.sh
    hooks/                stop-hook.sh, session-start-hook.sh,
                          precompact-hook.sh
    docs/                 ARCHITECTURE.md, MEMORY.md, HOOKS.md,
                          MULTI-AGENT.md, TOKEN-OPTIMIZATION.md

  skills/                 опциональные ingestion skills
  caddy/                  Caddyfile template (TLS)
  systemd/                unit-файлы
  scripts/                install, smoke-test, sanitize-check,
                          issue-agent-token, issue-hmac-secret,
                          hermes_signed_proxy, gbrain_doctor
```

---

## Требования

**На VPS (оба пути):**
- Ubuntu 22.04 LTS
- 4 vCPU, 8 GB RAM (recall на 50k+ файлах требует RAM)
- 20 GB диск (vault + Postgres + эмбеддинги)
- SSH-доступ (key-based)
- Опционально: домен с A-record на VPS для TLS через Caddy. Без домена — через Tailscale или SSH-туннели.

**Локально (оба пути):**
- macOS, Linux или WSL на Windows
- Claude Code CLI (`claude`), Anthropic Max или аналогичный план
- Python 3.11+
- `crontab` (по умолчанию на Mac/Linux)
- Tailscale (опционально, если без публичного домена)

**Дополнительно для Путь B:**
- Папка `~/.claude-lab/<agent-id>/` на каждого агента (install-скрипт создаёт)
- Возможность запустить Claude Code с project-флагом (`claude --project ~/.claude-lab/<agent-id>/.claude`)
- Per-workspace crontab entries (install добавляет 3 строки на агента)

**Аккаунты:**
- Telegram-аккаунт (для общения с inbox-ботом)
- Telegram-бот от `@BotFather` (бесплатно, 60 секунд). Один бот на агента — токены никогда не шарятся.

**Опциональные API-ключи** (только если включишь соответствующий ingestion skill):
- Groq — Whisper-транскрипция голосовых
- HikerAPI — Instagram captions
- TranscriptAPI — YouTube транскрипты
- SocialData — X/Twitter
- Perplexity — web research

---

## Что это НЕ

- Не multi-tenant SaaS. Один vault на VPS, один пользователь (или маленькая команда) на vault.
- Не chat UI. Нет web-фронтенда. Vault — markdown, recall — MCP API, бот — для inbox-capture.
- Не замена сессионного контекста. Это **долговременная** память; короткая всё ещё в промпте.
- Не vector database product. Postgres + pgvector + FTS — намеренный выбор: markdown остаётся каноничен, индекс пересчитываемый.
- Не battle-tested at scale. Дизайн под solo/small team — vault до ~100k нот, до ~10 персональных агентов на мозг.
- Не замена IDE. Workspace'ы Путь B — home-папки агентов, не репозитории проектов.

---

## License

Apache License 2.0. Полный текст в [LICENSE](LICENSE).

---

## Acknowledgements

- **FastMCP** — Python MCP framework, на нём построены 4 сервиса.
- **FastEmbed** — embedding library, multilingual-e5-large (1024 dims, на CPU).
- **pgvector** — Postgres-расширение для векторов и HNSW-индексов.
- **Caddy** — TLS reverse proxy.
- **Hermes Agent** ([NousResearch](https://github.com/NousResearch/hermes-agent)) — фреймворк, под который сделана HMAC-схема и sidecar proxy. Сам Hermes мы не патчим — `gbrain` адаптирован под его публичный контракт.
- `agent-template/` — порт из [`qwwiwi/public-architecture-claude-code`](https://github.com/qwwiwi/public-architecture-claude-code). Поверх добавлено MCP-подключение к gbrain.
- Структура vault'а (12 пронумерованных scope'ов) вдохновлена PARA, Zettelkasten и проектом Cognee.

Контрибуции, баг-репорты и форки приветствуются. Открывай issue или PR в upstream-репо.
