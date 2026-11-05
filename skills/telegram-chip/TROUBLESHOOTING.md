# Troubleshooting — telegram-chip

## Не удается получить TELEGRAM_API_ID / TELEGRAM_API_HASH

**Симптомы:** `my.telegram.org/apps` ведёт себя нестабильно, не логинит или не отдает ключи.

**Что делать:**
1. Открыть `my.telegram.org/apps` с телефона через мобильный интернет (без Wi‑Fi/VPN),
   **или** использовать чистую private/incognito-сессию в браузере.
2. Ввести ключи вручную в `.env`.
3. Не хранить ключи в публичных заметках/чатах.

---

## Error: There is no current event loop in thread 'MainThread'

**Причина:** старый sync-путь Telethon на новых версиях Python.

**Решение:**
- использовать обновлённый `session_string_generator.py` (async версия),
- запускать только через `./generate-session.sh`.

---

## AuthKeyDuplicatedError

```
The authorization key (session file) was used under two different IP addresses simultaneously
```

**Причина:** Два процесса используют один `TELETHON_SESSION_STRING` одновременно. Telegram блокирует сессию навсегда.

**Решение:**
1. Убить ВСЕ процессы с Telethon: `ps aux | grep telethon | grep -v grep`
2. Сгенерировать новую сессию: `python3 session_string_generator.py`
3. Обновить session string ВЕЗДЕ (все .env файлы)
4. Запустить ТОЛЬКО telegram-chip-api — все остальные скрипты ходят через HTTP API

**Профилактика:** Один session string = один процесс (telegram-chip-api). Все остальные скрипты используют HTTP API `http://127.0.0.1:8080`.

---

## Fail2ban блокирует SSH

```
Connection to <user>@<server_ip>:22 exited: Connect failed: Connection refused
```

**Причина:** fail2ban забанил IP после неудачных попыток входа.

**Проверка:**
```bash
sudo iptables -L f2b-sshd -n
```

**Разбан:**
```bash
sudo fail2ban-client set sshd unbanip <CLIENT_IP>
```

**Whitelist** (чтобы больше не банил): `/etc/fail2ban/jail.local`
```
[DEFAULT]
ignoreip = 127.0.0.1/8 ::1 <TRUSTED_IP>
```

---

## SSH host key mismatch

```
ssh-ed25519 host key mismatch for <server_ip>
```

**Причина:** Сервер переустановлен, ключ изменился.

**Решение:**
```bash
ssh-keygen -R <server_ip>
ssh <user>@<server_ip>
```

---

## telegram-chip-api не стартует

**Проверка:**
```bash
systemctl status telegram-chip-api
journalctl -u telegram-chip-api -n 50
```

**Частые причины:**
- Нет `.env` в the skill directory
- Невалидный session string
- Порт 8080 занят: `ss -tlnp | grep :8080`
- Lock файл остался: `rm /tmp/telegram-core.lock`

---

## Как сгенерировать новую сессию

```bash
cd /path/to/telegram-chip
python3 session_string_generator.py
```

Скрипт запросит код подтверждения из Telegram. После генерации обновить:
- every `.env` file that holds the session string (`TELEGRAM_SESSION_STRING` / `TELETHON_SESSION_STRING`)

Затем:
```bash
sudo systemctl restart telegram-chip-api
curl http://127.0.0.1:8080/health
```
