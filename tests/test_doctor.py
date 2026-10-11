"""`agent-medic doctor`: a new user's setup, checked, with what to fix."""

import subprocess
import sys

import pytest

from medic import doctor

KEYS = ("NEATLOGS_API_KEY", "NEATLOGS_TOKEN", "NEATLOGS_PROJECT_ID", "NEATLOGS_USER_EMAIL", "ANTHROPIC_API_KEY",
        "OLLAMA_API_KEY", "FIXER_PROVIDER", "LLM_PROVIDER", "XAI_API_KEY", "MEDIC_PROJECT")


@pytest.fixture
def bare(monkeypatch):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)


def by_name(checks):
    return {c.name: c for c in checks}


def test_an_empty_folder_says_what_to_do_first(tmp_path, bare):
    checks = by_name(doctor.run(tmp_path))
    assert checks["medic.toml"].status == doctor.FAIL and "agent-medic init" in checks["medic.toml"].fix
    assert checks["neatlogs API key"].status == doctor.FAIL
    assert checks["fixer model"].status in (doctor.WARN, doctor.FAIL)          # never a silent local fallback
    assert "problem(s) to fix first" in doctor.render(list(checks.values()))


def test_a_connected_project_reports_its_gaps(tmp_path, bare, monkeypatch):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "app.py").write_text("def answer(question):\n    return 'ok'\n")
    (tmp_path / "medic.toml").write_text('[agent]\nentrypoint = "app:answer"\nfiles = ["app.py"]\n'
                                         '[deploy]\nmode = "managed"\n')
    monkeypatch.setenv("FIXER_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    checks = by_name(doctor.run(tmp_path))
    assert checks["medic.toml"].status == doctor.OK
    assert checks["fixer model"].status == doctor.OK
    assert checks["known-answer checks"].status == doctor.FAIL and "evals bootstrap" in checks["known-answer checks"].fix
    assert checks["app tracing"].status == doctor.WARN                          # no neatlogs.init in app.py
    assert checks["app interpreter"].status == doctor.OK                       # app:answer imports with this python
    assert "GitHub CLI" not in checks                                          # managed mode needs no PRs


def test_an_app_that_cannot_import_is_a_failure_with_the_reason(tmp_path, bare):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "app.py").write_text("import some_missing_dependency\n\ndef answer(q):\n    return q\n")
    (tmp_path / "medic.toml").write_text('[agent]\nentrypoint = "app:answer"\nfiles = ["app.py"]\n'
                                         f'python = "{sys.executable.replace(chr(92), "/")}"\n[deploy]\nmode = "managed"\n')
    check = by_name(doctor.run(tmp_path))["app interpreter"]
    assert check.status == doctor.FAIL and "some_missing_dependency" in check.detail
