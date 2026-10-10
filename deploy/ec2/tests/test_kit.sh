#!/usr/bin/env bash
# Self-check for the EC2 deployment kit. Laptop only: no AWS, no network.
# Mocks destructive operations (git remotes stay local, the Medic CLI is faked).
# Usage: bash deploy/ec2/tests/test_kit.sh   (from the repo root)
set -u

KIT="$(cd "$(dirname "$0")/.." && pwd)"
PASS=0
FAIL=0

ok() { PASS=$((PASS + 1)); echo "ok: $1"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL: $1"; }
assert_eq() { [ "$2" = "$3" ] && ok "$1" || bad "$1 (want [$2], got [$3])"; }
assert_contains() { grep -qF "$2" "$3" && ok "$1" || bad "$1 (missing [$2] in $3)"; }
assert_not_contains() { grep -qF "$2" "$3" && bad "$1 (forbidden [$2] in $3)" || ok "$1"; }

echo "== syntax =="
for f in setup.sh chaos.sh status.sh; do
  if bash -n "$KIT/$f"; then ok "bash -n $f"; else bad "bash -n $f"; fi
done

echo "== token handling (static) =="
assert_not_contains "no token in clone URL" "x-access-token:" "$KIT/setup.sh"
assert_not_contains "no token interpolation into URL" 'GITHUB_TOKEN}@' "$KIT/setup.sh"
assert_contains "credential helper used" "credential.helper=" "$KIT/setup.sh"
assert_contains "helper cleaned by trap" "trap " "$KIT/setup.sh"
assert_contains "helper file locked down" "chmod 0600" "$KIT/setup.sh"
assert_contains "helper dir locked down" "chmod 0700" "$KIT/setup.sh"
assert_contains "origin verified token-free" 'remote get-url origin' "$KIT/setup.sh"

echo "== ssh handling (static) =="
assert_not_contains "no fragile ssh -T probe" "ssh -o BatchMode=yes -T" "$KIT/setup.sh"
assert_contains "deploy key path documented" "/root/.ssh/id_agent_medic" "$KIT/setup.sh"
assert_contains "ssh failure is actionable" "known_hosts (README step 2b)" "$KIT/setup.sh"

echo "== timer activation (static) =="
assert_contains "timer started at setup" "enable --now medic-chaos.timer" "$KIT/setup.sh"
assert_contains "README documents started timer" "enable --now medic-chaos.timer" "$KIT/README.md"
assert_contains "first run matches unit" "OnBootSec=30min" "$KIT/systemd/medic-chaos.timer"

echo "== token clone (functional, local repos only) =="
# shellcheck disable=SC1090
source "$KIT/setup.sh"
WORK="$(mktemp -d)"
BARE="$WORK/upstream.git"
git init -q --bare "$BARE"
export TMPDIR="$WORK/tmp"
mkdir -p "$TMPDIR"
if REPO_URL="$BARE" GITHUB_TOKEN=secret-token-xyz clone_with_token "$WORK/clone"; then
  ok "token clone succeeds"
else
  bad "token clone succeeds"
fi
remote="$(git -C "$WORK/clone" remote get-url origin)"
case "$remote" in *upstream.git) ok "origin points at the cloned repo";; *) bad "origin points at the cloned repo (got [$remote])";; esac
case "$remote" in *secret-token-xyz*) bad "origin remote is token-free";; *) ok "origin remote is token-free";; esac
if grep -rq "secret-token-xyz" "$WORK/clone/.git"; then bad "token absent from .git"; else ok "token absent from .git"; fi
if [ -z "$(ls -A "$TMPDIR")" ]; then ok "helper temp dir cleaned"; else bad "helper temp dir cleaned"; fi
if REPO_URL=/nonexistent/repo.git GITHUB_TOKEN=secret-token-xyz clone_with_token "$WORK/nope" 2>/dev/null; then
  bad "failed clone returns nonzero"
else
  ok "failed clone returns nonzero"
fi
if [ -z "$(ls -A "$TMPDIR")" ]; then ok "helper temp dir cleaned on failure"; else bad "helper temp dir cleaned on failure"; fi
if SSH_KEY=/nonexistent/key clone_with_ssh "$WORK/none" 2>/dev/null; then
  bad "missing key returns nonzero"
else
  [ $? -eq 2 ] && ok "missing key returns code 2" || bad "missing key returns code 2"
fi
rm -rf "$WORK"
unset TMPDIR

echo "== chaos/reset failure propagation (mocked CLI) =="
chaos_case() { # name reset_rc scenario_rc want_rc want_log... (log file assertions after)
  local name="$1" reset_rc="$2" scenario_rc="$3" want_rc="$4"; shift 4
  local dir fake log rc
  dir="$(mktemp -d)"
  fake="$dir/fake-py"
  printf '#!/usr/bin/env bash\nif [ "$5" = "normal" ]; then echo "reset ran" >> "%s/runs"; exit %s; fi\necho "scenario ran: $*" >> "%s/runs"; exit %s\n' \
    "$dir" "$reset_rc" "$dir" "$scenario_rc" > "$fake"
  chmod +x "$fake"
  if APP_DIR="$dir" MEDIC_PYTHON="$fake" CHAOS_INDEX=0 bash "$KIT/chaos.sh"; then rc=0; else rc=$?; fi
  log="$dir/.medic/logs/chaos.log"
  assert_eq "$name exit code" "$want_rc" "$rc"
  for want in "$@"; do assert_contains "$name log" "$want" "$log"; done
  echo "$name runs: $(cat "$dir/runs" 2>/dev/null | tr '\n' '|')"
  rm -rf "$dir"
}
chaos_case "reset+scenario ok" 0 0 0 "restored normal" "applied: world chaos schema_v2"
chaos_case "scenario fails" 0 1 1 "restored normal" "FAILED to apply"
chaos_case "reset fails" 1 0 1 "reset to normal FAILED"
chaos_case "both fail" 1 1 1 "reset to normal FAILED"
# A failed reset must never claim success and must not run the scenario.
dir="$(mktemp -d)"
fake="$dir/fake-py"
printf '#!/usr/bin/env bash\nif [ "$5" = "normal" ]; then exit 1; fi\necho "scenario ran" >> "%s/runs"; exit 0\n' "$dir" > "$fake"
chmod +x "$fake"
APP_DIR="$dir" MEDIC_PYTHON="$fake" CHAOS_INDEX=0 bash "$KIT/chaos.sh" 2>/dev/null || true
assert_not_contains "no success message after failed reset" "restored normal" "$dir/.medic/logs/chaos.log"
[ -f "$dir/runs" ] && bad "scenario skipped after failed reset" || ok "scenario skipped after failed reset"
rm -rf "$dir"

echo "== consistency across kit =="
for unit in medic-world-init medic-kb medic-traffic medic-watch medic-dashboard; do
  [ -f "$KIT/systemd/$unit.service" ] && ok "unit file $unit" || bad "unit file $unit"
  grep -q "$unit" "$KIT/setup.sh" && ok "setup references $unit" || bad "setup references $unit"
  grep -q "$unit" "$KIT/status.sh" && ok "status references $unit" || bad "status references $unit"
done
[ -f "$KIT/systemd/medic-chaos.timer" ] && ok "timer file" || bad "timer file"
grep -q "medic-chaos" "$KIT/status.sh" && ok "status references chaos" || bad "status references chaos"

echo
echo "pass=$PASS fail=$FAIL"
[ "$FAIL" -eq 0 ]
