#!/bin/sh
# Push the shipped WINGO code to a Lightsail instance and (re)start the service.
#
# This is the handoff script for:
#   "ship the previous model codes to light sailagent"
#
# It does NOT invent a remote host or SSH key. You must tell it where to go.
# Run it like:
#
#   sh ./deploy/ship_to_lightsail.sh \
#     -i ~/.ssh/lightsail_key \
#     -h <lightsail-ip> \
#     [-u ubuntu] \
#     [-d /opt/wingo-analyzer]
#
# What it does once the connection works:
#   1. packs the project (excluding .venv, .git, logs, data, snapshots,
#      .env*, *.db, node_modules, __pycache__, playwright browsers, dist)
#   2. uploads that tarball to the instance
#   3. unpacks it into the target app dir
#   4. (optionally) copies a local .env.local over the remote .env if you
#      pass -e .env.local
#   5. runs the Lightsail installer from the remote app dir
#   6. restarts the systemd unit
#   7. tails the first 40 log lines
#
# It never prints key material. It only prints key *names* it tries to use.

set -eu

KEY=""
HOST=""
USER="ubuntu"
APP_DIR="/opt/wingo-analyzer"
ENV_SRC=""

usage() {
  printf "%s\n" \
    "Usage: sh ./deploy/ship_to_lightsail.sh [options]" \
    "" \
    "Options:" \
    "  -i PATH   SSH private key (required)" \
    "  -h HOST   Lightsail instance IP or hostname (required)" \
    "  -u USER   remote user (default: ubuntu)" \
    "  -d DIR    remote app dir (default: /opt/wingo-analyzer)" \
    "  -e PATH   local env file to upload as remote .env (optional)" \
    "" \
    "Example:" \
    "  sh ./deploy/ship_to_lightsail.sh -i ~/.ssh/lightsail_key -h 203.0.113.5"
  exit 1
}

while getopts "i:h:u:d:e:" opt; do
  case "$opt" in
    i) KEY="$OPTARG" ;;
    h) HOST="$OPTARG" ;;
    u) USER="$OPTARG" ;;
    d) APP_DIR="$OPTARG" ;;
    e) ENV_SRC="$OPTARG" ;;
    *) usage ;;
  esac
done

if [ -z "$KEY" ]; then
  echo "!!  missing -i <ssh-key>" >&2
  usage
fi

if [ -z "$HOST" ]; then
  echo "!!  missing -h <lightsail-ip>" >&2
  usage
fi

if [ ! -f "$KEY" ]; then
  echo "!!  ssh key not found: $KEY" >&2
  exit 1
fi

chmod 600 "$KEY"

PROJECT_ROOT="$(pwd)"
PAYLOAD="/tmp/wingo-ship-$$.tgz"

cleanup() {
  rm -f "$PAYLOAD"
}
trap cleanup EXIT

echo "==> packing project from $PROJECT_ROOT"
tar czf "$PAYLOAD" \
  --exclude='.venv' \
  --exclude='.git' \
  --exclude='*.db' \
  --exclude='*.db-wal' \
  --exclude='*.db-shm' \
  --exclude='*.log' \
  --exclude='logs' \
  --exclude='data' \
  --exclude='snapshots' \
  --exclude='.playwright-browsers' \
  --exclude='.env' \
  --exclude='.env.local' \
  --exclude='.env.production' \
  --exclude='node_modules' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='*.pyo' \
  --exclude='.pytest_cache' \
  --exclude='.mypy_cache' \
  --exclude='*.egg-info' \
  --exclude='dist' \
  -C "$PROJECT_ROOT" .

echo "    payload: $(du -h "$PAYLOAD" | cut -f1)"

echo "==> uploading to $USER@$HOST:$APP_DIR/ (creating dir if needed)"
ssh -i "$KEY" -o "StrictHostKeyChecking=no" -o "ConnectTimeout=20" \
  "$USER@$HOST" "mkdir -p '$APP_DIR'" || {
  echo "!!  cannot reach $USER@$HOST with key $KEY" >&2
  exit 1
}

scp -i "$KEY" -o "StrictHostKeyChecking=no" -o "ConnectTimeout=20" \
  "$PAYLOAD" "$USER@$HOST:$APP_DIR/payload.tgz" || {
  echo "!!  upload failed" >&2
  exit 1
}

if [ -n "$ENV_SRC" ] && [ -f "$ENV_SRC" ]; then
  echo "==> uploading env file $ENV_SRC -> $APP_DIR/.env"
  scp -i "$KEY" -o "StrictHostKeyChecking=no" -o "ConnectTimeout=20" \
    "$ENV_SRC" "$USER@$HOST:$APP_DIR/.env" || {
    echo "!!  env upload failed (non-fatal)" >&2
  }
fi

echo "==> running install on the instance"
ssh -i "$KEY" -o "StrictHostKeyChecking=no" -o "ConnectTimeout=20" \
  "$USER@$HOST" "cd '$APP_DIR' && sh ./deploy/install-lightsail.sh" || {
  echo "!!  install script failed on the instance" >&2
  echo "    tail the logs with:" >&2
  echo "      ssh -i $KEY $USER@$HOST journalctl -u wingo-analyzer -n 60 --no-pager" >&2
  exit 1
}

echo "==> restarting the service"
ssh -i "$KEY" -o "StrictHostKeyChecking=no" -o "ConnectTimeout=20" \
  "$USER@$HOST" "sudo systemctl restart wingo-analyzer" || {
  echo "!!  restart command failed" >&2
}

echo "==> recent logs"
ssh -i "$KEY" -o "StrictHostKeyChecking=no" -o "ConnectTimeout=20" \
  "$USER@$HOST" "sudo journalctl -u wingo-analyzer -n 40 --no-pager" || true

echo ""
echo "Done. If the service is not healthy, run on the instance:"
echo "  sudo journalctl -u wingo-analyzer -f"
echo "  sudo systemctl status wingo-analyzer"
