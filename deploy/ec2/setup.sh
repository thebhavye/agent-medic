#!/usr/bin/env bash
# Provision the Agent Medic demo box (Ubuntu 24.04). Idempotent: safe to re-run.
# Usage: sudo GITHUB_TOKEN=<fine-grained-token> ./setup.sh   (token never printed)
#    or: sudo ./setup.sh                                      (uses the ssh deploy key
#        at /root/.ssh/id_agent_medic; see README for installing it)
set -euo pipefail

APP_DIR=/opt/agent-medic
REPO_URL=https://github.com/thebhavye/agent-medic
REPO_SSH_URL=git@github.com:thebhavye/agent-medic.git
SSH_KEY=/root/.ssh/id_agent_medic
APP_USER=medic


# Run one git command with the token, persisting none of it: git gets the
# credential from a temporary helper script (owner-only, executable, removed
# afterwards). The script reads the token from the environment, so the token is
# never written to disk, the clone URL, or .git/config. Never echo the token or
# enable tracing here.
git_with_token() {
  local cred_dir helper rc
  cred_dir="$(mktemp -d)" || return 1
  chmod 0700 "$cred_dir"
  helper="$cred_dir/helper.sh"
  printf '#!/bin/sh\nprintf "username=%%s\\n" "x-access-token"\nprintf "password=%%s\\n" "$MEDIC_GIT_TOKEN"\n' > "$helper"
  chmod 0700 "$helper"                       # git executes the helper, so it needs +x
  trap 'rm -rf "$cred_dir"' INT TERM
  MEDIC_GIT_TOKEN="$GITHUB_TOKEN" git -c credential.helper= -c "credential.helper=$helper" "$@"
  rc=$?
  trap - INT TERM
  rm -rf "$cred_dir"
  return "$rc"
}


# Run one git command over SSH with the dedicated deploy key. GitHub's host key
# must already be in /root/.ssh/known_hosts (README step 2b): an unknown host
# stops here instead of being trusted on first use. Returns 2 without the key.
git_with_ssh() {
  [ -f "$SSH_KEY" ] || return 2
  GIT_SSH_COMMAND="ssh -i $SSH_KEY -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes" \
    git "$@"
}


# Every command that talks to GitHub (clone, fetch, pull) goes through here, so
# re-running setup on an existing box authenticates the same way the clone did.
git_auth() {
  if [ -n "${GITHUB_TOKEN:-}" ]; then git_with_token "$@"; else git_with_ssh "$@"; fi
}


clone_with_token() { git_with_token clone "$REPO_URL" "$1"; }
clone_with_ssh() { git_with_ssh clone "$REPO_SSH_URL" "$1"; }


clone_repo() {
  local dest="$1"
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    clone_with_token "$dest" || {
      echo "clone with GITHUB_TOKEN failed; check the token's expiry and repo access." >&2
      return 1
    }
    # Belt and braces: origin must be exactly the public URL, never credential-bearing.
    [ "$(git -C "$dest" remote get-url origin)" = "$REPO_URL" ] || {
      echo "refusing to keep an unexpected origin remote." >&2
      return 1
    }
  else
    # No token: the deploy key must already be installed (README step 2b).
    [ -f "$SSH_KEY" ] || {
      echo "no GITHUB_TOKEN and no ssh key at $SSH_KEY." >&2
      echo "Either re-run with GITHUB_TOKEN set, or install the deploy key per README step 2b." >&2
      return 1
    }
    clone_with_ssh "$dest" || {
      echo "ssh clone failed; check the deploy key is registered on the repo and" >&2
      echo "github.com is in /root/.ssh/known_hosts (README step 2b)." >&2
      return 1
    }
  fi
}


# shellcheck disable=SC1090
if [ "${BASH_SOURCE[0]:-$0}" != "$0" ]; then
  return 0 2>/dev/null  # sourced by tests: helpers above are loaded, nothing runs
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root: sudo $0" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3.12 python3.12-venv curl > /dev/null

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  install -m 0755 /root/.local/bin/uv /usr/local/bin/uv
fi

if [ ! -d "$APP_DIR/.git" ]; then
  clone_repo "$APP_DIR"
else
  echo "repo exists at $APP_DIR, updating to latest main."
  git_auth -C "$APP_DIR" fetch origin || {
    echo "fetch failed: re-run with the same auth the box was set up with (GITHUB_TOKEN or the deploy key)." >&2
    exit 1
  }
  git -C "$APP_DIR" merge --ff-only origin/main || echo "update skipped (local changes?); continuing."
fi

if ! id -u "$APP_USER" >/dev/null 2>&1; then
  useradd -r -m -s /usr/sbin/nologin "$APP_USER"
fi

if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  uv venv "$APP_DIR/.venv" --python 3.12
fi
uv pip install --python "$APP_DIR/.venv/bin/python" -e "$APP_DIR"

mkdir -p "$APP_DIR/.medic/logs"
chown -R "$APP_USER:$APP_USER" "$APP_DIR/.medic"

chmod +x "$APP_DIR/deploy/ec2/chaos.sh" "$APP_DIR/deploy/ec2/status.sh"
cp "$APP_DIR/deploy/ec2/systemd/"*.service "$APP_DIR/deploy/ec2/systemd/"*.timer /etc/systemd/system/
systemctl daemon-reload
# Enable only: nothing starts until .env is in place. In particular the chaos
# timer must not start here; on a box up for over 30 minutes OnBootSec has
# already passed, so it would break the world before the services exist.
systemctl enable medic-world-init medic-kb medic-traffic medic-watch medic-dashboard medic-chaos.timer

echo "setup done. Next: copy .env to $APP_DIR/.env (see README), then:"
echo "  systemctl start medic-kb medic-traffic medic-watch medic-dashboard medic-chaos.timer"
