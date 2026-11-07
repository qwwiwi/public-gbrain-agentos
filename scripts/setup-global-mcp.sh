#!/usr/bin/env bash
# setup-global-mcp.sh — выпустить токен агента и подключить gbrain к Claude Code
# на user-scope, чтобы мозг был доступен из любого каталога, а не только из
# workspace'а с локальным .mcp.json.
#
# Запуск (обычным пользователем, не под root — иначе конфиг уедет в /root):
#   scripts/setup-global-mcp.sh <agent-id> <scopes> [read-scopes]
#
# Примеры:
#   scripts/setup-global-mcp.sh my-agent '20-daily,90-inbox'
#   scripts/setup-global-mcp.sh my-agent '20-daily,90-inbox' '*'
#   GBRAIN_BASE_URL=https://gbrain.example.com scripts/setup-global-mcp.sh my-agent 'read'
#
# Переменные окружения:
#   GBRAIN_BASE_URL  — публичный адрес за Caddy. Если не задан, используется
#                      loopback с портами сервисов (локальная установка).
#   INSTALL_DIR      — каталог установки (по умолчанию /opt/gbrain).
#   SERVICE_USER     — служебный пользователь для peer-аутентификации (gbrain).
#
# Сырой токен печатается скриптом выпуска ровно один раз. Здесь он
# перехватывается в переменную и уходит прямо в ~/.claude.json — в stdout,
# логи и историю команд не попадает.

set -euo pipefail
umask 077

AGENT_ID="${1:-}"
WRITE_SCOPES="${2:-}"
READ_SCOPES="${3:-}"

INSTALL_DIR="${INSTALL_DIR:-/opt/gbrain}"
SERVICE_USER="${SERVICE_USER:-gbrain}"

if [ -z "$AGENT_ID" ] || [ -z "$WRITE_SCOPES" ]; then
  echo "usage: $0 <agent-id> <scopes> [read-scopes]" >&2
  exit 2
fi

if [ "$(id -u)" -eq 0 ]; then
  echo "ERROR: не запускай под root — 'claude mcp add' пишет в ~/.claude.json" >&2
  echo "       того пользователя, от чьего имени идёт запуск." >&2
  exit 1
fi

if ! command -v claude >/dev/null 2>&1; then
  echo "ERROR: в PATH нет 'claude' — нужен Claude Code CLI" >&2
  exit 1
fi

# Токен выпускается от имени служебного пользователя: Postgres принимает
# подключение по unix-сокету через peer-аутентификацию, где системный
# пользователь должен совпадать с ролью БД.
echo "==> Выпускаю токен: агент=${AGENT_ID} write=${WRITE_SCOPES} read=${READ_SCOPES:-$WRITE_SCOPES}"

ISSUE_ARGS=(--agent "$AGENT_ID" --scopes "$WRITE_SCOPES")
if [ -n "$READ_SCOPES" ]; then
  ISSUE_ARGS+=(--read-scopes "$READ_SCOPES")
fi

TOKEN="$(sudo -u "$SERVICE_USER" \
  "$INSTALL_DIR/.venv/bin/python" \
  "$INSTALL_DIR/scripts/issue-agent-token.py" \
  "${ISSUE_ARGS[@]}")"

if [ -z "$TOKEN" ]; then
  echo "ERROR: токен не выпущен" >&2
  exit 1
fi

# Топология адресов. За Caddy сервисы разведены по путям, при локальной
# установке — по портам loopback. Имена серверов одинаковы в обоих случаях:
# на них завязаны имена тулзов (mcp__gbrain-recall__recall и т.д.).
if [ -n "${GBRAIN_BASE_URL:-}" ]; then
  BASE="${GBRAIN_BASE_URL%/}"
  SERVERS="gbrain-memory:${BASE}/memory/mcp
gbrain-recall:${BASE}/recall/mcp
gbrain-swarm:${BASE}/swarm/mcp
gbrain-tasks:${BASE}/task/mcp"
else
  SERVERS="gbrain-memory:http://127.0.0.1:8767/mcp
gbrain-recall:http://127.0.0.1:8768/mcp
gbrain-swarm:http://127.0.0.1:8766/mcp
gbrain-tasks:http://127.0.0.1:8769/mcp"
fi

while IFS= read -r entry; do
  name="${entry%%:*}"
  url="${entry#*:}"
  echo "==> ${name} -> ${url}"
  # remove идемпотентен по смыслу: молча пропускаем, если сервера ещё нет.
  claude mcp remove -s user "$name" >/dev/null 2>&1 || true
  claude mcp add --transport http --scope user "$name" "$url" \
    --header "Authorization: Bearer ${TOKEN}"
done <<< "$SERVERS"

unset TOKEN

echo
echo "Готово. Серверы видны из любого каталога. Проверка:"
echo "  claude mcp list"
echo
echo "Локальный .mcp.json в каталоге проекта перекрывает user-scope —"
echo "workspace со своим токеном продолжит работать на нём."
