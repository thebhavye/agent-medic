#!/usr/bin/env bash
# Provision the Agent Medic demo box (Ubuntu 24.04). Idempotent: safe to re-run.
# Usage: sudo GITHUB_TOKEN=<fine-grained-token> ./setup.sh   (token never printed)
#    or: sudo ./setup.sh                                      (uses an ssh deploy key)
set -euo pipefail

APP_DIR=/opt/agent-medic
REPO_URL=https://github.com/thebhavye/agent-medic
APP_USER=medic

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
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    git clone "https://x-access-token:${GITHUB_TOKEN}@github.com/thebhavye/agent-medic" "$APP_DIR"
  elif [ -d /root/.ssh ] && ssh -o BatchMode=yes -T git@github.com 2>&1 | grep -q "successfully authenticated"; then
    git clone git@github.com:thebhavye/agent-medic "$APP_DIR"
  else
    echo "repo is private: set GITHUB_TOKEN or add an ssh deploy key, then re-run." >&2
    exit 1
  fi
else
  echo "repo exists at $APP_DIR, updating to latest main."
  git -C "$APP_DIR" fetch origin
  git -C "$APP_DIR" pull --ff-only origin main || echo "pull skipped (local changes?); continuing."
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
systemctl enable medic-world-init medic-kb medic-traffic medic-watch medic-dashboard medic-chaos.timer

echo "setup done. Next: copy .env to $APP_DIR/.env (see README), then:"
echo "  systemctl start medic-kb medic-traffic medic-watch medic-dashboard"
