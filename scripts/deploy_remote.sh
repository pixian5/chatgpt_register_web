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
systemctl restart "${SERVICE_NAME}"
systemctl --no-pager --full status "${SERVICE_NAME}" | sed -n '1,30p'
