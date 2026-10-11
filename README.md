# Agent Medic

neatlogs finds what broke in your AI agent and why. Medic heals it.

Medic watches a production agent through its neatlogs traces and alerts. When answers start
failing it does three things:

1. **First aid.** Within minutes, users get answers again: the last known good version of the
   broken part (a prompt, a retrieval function, a tool) is served from a signed bundle while
   the cause is found. With armed mode on, the runtime engages it by itself.
2. **The cure.** Medic finds the commit that caused it (bisection on real traces), reads why
   that change was made from its Entire checkpoint, writes a fix that keeps that intent and
   proves it on known-answer questions. The fix arrives as a pull request with the evidence.
3. **Immunity.** Once the merge is deployed, Medic confirms the heal on production traces of
   the release that contains it, then removes first aid. If the fix doesn't hold, it opens a
   revert PR. It writes what it found and did back onto the neatlogs triage card.

```
neatlogs alert / trace drift ──► first aid (minutes) ──► bisect + Entire intent ──► fix PR
                                                                                    │ merged
        revert PR ◄── regressed ── confirm on production traces of the fixed release ◄┘
```

## Use it on your agent

Your agent is any Python function that answers one question. A RAG app, a support bot, a
tool-using agent: `answer(question) -> str`.

```bash
pipx install "agent-medic @ git+https://github.com/thebhavye/agent-medic.git"   # on your PATH everywhere
cd your-app
agent-medic init               # dry run: shows the diff it would make
agent-medic init --write
```

`init` reads your code and finds the function that answers questions, the parts it calls on
the way to the model (retrieval, prompt building, tools) and the settings on that path
(prompt templates, model names, relevance cutoffs, top-k). It then makes one reviewable change:

- decorators so Medic can swap each part for its last good version: `@medic.entrypoint`,
  `@medic.surface("query_data.retrieve")`, `medic.value("query_data.min_relevance", MIN_RELEVANCE)`
  (names carry the module, so two modules' `search` never share first aid)
- `neatlogs.init(...)` tagged with the environment and release, if you weren't tracing yet
- a `medic.toml` with what it found, including your virtualenv and branch
- `.medic/` in `.gitignore`, and `agent-medic` + `neatlogs` in `requirements.txt`

If your app is a script with no function to call, `init` prints a prompt to give your coding
agent ("extract an `answer(question)` function from main") and you run it again.

Then:

```bash
agent-medic evals bootstrap --questions questions.txt   # a new app: a few real questions
agent-medic evals bootstrap                             # an app with traffic: from healthy traces
agent-medic neatlogs setup                              # detections + alert rules in your project
agent-medic doctor                                      # checks the setup, says what to fix
agent-medic start --dashboard                           # Medic in the background, dashboard on :8790
agent-medic status                                      # running? any open incident?
agent-medic stop                                        # stops everything it started
```

Run these from your app's folder. Keys go in your app's `.env` (`NEATLOGS_API_KEY`, the model
key for Medic's fixer); `agent-medic logs` shows the end of Medic's log, and `agent-medic watch`
runs it in the foreground instead.

Nobody writes tests here. `evals bootstrap` has your current code answer each question five
times and keeps the facts every answer agrees on (names, numbers, quoted terms) as the expected
answer. A later release that loses them fails the check. An open question with several right
answers ("What does the Caterpillar ask?") accepts any of the answers it gave, so the checks
don't flap; one whose answers share nothing checkable is skipped, with the reason.

In production, deploy with `GIT_SHA` set to the commit and `MEDIC_ENV=prod`. To get first aid
on a remote server, run Medic's dashboard where your production can reach it and point the
runtime at it:

```bash
# on Medic's machine
MEDIC_CONTROL_TOKEN=... MEDIC_RUNTIME_KEY=... agent-medic dashboard --host 0.0.0.0
# in your production environment
MEDIC_CONTROL=https://medic.example.com/control/first_aid.json
MEDIC_STANDBY=https://medic.example.com/control/standby.json      # armed mode
MEDIC_CONTROL_TOKEN=...   MEDIC_RUNTIME_KEY=...
```

Bundles are signed with `MEDIC_RUNTIME_KEY` (it is never sent anywhere), bound to one
release and they expire. They are served only with the fetch token; with no token configured
the endpoints don't exist. The dashboard speaks plain HTTP, so put TLS in front of it (any
reverse proxy); the runtime fetches remote bundles over HTTPS only. The runtime is
standard-library Python; a fetch is capped at 3 seconds and cached for a few seconds, and if
Medic is unreachable or a bundle doesn't verify, your agent runs as written.

`medic.toml`:

```toml
[agent]
entrypoint = "query_data:answer"
files = ["providers.py", "query_data.py"]
python = ".venv/Scripts/python.exe"

[neatlogs]
workflow = "rag-tutorial"

[deploy]
mode = "pr"          # pr: fixes arrive as pull requests | managed: Medic deploys them itself
pr = "open"          # open: Medic opens the PR | ready: Medic prepares it and you open it
branch = "main"
```

With `pr = "ready"` (or `agent-medic init --pr ready`), nothing is pushed to your repo until you
say so. Medic keeps the fix branch local, writes the PR description with the evidence to
`.medic/prs/<incident>.md`, and shows it on the dashboard. `agent-medic approve <incident>` has
Medic open it. If you open it yourself with the printed `git push` / `gh pr create` commands,
Medic picks it up either way.

## What the sponsors' tools do in it

- **neatlogs** is the eyes. Production traces (tagged by release) are what Medic watches,
  bisects on and confirms the heal with. `neatlogs setup` creates detections and alert rules,
  a webhook wakes Medic, and Medic reads the alert's sample traces, the triage card and its
  investigation over the neatlogs MCP, then writes the outcome back on the card.
- **Entire** is the memory. The culprit commit's checkpoint tells the fixer what the change
  was for, so the fix keeps the intent (still saves tokens) instead of just reverting it.
- **cfo.ai** prices it: cost per resolved incident, from the measured runs in
  [docs/business](docs/business).

## Try it without your own app

The repo includes a production world: a research agent over a knowledge-base API, live
traffic, and faults to inject.

```bash
uv venv && uv pip install -e ".[dev]"
cp .env.example .env            # NEATLOGS_API_KEY, model keys
agent-medic world init
agent-medic kb &                # the knowledge-base API
agent-medic traffic &           # production users
agent-medic watch &
agent-medic dashboard           # http://127.0.0.1:8790
agent-medic world release short-params   # ship a release whose tool schema no longer matches its handler
```

`agent-medic world releases` lists the other releases (some harmless, some broken).

[docs/benchmark.md](docs/benchmark.md): 11/11 unattended runs across seven scenarios, from a
bad release to an upstream API change, an outage and stale data (the last two are escalated
to a human, since no code fix can repair them).

## Layout

| Path | What |
|---|---|
| `medic_runtime/` | Ships inside your agent: entrypoint, surfaces, values, signed bundles, armed mode |
| `medic/init.py`, `medic/evals.py` | Onboarding: the reviewable diff, known-answer questions from real answers |
| `medic/agent.py` | The incident loop: triage, investigate, first aid, cure, deliver, monitor |
| `medic/neatlogs_link.py` | Detections, alert rules, webhook, triage cards and investigations |
| `medic/delivery.py` | Fix PRs, merge tracking, revert PRs |
| `medic/runner.py` | Runs any `answer(question)` on a set of questions, traced into neatlogs |
| `bisect_agent/` | agent-bisect: finds the culprit commit on traces, asks Entire why, writes the fix |
| `target_agent/` | The demo world's agent |
