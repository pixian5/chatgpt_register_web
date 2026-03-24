#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/root/c/chatgpt_register_web}"
APP_PORT="${APP_PORT:-52789}"
REPO_URL="${REPO_URL:-https://github.com/pixian5/chatgpt_register_web.git}"
BRANCH="${BRANCH:-main}"
SERVICE_NAME="${SERVICE_NAME:-chatgpt-register-web}"
BACKUP_SUFFIX="${BACKUP_SUFFIX:-$(date +%Y%m%d-%H%M%S)}"
BACKUP_DIR="${APP_DIR}/.deploy-backup-${BACKUP_SUFFIX}"

mkdir -p "$(dirname "$APP_DIR")"

if [ ! -d "$APP_DIR/.git" ]; then
  git clone "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR"

read_env_value() {
  local file="$1"
  local key="$2"
  if [ ! -f "$file" ]; then
    return 0
  fi
  local line
  line="$(grep -E "^${key}=" "$file" | tail -n 1 || true)"
  printf '%s' "${line#*=}"
}

pick_env_value() {
  local incoming="$1"
  local existing="$2"
  local fallback="$3"
  if [ -n "$incoming" ]; then
    printf '%s' "$incoming"
  elif [ -n "$existing" ]; then
    printf '%s' "$existing"
  else
    printf '%s' "$fallback"
  fi
}

EXISTING_ENV_FILE="$APP_DIR/.env"
EXISTING_DUCKMAIL_API_BASE="$(read_env_value "$EXISTING_ENV_FILE" "DUCKMAIL_API_BASE")"
EXISTING_DUCKMAIL_DOMAIN="$(read_env_value "$EXISTING_ENV_FILE" "DUCKMAIL_DOMAIN")"
EXISTING_DUCKMAIL_BEARER="$(read_env_value "$EXISTING_ENV_FILE" "DUCKMAIL_BEARER")"
EXISTING_POOL_BASE_URL="$(read_env_value "$EXISTING_ENV_FILE" "POOL_BASE_URL")"
EXISTING_POOL_TOKEN="$(read_env_value "$EXISTING_ENV_FILE" "POOL_TOKEN")"
EXISTING_POOL_TARGET_TYPE="$(read_env_value "$EXISTING_ENV_FILE" "POOL_TARGET_TYPE")"
EXISTING_POOL_TARGET_COUNT="$(read_env_value "$EXISTING_ENV_FILE" "POOL_TARGET_COUNT")"
EXISTING_POOL_PROXY="$(read_env_value "$EXISTING_ENV_FILE" "POOL_PROXY")"
EXISTING_POOL_PROBE_WORKERS="$(read_env_value "$EXISTING_ENV_FILE" "POOL_PROBE_WORKERS")"
EXISTING_POOL_DELETE_WORKERS="$(read_env_value "$EXISTING_ENV_FILE" "POOL_DELETE_WORKERS")"
EXISTING_POOL_INTERVAL_MIN="$(read_env_value "$EXISTING_ENV_FILE" "POOL_INTERVAL_MIN")"
EXISTING_PROXY="$(read_env_value "$EXISTING_ENV_FILE" "PROXY")"
EXISTING_WORKERS="$(read_env_value "$EXISTING_ENV_FILE" "WORKERS")"
EXISTING_PROXY_TEST_WORKERS="$(read_env_value "$EXISTING_ENV_FILE" "PROXY_TEST_WORKERS")"
EXISTING_ENABLE_OAUTH="$(read_env_value "$EXISTING_ENV_FILE" "ENABLE_OAUTH")"
EXISTING_OAUTH_REQUIRED="$(read_env_value "$EXISTING_ENV_FILE" "OAUTH_REQUIRED")"
EXISTING_OAUTH_ISSUER="$(read_env_value "$EXISTING_ENV_FILE" "OAUTH_ISSUER")"
EXISTING_OAUTH_CLIENT_ID="$(read_env_value "$EXISTING_ENV_FILE" "OAUTH_CLIENT_ID")"
EXISTING_OAUTH_REDIRECT_URI="$(read_env_value "$EXISTING_ENV_FILE" "OAUTH_REDIRECT_URI")"

cat > "$APP_DIR/.env" <<EOF
DUCKMAIL_API_BASE=$(pick_env_value "${DUCKMAIL_API_BASE:-}" "${EXISTING_DUCKMAIL_API_BASE}" "")
DUCKMAIL_DOMAIN=$(pick_env_value "${DUCKMAIL_DOMAIN:-}" "${EXISTING_DUCKMAIL_DOMAIN}" "")
DUCKMAIL_BEARER=$(pick_env_value "${DUCKMAIL_BEARER:-}" "${EXISTING_DUCKMAIL_BEARER}" "")
POOL_BASE_URL=$(pick_env_value "${POOL_BASE_URL:-}" "${EXISTING_POOL_BASE_URL}" "")
POOL_TOKEN=$(pick_env_value "${POOL_TOKEN:-}" "${EXISTING_POOL_TOKEN}" "")
POOL_TARGET_TYPE=$(pick_env_value "${POOL_TARGET_TYPE:-}" "${EXISTING_POOL_TARGET_TYPE}" "codex")
POOL_TARGET_COUNT=$(pick_env_value "${POOL_TARGET_COUNT:-}" "${EXISTING_POOL_TARGET_COUNT}" "666")
POOL_PROXY=$(pick_env_value "${POOL_PROXY:-}" "${EXISTING_POOL_PROXY}" "")
POOL_PROBE_WORKERS=$(pick_env_value "${POOL_PROBE_WORKERS:-}" "${EXISTING_POOL_PROBE_WORKERS}" "40")
POOL_DELETE_WORKERS=$(pick_env_value "${POOL_DELETE_WORKERS:-}" "${EXISTING_POOL_DELETE_WORKERS}" "10")
POOL_INTERVAL_MIN=$(pick_env_value "${POOL_INTERVAL_MIN:-}" "${EXISTING_POOL_INTERVAL_MIN}" "30")
PROXY=$(pick_env_value "${PROXY:-}" "${EXISTING_PROXY}" "")
WORKERS=$(pick_env_value "${WORKERS:-}" "${EXISTING_WORKERS}" "1")
PROXY_TEST_WORKERS=$(pick_env_value "${PROXY_TEST_WORKERS:-}" "${EXISTING_PROXY_TEST_WORKERS}" "20")
ENABLE_OAUTH=$(pick_env_value "${ENABLE_OAUTH:-}" "${EXISTING_ENABLE_OAUTH}" "true")
OAUTH_REQUIRED=$(pick_env_value "${OAUTH_REQUIRED:-}" "${EXISTING_OAUTH_REQUIRED}" "true")
OAUTH_ISSUER=$(pick_env_value "${OAUTH_ISSUER:-}" "${EXISTING_OAUTH_ISSUER}" "https://auth.openai.com")
OAUTH_CLIENT_ID=$(pick_env_value "${OAUTH_CLIENT_ID:-}" "${EXISTING_OAUTH_CLIENT_ID}" "app_EMoamEEZ73f0CkXaXp7hrann")
OAUTH_REDIRECT_URI=$(pick_env_value "${OAUTH_REDIRECT_URI:-}" "${EXISTING_OAUTH_REDIRECT_URI}" "http://localhost:1455/auth/callback")
EOF
chmod 600 "$APP_DIR/.env"

git fetch origin "$BRANCH"
git checkout "$BRANCH"

mkdir -p "$BACKUP_DIR"
for path in ak.txt rk.txt registered_accounts.txt .env .env.local config.local.json config.json codex_tokens; do
  if [ -e "$path" ]; then
    cp -a "$path" "$BACKUP_DIR/"
  fi
done

git reset --hard "origin/${BRANCH}"
git clean -fd

for path in ak.txt rk.txt registered_accounts.txt .env .env.local config.local.json config.json codex_tokens; do
  if [ -e "$BACKUP_DIR/$path" ]; then
    rm -rf "$path"
    cp -a "$BACKUP_DIR/$path" "$path"
  fi
done

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

.venv/bin/python -m ensurepip --upgrade
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -r requirements.txt

cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=chatgpt_register_web
After=network.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/.venv/bin/python -m uvicorn web_app:app --host 0.0.0.0 --port ${APP_PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}" >/dev/null 2>&1 || true
systemctl stop "${SERVICE_NAME}" >/dev/null 2>&1 || true
if command -v fuser >/dev/null 2>&1; then
  fuser -k "${APP_PORT}/tcp" >/dev/null 2>&1 || true
fi
pkill -f "${APP_DIR}/.venv/bin/python -m uvicorn web_app:app --host 0.0.0.0 --port ${APP_PORT}" >/dev/null 2>&1 || true
sleep 1
systemctl restart "${SERVICE_NAME}"
systemctl --no-pager --full status "${SERVICE_NAME}" | sed -n '1,30p'
