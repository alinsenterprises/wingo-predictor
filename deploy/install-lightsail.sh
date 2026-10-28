#!/bin/sh
# Install the Wingo prediction analyzer on an Amazon Lightsail Linux instance
# (Ubuntu/Debian) so it runs continuously under systemd.
#
# Usage (from the project root, on the instance):
#   sh ./deploy/install-lightsail.sh
#
# Override defaults if needed:
#   APP_DIR=/opt/wingo-analyzer SERVICE_USER=ubuntu sh ./deploy/install-lightsail.sh
set -eu

APP_DIR="${APP_DIR:-$PWD}"
SERVICE_NAME="wingo-analyzer"
SERVICE_USER="${SERVICE_USER:-ubuntu}"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
else
  SUDO="sudo"
fi

echo "==> checking prerequisites"
command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "Python 3.10+ is required" >&2; exit 1; }

echo "==> creating virtualenv in $APP_DIR/.venv"
if [ ! -d "$APP_DIR/.venv" ]; then
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
"$APP_DIR/.venv/bin/python" -m pip install -r "$APP_DIR/requirements.txt"

echo "==> installing Chromium for the four browser heads"
# Browsers live inside the project (.playwright-browsers/) so the service finds
# them regardless of which user systemd runs it as. --only-shell = headless-only.
PLAYWRIGHT_BROWSERS_PATH="$APP_DIR/.playwright-browsers" \
  "$APP_DIR/.venv/bin/playwright" install chromium --only-shell
# System libraries Chromium links against (needs root/sudo, best effort).
PLAYWRIGHT_BROWSERS_PATH="$APP_DIR/.playwright-browsers" \
  "$APP_DIR/.venv/bin/playwright" install-deps chromium >/dev/null 2>&1 \
  || echo "!!  browser system libraries not installed automatically - run:"

echo "     $APP_DIR/.venv/bin/playwright install-deps chromium"

mkdir -p "$APP_DIR/logs" "$APP_DIR/data" "$APP_DIR/snapshots"
if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/env.example" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  echo "!!  created $APP_DIR/.env from env.example - edit it with your credentials:"
  echo "      nano $APP_DIR/.env"
fi
if ! grep -q '^TELEGRAM_BOT_TOKEN=.\+' "$APP_DIR/.env" 2>/dev/null; then
  echo "!!  TELEGRAM_BOT_TOKEN is still empty in $APP_DIR/.env"
fi

echo "==> installing systemd unit $UNIT_FILE"
$SUDO tee "$UNIT_FILE" > /dev/null <<EOF
[Unit]
Description=Wingo prediction analyzer for dmfirst1.com
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${APP_DIR}/.env
ExecStart=${APP_DIR}/.venv/bin/python -m wingo_analyzer run
Restart=always
RestartSec=5
StartLimitIntervalSec=0
TimeoutStopSec=30
KillSignal=SIGTERM
StandardOutput=journal
StandardError=journal
SyslogIdentifier=wingo-analyzer
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

$SUDO systemctl daemon-reload
$SUDO systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
$SUDO systemctl restart "$SERVICE_NAME"

echo "==> waiting for the service to come up"
sleep 3
$SUDO systemctl --no-pager status "$SERVICE_NAME" || true

cat <<EOF

Done. Useful commands:
  journalctl -u ${SERVICE_NAME} -f          # follow logs
  systemctl restart ${SERVICE_NAME}         # restart after editing ${APP_DIR}/.env
  ${APP_DIR}/.venv/bin/python -m wingo_analyzer check   # validate all 4 games
  ${APP_DIR}/.venv/bin/python -m wingo_analyzer stats --mode 1min

Data lands in two folders:
  ${APP_DIR}/data/      rolling SQLite dataset + data/history/<game>.jsonl archives
  ${APP_DIR}/snapshots/ photos: one per game every 2 minutes + one per error

On reboot the service starts automatically (systemctl is enabled).
EOF
