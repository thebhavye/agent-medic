#!/usr/bin/env bash
# Break the demo world on a schedule: restore calm, then apply one random scenario.
# Runs as the medic-chaos systemd service (see systemd/medic-chaos.*). Existing CLI only.
# Exit code: 0 only when both reset and scenario succeed; nonzero otherwise, so
# systemd and monitoring see failures. APP_DIR, MEDIC_PYTHON and CHAOS_INDEX may
# be overridden (tests mock the CLI this way).
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

APP_DIR="${APP_DIR:-/opt/agent-medic}"
LOG_FILE="$APP_DIR/.medic/logs/chaos.log"
PY="${MEDIC_PYTHON:-$APP_DIR/.venv/bin/python}"

mkdir -p "$(dirname "$LOG_FILE")"
stamp="$(date -u +%FT%TZ)"

if ! "$PY" -m medic.cli world chaos normal >>"$LOG_FILE" 2>&1; then
  rc=$?
  echo "== $stamp reset to normal FAILED (exit $rc); leaving the world as-is ==" >>"$LOG_FILE"
  exit 1
fi
echo "== $stamp chaos window (every ${INTERVAL_HOURS}h): restored normal ==" >>"$LOG_FILE"

pick="${SCENARIOS[${CHAOS_INDEX:-$((RANDOM % ${#SCENARIOS[@]}))}]}"
# shellcheck disable=SC2086
if ! $PY -m medic.cli $pick >>"$LOG_FILE" 2>&1; then
  rc=$?
  echo "== $stamp FAILED to apply: $pick (exit $rc; reset had succeeded, see above) ==" >>"$LOG_FILE"
  exit 1
fi
echo "== $stamp applied: $pick ==" >>"$LOG_FILE"
