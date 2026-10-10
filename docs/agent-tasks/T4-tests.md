# T4: Tests for modules that have none

**Goal:** unit tests for the parts of Agent Medic that are currently only exercised by live
runs. Tests must be fast (no network, no model calls, no neatlogs) and deterministic.

**You own:** `tests/test_watcher.py`, `tests/test_evidence.py`, `tests/test_canary.py`,
`tests/test_external_review.py` (new files only; don't edit existing tests or any module).
Task name for the PR branch: `tests`.

Read the modules first; test their *behavior*, not their internals. Use `tmp_path` and
`monkeypatch.setenv("MEDIC_DIR", ...)` / `monkeypatch.setattr` to isolate state. Note that
`medic/paths.py` reads `MEDIC_DIR` at import time, so patch the attributes on `medic.paths`
(e.g. `monkeypatch.setattr(paths, "STATE", tmp_path / "state.json")`) rather than relying on the
environment variable alone.

## What to cover

1. `tests/test_watcher.py` (`medic/watcher.py`):
   - `compare()`: each rule fires at its threshold and not just below it (abs rules for rates,
     ratio rules for tokens/tool calls); a zero baseline never divides by zero.
   - `Watcher.poll()` with a fake reader (an object with `list_traces`, `spans`, `payload`
     returning canned dicts in the neatlogs shapes documented in the shared rules): adds only
     finalized `env:prod` traces from releases that exist in a temp deploy git repo (create one
     with `git init` + one commit in `tmp_path`, patch `paths.DEPLOY`), skips already-seen
     ones, caps reads at `MAX_NEW`, and survives a reader that raises for one trace.
   - `Watcher.check()`: freezes a baseline only from a healthy window; returns signals after.
2. `tests/test_evidence.py` (`medic/evidence.py`):
   - `collect()` builds tool errors from ERROR tool spans (payload `error` JSON with `type` and
     `message`), question and answer from the AGENT span payload, `repeated_calls` from
     identical tool inputs; caches only finalized traces.
   - `TraceEvidence.non_answer` / `failed` on the phrases in `NON_ANSWER`; `window()` metrics.
3. `tests/test_canary.py` (`medic/canary.py`):
   - `due()` respects the interval; `regressed()` returns a signal only after a baseline exists
     and the drop is ≥ `DROP`. Don't call `run()` (it spawns an eval); test it only if you can
     stub `subprocess.run` cleanly.
4. `tests/test_external_review.py` (`medic/external.py`):
   - `review()` flags an over-proportionate `update_model` fix, a newly added broad
     `except Exception:` block, and a reference to a missing module attribute; passes a
     one-line model change. Build a temp target spec whose `repo` is `tmp_path`, `subdir` a
     folder with an `agents.py`, and `python` pointing at the current interpreter
     (`sys.executable`; set `spec["python"]` to an absolute path and adjust if `workdir()`
     joins it, read the code).
   - `fault_class()`, `describe_failure()`, `_public()` (strips Windows and POSIX paths before
     `*.py` names but keeps the file name), `TRANSIENT` and `QUOTA` regexes on real messages:
     `503 UNAVAILABLE`, `429 RESOURCE_EXHAUSTED ... quota`, `404 NOT_FOUND`, `operation timed out`.

## Acceptance

- New tests pass alone and with the full suite in under ~60 s total.
- No network access (monkeypatch anything that would call httpx or subprocess to neatlogs).
- If a test reveals a real bug, mark that test `xfail(strict=True, reason=...)` and describe
  the bug in your PR body instead of changing the module.
- PR via the shared rules' worktree procedure.
