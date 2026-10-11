# Quickstart: put Agent Medic on your AI feature

About 15 minutes, from nothing installed to Medic watching your app. You need a Python app
whose AI feature answers questions through one function, for example a RAG chatbot or a support
bot: `answer(question) -> str`.

## 0. What you need

- Python 3.10 or newer, `git`, and the GitHub CLI (`gh auth login`) if fixes should arrive as
  pull requests.
- A neatlogs project ([neatlogs.com](https://neatlogs.com)) with:
  - an ingest key: **Settings → API Keys** (`NEATLOGS_API_KEY`)
  - read access: **Settings → Service Accounts**, scope `observability:read`
    (`NEATLOGS_TOKEN`, `NEATLOGS_PROJECT_ID`)
- A model key for Medic's fixer: `ANTHROPIC_API_KEY`, or `OLLAMA_API_KEY` with
  `FIXER_PROVIDER=ollama_cloud`.
- Optional: [Entire](https://entire.io) (`entire enable`) in your repo. Then Medic can read *why*
  the change that broke things was made, and keep that intent in its fix.

## 1. Install

```bash
pipx install "agent-medic @ git+https://github.com/thebhavye/agent-medic.git"
```

`pipx` puts `agent-medic` on your PATH in every terminal. `pip install` into a virtualenv works too.

## 2. Connect your app (one reviewable diff)

From your app's folder:

```bash
agent-medic init           # dry run: shows every change it would make
agent-medic init --write   # apply it
```

`init` finds the function that answers questions, the parts it calls on the way to the model
(retrieval, prompt building, tools) and the settings on that path (prompts, model names,
cutoffs). It then:

- marks each part so Medic can swap in its last good version during an incident
- adds neatlogs tracing tagged with your environment and release, if you weren't tracing yet
- writes `medic.toml`, and adds `agent-medic` and `neatlogs` to your dependencies and `.medic/`
  to `.gitignore`

If your AI feature is a script with no function to call, `init` prints a prompt for your coding
agent to extract one. Run `init` again afterwards.

Install the new dependencies (`pip install -r requirements.txt`) and commit.

## 3. Keys

Put these in your app's `.env`. Medic reads the `.env` of the folder you run it from.

```
NEATLOGS_API_KEY=...
NEATLOGS_TOKEN=...
NEATLOGS_PROJECT_ID=...
NEATLOGS_USER_EMAIL=you@example.com     # lets Medic update neatlogs' fix cards
ANTHROPIC_API_KEY=...                   # or OLLAMA_API_KEY + FIXER_PROVIDER=ollama_cloud
```

## 4. Known-answer checks (no tests to write)

Write a few real questions your users ask, one per line, in `questions.txt`. Then:

```bash
agent-medic evals bootstrap --questions questions.txt
```

Your current code answers each question five times. The facts every answer agrees on become the
expected answer, and an open question with several right answers accepts any of them. Once your
app has production traffic, `agent-medic evals bootstrap` learns from healthy production answers
instead.

## 5. neatlogs alerts

```bash
agent-medic neatlogs setup
```

This creates Medic's detections and alert rules in your neatlogs project. It's safe to run again.

## 6. Check everything

```bash
agent-medic doctor
```

`doctor` checks every requirement above, plus whether your app's own Python can import your
answer function, and prints the fix for anything missing. Run it until it says `ready`.

## 7. Deploy with the release tag

Wherever your app runs in production, set:

```
GIT_SHA=<the commit you deployed>
MEDIC_ENV=prod
```

Medic tells releases apart by this tag, and it's how Medic knows when its fix has shipped.

## 8. Start Medic

```bash
agent-medic start --dashboard     # in the background; dashboard on http://127.0.0.1:8790
agent-medic status                # is it running? any open incident?
agent-medic logs                  # the end of its log
agent-medic stop                  # stops everything it started
```

## What happens when your AI feature breaks

1. Medic notices: a neatlogs alert, a jump in failing answers, or known-answer checks that
   start failing while nothing errors.
2. **First aid:** it serves the last known good version of the broken part, after showing that
   this fixes the known-answer checks.
3. **The cure:** it finds the commit that broke it, reads why the change was made (from Entire),
   writes a fix that keeps that intent, and verifies it.
4. **A pull request** with all the evidence. By default Medic opens it. With `pr = "ready"` under
   `[deploy]` in `medic.toml`, nothing is pushed until you run `agent-medic approve <incident>` or
   open it yourself.
5. You review, merge and deploy as usual. Medic confirms the heal on real traffic and removes
   first aid. If the fix didn't hold, it opens a revert PR.

To list what Medic is working on: `agent-medic incidents`, then `agent-medic incident <id>`.

## First aid on a remote server

If production runs on another machine, it fetches Medic's signed first-aid bundles over HTTPS.
See "Use it on your agent" in the [README](../README.md) for `MEDIC_CONTROL`, `MEDIC_STANDBY`,
`MEDIC_RUNTIME_KEY` and `MEDIC_CONTROL_TOKEN`.

## Troubleshooting

- **`agent-medic` isn't found:** install with `pipx`, or activate the virtualenv you installed it into.
- **Medic stops right after `start`:** `start` prints the end of its log. Run `agent-medic doctor`.
- **The known-answer checks flap on healthy code:** re-run `evals bootstrap` with more runs
  (`--runs 7`), or remove a question whose right answer genuinely varies.
- **Nothing happens in neatlogs:** check that production traces carry `env:prod` and a
  `release:` tag (`doctor` checks the code; your deploy must set `GIT_SHA`).
