"""Every module an installed Medic loads imports without the repo's scripts/ folder, which the
package doesn't ship (seen live: `import seed_history` crashed every pip-installed `watch`)."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = ["medic.agent", "medic.cli", "medic.canary", "medic.dashboard", "medic.doctor", "medic.init",
           "medic.evals", "medic.runner", "medic.remedy", "medic_runtime", "bisect_agent.cli"]


def test_installed_modules_import_without_the_scripts_folder(tmp_path):
    code = ("import sys\n"
            f"sys.path = [p for p in sys.path if 'scripts' not in p]\n"
            f"for m in {MODULES!r}:\n    __import__(m)\n"
            "assert 'seed_history' not in sys.modules\nprint('ok')\n")
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert out.returncode == 0 and "ok" in out.stdout, out.stderr[-1500:]


def test_a_slow_entire_graph_never_sinks_a_finding(tmp_path, monkeypatch):
    import subprocess as sp

    from medic import anatomy
    monkeypatch.setattr(anatomy, "_entire", lambda repo: "entire")

    def slow(*a, **k):
        raise sp.TimeoutExpired(a[0], 60)
    monkeypatch.setattr(anatomy.subprocess, "run", slow)
    assert anatomy.graph_commit(tmp_path, "abc") == []
    assert anatomy.graph_impact(tmp_path, "answer") == ""
