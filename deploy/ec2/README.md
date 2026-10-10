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

Copy the kit and run setup (from the laptop, repo root):

```bash
scp -i agent-medic-key.pem deploy/ec2/setup.sh ubuntu@$EC2_IP:/tmp/setup.sh
ssh -i agent-medic-key.pem ubuntu@$EC2_IP \
  'sudo GITHUB_TOKEN=<fine-grained-token-with-contents-read> bash /tmp/setup.sh'
```

No token? Add an ssh deploy key to the repo (Settings -> Deploy keys) instead:
`ssh -i agent-medic-key.pem ubuntu@$EC2_IP 'sudo bash /tmp/setup.sh'`.
The token is never printed or stored; it lives only in that one command line.

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
sudo systemctl start medic-kb medic-traffic medic-watch medic-dashboard
sudo /opt/agent-medic/deploy/ec2/status.sh
```

- Dashboard: `http://<EC2_IP>:8790` (give traffic + watch ~2 minutes first).
- Logs: `journalctl -u medic-watch -f`, or any unit in `status.sh`.
- Chaos log: `/opt/agent-medic/.medic/logs/chaos.log` (first break ~30 min
  after boot, then every 2 hours; `OnUnitActiveSec=2h` in
  `systemd/medic-chaos.timer`).

If `medic-dashboard` shows "skipped" the dashboard module is missing on this
branch — the other services are unaffected by design.

## 5. Stop to save credits

```bash
sudo systemctl stop 'medic-*'   # pause the demo, keep the box
```

- Short break: stop the instance in the console (compute charges halt).
- Done for good: **terminate** the instance (and delete the key pair) —
  a stopped box still bills ~$1.28/month for its volume.
