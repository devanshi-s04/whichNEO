#!/usr/bin/env bash
# Install WhichNEO as systemd --user services. No root required.
# Run from the repository root ON the host:  ./deploy/epyc_setup.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$APP_DIR/.venv"
PY="$VENV/bin/python3"
PORT="${WHICHNEO_PORT:-8080}"
UNIT_DIR="$HOME/.config/systemd/user"

echo "Installing WhichNEO from $APP_DIR (port $PORT)"

if [ ! -x "$PY" ]; then
  echo "==> creating virtualenv"
  python3 -m venv "$VENV"
fi
echo "==> installing dependencies"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo "==> running selftest"
( cd "$APP_DIR" && "$PY" selftest.py | tail -1 )

echo "==> priming the database (first cycle may take ~1 minute)"
( cd "$APP_DIR" && "$PY" update_neocp.py )

mkdir -p "$UNIT_DIR"

cat > "$UNIT_DIR/whichneo-update.service" <<EOF
[Unit]
Description=WhichNEO NEOCP update cycle (L01 Tican)
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$APP_DIR
ExecStart=$PY update_neocp.py
TimeoutStartSec=280
EOF

cat > "$UNIT_DIR/whichneo-update.timer" <<EOF
[Unit]
Description=Run the WhichNEO NEOCP update every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
AccuracySec=10s
Persistent=true

[Install]
WantedBy=default.target
EOF

cat > "$UNIT_DIR/whichneo-web.service" <<EOF
[Unit]
Description=WhichNEO observer board (L01 Tican)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
Environment=WHICHNEO_PORT=$PORT
ExecStart=$PY -m waitress --host=0.0.0.0 --port=$PORT --threads=8 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

echo "==> enabling services"
systemctl --user daemon-reload
systemctl --user enable --now whichneo-update.timer whichneo-web.service

# Without lingering, everything stops the moment you log out.
if ! loginctl show-user "$USER" 2>/dev/null | grep -q 'Linger=yes'; then
  echo "==> enabling lingering so it survives logout"
  loginctl enable-linger "$USER" || \
    echo "    could not enable lingering; services will stop at logout"
fi

echo
echo "Done. Check it:"
echo "  systemctl --user status whichneo-web.service"
echo "  systemctl --user list-timers whichneo-update.timer"
echo "  curl -s localhost:$PORT/status | python3 -m json.tool"
echo
echo "View it from your laptop:"
echo "  ssh -N -L $PORT:localhost:$PORT $USER@$(hostname -f 2>/dev/null || hostname)"
echo "  then open http://localhost:$PORT"
