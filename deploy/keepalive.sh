#!/usr/bin/env bash
# Restart the board if it is not answering.
#
# For hosts without systemd. run.sh supervises its own two children, but
# nothing supervises run.sh -- so a hard crash or a container restart leaves
# the board down until a human notices. Install from the repo root:
#
#   ./deploy/install_keepalive.sh
#
# which adds a @reboot entry and a check every few minutes.
set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${WHICHNEO_PORT:-12600}"
LOCK="$APP_DIR/data/keepalive.lock"
LOG="$APP_DIR/data/keepalive.log"

mkdir -p "$APP_DIR/data"

# Only one keepalive at a time, or a slow start spawns duplicates.
exec 9>"$LOCK"
flock -n 9 || exit 0

say() { echo "$(date -u '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

if curl -fsS --max-time 10 -o /dev/null "http://127.0.0.1:$PORT/status" 2>/dev/null; then
  exit 0     # healthy, nothing to do
fi

# Not answering. Clear out any half-dead remnants before restarting, or the
# new instance will fail to bind.
say "board not answering on $PORT — restarting"
pkill -u "$(id -u)" -f "waitress --host=.* --port=$PORT" 2>/dev/null
pkill -u "$(id -u)" -f "$APP_DIR/run.sh" 2>/dev/null
sleep 2

cd "$APP_DIR" || exit 1
WHICHNEO_PORT="$PORT" WHICHNEO_HOST="${WHICHNEO_HOST:-0.0.0.0}" \
  nohup ./run.sh >> "$APP_DIR/data/server.log" 2>&1 &

sleep 10
if curl -fsS --max-time 10 -o /dev/null "http://127.0.0.1:$PORT/status" 2>/dev/null; then
  say "restarted successfully"
else
  say "RESTART FAILED — see data/server.log"
fi
