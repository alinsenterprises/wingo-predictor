#!/bin/sh
# Install the Wingo prediction analyzer on an EC2 Ubuntu instance so it runs
# 24/7 under systemd (Restart=always, enabled at boot).
#
# Usage (on the instance, from the project root):
#   sh deploy/install-ec2.sh
#
# The service reads its credentials from the app-dir env file; this script
# never prints or modifies secret values - create that file yourself (e.g.
# copy it from your workspace with scp) and restart the service.
#
# Override defaults:  APP_DIR=/opt/wingo-analyzer SERVICE_USER=ubuntu sh deploy/install-ec2.sh
set -eu

APP_DIR="${APP_DIR:-$PWD}"
SERVICE_NAME="wingo-analyzer"
SERVICE_USER="${SERVICE_USER:-ubuntu}"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
ENV_FILE="$APP_DIR/.env"

if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

echo "==> prerequisites"
command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "Python 3.10+ is required" >&2; exit 1; }

echo "==> virtualenv in $APP_DIR/.venv"
if [ ! -d "$APP_DIR/.venv" ]; then
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/python" -m pip install -q --upgrade pip
"$APP_DIR/.venv/bin/python" -m pip install -q -r "$APP_DIR/requirements.txt"
echo "    deps: $("$APP_DIR/.venv/bin/python" -m pip list --format=freeze | tr '\n' ' ')"

echo "==> checking the SQLite dataset"
"$APP_DIR/.venv/bin/python" - <<'PY'
import os
import sqlite3
import sys
db = "data/wingo.db"
if not os.path.exists(db):
    print("    no database yet - it will backfill from the API on first start")
    sys.exit(0)
try:
    conn = sqlite3.connect(db)
    ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
    conn.close()
except Exception as exc:                      # noqa: BLE001
    ok = f"error: {exc}"
if ok == "ok":
    print("    database integrity: ok")
else:
    print(f"    database unusable ({ok}) - clearing so it backfills fresh")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db + suffix):
            os.remove(db + suffix)
PY

mkdir -p "$APP_DIR/logs" "$APP_DIR/data" "$APP_DIR/snapshots"

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

echo "==> waiting for the service"
sleep 4
$SUDO systemctl --no-pager status "$SERVICE_NAME" | head -12 || true
echo "==> recent log"
$SUDO journalctl -u "$SERVICE_NAME" -n 8 --no-pager || true

if [ ! -f "$ENV_FILE" ]; then
  echo ""
  echo "!!  no env file at $ENV_FILE - the service runs headless without it."
  echo "    Copy it from your workspace, then restart:"
  echo "      scp .env.local ubuntu@<instance-ip>:$ENV_FILE"
  echo "      sudo systemctl restart $SERVICE_NAME"
fi

cat <<EOF

Done. 24/7 behaviour:
  systemctl status ${SERVICE_NAME}     # live status
  journalctl -u ${SERVICE_NAME} -f     # follow logs
  sudo systemctl restart ${SERVICE_NAME}   # after changing the env file
On reboot the service starts automatically (unit is enabled).
EOF
