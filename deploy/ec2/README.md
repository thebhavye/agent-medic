# Agent Medic on EC2 (24/7 demo)

One `t3.small` in `us-east-1` runs the whole world: knowledge-base API,
production traffic, Medic watching, the dashboard, and a chaos timer that
breaks something every 2 hours. **You create nothing here except the instance;
no AWS CLI needed, everything is console + copy-paste.** Models come from
Ollama Cloud (`OLLAMA_API_KEY` in `.env`); Bedrock is not used.

## Cost (t3.small, us-east-1)

| period | compute ($0.0208/h) | + 16 GB gp3 (~$1.28/mo) |
|---|---|---|
| hour | $0.02 | - |
| day | $0.50 | ~$0.54 |
| week | $3.50 | ~$3.80 |
| 30 days | $15.18 | ~$16.46 |

The $25 team budget covers ~7 weeks of one box. **Stop (or terminate) the
instance when the demo is over** — stopping halts compute charges (a stopped
box still pays pennies for the EBS volume; terminating pays nothing).

## 1. Launch the instance (console)

1. EC2 -> Launch instance. Name: `agent-medic-demo`.
2. AMI: Ubuntu Server 24.04 LTS (64-bit x86). Instance type: `t3.small`.
3. Key pair: create new (`agent-medic-key`, .pem), keep the file.
4. Network: VPC **`Hackathon_VPC`** (do not create a VPC), any subnet,
   auto-assign public IP enabled.
5. Storage: 16 GB gp3 (EBS must stay under 50 GB).
6. Security group: new `agent-medic-sg` with:
   - SSH (22) from **My IP** only.
   - Custom TCP **8790** (dashboard): My IP if only you watch, `0.0.0.0/0`
     if judges open it on their own laptops. Port 8790 serves a read-only
     status page; pick My IP when in doubt.
7. Launch. Note the public IP, e.g. `1.2.3.4` (use `EC2_IP` below).

## 2. Connect and provision

```bash
chmod 400 agent-medic-key.pem
ssh -i agent-medic-key.pem ubuntu@$EC2_IP
```

Copy the kit and run setup (from the laptop, repo root). With a token:

```bash
scp -i agent-medic-key.pem deploy/ec2/setup.sh ubuntu@$EC2_IP:/tmp/setup.sh
ssh -i agent-medic-key.pem ubuntu@$EC2_IP \
  'sudo GITHUB_TOKEN=<fine-grained-token-with-contents-read> bash /tmp/setup.sh'
```

The token is used once through a temporary credential helper that is deleted
immediately after cloning; it never appears in the clone URL, `.git/config`,
or logs. `origin` stays the plain public URL.

### 2b. Without a token: SSH deploy key (end-to-end)

The EC2 login PEM (`agent-medic-key.pem`) only logs you into the instance —
GitHub knows nothing about it, so it cannot clone the private repo. Create a
separate deploy key for repository access (on the laptop):

```bash
ssh-keygen -t ed25519 -f agent-medic-deploy -N "" -C "agent-medic-demo"
# add agent-medic-deploy.pub to the repo: GitHub -> thebhavye/agent-medic ->
# Settings -> Deploy keys -> Add deploy key (read access is enough)
```

Install the private half on the instance where `setup.sh` expects it
(`/root/.ssh/id_agent_medic`, `chmod 600`), plus GitHub's verified host key.
Get the fingerprints from GitHub's docs ("SSH key fingerprints") and confirm
each line below matches before trusting it:

```bash
scp -i agent-medic-key.pem deploy/ec2/setup.sh agent-medic-deploy ubuntu@$EC2_IP:/tmp/
ssh -i agent-medic-key.pem ubuntu@$EC2_IP
sudo mkdir -p /root/.ssh && sudo chmod 700 /root/.ssh
sudo install -m 600 -o root -g root /tmp/agent-medic-deploy /root/.ssh/id_agent_medic
ssh-keyscan -t rsa,ecdsa,ed25519 github.com > /tmp/gh.pub
ssh-keygen -lf /tmp/gh.pub   # compare against the fingerprints in GitHub's docs
cat /tmp/gh.pub | sudo tee -a /root/.ssh/known_hosts > /dev/null
sudo ssh -i /root/.ssh/id_agent_medic -o BatchMode=yes -T git@github.com
# expect "Hi thebhavye/...! You've successfully authenticated" (GitHub exits 1
# alongside that message; the message is what counts)
sudo bash /tmp/setup.sh
```

`setup.sh` without `GITHUB_TOKEN` clones with exactly that key
(`ssh -i /root/.ssh/id_agent_medic -o IdentitiesOnly=yes -o BatchMode=yes
-o StrictHostKeyChecking=yes`) and fails with an actionable error if the key
is missing or rejected, or if github.com is not in `known_hosts`. It never
trusts an unverified host key, never probes with `ssh -T` status codes and
never continues past a failed clone.

Re-running `setup.sh` later updates the repo with the same authentication:
pass `GITHUB_TOKEN` again if the box was set up with a token, or keep the
deploy key in place.

## 3. Copy `.env` (never commit it)

```bash
scp -i agent-medic-key.pem .env ubuntu@$EC2_IP:/tmp/medic.env
ssh -i agent-medic-key.pem ubuntu@$EC2_IP \
  'sudo install -m 600 -o medic -g medic /tmp/medic.env /opt/agent-medic/.env && rm /tmp/medic.env'
```

`.env` needs at least `OLLAMA_API_KEY`, `NEATLOGS_API_KEY`,
`NEATLOGS_TOKEN`, `NEATLOGS_PROJECT_ID`. (`setup.sh` does not touch `.env`.)

## 4. Start and watch

```bash
ssh -i agent-medic-key.pem ubuntu@$EC2_IP
sudo systemctl start medic-kb medic-traffic medic-watch medic-dashboard medic-chaos.timer
sudo /opt/agent-medic/deploy/ec2/status.sh
```

- Dashboard: `http://<EC2_IP>:8790` (give traffic + watch ~2 minutes first).
- Logs: `journalctl -u medic-watch -f`, or any unit in `status.sh`.
- Chaos log: `/opt/agent-medic/.medic/logs/chaos.log`. `setup.sh` only enables
  the timer; the start command above starts it, after `.env` is in place
  (starting it earlier could break the world before the services run). Then
  every 2 hours (`OnUnitActiveSec=2h` in `systemd/medic-chaos.timer`; on a
  box up for over 30 minutes the first break comes right away). Confirm with
  `systemctl list-timers medic-chaos.timer`. A failed chaos run exits nonzero,
  so it shows up in `journalctl -u medic-chaos` — a merely active timer does
  not mean chaos has run; check the log.

If `medic-dashboard` shows "skipped" the dashboard module is missing on this
branch — the other services are unaffected by design.

## Kit self-check (laptop, no AWS needed)

```bash
bash deploy/ec2/tests/test_kit.sh
```

It syntax-checks the scripts, verifies no credentials are embedded in Git
URLs, clones a throwaway local repo through the token helper to prove the
secret never lands in `.git/config`, exercises all four chaos/reset
failure combinations with a mocked CLI, and cross-checks service/timer
names across setup, README, and the unit files. It mocks `systemctl` and
the Medic CLI; first boot on EC2 still needs a smoke test (`status.sh`,
dashboard loads, one chaos entry in the log).

## 5. Stop to save credits

```bash
sudo systemctl stop 'medic-*'   # pause the demo, keep the box
```

- Short break: stop the instance in the console (compute charges halt).
- Done for good: **terminate** the instance (and delete the key pair) —
  a stopped box still bills ~$1.28/month for its volume.
