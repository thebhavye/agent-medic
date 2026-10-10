#!/usr/bin/env bash
# One screen of Agent Medic health on the EC2 instance.
set -u

APP_DIR=/opt/agent-medic
PY="$APP_DIR/.venv/bin/python"
SERVICES="medic-world-init medic-kb medic-traffic medic-watch medic-dashboard medic-chaos"

echo "=== services ==="
for svc in $SERVICES; do
  printf '%-20s %s\n' "$svc" "$(systemctl is-active "$svc" 2>/dev/null || echo unknown)"
done
echo
echo "=== timers ==="
systemctl list-timers medic-chaos.timer --no-pager 2>/dev/null || true
echo
echo "=== deployed release ==="
git -C "$APP_DIR/.medic/deploy" log -1 --oneline 2>/dev/null || echo "no deploy yet"
echo
echo "=== incidents ==="
(cd "$APP_DIR" && "$PY" -m medic.cli incidents 2>/dev/null) || echo "incidents unavailable"
for svc in $SERVICES; do
  echo
  echo "=== journal: $svc (last 20) ==="
  journalctl -u "$svc" -n 20 --no-pager 2>/dev/null || echo "no journal"
done
