#!/usr/bin/env bash
# Run this ON epyc (or any candidate host) before installing anything.
# It answers, in one go, whether the machine can actually host WhichNEO.
# Read-only: checks things, changes nothing.

echo "WhichNEO preflight — $(hostname)"
echo "================================================"

ok()   { printf '  \033[32mOK\033[0m    %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAILED=1; }
warn() { printf '  \033[33m?\033[0m     %s\n' "$1"; }

FAILED=0

echo
echo "Python"
if command -v python3 >/dev/null; then
  V=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
  MAJ=${V%%.*}; MIN=${V##*.}
  if [ "$MAJ" -ge 3 ] && [ "$MIN" -ge 9 ]; then ok "python3 $V"
  else bad "python3 $V — need 3.9+ (zoneinfo for local time)"; fi
else bad "no python3 on PATH"; fi
python3 -m venv --help >/dev/null 2>&1 && ok "venv module present" \
  || bad "python3-venv missing — cannot make an isolated environment"

echo
echo "Timezone data (the board renders Visnjan local time)"
python3 - <<'PY' 2>/dev/null && ok "Europe/Zagreb resolves" || bad "no tzdata — times would fall back to UTC (pip install tzdata)"
from zoneinfo import ZoneInfo
ZoneInfo("Europe/Zagreb")
PY

echo
echo "Outbound access to MPC"
for U in https://www.minorplanetcenter.net/iau/NEO/neocp.txt \
         https://cgi.minorplanetcenter.net/cgi-bin/confirmeph2.cgi; do
  C=$(curl -s -o /dev/null -w '%{http_code}' --max-time 25 "$U" 2>/dev/null)
  [ "$C" = "200" ] && ok "$C  $U" || bad "$C  $U — blocked or proxied?"
done

echo
echo "Keeping it running"
if command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
  ok "systemd --user available"
  if loginctl show-user "$USER" 2>/dev/null | grep -q 'Linger=yes'; then
    ok "lingering already enabled (survives logout)"
  else
    warn "lingering off — run: loginctl enable-linger \$USER"
    warn "  without it, services stop when you log out"
  fi
else
  warn "no systemd --user — fall back to 'nohup ./run.sh &' or a @reboot cron"
fi

echo
echo "Disk"
AVAIL=$(df -Pk "$HOME" | awk 'NR==2{print int($4/1024)}')
[ "${AVAIL:-0}" -gt 500 ] && ok "${AVAIL} MB free in \$HOME (need ~200)" \
  || bad "only ${AVAIL} MB free in \$HOME — need ~200"
command -v quota >/dev/null && quota -s 2>/dev/null | tail -2

echo
echo "Port"
PORT=${WHICHNEO_PORT:-8080}
if command -v ss >/dev/null && ss -ltn 2>/dev/null | grep -q ":$PORT "; then
  bad "port $PORT already in use — pick another with WHICHNEO_PORT"
else
  ok "port $PORT looks free"
fi

echo
echo "================================================"
if [ "${FAILED:-0}" = "1" ]; then
  echo "Some checks FAILED — fix those before installing."
  exit 1
fi
echo "Preflight passed. Proceed with deploy/epyc_setup.sh"
echo
echo "Note: this cannot tell you whether the host reaps long-running user"
echo "processes, or whether a port can be exposed publicly. Both are"
echo "questions for the sysadmins — see deploy/EPYC.md."
