# T1: Live dashboard for Agent Medic

**Goal:** one web page that shows, live, a production AI agent breaking and Agent Medic healing
it. It's for the 3-minute demo video and for judges, so it must read at a glance and look
polished.

**You own:** `medic/dashboard.py`, `medic/static/` (if you split out HTML/JS/CSS),
`tests/test_dashboard.py`. Task name for the PR branch: `dashboard`.

## What to build

`python -m medic.dashboard [--port 8790] [--medic-dir PATH]` starts a small HTTP server
(standard library `http.server`, no Flask) that serves:

- `GET /`: the dashboard page (self-contained HTML: inline CSS and JS, no CDN, no build step).
- `GET /api/state`: JSON assembled from `MEDIC_DIR` (formats in the shared rules):
  `production` (from `state.json`: traces, baseline, current window of the last 6 traces,
  canary), `incidents` (all `incidents/*.json`, newest first), `deploy` (current release from
  `git -C <MEDIC_DIR>/deploy log -1 --format=%h|%s`, if that repo exists), `bench` (all
  `bench/*/result.json`), `external` (`external/case-*.json`). Read files fresh on every request
  and skip missing or half-written files instead of failing.

The page polls `/api/state` every 3 seconds and renders:

1. **Header:** "Agent Medic", the deployed release (short sha + subject), and a large status
   pill: `HEALTHY` (green), `INCIDENT · <phase>` (amber) or `ESCALATED` (red).
2. **Production health:** the last ~60 production traces from `state.json.traces` as a strip
   of dots (green ok, red failed: `status == "ERROR"` or a non-answer), with small line charts
   of tool calls and tokens per trace beneath. Draw a vertical marker with the short sha
   wherever `release` changes. Next to it, baseline vs current window for `failure_rate`,
   `tool_error_rate`, `tokens_per_run`, `tool_calls_per_run`, and the latest canary pass rate.
3. **Current incident** (newest open one, else newest overall):
   - a phase stepper `open → triaged → investigated → decided → remedied → deployed →
     resolved` (or ending in `escalated`), each step showing its time from `timeline`;
   - cards: signals (`baseline → current`), triage summary, each investigator's verdict badge
     and detail, the Coordinator's decision (cause, remedy, confidence, rationale bullets;
     highlight `disagreements` as "Settled: ..."), fix attempts (accepted or rejected badge,
     feedback, and edits as a diff: `find` lines red, `replace` lines green), then deploy and
     monitor results.
4. **Incident history:** table of all incidents: id, opened (local time), cause, outcome,
   time to resolve (last `timeline.t` − `opened_at`), fix attempts.
5. **Benchmark:** table from `bench/*/result.json`: scenario, outcome, cause, remedy, culprit
   check, detect, resolve, pass; plus "N/M passed".
6. **Real-world case** (if `external/case-*.json` exists): target, cause, the accepted fix as a
   diff, verification runs (topic, exit, trace id, trace status), review problems of rejected
   attempts, and the Entire Trail URL found in `proposal.trail` as a link.

## Design

- Dark theme by default, light theme via `prefers-color-scheme`, colors as CSS variables.
- Hierarchy: status first, then the current incident, then history. Generous spacing, system
  font stack, monospace for code, shas and ids. Animate the stepper when a phase changes.
- Must look good at 1280×720 (screen recording) and stay readable on a phone (single column
  under 700px wide).
- **Escape all text from JSON before inserting it** (model output can contain HTML).

## Acceptance

- `.venv\Scripts\python.exe -m medic.dashboard --medic-dir .medic` (in the shared folder) shows
  the real data already there: incidents, 6 benchmark runs, and the neatlogs case.
- An empty `--medic-dir` shows a friendly empty state with no errors.
- While Medic writes new incidents, the page updates without a reload.
- `tests/test_dashboard.py` builds `/api/state` from a temp `MEDIC_DIR` containing one fake
  incident and one fake bench result, and checks the shape. Full suite passes.
- PR via the shared rules' worktree procedure; include in the PR body how to run it and what
  each section shows.
