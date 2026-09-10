#!/usr/bin/env bash
# Start the updater loop and the web server together, and stop both if
# either dies -- a board serving silently stale data is worse than one that
# is visibly down.
set -euo pipefail

PORT="${WHICHNEO_PORT:-8080}"
HOST="${WHICHNEO_HOST:-0.0.0.0}"

python3 update_neocp.py --loop &
UPDATER=$!

# waitress rather than gunicorn: pure Python, so the same command works on
# the observatory's Windows machine as on Linux.
python3 -m waitress --host="$HOST" --port="$PORT" --threads=8 app:app &
WEB=$!

trap 'kill $UPDATER $WEB 2>/dev/null || true' INT TERM
wait -n $UPDATER $WEB
kill $UPDATER $WEB 2>/dev/null || true
wait || true
