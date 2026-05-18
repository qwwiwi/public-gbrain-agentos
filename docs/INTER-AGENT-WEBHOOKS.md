# Inter-agent webhooks — full reference

Когда агент A автономно будит агента B с задачей, между ними должен быть машинный канал. У gbrain это **webhook поверх `swarm_mcp`**. Этот документ — полная reference на обе стороны: как отправлять (`swarm.notify` + worker) и как принимать (listener на стороне агента-получателя).

Стартовая точка для high-level понимания и зачем оно нужно — раздел [«Триггеры между агентами»](../README.md#триггеры-между-агентами-inter-agent-webhooks) в корневом README. Здесь — глубже: code, env, debugging.

---

## 1. Полный flow

```
Агент A (любой runtime: Claude Code, Hermes, кастом)
    │
    │ MCP-вызов: notify(to_agent="B", payload={"type":"task_assigned",...})
    ▼
swarm_mcp (FastAPI/FastMCP на VPS, :8766)
    │
    │ INSERT в delivery_outbox
    │ status=pending, attempts=0
    │ task_id="A::B::<hex>"
    ▼
swarm_mcp worker (отдельный systemd unit, тот же VPS)
    │
    │ poll'ит outbox каждые ~2s
    │ берёт next pending → читает AGENT_GATEWAYS["B"] env
    ▼
HTTP POST {AGENT_GATEWAYS["B"]}/webhook
    │
    │ Headers:
    │   Content-Type: application/json
    │   Authorization: Bearer <token>           (если AGENT_GATEWAY_AUTH=bearer:env:...)
    │   X-Hermes-Signature: sha256=<hex>        (если AGENT_GATEWAY_AUTH=hmac:env:...)
    │   X-Hermes-Timestamp: <unix>
    │ Body: JSON с payload + task_id + from_agent
    ▼
Webhook listener на стороне агента B
    │
    │ verify auth (Bearer hash compare ИЛИ HMAC compare_digest)
    │ парсит body
    │ инжектит payload в runtime агента B
    ▼
Сессия агента B получает trigger как новое сообщение
    │
    │ обработка → ack: notify back ИЛИ task_review() ИЛИ swarm.ack(task_id)
    ▼
worker помечает outbox: status=acked, цикл закрыт
```

**Retry policy.** Worker делает до `max_attempts=5` попыток с exponential backoff (~10s, 30s, 60s, 120s, 300s между попытками). После 5 failures — `status=failed`. Manual replay через ручное `notify` с тем же payload (idempotent по task_id если передан).

---

## 2. Sender side — как агент A отправляет

### MCP-вызов

```python
result = mcp__gbrain-swarm__notify(
    to_agent="kaelthas",  # canonical agent id, без префиксов sa-/agent-
    payload={
        "type": "task_assigned",   # или ping, content_request, escalation, etc
        "task_id": 42,
        "title": "Напиши пост про X",
        "from_agent": "silvana",
        "priority": "high",
    },
    max_attempts=5,  # default
    task_id=None,    # auto-generated если не передан
)
# returns: {"task_id": "silvana::kaelthas::abc123", "status": "pending"}
```

### Что worker делает дальше

Сразу после `notify` — запись в `delivery_outbox` со `status=pending`. Через ~2-10s worker подбирает её, читает `AGENT_GATEWAYS` env, делает POST. Если POST вернул 2xx — `status=acked`. Если 4xx/5xx или timeout (default 10s) — `attempts++`, ставит `next_retry_at`, попадёт в следующий цикл.

### Debugging доставки

```python
delivery = mcp__gbrain-swarm__get_delivery(task_id="silvana::kaelthas::abc123")
# returns:
# {
#   "task_id": "...",
#   "from_agent": "silvana",
#   "to_agent": "kaelthas",
#   "status": "pending" | "acked" | "failed",
#   "attempts": 3,
#   "max_attempts": 5,
#   "payload": {...},
#   "created_at": "...",
#   "updated_at": "...",
#   "next_retry_at": "...",
#   "last_error": "ConnectionRefusedError(...)" | null,
# }
```

`status=failed` + `last_error` = либо listener мёртв, либо AGENT_GATEWAYS не указывает на правильный URL, либо auth не сходится. См. секцию «Debugging» ниже.

---

## 3. Worker setup — AGENT_GATEWAYS env

Worker — это systemd unit `gbrain-swarm-worker.service` на VPS (или эквивалент). Конфиг через drop-in:

```bash
sudo install -d /etc/systemd/system/gbrain-swarm-worker.service.d
sudoedit /etc/systemd/system/gbrain-swarm-worker.service.d/webhook.conf
```

Содержимое:

```ini
[Service]
Environment="AGENT_GATEWAYS={\"alice\":\"http://127.0.0.1:8089/webhook\",\"bob\":\"http://127.0.0.1:8091/webhook\",\"carol\":\"http://10.0.0.5:8089/webhook\"}"
Environment="AGENT_GATEWAY_AUTH={\"alice\":\"bearer:env:ALICE_WEBHOOK_TOKEN\",\"bob\":\"hmac:env:BOB_WEBHOOK_HMAC\",\"carol\":\"none\"}"
Environment="ALICE_WEBHOOK_TOKEN=<raw_token>"
Environment="BOB_WEBHOOK_HMAC=<raw_hmac_secret>"
Environment="GBRAIN_HMAC_OUTBOUND_ENABLED=1"
```

Затем:

```bash
sudo systemctl daemon-reload
sudo systemctl restart gbrain-swarm-worker
journalctl -u gbrain-swarm-worker -f  # tail для verify
```

### AGENT_GATEWAY_AUTH формат

| Mode | Пример | Поведение |
|---|---|---|
| `bearer:env:VAR_NAME` | `bearer:env:ALICE_WEBHOOK_TOKEN` | Worker берёт raw token из env, шлёт `Authorization: Bearer <raw>` |
| `hmac:env:VAR_NAME` | `hmac:env:BOB_WEBHOOK_HMAC` | Worker берёт raw secret из env, подписывает `<timestamp>.<body>` через HMAC-SHA256, шлёт `X-Hermes-Signature` + `X-Hermes-Timestamp` |
| `none` | `none` | Без auth headers. **Только** для localhost-only listeners с firewall'ом, где исключён сторонний доступ. |

Глобальный kill-switch для HMAC: `GBRAIN_HMAC_OUTBOUND_ENABLED=0` отключает HMAC-подписание всех outbound webhook (Bearer продолжает работать). Используй для аварийного rollback.

---

## 4. Receiver: Claude Code через jarvis-channel plugin

[`qwwiwi/dashi-plugin-claude-code`](https://github.com/qwwiwi/dashi-plugin-claude-code) — готовый plugin, который превращает Claude Code сессию в webhook receiver. Содержит:

- HTTP listener на configurable порту (default `:8089`)
- Bearer auth через `WEBHOOK_TOKEN` env
- Inject mechanism: пишет payload через [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) plugin protocol → user-facing message в активной сессии
- Telegram bot для outbound: каждый агент имеет свой бот через `BotFather`, ответы идут в Telegram чат принцу
- systemd unit (Linux) + launchd plist (macOS) шаблоны

**Setup (TL;DR):**

```bash
git clone https://github.com/qwwiwi/dashi-plugin-claude-code.git plugin
cd plugin && npm install
# Backup current workspace .mcp.json + settings.json
# Edit channel.env: bot token, webhook port, workspace path
sudo cp examples/channel-thrall.service /etc/systemd/system/  # пример, переименуй под agent-id
sudo systemctl daemon-reload && sudo systemctl enable --now channel-<agent>
```

Полный мануал: [docs/02-where-to-place-plugin.md](https://github.com/qwwiwi/dashi-plugin-claude-code/blob/main/docs/02-where-to-place-plugin.md) и [docs/03-installation.md](https://github.com/qwwiwi/dashi-plugin-claude-code/blob/main/docs/03-installation.md) в plugin репо.

---

## 5. Receiver: Hermes Agent через локальный aiohttp listener

[Hermes Agent](https://github.com/NousResearch/hermes-agent) (NousResearch) запускается как `python -m hermes_cli.main gateway run` и long-poll'ит Telegram API. У него **нет native HTTP endpoint** на inbound webhook. Pattern — отдельный sidecar listener рядом с Hermes daemon.

### Reference implementation

См. [`agent-template/scripts/webhook_listener.py`](../agent-template/scripts/webhook_listener.py) — минимальный aiohttp listener (~80 строк) который:

1. Слушает `POST /webhook` на `127.0.0.1:8091` (configurable через `WEBHOOK_PORT` env)
2. Верифицирует Bearer (из `WEBHOOK_BEARER` env) ИЛИ HMAC (из `WEBHOOK_HMAC_SECRET` env) — в зависимости от того что задано
3. Парсит JSON body, добавляет `received_at` timestamp
4. Пишет в `~/.hermes/inbox/{timestamp}_{from_agent}.json`
5. Возвращает `200 OK` (idempotent — повторный POST с тем же task_id безопасен)

Hermes daemon забирает inbox через свой native message handler (или через cron-pickup-script, в зависимости от твоей версии). Если у тебя кастомная версия Hermes — настрой свой pickup в зависимости от того как у тебя интегрируется внешний message source.

### Setup на macOS (launchd)

```bash
# 1. Сохрани token (chmod 600):
mkdir -p ~/.secrets
echo "<raw_webhook_token>" > ~/.secrets/illidan-webhook.token
chmod 600 ~/.secrets/illidan-webhook.token

# 2. launchd plist:
cat > ~/Library/LaunchAgents/ai.gbrain.hermes-webhook.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>ai.gbrain.hermes-webhook</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/Users/YOU/hermes-agents/Illidan/scripts/webhook_listener.py</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>WEBHOOK_PORT</key><string>8091</string>
        <key>WEBHOOK_BEARER_FILE</key><string>/Users/YOU/.secrets/illidan-webhook.token</string>
        <key>HERMES_INBOX_DIR</key><string>/Users/YOU/.hermes/inbox</string>
    </dict>
    <key>KeepAlive</key><true/>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>/Users/YOU/.hermes/logs/webhook-listener.log</string>
    <key>StandardErrorPath</key><string>/Users/YOU/.hermes/logs/webhook-listener.err</string>
</dict>
</plist>
PLIST

launchctl load ~/Library/LaunchAgents/ai.gbrain.hermes-webhook.plist
# Verify:
curl -X POST http://127.0.0.1:8091/webhook \
    -H "Authorization: Bearer $(cat ~/.secrets/illidan-webhook.token)" \
    -H "Content-Type: application/json" \
    -d '{"type":"ping","from_agent":"smoke"}'
# Expect: 200 OK, ls ~/.hermes/inbox/ показывает новый JSON
```

### Setup на Linux (systemd)

```ini
# /etc/systemd/system/hermes-webhook.service
[Unit]
Description=Hermes inbound webhook listener
After=network-online.target
Requires=network-online.target

[Service]
Type=simple
User=hermes
WorkingDirectory=/home/hermes/hermes-agents/Illidan
ExecStart=/usr/bin/python3 /home/hermes/hermes-agents/Illidan/scripts/webhook_listener.py
EnvironmentFile=/etc/hermes/webhook.env
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

`/etc/hermes/webhook.env` (chmod 600 root:hermes):

```
WEBHOOK_PORT=8091
WEBHOOK_BEARER_FILE=/etc/hermes/webhook.token
HERMES_INBOX_DIR=/home/hermes/.hermes/inbox
```

### Reverse SSH tunnel (если worker НЕ может достучаться напрямую)

Если worker живёт на VPS, а listener на ноутбуке/Mac mini за NAT — туннель в обратную сторону:

```bash
# autossh keeps the tunnel up:
autossh -M 0 -f -N -o "ServerAliveInterval 60" -o "ServerAliveCountMax 3" \
    -R 127.0.0.1:8091:127.0.0.1:8091 \
    user@gbrain.example.com
```

После туннеля worker может ходить на `http://127.0.0.1:8091/webhook` на VPS, который форвардится через SSH на твой Mac mini. В `AGENT_GATEWAYS` укажи `"alice":"http://127.0.0.1:8091/webhook"` — это URL с точки зрения worker'а на VPS.

**Альтернатива: Tailscale.** Если у тебя Tailscale, listener bind на Tailscale IP (`100.x.x.x:8091`), worker ходит через Tailscale ACL. Не нужен SSH tunnel.

---

## 6. Receiver: кастомный runtime

Любой HTTP-listener подойдёт. Требования:

- `POST /webhook` принимает JSON body
- Verify Bearer (sha256 в `agent_tokens` ИЛИ HMAC если используешь HMAC-mode)
- Inject payload в свою runtime сессию (специфично для твоего фреймворка)
- Вернуть `200 OK` после успешной inject (или `4xx`/`5xx` для retry от worker'а)

Reference: [`agent-template/scripts/webhook_listener.py`](../agent-template/scripts/webhook_listener.py) — copy и адаптируй inject шаг под свой runtime.

---

## 7. Security

### Auth — обязателен

Никаких `none` mode listeners в публичный интернет. Допустимо только для:

- Bind на `127.0.0.1` + worker на той же машине (worker делает loopback POST)
- Bind на Tailscale IP + worker в той же Tailscale tailnet (ACL изолирует доступ)

Для всех остальных случаев — Bearer или HMAC mandatory.

### Bot isolation hard rule

Если у твоих агентов есть Telegram-боты — каждый агент использует **ТОЛЬКО** свой бот. Worker НЕ должен иметь возможность отправлять Telegram сообщения через чужие bot tokens как обходной путь. Это правило защищает от:

- Spoofing (агент A пишет от имени агента B)
- Token leakage (компрометация одного агента не даёт доступа к другим)
- Audit confusion (нельзя восстановить кто реально отправил)

В webhook flow alternative — escape через Telegram Bot API на чужой бот **запрещён**. Если webhook не доходит — фикси webhook, не обходи.

### Payload содержит секреты?

Нет. Payload — это координационный trigger («у тебя задача N»), не данные. Если нужны secrets (API ключи, токены) — они лежат в файлах с `chmod 600` на стороне receiver'а, payload только указывает path или env var name.

Если ты НЕНАМЕРЕННО положил secret в payload (например, API ключ внешнего сервиса для немедленного использования) — он попадёт в `delivery_outbox.payload` JSONB column в БД и в worker logs. Это не secure path. Перенеси secret в side-channel (env, файл, vault).

### Output filter

Логирование payload — через redact pattern:

```python
import re
TOXIC = re.compile(r'(Bearer\s+\S+|sk-[A-Za-z0-9_-]+|hmac_[A-Za-z0-9]+|password=\S+)', re.IGNORECASE)
def safe_log(payload_str: str) -> str:
    return TOXIC.sub('<REDACTED>', payload_str)
```

Применяй в listener'е перед `logger.info(...)` и в worker'е перед `journalctl`. Без этого Bearer токены попадут в logs → потенциальный leak через log shipping.

### Replay protection

HMAC pattern бьёт по `<timestamp>.<body>`. Server проверяет `abs(now - ts) < HMAC_TIMESTAMP_TOLERANCE_SECONDS` (default 300s). Внутри окна — replay возможен (нет server-side nonce cache by design). Для write-heavy endpoints — ужесточи tolerance до 60s.

Bearer pattern не имеет replay protection. Если боишься replay — поверх Bearer прикрути TLS (Caddy/nginx с HTTPS) и rate-limiting.

---

## 8. Debugging

### Доставка зависла в pending

```python
# Проверь delivery status
delivery = mcp__gbrain-swarm__get_delivery(task_id="...")
print(delivery)  # status, attempts, last_error
```

Если `status=pending, attempts=0` дольше 30s → worker не подбирает (либо worker умер, либо outbox lock). Проверь worker:

```bash
ssh root@<vps> 'systemctl status gbrain-swarm-worker --no-pager | head -10'
ssh root@<vps> 'journalctl -u gbrain-swarm-worker --since "5 min ago" --no-pager | tail -30'
```

### Доставка failed с `last_error`

| `last_error` начинается с... | Причина | Фикс |
|---|---|---|
| `ConnectionRefusedError` / `Cannot connect` | Listener мёртв | `ssh <listener-host> 'curl http://127.0.0.1:<port>/webhook'` — проверь что listener бинд. Если за SSH tunnel — `ps aux \| grep autossh` |
| `404 Not Found` | Path ≠ `/webhook` | Проверь `AGENT_GATEWAYS` URL — listener может слушать `/inbox` или другой path |
| `401 Unauthorized` | Auth mismatch | Сверь Bearer/HMAC secret между worker env (`AGENT_GATEWAY_AUTH` value) и listener env. После rotate'а одного — rotate второго |
| `Timeout (10s)` | Listener inject шаг слишком долгий | Listener должен возвращать 200 БЫСТРО (≤2s), inject делать async в background. Иначе worker считает доставку failed |
| `502 Bad Gateway` | reverse SSH tunnel оборван | autossh должен пересоздать, но проверь `ServerAliveInterval`. Если NAT режет — поставь Tailscale |
| `dns lookup failed` | AGENT_GATEWAYS URL содержит hostname который VPS не резолвит | Используй IP или Tailscale hostname |

### Listener получает POST но inject не работает

Логи listener'а — первый шаг:

```bash
# macOS launchd:
tail -100 ~/.hermes/logs/webhook-listener.log
tail -100 ~/.hermes/logs/webhook-listener.err

# Linux systemd:
journalctl -u hermes-webhook -f
```

Если в logs: «verify failed» — auth mismatch (см. таблицу выше). Если «inject failed: <runtime error>» — баг в твоей inject-логике, специфично для runtime.

### Сессия агента не реагирует на инжект

Зависит от runtime:

- **Claude Code через jarvis-channel:** проверь что Claude session жива (tmux pane активен, нет «Waiting for prompt»). Если зависла — рестарт unit (`systemctl restart channel-<agent>`)
- **Hermes:** проверь что `~/.hermes/inbox/` пополняется новыми JSON. Если да — Hermes daemon не подхватывает, проверь его inbox handler (cron-pickup, native polling, etc)
- **Custom runtime:** specific to your inject mechanism

---

## 9. Идемпотентность и replay

`swarm.notify` с тем же `task_id` (или без него — auto-generated unique) — **не идемпотентен** на уровне outbox: каждый вызов создаёт новую запись. Однако если ты явно передаёшь `task_id`, повторный вызов с тем же значением вернёт ту же запись (на уровне Postgres UNIQUE constraint).

Для ручного replay failed delivery:

```python
# Опция 1: новый notify с тем же payload (новый task_id)
new = mcp__gbrain-swarm__notify(to_agent=delivery.to_agent, payload=delivery.payload)

# Опция 2: вручную через psql (для админа)
# UPDATE delivery_outbox SET status='pending', attempts=0, next_retry_at=NOW()
# WHERE task_id='<id>' AND status='failed';
```

Listener должен быть idempotent — если worker делает retry после failed timeout, тот же payload может прийти повторно. Inject должен detect duplicates по `task_id` если это критично для runtime.

---

## 10. Cross-references

- Корневой README раздел: [«Триггеры между агентами»](../README.md#триггеры-между-агентами-inter-agent-webhooks)
- jarvis-channel plugin (Claude Code receiver): [`qwwiwi/dashi-plugin-claude-code`](https://github.com/qwwiwi/dashi-plugin-claude-code)
- Hermes outgoing HMAC + sidecar proxy: [`docs/hermes-integration.md`](hermes-integration.md)
- Worker AGENT_GATEWAYS spec: [`docs/hermes-integration.md` §7](hermes-integration.md#7-outbound-hmac-swarm-worker)
- Reference listener: [`agent-template/scripts/webhook_listener.py`](../agent-template/scripts/webhook_listener.py)
- Architecture overview: [`docs/architecture.md`](architecture.md)
- Security model: [`docs/security.md`](security.md)
- Troubleshooting общий: [`docs/troubleshooting.md`](troubleshooting.md)
