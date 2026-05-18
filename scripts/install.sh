#!/usr/bin/env bash
# install.sh — gbrain bootstrap for a fresh Ubuntu 22.04 LTS VPS.
#
# Usage:
#   sudo bash scripts/install.sh
#
# Reads configuration from .env (or environment). Idempotent: re-running is safe.
# See README.md for the full deployment guide.

set -euo pipefail
set -o pipefail

# ---------------------------------------------------------------------------
# 0. Logging helpers
# ---------------------------------------------------------------------------

log()  { printf '[install %s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
die()  { printf '[install ERROR] %s\n' "$*" >&2; exit 1; }
note() { printf '\n=== %s ===\n' "$*"; }

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# 1. Platform check
# ---------------------------------------------------------------------------

note "1. Platform check"

if [ ! -r /etc/os-release ]; then
  die "cannot read /etc/os-release — this script supports Ubuntu 22.04 LTS only"
fi
# shellcheck disable=SC1091
. /etc/os-release
if [ "${ID:-}" != "ubuntu" ] || { [ "${VERSION_ID:-}" != "22.04" ] && [ "${VERSION_ID:-}" != "24.04" ]; }; then
  die "unsupported platform: ID=${ID:-?} VERSION_ID=${VERSION_ID:-?} (need ubuntu 22.04 or 24.04)"
fi
log "platform ok: ubuntu ${VERSION_ID}"

if [ "$(id -u)" -ne 0 ]; then
  die "must run as root (sudo bash scripts/install.sh)"
fi

# ---------------------------------------------------------------------------
# 2. Load .env
# ---------------------------------------------------------------------------

note "2. Loading .env"

if [ -f "$REPO_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$REPO_ROOT/.env"
  set +a
  log ".env loaded"
else
  log ".env not present, falling back to environment + defaults"
fi

# Canonical env var names (single source of truth).
: "${INSTALL_DIR:=/opt/gbrain}"
: "${SERVICE_USER:=gbrain}"
: "${ETC_DIR:=/etc/gbrain}"
: "${LOG_DIR:=/var/log/gbrain}"
: "${STATE_DIR:=/var/lib/gbrain}"
: "${PG_HOST:=/var/run/postgresql}"
: "${PG_PORT:=5432}"
: "${PG_DATABASE:=gbrain}"
: "${PG_USER:=gbrain}"
: "${PG_PASSWORD:=}"
: "${MCP_MEMORY_PORT:=8767}"
: "${MCP_RECALL_PORT:=8768}"
: "${MCP_SWARM_PORT:=8766}"
: "${VAULT_ROOT:=$INSTALL_DIR/vault}"
: "${DOMAIN:=}"
: "${ACME_EMAIL:=}"

log "INSTALL_DIR=$INSTALL_DIR SERVICE_USER=$SERVICE_USER"
log "PG_DATABASE=$PG_DATABASE PG_USER=$PG_USER"
log "DOMAIN=${DOMAIN:-<unset, skip caddy>}"

# ---------------------------------------------------------------------------
# 3. apt packages (Postgres 16 + pgvector from apt.postgresql.org)
# ---------------------------------------------------------------------------

note "3. apt packages"

export DEBIAN_FRONTEND=noninteractive

apt-get update -y

# python3.11 from deadsnakes on 22.04
if ! command -v python3.11 >/dev/null 2>&1; then
  log "installing python3.11 (deadsnakes)"
  apt-get install -y software-properties-common
  add-apt-repository -y ppa:deadsnakes/ppa
  apt-get update -y
fi

# Postgres 16 from apt.postgresql.org (Ubuntu 22.04 universe only has 14).
# pgvector for PG 16 is in the same repo as postgresql-16-pgvector.
if [ ! -f /etc/apt/sources.list.d/pgdg.list ]; then
  log "adding apt.postgresql.org repo for Postgres 16"
  apt-get install -y curl ca-certificates gnupg lsb-release
  install -d /usr/share/keyrings
  curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    | gpg --dearmor -o /usr/share/keyrings/postgresql-archive-keyring.gpg
  echo "deb [signed-by=/usr/share/keyrings/postgresql-archive-keyring.gpg] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list
  apt-get update -y
fi

# Caddy stable from cloudsmith (Ubuntu universe has very old Caddy v2.4).
if [ ! -f /etc/apt/sources.list.d/caddy-stable.list ]; then
  log "adding caddy stable apt repo"
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  echo "deb [signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg] https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main" \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
fi

apt-get install -y --no-install-recommends \
  python3.11 python3.11-venv python3.11-dev \
  postgresql-16 postgresql-16-pgvector \
  caddy \
  git curl jq ca-certificates gettext-base build-essential libpq-dev

log "apt install done"

# ---------------------------------------------------------------------------
# 4. Service user + directories
# ---------------------------------------------------------------------------

note "4. user + directories"

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
  log "created system user $SERVICE_USER"
else
  log "user $SERVICE_USER already exists"
fi

mkdir -p \
  "$INSTALL_DIR" \
  "$INSTALL_DIR/services" \
  "$INSTALL_DIR/vault" \
  "$INSTALL_DIR/migrations" \
  "$INSTALL_DIR/secrets" \
  "$INSTALL_DIR/.cache" \
  "$ETC_DIR" \
  "$LOG_DIR" \
  "$STATE_DIR" \
  "$STATE_DIR/fastembed"

chmod 700 "$INSTALL_DIR/secrets" "$ETC_DIR"

# ---------------------------------------------------------------------------
# 5. Sync repo into install dir
# ---------------------------------------------------------------------------

note "5. sync repo → $INSTALL_DIR"

# Use rsync if available, else cp -a. Exclude development artifacts.
if command -v rsync >/dev/null 2>&1; then
  rsync -a \
    --delete \
    --exclude '.git' \
    --exclude '.venv' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.env' \
    --exclude 'secrets/' \
    "$REPO_ROOT/" "$INSTALL_DIR/"
else
  cp -a "$REPO_ROOT/." "$INSTALL_DIR/"
fi

chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR" "$LOG_DIR" "$STATE_DIR"

# ---------------------------------------------------------------------------
# 6. Python venv + deps
# ---------------------------------------------------------------------------

note "6. python venv"

if [ ! -x "$INSTALL_DIR/.venv/bin/python" ]; then
  sudo -u "$SERVICE_USER" python3.11 -m venv "$INSTALL_DIR/.venv"
  log "venv created"
else
  log "venv already exists"
fi

if [ -f "$INSTALL_DIR/requirements.txt" ]; then
  sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
  sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"
  log "pip install done"
else
  log "WARNING: $INSTALL_DIR/requirements.txt missing — skipping pip install"
fi

# ---------------------------------------------------------------------------
# 7. Postgres database + pgvector + password
# ---------------------------------------------------------------------------

note "7. postgres"

systemctl enable --now postgresql

# Create role if absent (idempotent)
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$PG_USER';" | grep -q 1; then
  sudo -u postgres createuser "$PG_USER"
  log "created postgres role $PG_USER"
else
  log "role $PG_USER exists"
fi

# Generate a password if one isn't already provided (idempotent across re-runs
# by persisting the value into $ETC_DIR/secrets.env on first install).
if [ -z "$PG_PASSWORD" ] && [ -f "$ETC_DIR/secrets.env" ]; then
  # Reuse the password from a previous install if present.
  EXISTING_PW="$(grep -E '^PG_PASSWORD=' "$ETC_DIR/secrets.env" | head -1 | cut -d= -f2- || true)"
  if [ -n "$EXISTING_PW" ]; then
    PG_PASSWORD="$EXISTING_PW"
    log "reusing PG_PASSWORD from $ETC_DIR/secrets.env"
  fi
fi
if [ -z "$PG_PASSWORD" ]; then
  PG_PASSWORD="$(openssl rand -hex 32)"
  log "generated new PG_PASSWORD (will be written to $ETC_DIR/secrets.env)"
fi

# Always (re)apply the password to the postgres role to keep them in sync.
sudo -u postgres psql -v ON_ERROR_STOP=1 \
  -c "ALTER USER $PG_USER WITH PASSWORD '$PG_PASSWORD';" >/dev/null
log "postgres role $PG_USER password set"

# Create DB if absent (idempotent)
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$PG_DATABASE';" | grep -q 1; then
  sudo -u postgres createdb -O "$PG_USER" "$PG_DATABASE"
  log "created database $PG_DATABASE"
else
  log "database $PG_DATABASE exists"
fi

sudo -u postgres psql -d "$PG_DATABASE" -c "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null
log "pgvector extension ready"

# ---------------------------------------------------------------------------
# 8. Write secrets.env BEFORE running migrations / issuing tokens
# ---------------------------------------------------------------------------

note "8. secrets.env"

# Single canonical EnvironmentFile consumed by all systemd units.
# Names MUST match what services/shared/config.py and the worker scripts read.
cat > "$ETC_DIR/secrets.env" <<EOF
# Generated by scripts/install.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ).
# Re-running install.sh preserves existing values where safe.
PG_HOST=$PG_HOST
PG_PORT=$PG_PORT
PG_DATABASE=$PG_DATABASE
PG_USER=$PG_USER
PG_PASSWORD=$PG_PASSWORD
VAULT_ROOT=$VAULT_ROOT
LOG_DIR=$LOG_DIR
STATE_DIR=$STATE_DIR
FASTEMBED_CACHE_DIR=$STATE_DIR/fastembed
EOF
# Bind MCP servers to loopback only (Caddy fronts them). Kept out of the
# heredoc above so a re-run's `cat >` overwrite is followed by exactly one
# append — idempotent, no duplicate MCP_HOST lines accumulate.
echo "MCP_HOST=127.0.0.1" >> "$ETC_DIR/secrets.env"
chmod 600 "$ETC_DIR/secrets.env"
chown "$SERVICE_USER":"$SERVICE_USER" "$ETC_DIR/secrets.env"
log "wrote $ETC_DIR/secrets.env (review and add provider API keys as needed)"

# Also write a private install-time .env so the issue-token script and other
# manual CLI tools can read PG_PASSWORD without sudo.
INSTALL_ENV="$INSTALL_DIR/.env"
cat > "$INSTALL_ENV" <<EOF
PG_HOST=$PG_HOST
PG_PORT=$PG_PORT
PG_DATABASE=$PG_DATABASE
PG_USER=$PG_USER
PG_PASSWORD=$PG_PASSWORD
VAULT_ROOT=$VAULT_ROOT
MCP_MEMORY_PORT=$MCP_MEMORY_PORT
MCP_RECALL_PORT=$MCP_RECALL_PORT
MCP_SWARM_PORT=$MCP_SWARM_PORT
EOF
chmod 600 "$INSTALL_ENV"
chown "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_ENV"

# ---------------------------------------------------------------------------
# 9. Run migrations
# ---------------------------------------------------------------------------

note "9. migrations"

if [ -d "$INSTALL_DIR/migrations" ] && [ -f "$INSTALL_DIR/scripts/migrate.sh" ]; then
  PG_DATABASE="$PG_DATABASE" bash "$INSTALL_DIR/scripts/migrate.sh"
else
  log "WARNING: migrations dir or migrate.sh missing — skipping"
fi

# ---------------------------------------------------------------------------
# 10. Generate admin agent token
# ---------------------------------------------------------------------------

note "10. admin token"

ADMIN_TOKEN_FILE="$INSTALL_DIR/secrets/admin.token"

if [ ! -s "$ADMIN_TOKEN_FILE" ]; then
  if [ -x "$INSTALL_DIR/.venv/bin/python" ] && [ -f "$INSTALL_DIR/scripts/issue-agent-token.py" ]; then
    # The redirect runs in this (root) shell; that's intentional since the
    # secrets dir is root-owned at this point. We chown to SERVICE_USER below.
    # shellcheck disable=SC2024
    PG_HOST="$PG_HOST" PG_PORT="$PG_PORT" \
    PG_DATABASE="$PG_DATABASE" PG_USER="$PG_USER" PG_PASSWORD="$PG_PASSWORD" \
      sudo -E -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/python" \
      "$INSTALL_DIR/scripts/issue-agent-token.py" \
      --agent admin --scopes '*' \
      > "$ADMIN_TOKEN_FILE"
    chmod 600 "$ADMIN_TOKEN_FILE"
    chown "$SERVICE_USER":"$SERVICE_USER" "$ADMIN_TOKEN_FILE"
    if [ ! -s "$ADMIN_TOKEN_FILE" ]; then
      die "admin token generation produced an empty file ($ADMIN_TOKEN_FILE) — check above for errors"
    fi
    log "admin token written to $ADMIN_TOKEN_FILE"
  else
    die "cannot issue admin token (venv or script missing)"
  fi
else
  log "admin token already exists at $ADMIN_TOKEN_FILE"
fi

# ---------------------------------------------------------------------------
# 11. Pre-download FastEmbed model (avoids first-request OOM under hardening)
# ---------------------------------------------------------------------------

note "11. FastEmbed model pre-download"

FASTEMBED_MODEL="${FASTEMBED_MODEL:-intfloat/multilingual-e5-large}"
if [ -x "$INSTALL_DIR/.venv/bin/python" ]; then
  FASTEMBED_CACHE_DIR="$STATE_DIR/fastembed" \
    sudo -E -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/python" \
      -c "from fastembed import TextEmbedding; TextEmbedding('$FASTEMBED_MODEL', cache_dir='$STATE_DIR/fastembed'); print('fastembed model ready')" \
    || log "WARNING: fastembed pre-download failed (recall-mcp will retry on first call)"
else
  log "venv missing — skipping fastembed pre-download"
fi

# ---------------------------------------------------------------------------
# 12. Render + install systemd units
# ---------------------------------------------------------------------------

note "12. systemd units"

INSTALLED_UNITS=()
for tpl in "$INSTALL_DIR"/systemd/*.service.template; do
  [ -f "$tpl" ] || continue
  base="$(basename "$tpl" .service.template)"
  out="/etc/systemd/system/gbrain-${base}.service"
  sed \
    -e "s|{{INSTALL_DIR}}|$INSTALL_DIR|g" \
    -e "s|{{SERVICE_USER}}|$SERVICE_USER|g" \
    -e "s|{{ETC_DIR}}|$ETC_DIR|g" \
    -e "s|{{LOG_DIR}}|$LOG_DIR|g" \
    -e "s|{{STATE_DIR}}|$STATE_DIR|g" \
    "$tpl" > "$out"
  log "installed $out"
  INSTALLED_UNITS+=("gbrain-${base}")
done

systemctl daemon-reload

# ---------------------------------------------------------------------------
# 13. Caddy (optional, only if DOMAIN set)
# ---------------------------------------------------------------------------

note "13. caddy"

if [ -n "$DOMAIN" ] && [ -n "$ACME_EMAIL" ]; then
  mkdir -p /etc/caddy/Caddyfile.d
  DOMAIN="$DOMAIN" ACME_EMAIL="$ACME_EMAIL" \
    envsubst < "$INSTALL_DIR/caddy/Caddyfile.template" \
    > /etc/caddy/Caddyfile.d/gbrain.caddy
  log "rendered /etc/caddy/Caddyfile.d/gbrain.caddy"

  # Ensure main Caddyfile imports the .d directory
  if [ -f /etc/caddy/Caddyfile ] && ! grep -q 'Caddyfile.d/' /etc/caddy/Caddyfile; then
    echo 'import Caddyfile.d/*.caddy' >> /etc/caddy/Caddyfile
    log "added 'import Caddyfile.d/*.caddy' to /etc/caddy/Caddyfile"
  fi

  systemctl reload caddy || systemctl restart caddy
  log "caddy reloaded"
else
  log "DOMAIN or ACME_EMAIL unset — skipping Caddy (use Tailscale-only setup)"
fi

# ---------------------------------------------------------------------------
# 14. Start services (memory-mcp, recall-mcp, swarm-mcp, swarm-worker, ingest-worker)
# ---------------------------------------------------------------------------

note "14. start services"

systemctl enable --now \
  gbrain-memory-mcp \
  gbrain-recall-mcp \
  gbrain-swarm-mcp \
  gbrain-swarm-worker \
  gbrain-ingest-worker

sleep 3
systemctl --no-pager status \
  gbrain-memory-mcp \
  gbrain-recall-mcp \
  gbrain-swarm-mcp \
  gbrain-swarm-worker \
  gbrain-ingest-worker || true

# ---------------------------------------------------------------------------
# 15. Smoke test
# ---------------------------------------------------------------------------

note "15. smoke test"

if [ -x "$INSTALL_DIR/scripts/smoke-test.sh" ]; then
  if MCP_MEMORY_PORT="$MCP_MEMORY_PORT" MCP_RECALL_PORT="$MCP_RECALL_PORT" \
     MCP_SWARM_PORT="$MCP_SWARM_PORT" \
     bash "$INSTALL_DIR/scripts/smoke-test.sh"; then
    log "smoke test passed"
  else
    log "WARNING: smoke test failed — check journalctl -u gbrain-*"
  fi
else
  log "smoke test script missing — skipping"
fi

# ---------------------------------------------------------------------------
# 16. Done
# ---------------------------------------------------------------------------

note "16. done"

cat <<EOF

gbrain install complete.

EOF

log "admin token written to $ADMIN_TOKEN_FILE. Read: sudo cat $ADMIN_TOKEN_FILE"

cat <<EOF

Next steps:
  1. Verify services are listening:  ss -tlnp | grep -E '876[678]'
  2. Issue per-agent tokens:         $INSTALL_DIR/.venv/bin/python $INSTALL_DIR/scripts/issue-agent-token.py --agent <name> --scopes 'read,write'
  3. Point your local agents at:     http(s)://<host>/{memory,recall,swarm}/mcp
  4. Set up the inbox-agent locally: bash $INSTALL_DIR/scripts/install-local.sh
  5. Review $ETC_DIR/secrets.env and add provider API keys you want available.

Logs:    journalctl -u gbrain-memory-mcp -f
Vault:   $VAULT_ROOT
Secrets: $INSTALL_DIR/secrets/ (mode 0600)

EOF
