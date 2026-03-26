#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/root/c/chatgpt_register_web}"
APP_PORT="${APP_PORT:-51111}"
REPO_URL="${REPO_URL:-https://github.com/pixian5/chatgpt_register_web.git}"
BRANCH="${BRANCH:-main}"
SERVICE_NAME="${SERVICE_NAME:-balance-web-51111}"

mkdir -p "$(dirname "$APP_DIR")"

if [ ! -d "$APP_DIR/.git" ]; then
  git clone "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git reset --hard "origin/${BRANCH}"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

.venv/bin/python -m ensurepip --upgrade
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -r requirements.txt

cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=balance web 51111
After=network.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/python -m uvicorn balance_web:app --host 0.0.0.0 --port ${APP_PORT}
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
pkill -f "uvicorn balance_web:app --host 0.0.0.0 --port ${APP_PORT}" >/dev/null 2>&1 || true
sleep 1
systemctl restart "${SERVICE_NAME}"
systemctl --no-pager --full status "${SERVICE_NAME}" | sed -n '1,30p'
