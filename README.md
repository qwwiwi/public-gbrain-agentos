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

## Что умеет gbrain

| Возможность | Что это значит на практике |
|---|---|
| **Persistent vault** | Plain markdown на диске VPS. 12 scope'ов (10-strategy, 20-daily, 30-decisions, 40-learnings, 50-external, 60-handoff, 70-runbooks, 80-error-patterns, 90-inbox, 95-artifacts, 99-archive). Не теряется при `/clear`, `/compact` или смерти сессии. |
| **Гибридный recall** | Семантический поиск (FastEmbed multilingual-e5-large, 1024-dim) + лексический FTS (Postgres tsvector), слиты через Reciprocal Rank Fusion, переранжированы по типу и свежести. Один MCP-вызов `recall(query, limit=N)` — получаешь top-N релевантных markdown'ов с метаданными. |
| **Scoped write API** | 5 типов нот: decisions, runbooks, error-patterns, daily logs, external. Каждая идёт в свою папку с frontmatter, sha256-дедупликацией и audit_log. Scope-based RBAC: inbox-agent не может писать decisions, coder не может писать в archive. |
| **Inter-agent шина** | swarm_mcp: notify, list_my_pending, ack, broadcast, escalate. Любой агент будит другого через `notify(to_agent, payload)` — swarm worker делает POST на webhook listener целевого агента (`AGENT_GATEWAYS` env), и payload приземляется в активную сессию ≤30s. Два готовых receiver-pattern: Claude Code (через jarvis-channel plugin) и Hermes Agent (через локальный aiohttp listener + launchd). Полный flow и шаблоны: [docs/INTER-AGENT-WEBHOOKS.md](docs/INTER-AGENT-WEBHOOKS.md). |
| **Tasks state machine** | task_mcp: `new → progress → review → done` (+ `blocked`). task_history (audit trail), agent heartbeat с metadata (host/role/model/version). 13 MCP-тулов покрывают весь жизненный цикл. |
| **Identity и audit** | Каждый агент — Bearer-токен в `agent_tokens` (sha256 stored, raw printed once). ASGI middleware `AuthCaptureMiddleware` кладёт identity в ContextVar — no silent fallback. Каждая запись logged в `audit_log` с `agent`, `action`, `timestamp`, `payload_sha256`. |
| **HMAC dual-auth** | Параллельный путь аутентификации для Hermes Agent: `X-Hermes-Signature` + `X-Hermes-Timestamp` поверх HMAC-SHA256(`<timestamp>.<body>`). Constant-time compare, 5-минутный tolerance. Same scopes, same RBAC. |
| **Telegram inbox** | Локальный бот пересылает форварды + voice + ссылки в vault с dual-write (raw/ локально для приватного, brain удалённо для retrievable). Daily digest cron в 09:00. Voice → Groq Whisper транскрипт → markdown. |
| **Memory hooks (Путь B)** | Stop hook записывает каждую сессию в `hot/recent.md`. SessionStart hook поднимает топ-N последних entries в context window. PreCompact hook сохраняет summary до автокомпакта Claude Code. Cron-ротация `hot → warm → cold` (14 дней TTL). |
| **Agent generator (Путь B)** | `agent-template/install.sh` — один Bash-скрипт спрашивает 8 параметров (agent id, role, owner, MCP host, model, ...) и собирает полный Claude Code workspace со слойной памятью и `.mcp.json` уже подключённым к мозгу. Запускай столько раз, сколько агентов нужно. |

---

## Какие проблемы это решает

1. **«Агент забывает всё после /clear или /compact».** Сессионная память Claude Code эфемерна. gbrain пишет на диск, recall достаёт обратно. Можно `/clear` без потерь.
2. **«У меня 3 агента и они дублируют работу».** Без общей памяти координатор не знает что кодер уже решил, ревьюер не видит decision rationale. Через recall_mcp любой агент видит ноты всех остальных.
3. **«Промпт раздулся до 150k токенов от истории».** Lazy retrieval вместо eager context: держи в промпте только активную задачу, добирай decisions/runbooks через recall по запросу.
4. **«Identity подменяется на default agent».** Системы которые ходят через один service-account API ключ не могут различать кто что записал. Здесь — per-agent Bearer + audit_log с привязкой к identity, нельзя замаскироваться.
5. **«Inter-agent коммуникация на bash-скриптах и Firebase RTDB».** Coordination через MCP с outbox state machine, idempotent retry, scope-based delivery. Не нужен Firebase / Redis / RabbitMQ — Postgres хватает.
6. **«Hermes/MiniMax/локальная модель не умеет Bearer».** HMAC-путь + sidecar proxy решают это без патчинга самого Hermes — gbrain адаптирован под публичный контракт фреймворка.
7. **«Backup vault'а — это бэкап БД с эмбеддингами».** Не нужно: markdown остаётся каноничен. Потерял Postgres → reindex из vault'а за 5 минут. Потерял vault → восстанавливаешь из git/tar.
8. **«Vendor lock-in на векторной БД».** pgvector + FTS — стандартный Postgres, не Pinecone/Weaviate/Qdrant. Миграция = `pg_dump`.

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

## Триггеры между агентами (inter-agent webhooks)

**Зачем это.** Если ты гоняешь 2+ агентов и хочешь чтобы они работали как команда, а не как набор изолированных терминалов — нужен механизм когда агент A автономно будит агента B с задачей. Иначе каждый trigger требует человека-посредника, и тебе придётся самому пересылать «координатор сказал, кодер сделай вот это» — это убивает смысл многоагентной системы.

**gbrain делает это через webhook-доставку поверх swarm_mcp.** Один MCP-вызов — и доставка происходит автоматически за ≤30s без cron, без polling, без участия человека. Принцип agent-native: API между агентами, не GUI.

### Архитектура (полный flow)

```
Агент A (любой runtime)
    │
    │ mcp__gbrain-swarm__notify(to_agent="B", payload={...})
    ▼
swarm_mcp server (VPS)
    │
    │ enqueue в outbox (state machine: pending → delivered → acked|failed)
    ▼
swarm_mcp worker (VPS)
    │
    │ читает AGENT_GATEWAYS["B"] → POST {url}/webhook
    │ + Authorization: Bearer ИЛИ X-Hermes-Signature + X-Hermes-Timestamp
    │ + JSON body с payload
    ▼
Webhook listener агента B (его runtime)
    │
    │ verify auth → парсит payload → инжектит в активную сессию
    ▼
Сессия агента B получает trigger
    │
    │ читает task из gbrain → выполняет → mcp__gbrain-swarm__ack(task_id)
    ▼
outbox запись помечена acked, цикл закрыт
```

### Два готовых receiver-pattern

**1. Claude Code через jarvis-channel plugin** — для агентов которые живут в `claude` CLI сессии.

Plugin [`qwwiwi/dashi-plugin-claude-code`](https://github.com/qwwiwi/dashi-plugin-claude-code) поднимает HTTP listener (typically `:8089`), принимает webhook от swarm worker, инжектит payload как сообщение прямо в активную Claude Code сессию. Та обрабатывает как обычный пользовательский ввод, видит tools, отвечает. Deploy: один `npm install` + systemd unit (Linux) или launchd plist (macOS). Reverse SSH tunnel если listener за NAT.

**2. Hermes Agent через локальный aiohttp listener** — для агентов на [Hermes Agent](https://github.com/NousResearch/hermes-agent) фреймворке.

Hermes — это Telegram polling-bot, у него нет native webhook endpoint. Pattern: отдельный Python aiohttp/FastAPI listener стоит рядом с Hermes daemon (typically `127.0.0.1:8091`), принимает POST `/webhook`, верифицирует Bearer или HMAC, пишет payload в `~/.hermes/inbox/{timestamp}.json`. Hermes daemon забирает inbox через свой message handler. launchd plist обеспечивает KeepAlive после reboot. Шаблон listener: [`agent-template/scripts/webhook_listener.py`](agent-template/scripts/webhook_listener.py).

**Кастомный runtime?** Любой HTTP-listener поверх любого фреймворка подойдёт. Требования: POST `/webhook`, verify Bearer/HMAC, inject в твою runtime сессию. См. [docs/INTER-AGENT-WEBHOOKS.md](docs/INTER-AGENT-WEBHOOKS.md) — там reference implementation на ~80 строк aiohttp.

### Setup нового агента — 3 шага

1. **Deploy listener** для своего runtime (jarvis-channel ИЛИ Hermes ИЛИ кастом). Listener держит порт на localhost, верифицирует Bearer/HMAC, инжектит payload в сессию.
2. **Зарегистрируй URL** в swarm worker на VPS — drop-in env `AGENT_GATEWAYS` в `/etc/systemd/system/gbrain-swarm-worker.service.d/webhook.conf`:
   ```
   AGENT_GATEWAYS={"alice":"http://127.0.0.1:8089/webhook","bob":"http://127.0.0.1:8091/webhook"}
   ```
   Опционально `AGENT_GATEWAY_AUTH` для per-agent secrets (см. §7 в [docs/hermes-integration.md](docs/hermes-integration.md)). Затем `sudo systemctl restart gbrain-swarm-worker`.
3. **Smoke test**:
   ```python
   mcp__gbrain-swarm__notify(to_agent="alice", payload={"type":"ping","from":"tester"})
   mcp__gbrain-swarm__get_delivery(task_id="<returned>")  # ожидаем status=acked
   ```

### Безопасность

- **Auth обязателен.** Bearer (sha256 в `agent_tokens`) ИЛИ HMAC (per-request signature). Plaintext webhook без auth = открытый RCE.
- **Listener bind на 127.0.0.1**, не `0.0.0.0`. Если worker на VPS, а listener на ноутбуке — используй reverse SSH tunnel (`autossh -R 8091:127.0.0.1:8091`) или Tailscale. Не выставляй listener в публичный интернет.
- **Bot isolation hard rule.** Если у твоих агентов есть Telegram-боты — каждый агент использует ТОЛЬКО свой бот. Worker НЕ должен отправлять через чужие bot tokens как обходной путь.
- **Output filter.** Любое логирование payload — через redact pattern (Bearer, токены, секреты). См. ошибки от которых мы защищались: [docs/INTER-AGENT-WEBHOOKS.md#security](docs/INTER-AGENT-WEBHOOKS.md#security).

### Debugging

```python
# Куда делась доставка?
mcp__gbrain-swarm__get_delivery(task_id="<id>")
# → status: pending|delivered|acked|failed, attempts, last_error, next_retry_at

# Worker логи на VPS:
journalctl -u gbrain-swarm-worker --since "10 min ago" | grep <agent-name>

# Retries: 5 attempts с exponential backoff, потом status=failed (нужен ручной replay).
```

Полный мануал с code snippets, runtime-specific recipes, troubleshooting матрицей: [docs/INTER-AGENT-WEBHOOKS.md](docs/INTER-AGENT-WEBHOOKS.md).

---

## Интеграция с агентами

gbrain — это **MCP-сервер**, не закрытая платформа. Любой фреймворк, говорящий по протоколу Model Context Protocol (или умеющий слать HTTP+JSON-RPC), подключается напрямую. Ниже — четыре пути для самых распространённых стеков.

| Фреймворк | Транспорт | Auth | Setup time |
|---|---|---|---|
| Claude Code | MCP streamable-http (нативно) | Bearer | ~2 мин |
| Openclaw | MCP streamable-http (нативно) | Bearer | ~5 мин |
| Codex (OpenAI CLI) | MCP streamable-http (нативно) | Bearer | ~3 мин |
| Hermes Agent | HTTP JSON-RPC через sidecar | HMAC `<timestamp>.<body>` | ~10 мин |

---

### Claude Code

Нативный путь. Claude Code поддерживает MCP через `.mcp.json` в корне workspace'а — выпустил Bearer, добавил 4 сервера, и тулзы появляются в context window сами.

1. Выпусти токен на сервере: `python scripts/issue-agent-token.py --agent <agent-id> --scopes read,write` — печатает raw Bearer **один раз**.
2. Добавь в `~/.claude-lab/<agent-id>/.claude/.mcp.json`:

   ```json
   {
     "mcpServers": {
       "gbrain-memory": {
         "type": "http",
         "url": "https://gbrain.example.com/memory/mcp",
         "headers": {"Authorization": "Bearer <RAW_TOKEN>"}
       },
       "gbrain-recall": {
         "type": "http",
         "url": "https://gbrain.example.com/recall/mcp",
         "headers": {"Authorization": "Bearer <RAW_TOKEN>"}
       },
       "gbrain-swarm": {
         "type": "http",
         "url": "https://gbrain.example.com/swarm/mcp",
         "headers": {"Authorization": "Bearer <RAW_TOKEN>"}
       },
       "gbrain-tasks": {
         "type": "http",
         "url": "https://gbrain.example.com/task/mcp",
         "headers": {"Authorization": "Bearer <RAW_TOKEN>"}
       }
     }
   }
   ```

3. Запусти `claude` в этом workspace'е. Проверь подключение: спроси «вызови `mcp__gbrain-recall__recall` с query=test, limit=1».
4. (Путь B) Если ставишь через `agent-template/install.sh` — шаги 1–3 автоматизированы.

Что доступно сразу: `mcp__gbrain-memory__create_*_note`, `mcp__gbrain-recall__{recall,get,recent,related,stats}`, `mcp__gbrain-swarm__{notify,list_my_pending,ack,...}`, `mcp__gbrain-tasks__{task_*,agent_*}`.

---

### Openclaw

[Openclaw](https://github.com/openclaw/openclaw) — оркестратор Claude-агентов с Telegram-интерфейсом. Поддерживает MCP в той же `.mcp.json` форме что и Claude Code: оркестратор подкладывает её в headless-сессию каждого spawn'нутого агента.

1. Выпусти токены — по одному на каждого Openclaw-агента (`coordinator`, `coder`, `reviewer`, ...). Identity-разделение важно: audit_log привяжет каждую запись к правильному агенту.
2. В Openclaw workspace, в `agents/<agent-name>/.mcp.json` пропиши те же 4 сервера, что для Claude Code (`Authorization: Bearer <RAW_TOKEN_FOR_THIS_AGENT>`).
3. Опционально: добавь recall как pre-task hook — оркестратор перед запуском агента вызывает `mcp__gbrain-recall__recall(query=<task title>, limit=5)` и кладёт результаты в system prompt. Снижает «забывание» между orchestration steps.
4. Webhook-маршрутизация: в `swarm_mcp` ответ может вернуться в Openclaw gateway (через `webhook_url` в payload) — оркестратор сам разбудит нужного агента. Пример: `services/swarm_mcp/worker.py` уже шлёт `POST <gateway>/webhook` при `notify`.

Tip: Openclaw + gbrain делятся одним vault'ом между всеми агентами оркестратора — coordinator видит decisions кодера через recall без явного passing context.

---

### Codex (OpenAI CLI)

[Codex CLI](https://github.com/openai/codex) — командный AI-кодер от OpenAI на GPT-5.5. Поддерживает MCP через `.mcp.json` (с версии v0.110+).

1. Выпусти Codex-агенту собственный Bearer (`--agent codex-reviewer` если используешь как ревьюера).
2. В рабочей папке создай `.mcp.json` идентичной структуры (см. секцию «Claude Code» выше) — Codex CLI парсит тот же формат.
3. Запусти: `codex --mcp-config .mcp.json`. Codex увидит 4 gbrain-сервера в своём tool catalog.
4. Типичный use-case: **Codex как Master Reviewer** — он берёт `task_get(task_id)`, читает `recall(query=related)`, делает review, пишет findings через `create_error_pattern_note`, затем `task_review(note=...)`. Полный цикл без человека-посредника.

Edge case: Codex CLI отдаёт MCP-вызовы без streaming (`streamable-http` upgrade headers могут отсутствовать). gbrain принимает оба режима — fallback на чистый JSON-RPC.

---

### Hermes Agent

[Hermes Agent](https://github.com/NousResearch/hermes-agent) (NousResearch) — фреймворк, который подписывает все запросы HMAC-схемой `<timestamp>.<body>`. Bearer-токены он не понимает. gbrain принимает **обе** схемы аутентификации одновременно через общий middleware. Сам Hermes не патчим.

#### Как работает

- В таблице `agent_tokens` у агента есть оба поля: `token_sha256` (для Bearer) и `hmac_secret_sha256` (для HMAC). Любое можно `NULL` — один из двух обязателен.
- ASGI middleware `services/shared/asgi_auth.py` (`HermesAwareAuthMiddleware`) читает один из двух заголовков:
  - `Authorization: Bearer <token>` — стандартный путь
  - `X-Hermes-Signature: sha256=<hex>` + `X-Hermes-Timestamp: <unix>` — HMAC-путь
- HMAC проверяется constant-time (`hmac.compare_digest`), timestamp tolerance — 5 минут (настраивается через `HMAC_TIMESTAMP_TOLERANCE_SECONDS`).
- Identity-проверка одна и та же: scope-based RBAC через `agent_tokens.can_write_scopes` / `can_read_scopes`.

#### Sidecar proxy для клиентов, которые не умеют HMAC

Hermes выпускает MCP tool-calls без HMAC-подписи на каждый вызов. Чтобы не патчить Hermes, в репо есть `scripts/hermes_signed_proxy.py` (Starlette + httpx + uvicorn):

```
Hermes → http://localhost:9100/{memory,recall,swarm,task}/mcp
              ↓ proxy подписывает каждый запрос
              ↓ X-Hermes-Signature: sha256=...
              ↓ X-Hermes-Timestamp: <now>
              ↓
         https://gbrain.example.com/{memory,recall,swarm,task}/mcp
```

Запуск: `python scripts/hermes_signed_proxy.py --listen 0.0.0.0:9100 --upstream https://gbrain.example.com --secret-env GBRAIN_HMAC_SECRET --agent <agent-id>`.

#### Выпуск HMAC-секрета

```bash
python scripts/issue-hmac-secret.py --agent <agent-id>
# Печатает raw HMAC secret ОДИН РАЗ в stdout, sha256 сохраняется в БД.
# Скопируй secret в безопасное хранилище — больше его получить нельзя.
```

Полный walkthrough + примеры подписания: `docs/hermes-integration.md`.

#### Inbound webhook (приём триггеров от других агентов)

Hermes — это Telegram polling-bot, у него **нет native webhook endpoint**. Чтобы другие агенты могли триггерить Hermes через `mcp__gbrain-swarm__notify(to_agent="<hermes-agent-id>", ...)`, рядом с Hermes daemon ставится отдельный Python aiohttp listener:

```
swarm worker (VPS)                         Hermes host (Mac mini / Linux)
    │                                              │
    │  POST http://<host>:8091/webhook             │
    │  Authorization: Bearer <token>               │
    │  body: {"type":"task_assigned",...}          │
    ├─────────────reverse SSH tunnel──────────────►│
    │  -R 8091:127.0.0.1:8091                      │
    │                                              ▼
    │                                       webhook_listener.py
    │                                       (aiohttp, :8091)
    │                                              │
    │                                              │ verify Bearer / HMAC
    │                                              │ write ~/.hermes/inbox/{ts}.json
    │                                              ▼
    │                                       Hermes daemon
    │                                       читает inbox/ → trigger
```

Шаблон listener'а: [`agent-template/scripts/webhook_listener.py`](agent-template/scripts/webhook_listener.py) — минимальный aiohttp на ~80 строк с Bearer/HMAC verify и inbox dispatch. Запускается через launchd (macOS) или systemd (Linux), `KeepAlive=true` для рестарта после reboot.

После запуска listener'а — зарегистрируй URL в swarm worker `AGENT_GATEWAYS` (см. раздел «[Триггеры между агентами](#триггеры-между-агентами-inter-agent-webhooks)» выше) и сделай smoke `notify` для проверки.

Полный setup с reverse SSH tunnel, launchd plist примером, debugging-матрицей: [docs/INTER-AGENT-WEBHOOKS.md](docs/INTER-AGENT-WEBHOOKS.md) разделы «Receiver: Hermes Agent» и «Setup: macOS launchd».

---

### Любой другой MCP-клиент

LangChain, AutoGen, CrewAI, llama-index, голая `httpx`-обёртка — всё подключается. Минимальное требование: HTTP POST с `Content-Type: application/json` и JSON-RPC 2.0 envelope. Список тулов: `tools/list` метод. Шаг за шагом: `docs/architecture.md` секция «Custom MCP clients».

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
