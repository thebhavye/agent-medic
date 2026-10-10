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

from medic import paths, world


def run_batch(size: int, workers: int) -> dict:
    sha = world.head()
    cases = [json.loads(line) for line in paths.CASES.read_text(encoding="utf-8").splitlines() if line]
    batch = random.sample(cases, min(size, len(cases)))
    paths.TRAFFIC.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    cases_file = paths.TRAFFIC / f"batch-{stamp}.jsonl"
    out_file = paths.TRAFFIC / f"batch-{stamp}.json"
    cases_file.write_text("".join(json.dumps(c) + "\n" for c in batch), encoding="utf-8")
    cmd = [sys.executable, "-m", "target_agent.run_eval", "--cases", str(cases_file), "--out", str(out_file),
           "--session", f"prod-{sha[:8]}-{stamp}", "--tag", "env:prod", "--tag", f"release:{sha[:12]}",
           "--workers", str(workers)]
    env = {**os.environ, "PYTHONPATH": str(paths.DEPLOY), "KB_URL": paths.KB_URL}
    subprocess.run(cmd, cwd=paths.DEPLOY, env=env, capture_output=True, timeout=1800)
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
