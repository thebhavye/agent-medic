"""First aid: keep users served while Medic makes the cure.

A verified code fix takes minutes to write and longer to roll out. Meanwhile the broken
release keeps answering users. So right after diagnosis Medic stands in, inside the running
agent, the last healthy version of exactly what the culprit commit changed:

- only parts the agent registered with the runtime (`medic_runtime.surface` / `.value`),
- taken from the culprit's parent, so nothing else that shipped with it is undone,
- checked first: the known-answer eval must pass on the broken release with the bandage on,
- published as a signed bundle bound to the broken release, with an expiry.

The cure removes it. If there is nothing Medic can swap (the culprit changed unregistered
code, or the cause isn't in the code), it says so and the incident goes on to the cure.
"""

import ast
import json
import os
import subprocess
import time
from pathlib import Path

from medic import paths
from medic_runtime import bundle

TTL_S = 6 * 3600          # first aid buys time; it never quietly becomes the fix
RUNTIME_MODULE = "medic_runtime"


def bundle_path() -> Path:
    return paths.MEDIC_DIR / "control" / "first_aid.json"


def _key() -> str | None:
    return os.environ.get("MEDIC_RUNTIME_KEY") or None


def _show(root: Path, rev: str, file: str) -> str | None:
    proc = subprocess.run(["git", "show", f"{rev}:{file}"], cwd=root, capture_output=True, encoding="utf-8",
                          errors="replace")
    return proc.stdout if proc.returncode == 0 else None


def _parent(root: Path, rev: str) -> str | None:
    proc = subprocess.run(["git", "rev-parse", f"{rev}^"], cwd=root, capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else None


# -- what the agent registered ------------------------------------------------------------

def _aliases(tree: ast.Module) -> set[str]:
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names |= {a.asname or a.name for a in node.names if a.name == RUNTIME_MODULE}
    return names


def _runtime_call(node, aliases: set[str], attr: str) -> str | None:
    """`medic.surface("name")` / `medic.value("name", ...)`: the registered name, else None."""
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == attr
            and isinstance(node.func.value, ast.Name) and node.func.value.id in aliases
            and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
        return node.args[0].value
    return None


def registered(sources: dict[str, str]) -> dict:
    """{name: {"kind": "function"|"value", "file", "symbol"}} for everything the agent registered."""
    found = {}
    for file, text in sources.items():
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        aliases = _aliases(tree)
        if not aliases:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    name = _runtime_call(dec, aliases, "surface")
                    if name:
                        found[name] = {"kind": "function", "file": file, "symbol": node.name}
        for node in ast.walk(tree):
            name = _runtime_call(node, aliases, "value")
            if name and len(node.args) > 1 and isinstance(node.args[1], ast.Name):
                found.setdefault(name, {"kind": "value", "file": file, "symbol": node.args[1].id})
    return found


def _definition(text: str | None, kind: str, symbol: str):
    """(source, node) of a module-level function, or of the expression assigned to a module-level name."""
    if not text:
        return None
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    for node in tree.body:
        if kind == "function" and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol:
            return ast.get_source_segment(text, node), node
        if kind == "value" and isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == symbol for t in node.targets):
            return ast.get_source_segment(text, node.value), node.value
    return None


def symbol_source(text: str | None, kind: str, symbol: str) -> str | None:
    found = _definition(text, kind, symbol)
    return found[0] if found else None


def _find(root: Path, rev: str, reg: dict, files: list[str]):
    """The symbol's definition at `rev`: in the registering file, or wherever it's defined (a value
    is often registered in one module and defined in another, e.g. imported tool schemas)."""
    for file in [reg["file"], *[f for f in files if f != reg["file"]]]:
        found = _definition(_show(root, rev, file), reg["kind"], reg["symbol"])
        if found:
            return found
    return None


# -- the plan -----------------------------------------------------------------------------

def plan(root: Path, release: str, healthy: str, culprit: str | None = None,
         files: list[str] | None = None) -> dict:
    """What to stand in on `release`: registered parts the culprit changed, at the culprit's parent.

    Without a culprit, compares the broken release with the last healthy one instead."""
    files = files or paths.AGENT_FILES
    base = (_parent(root, culprit) if culprit else None) or healthy
    before_rev, after_rev = (base, culprit) if culprit else (healthy, release)
    live = {f: _show(root, release, f) for f in files}
    regs = registered({f: t for f, t in live.items() if t})
    out = {"base": base, "functions": {}, "values": {}, "skipped": [], "registered": sorted(regs)}
    for name, reg in sorted(regs.items()):
        old, new = _find(root, before_rev, reg, files), _find(root, after_rev, reg, files)
        if (old and old[0]) == (new and new[0]):
            continue                    # the culprit didn't touch it
        if old is None:
            out["skipped"].append(f"{name}: new in the culprit, no healthy version to stand in")
            continue
        if reg["kind"] == "function":
            out["functions"][name] = {"source": old[0], "from_release": base}
        else:
            try:
                out["values"][name] = ast.literal_eval(old[1])
            except (ValueError, SyntaxError):
                out["skipped"].append(f"{name}: its healthy value isn't a literal Medic can pin")
    return out


def empty(p: dict) -> bool:
    return not (p.get("functions") or p.get("values") or p.get("breakers"))


def describe(p: dict) -> str:
    parts = [f"{n}() from {str(f['from_release'])[:8]}" for n, f in sorted((p.get("functions") or {}).items())]
    parts += [f"{n} pinned to its healthy value" for n in sorted(p.get("values") or {})]
    parts += [f"{n} cut off" for n in sorted(p.get("breakers") or {})]
    return ", ".join(parts) or "nothing"


def payload(incident_id: str, release: str, p: dict) -> dict:
    now = time.time()
    return {"incident": incident_id, "release": release[:12], "issued_at": now, "expires_at": now + TTL_S,
            "functions": p.get("functions") or {}, "values": p.get("values") or {},
            "breakers": p.get("breakers") or {}, "fallback_models": p.get("fallback_models") or []}


# -- publishing ---------------------------------------------------------------------------

def write(path: Path, body: dict) -> Path:
    """Write a signed bundle atomically (a reader never sees half a file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(bundle.sign(body, _key()), indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return path


def publish(body: dict) -> Path:
    return write(bundle_path(), body)


def clear(incident_id: str | None = None) -> bool:
    """Remove the published bundle (only this incident's, when given). True if one was removed."""
    current = active(check_expiry=False)
    if current is None or (incident_id and current.get("incident") != incident_id):
        return False
    bundle_path().unlink(missing_ok=True)
    return True


def active(check_expiry: bool = True) -> dict | None:
    try:
        doc = json.loads(bundle_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not bundle.verify(doc, _key()):
        return None
    body = doc["payload"]
    if check_expiry and float(body.get("expires_at") or 0) < time.time():
        return None
    return body
