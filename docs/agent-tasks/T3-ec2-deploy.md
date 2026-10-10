# T3: EC2 deployment kit (run Agent Medic 24/7)

**Goal:** scripts and docs to run the whole demo world on one EC2 instance so judges can see it
live: the knowledge-base API, production traffic, Agent Medic watching, the dashboard, and a
chaos schedule that breaks something every so often. **Don't create any AWS resources.** Write
the kit; the founder runs it.

**You own:** `deploy/ec2/` (new directory). Task name for the PR branch: `ec2-deploy`.

## Hard constraints (the hackathon AWS account)

- Region **us-east-1** only. Allowed services: EC2, Lambda, IAM roles, load balancers.
- Use the pre-provisioned **`Hackathon_VPC`**; creating a VPC isn't allowed.
- **One** EC2 instance for the whole team; allowed types t2/t3/t3a nano..large. Recommend
  **t3.small**, Ubuntu 24.04 LTS, 16 GB gp3 (EBS must be ≤ 50 GB).
- Total budget **$25** for the team. Show hourly/daily/weekly cost and remind to stop it.
- Bedrock isn't allowed; models come from Ollama Cloud via `OLLAMA_API_KEY` in `.env`.

## What to build

1. `deploy/ec2/README.md`: console-only steps (no AWS CLI assumed): launch the instance in
   `Hackathon_VPC`, key pair, security group (SSH from "My IP" only; port 8790 for the
   dashboard, documented choice of My IP vs anywhere), connect, run `setup.sh`, copy `.env`
   with `scp` from the laptop (never commit it), check status, read logs, and **stop or
   terminate** to save credits. Cost table for t3.small.
2. `deploy/ec2/setup.sh` (idempotent, Ubuntu): installs git, Python 3.12 and `uv`; clones
   `https://github.com/thebhavye/agent-medic` into `/opt/agent-medic` (the repo is private:
   support a fine-grained GitHub token in `GITHUB_TOKEN` or a deploy key; document both; never
   echo the token); creates the venv; `uv pip install -e .`; creates a non-root `medic` user
   and `/opt/agent-medic/.medic`; installs and enables the systemd units below.
3. `deploy/ec2/systemd/` units, each `Restart=always`, `User=medic`,
   `WorkingDirectory=/opt/agent-medic`, `EnvironmentFile=/opt/agent-medic/.env`, logs to
   journald:
   - `medic-world-init.service` (oneshot): `python -m medic.cli world init` only if
     `/opt/agent-medic/.medic/deploy` doesn't exist.
   - `medic-kb.service`: `python -m medic.cli kb --port 8781`.
   - `medic-traffic.service` (after kb and world-init): `python -m medic.cli traffic
     --interval 20 --size 3`, with `Environment=LLM_PROVIDER=ollama_cloud`.
   - `medic-watch.service` (after traffic): `python -m medic.cli watch --interval 20`, with
     `LLM_PROVIDER=ollama_cloud`, `FIXER_PROVIDER=ollama_cloud`, `MEDIC_CANARY_INTERVAL_S=600`.
   - `medic-dashboard.service`: `python -m medic.dashboard --port 8790` (module comes from
     task T1; the unit must not break the others if it's missing).
4. `deploy/ec2/chaos.sh` + `medic-chaos.service` + `medic-chaos.timer`: every N hours (default
   2), restore `python -m medic.cli world chaos normal`, then pick one scenario at random from a
   configurable list and apply it with the existing CLI: `world chaos schema_v2|outage|stale_data`
   or `world release short-params|double-check|cap-docs`. Append what it did and when to
   `/opt/agent-medic/.medic/logs/chaos.log`. Scenario list and interval at the top.
5. `deploy/ec2/status.sh`: one screen with each service's state, the deployed release
   (`git -C .medic/deploy log -1 --oneline`), `python -m medic.cli incidents`, and the last 20
   lines of each service's journal.

## Acceptance

- `bash -n` passes on every script. Run `systemd-analyze verify` on the units if you can, or
  say you couldn't (you're on Windows).
- The README is a copy-pasteable path from "no instance" to "dashboard at http://<ip>:8790"
  and back to "terminated".
- No secrets anywhere in the kit. PR via the shared rules' worktree procedure.
