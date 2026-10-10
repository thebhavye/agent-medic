"""Production traffic: keep sending questions to whatever release is deployed.

Each batch runs the deployed code in a subprocess, so a new release (or a Medic fix) takes
effect on the next batch. Traces are tagged env:prod and release:<sha> in neatlogs.
"""

import json
import os
import random
import subprocess
import sys
import time

from bisect_agent import gitops
from medic import first_aid, paths, rollout


def run_batch(size: int, workers: int) -> dict:
    # During a canary rollout, a share of batches runs the candidate release instead.
    code_dir, sha = rollout.route()
    cases = [json.loads(line) for line in paths.CASES.read_text(encoding="utf-8").splitlines() if line]
    batch = random.sample(cases, min(size, len(cases)))
    paths.TRAFFIC.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    cases_file = paths.TRAFFIC / f"batch-{stamp}.jsonl"
    out_file = paths.TRAFFIC / f"batch-{stamp}.json"
    cases_file.write_text("".join(json.dumps(c) + "\n" for c in batch), encoding="utf-8")
    def run_from(code_dir, sha) -> None:
        cmd = [sys.executable, "-m", "target_agent.run_eval", "--cases", str(cases_file), "--out", str(out_file),
               "--session", f"prod-{sha[:8]}-{stamp}", "--tag", "env:prod", "--tag", f"release:{sha[:12]}",
               "--workers", str(workers)]
        # Production reads Medic's first-aid bundle; it applies only to the release it was issued for.
        # The standby arms the runtime to engage the last known good by itself; the state file
        # lets one batch's engagement protect the next batch's process too.
        env = {**os.environ, "PYTHONPATH": str(code_dir), "KB_URL": paths.KB_URL,
               "MEDIC_CONTROL": str(first_aid.bundle_path()), "MEDIC_RELEASE": sha[:12],
               "MEDIC_STANDBY": str(first_aid.standby_path()),
               "MEDIC_RUNTIME_STATE": str(first_aid.bundle_path().parent / "runtime-state.json")}
        subprocess.run(cmd, cwd=code_dir, env=env, capture_output=True, timeout=1800)

    try:
        run_from(code_dir, sha)
    except (OSError, subprocess.SubprocessError):
        if code_dir == paths.DEPLOY:
            raise
        # The canary ended between routing and running: serve this batch from the stable release.
        code_dir, sha = paths.DEPLOY, gitops.rev_parse("HEAD", paths.DEPLOY)
        run_from(code_dir, sha)
    results = json.loads(out_file.read_text(encoding="utf-8"))["cases"] if out_file.exists() else []
    return {"release": sha[:8], "n": len(results), "passed": sum(r["passed"] for r in results)}


def run(interval: float, size: int, workers: int, batches: int | None = None) -> None:
    done = 0
    while batches is None or done < batches:
        summary = run_batch(size, workers)
        print(f"[traffic] release {summary['release']}: {summary['passed']}/{summary['n']} answered correctly",
              flush=True)
        done += 1
        if batches is None or done < batches:
            time.sleep(interval)
