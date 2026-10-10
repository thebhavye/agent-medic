#!/usr/bin/env bash
# Break the demo world on a schedule: restore calm, then apply one random scenario.
# Runs as the medic-chaos systemd service (see systemd/medic-chaos.*). Existing CLI only.
set -u

# --- knobs (the every-N-hours interval itself lives in systemd/medic-chaos.timer) ---
INTERVAL_HOURS=2
SCENARIOS=(
  "world chaos schema_v2"
  "world chaos outage"
  "world chaos stale_data"
  "world release short-params"
  "world release double-check"
  "world release cap-docs"
)

APP_DIR=/opt/agent-medic
LOG_FILE="$APP_DIR/.medic/logs/chaos.log"
PY="$APP_DIR/.venv/bin/python"

mkdir -p "$(dirname "$LOG_FILE")"
stamp="$(date -u +%FT%TZ)"

"$PY" -m medic.cli world chaos normal >>"$LOG_FILE" 2>&1
echo "== $stamp chaos window (every ${INTERVAL_HOURS}h): restored normal ==" >>"$LOG_FILE"

pick="${SCENARIOS[$((RANDOM % ${#SCENARIOS[@]}))]}"
# shellcheck disable=SC2086
if $PY -m medic.cli $pick >>"$LOG_FILE" 2>&1; then
  echo "== $stamp applied: $pick ==" >>"$LOG_FILE"
else
  echo "== $stamp FAILED to apply: $pick (see above) ==" >>"$LOG_FILE"
fi
