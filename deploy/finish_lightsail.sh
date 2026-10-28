#!/bin/sh
# finish_lightsail.sh
#
# Run this ONCE from the project root where .ssh/wingo_ec2 exists.
# It does everything needed on the instance:
#   1. write /opt/wingo-analyzer/.env from the keys you gave
#   2. chown/chmod that .env
#   3. rewrite /etc/systemd/system/wingo-analyzer.service to /opt/wingo-analyzer
#   4. systemctl daemon-reload + restart
#   5. print the first 40 journal lines
#
# No ssh key path, no host, no username is prompted - it is all baked in
# from the environment variables you set.
#
# If you want to use a different key or host, edit the variables below.

set -eu

SSH_KEY="${SSH_KEY:-$HOME/.ssh/wingo_ec2}"
HOST="${HOST:?Set HOST to the server hostname or IP}"
USER="ubuntu"
APP_DIR="/opt/wingo-analyzer"

SSH="ssh -F /dev/null -o StrictHostKeyChecking=no -o ConnectTimeout=25 -o BatchMode=yes -i $SSH_KEY $USER@$HOST"
SCP="scp -F /dev/null -o StrictHostKeyChecking=no -o ConnectTimeout=25 -o BatchMode=yes -i $SSH_KEY"

echo "==> target: $USER@$HOST:$APP_DIR"

# 1. write the env file on the instance
$SSH "sudo bash -c 'cat > $APP_DIR/.env <<EOF
GEMINI_API_KEY=${GEMINI_API_KEY:-}
DMFIRST_LOGIN=false

GROQ_API_KEY=${GROQ_API_KEY:-}

UPI_ID=${UPI_ID:-}
ADMIN_CODE=${ADMIN_CODE:-}

OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-}
EOF
chown $USER:$USER $APP_DIR/.env
chmod 600 $APP_DIR/.env
echo env_written && ls -la $APP_DIR/.env'"

# 2. rewrite the unit file to run from /opt/wingo-analyzer
$SSH "sudo tee /etc/systemd/system/wingo-analyzer.service >/dev/null <<EOF
[Unit]
Description=Wingo prediction analyzer for dmfirst1.com
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$APP_DIR
EnvironmentFile=-$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/python -m wingo_analyzer run

Restart=always
RestartSec=5
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
echo unit_written'"

# 3. reload and restart
$SSH "sudo systemctl daemon-reload && sudo systemctl restart wingo-analyzer && echo restarted"

# 4. show the first log lines
echo ""
echo "==> recent logs"
$SSH "sudo journalctl -u wingo-analyzer -n 40 --no-pager"

echo ""
echo "Done. If the service is not healthy, run on your machine:"
echo "  ssh -i $SSH_KEY $USER@$HOST 'sudo journalctl -u wingo-analyzer -f'"
