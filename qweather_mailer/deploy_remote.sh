#!/usr/bin/env bash
set -euo pipefail

REMOTE_DIR="${REMOTE_DIR:-/root/c/qweather_mailer}"
SERVICE_NAME="${SERVICE_NAME:-qweather-mailer.service}"
TIMER_NAME="${TIMER_NAME:-qweather-mailer.timer}"
ENABLE_TIMER="${ENABLE_TIMER:-true}"

mkdir -p "$REMOTE_DIR"
cd "$REMOTE_DIR"

python3 -m venv .venv
.venv/bin/python -m ensurepip --upgrade
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -r requirements.txt

cat > "/etc/systemd/system/${SERVICE_NAME}" <<EOF
[Unit]
Description=QWeather daily mail sender
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=${REMOTE_DIR}
Environment=TZ=Asia/Shanghai
ExecStart=${REMOTE_DIR}/.venv/bin/python ${REMOTE_DIR}/weather_mailer.py
EOF

cat > "/etc/systemd/system/${TIMER_NAME}" <<EOF
[Unit]
Description=Run QWeather mailer every day at 00:00 Asia/Shanghai

[Timer]
OnCalendar=Asia/Shanghai *-*-* 00:00:00
Persistent=true
Unit=${SERVICE_NAME}

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
if [ "$ENABLE_TIMER" = "true" ]; then
  systemctl enable "${TIMER_NAME}" >/dev/null 2>&1 || true
  systemctl restart "${TIMER_NAME}"
else
  systemctl disable "${TIMER_NAME}" >/dev/null 2>&1 || true
  systemctl stop "${TIMER_NAME}" >/dev/null 2>&1 || true
fi
systemctl --no-pager --full status "${TIMER_NAME}" | sed -n '1,20p' || true
