# Tasks for a second coding agent

Self-contained tasks that run in parallel with the core work on `medic/` (investigators,
fixer, rollout). Paste **the shared rules below plus one task file** into the agent.

| Task | File | Owns these paths (nobody else edits them) | Size |
|---|---|---|---|
| T1 Live dashboard | [T1-dashboard.md](T1-dashboard.md) | `medic/dashboard.py`, `medic/static/`, `tests/test_dashboard.py` | 2-3 h |
| T2 Unit economics for cfo.ai | [T2-unit-economics.md](T2-unit-economics.md) | `scripts/unit_economics.py`, `docs/business/`, `tests/test_unit_economics.py` | 1-2 h |
| T3 EC2 deployment kit | [T3-ec2-deploy.md](T3-ec2-deploy.md) | `deploy/ec2/` | 1-2 h |
| T4 Tests for untested modules | [T4-tests.md](T4-tests.md) | `tests/test_watcher.py`, `tests/test_evidence.py`, `tests/test_canary.py`, `tests/test_external_review.py` | 1-2 h |

Do them in this order: T1, T2, T3, T4. One PR per task.

---

## Shared rules (paste these with every task)

You are working on **Agent Medic**, a Python 3.12 project in
`C:\Users\BHAVYE\projects\agent-medic` (GitHub: `thebhavye/agent-medic`). It is a team of
agents that watches a production AI agent through neatlogs traces, diagnoses failures, fixes
them, verifies the fix and confirms production recovered.

**Another agent is editing this same folder at the same time** (the core: `medic/agent.py`,
`medic/remedy.py`, `medic/external.py`, `medic/investigators/`, `medic/watcher.py`,
`bisect_agent/`, `target_agent/`). To avoid stepping on each other:

1. **Only create or edit the paths your task owns** (table above). Read anything; change
   nothing else. If another file needs a change, say so in your final summary instead.
2. **Never change git state in this folder:** no `git checkout`, `switch`, `branch -f`,
   `reset`, `stash`, `add -A`, `add .`, `commit`, `rebase` or `pull` here. The other agent
   commits from this folder; your files stay uncommitted here until your PR.
3. **Ship your task as a PR from a temporary worktree** that holds only your files:
   ```bash
   # from C:\Users\BHAVYE\projects\agent-medic, when the task is done
   git fetch origin
   git worktree add -b task/<task-name> ../agent-medic-pr-<task-name> origin/main
   # copy ONLY your task's paths into ../agent-medic-pr-<task-name>/ (same relative paths)
   cd ../agent-medic-pr-<task-name>
   git add <your paths>            # explicit paths only
   git commit -m "<what you built>"
   git push -u origin task/<task-name>
   gh pr create --base main --title "<title>" --body "<summary, how to run, how tested>"
   cd ../agent-medic && git worktree remove ../agent-medic-pr-<task-name>
   ```
   Then delete your copies of those files from the shared folder **only after** the
   founder says the PR is merged (they'll arrive back through `git pull` by the other agent).
4. **No AI attribution anywhere:** no `Co-Authored-By` lines, no "Generated with ..." text, no
   model or tool names as author, in commits, PR titles or bodies, or files. Commits use the
   repo's configured git user. If your tool adds such lines by default, turn that off.
5. **Never commit or print secrets** (`.env`, keys, tokens). Never commit anything under
   `.medic/` (runtime state, gitignored).
6. **Match the existing style:** standard library first, short docstrings that say *why*,
   little comment noise, no heavy new dependencies (propose one in your summary if you must).
7. **Tests:** run `.venv\Scripts\python.exe -m pytest -q` in the shared folder before you open
   the PR. It passes on `main`; if it fails because the other agent is mid-change in a file
   you don't own, note it and run your own test file alone.
8. Use the existing virtualenv: `C:\Users\BHAVYE\projects\agent-medic\.venv`. Don't
   reinstall or upgrade packages in it.
9. End with a short summary: what you built, how to run it, how you tested it, the PR link,
   and anything unfinished.

### Runtime data you can read (JSON, under `.medic/` in the shared folder)

`MEDIC_DIR` (default `<repo>/.medic`) holds:

- `state.json`: Watcher state. Keys: `traces` (list of trace evidence), `baseline` (metrics of
  the healthy window), `contract` (dependency API snapshot), `healthy_release`, `since`,
  `skipped`, and when the canary has run, `canary` (list of runs `{at, release, pass_rate, n,
  failed[]}`) and `canary_baseline`.
  Trace evidence keys: `trace_id, started_at, release, status, cost_usd, tokens, tool_calls,
  duration_ms, question, answer, tool_errors[{tool,type,message,input}], tool_inputs,
  repeated_calls`. Window metric keys: `n, failure_rate, tool_error_rate, non_answer_rate,
  cost_per_run, tokens_per_run, tool_calls_per_run, repeated_call_rate, releases`.
- `incidents/<id>.json`: one incident. Keys: `id, opened_at, phase,
  signals[{name,metric,baseline,current}], release, healthy_release, trace_ids,
  triage{summary,symptoms,dominant,investigators,examples}, findings{<investigator>:
  {verdict,confidence,detail,...}}, decision{cause,remedy,confidence,rationale,disagreements},
  remedy{attempts[{n,accepted,commit_title,diagnosis,edits[{path,find,replace}],feedback,
  metrics,usage{model,input_tokens,output_tokens},fixer_cost_usd}],branch,commit},
  deploy{deployed,release,at}, monitor{window,signals}, timeline[{t,phase,msg}]`.
  Phases: `open, triaged, investigated, decided, remedied, deployed, resolved, escalated`.
- `bench/<scenario>-<stamp>/result.json`: one benchmark run. Keys: `scenario, provider,
  fixer_provider, outcome, cause, remedy, culprit_expected, culprit_found, attempts, detect_s,
  resolve_s, fixer_tokens, fixer_cost_usd, checks{...}, passed, timeline, incident`. Each bench
  dir also has its own `incidents/`, `state.json` and `deploy/.bisect/results/*.json`.
- `deploy/.bisect/results/*.json` (and the same inside each bench dir): eval runs. Keys:
  `session, provider, model, cases[]`; case keys: `id, question, expect, passed, status, answer,
  error, turns, tool_calls, tool_errors, input_tokens, output_tokens, cost_usd, duration_s`.
- `external/case-<target>.json`: a case on an external agent (neatlogs' example). Keys: `id,
  target, phase, runs, triage, findings, decision, attempts[{n,accepted,blocked,edits,usage,
  verification[{topic,exit,trace_id,trace_status,...}],review,feedback}], proposal{branch,
  commit,trail,trail_created,findings}, timeline`.
- `logs/*.log`: plain-text logs of Medic, traffic and benchmark runs.
