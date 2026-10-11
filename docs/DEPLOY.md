# Run Medic on a server for your app

[QUICKSTART.md](QUICKSTART.md) gets Medic watching your app from your own machine. This page
covers a server that keeps watching 24/7 (an EC2 `t3.small` in the walkthrough, but any small
Ubuntu box works). With HTTPS, your production servers can fetch first aid, and neatlogs can
wake Medic through a webhook.

To run the self-contained **demo world** on EC2 (knowledge-base API, traffic and a chaos timer),
see [deploy/ec2/README.md](../deploy/ec2/README.md) instead.

## 1. The machine

- Ubuntu Server 24.04, `t3.small` is plenty (about $15 a month on demand).
- Security group: SSH (22) from your IP; **80 and 443** from anywhere. Caddy needs 80 to obtain
  the certificate and 443 to serve it.
- A host name pointing at it. No domain? `<public-ip>.sslip.io` resolves to the IP and gets a
  real certificate, for example `3.91.10.20.sslip.io`.

## 2. Install

```bash
sudo apt update && sudo apt install -y git pipx python3-venv gh caddy
pipx ensurepath && exec "$SHELL" -l
pipx install "agent-medic @ git+https://github.com/thebhavye/agent-medic.git"
gh auth login                 # fix PRs go to your repo with this account
```

## 3. Your app's checkout

Medic reads, bisects and fixes this checkout. Your production stays where it is.

```bash
git clone https://github.com/<you>/<your-app>.git ~/app && cd ~/app
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Create `~/app/.env` with the keys from the quickstart (neatlogs, fixer model), plus two secrets
for remote first aid:

```bash
echo "MEDIC_RUNTIME_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')" >> .env
echo "MEDIC_CONTROL_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')" >> .env
```

Then check everything:

```bash
agent-medic doctor
```

## 4. Keep Medic running (systemd)

The units assume user `ubuntu` and the checkout in `/home/ubuntu/app`. Edit them if yours differ.

```bash
sudo cp deploy/project/medic-watch.service deploy/project/medic-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now medic-watch medic-dashboard
journalctl -u medic-watch -f          # Medic's log
```

`pipx` doesn't install these files: copy them from the `deploy/project/` folder of the agent-medic
repository (`git clone` it next to your app, or copy the three small files by hand).
The dashboard listens on `127.0.0.1:8790` only. Caddy is the one door in.

## 5. HTTPS (Caddy)

Put your host name in `deploy/project/Caddyfile`, then:

```bash
sudo cp deploy/project/Caddyfile /etc/caddy/Caddyfile && sudo systemctl reload caddy
```

`https://<your-host>/` now shows the dashboard with a valid certificate.

## 6. Let neatlogs wake Medic

```bash
cd ~/app && agent-medic neatlogs setup --webhook-url https://<your-host>/hooks/neatlogs
```

Without a webhook, Medic reads neatlogs' alert history on every check, which also works, just a
little later.

## 7. Point production at Medic

On every server or container that runs your app, set:

```
GIT_SHA=<the deployed commit>
MEDIC_ENV=prod
MEDIC_CONTROL=https://<your-host>/control/first_aid.json
MEDIC_STANDBY=https://<your-host>/control/standby.json
MEDIC_RUNTIME_KEY=<same value as on the Medic server>
MEDIC_CONTROL_TOKEN=<same value as on the Medic server>
MEDIC_RUNTIME_STATE=/tmp/medic-runtime-state.json      # shared by the processes on one machine
```

- Bundles are signed with `MEDIC_RUNTIME_KEY` (never sent anywhere), bound to one release, and
  they expire.
- The dashboard serves them only with `MEDIC_CONTROL_TOKEN`; without one configured, the endpoints
  don't exist.
- A fetch is capped at 3 seconds and cached. If Medic is unreachable, your app runs as written.
- `MEDIC_RUNTIME_STATE` lets every worker on a machine (gunicorn workers, serverless warm
  instances) count failures together, so the runtime engages the last good version within a few
  failed requests even when no single worker sees enough of them.

## Operating it

| | |
|---|---|
| Is it running? | `systemctl status medic-watch medic-dashboard` |
| What is it doing? | `journalctl -u medic-watch -f`, or the dashboard |
| Open incidents | `cd ~/app && agent-medic incidents` |
| Open a prepared PR (`pr = "ready"`) | `cd ~/app && agent-medic approve <incident>` |
| Update Medic | `pipx upgrade agent-medic && sudo systemctl restart medic-watch medic-dashboard` |
| Stop paying | stop the instance; terminate it when you're done |
