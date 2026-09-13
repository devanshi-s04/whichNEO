#!/usr/bin/env bash
# Install the keepalive watchdog into the user crontab. Idempotent.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${WHICHNEO_PORT:-12600}"
TAG="# whichneo-keepalive"
KEEPALIVE="$APP_DIR/deploy/keepalive.sh"

chmod +x "$KEEPALIVE" "$APP_DIR/run.sh"

# Drop any previous entries, then add the current ones.
existing="$(crontab -l 2>/dev/null | grep -v "$TAG" || true)"
{
  [ -n "$existing" ] && printf '%s\n' "$existing"
  echo "@reboot WHICHNEO_PORT=$PORT $KEEPALIVE $TAG"
  echo "*/3 * * * * WHICHNEO_PORT=$PORT $KEEPALIVE $TAG"
} | crontab -

echo "Installed:"
crontab -l | grep "$TAG" | sed 's/^/  /'
echo
echo "Checks every 3 minutes and on boot. It only acts when /status stops"
echo "answering, so a healthy board is untouched."
echo "Log: $APP_DIR/data/keepalive.log"
