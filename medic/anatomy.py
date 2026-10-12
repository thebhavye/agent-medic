"""Agent anatomy: the parts of an agent that can be repaired, and which of them a fault touches.

Medic reads the agent's source and maps its repairable surfaces:

    prompt         system or instruction text the model is given
    model_config   which model, temperature, effort, token limits
    loop_limit     turn caps, retries, timeouts
    tool_schema    the tool definitions the model sees (names, parameters, descriptions)
    tool_handler   the code that runs when the model calls a tool
    client         code that talks to an outside dependency (HTTP APIs, model providers)

Surfaces come from the Python AST, with exact line ranges. Two Entire graph commands add what
the AST can't see: `entire graph commit` lists the entities a release changed, and `entire graph
impact` gives a suspected function's blast radius (callers, callees, files that change with it),
so a fix to one part doesn't silently break another.
"""

import ast
import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

PROMPT_NAME = re.compile(r"SYSTEM|PROMPT|INSTRUCTION", re.I)
MODEL_NAME = re.compile(r"MODEL|TEMPERATURE|EFFORT|MAX_TOKENS|TOP_P", re.I)
LIMIT_NAME = re.compile(r"MAX_|LIMIT|RETRIES|TIMEOUT|BUDGET", re.I)
CLIENT_CALL = re.compile(r"^(httpx|requests|urllib|aiohttp)\.|\.(generate_content|generate_content_stream|create|"
                         r"chat|completions)$")


@dataclass
class Surface:
    kind: str
    name: str
    file: str
    start: int
    end: int
    symbol: str = ""          # the Python name that holds it (variable, function, dict owner)
    snippet: str = ""
    notes: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _src(lines: list[str], node: ast.AST) -> str:
    return "\n".join(lines[node.lineno - 1:node.end_lineno])[:1200]


def _const_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values if isinstance(v, ast.Constant))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = _const_str(node.left), _const_str(node.right)
        return (a or "") + (b or "") if a is not None or b is not None else None
    return None


def _call_name(node: ast.Call) -> str:
    f = node.func
    parts = []
    while isinstance(f, ast.Attribute):
        parts.append(f.attr)
        f = f.value
    if isinstance(f, ast.Name):
        parts.append(f.id)
    return ".".join(reversed(parts))


def scan_file(path: Path, rel: str) -> list[Surface]:
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    lines = text.splitlines()
    out: list[Surface] = []
    schema_names: set[str] = set()

    for node in ast.walk(tree):
        # Module- or function-level constants: prompts, model settings, limits.
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            var, value = node.targets[0].id, node.value
            s = _const_str(value)
            # Prompt template objects, e.g. SystemPromptTemplate("You are ...").
            if s is None and isinstance(value, ast.Call) and _call_name(value).endswith("PromptTemplate") and value.args:
                s = _const_str(value.args[0])
                var = var if PROMPT_NAME.search(var) else f"{var}_prompt"
            if s is not None and PROMPT_NAME.search(var) and len(s) > 20:
                out.append(Surface("prompt", "system" if "SYSTEM" in var.upper() else var.lower(), rel,
                                   node.lineno, node.end_lineno, var, _src(lines, node), {"text": s[:600]}))
            elif MODEL_NAME.search(var) and isinstance(value, ast.Constant) and not isinstance(value.value, bool):
                out.append(Surface("model_config", str(value.value) if "MODEL" in var.upper() else var.lower(), rel,
                                   node.lineno, node.end_lineno, var, _src(lines, node), {"value": value.value}))
            elif (LIMIT_NAME.search(var) and isinstance(value, ast.Constant)
                  and isinstance(value.value, (int, float)) and not isinstance(value.value, bool)):
                out.append(Surface("loop_limit", var.lower(), rel, node.lineno, node.end_lineno, var,
                                   _src(lines, node), {"value": value.value}))
        # Tool schemas: dicts with a "name" and a parameter schema.
        if isinstance(node, ast.Dict):
            keys = {k.value: v for k, v in zip(node.keys, node.values) if isinstance(k, ast.Constant)}
            if "name" in keys and ({"input_schema", "parameters"} & keys.keys()):
                name = _const_str(keys["name"])
                if name:
                    schema_names.add(name)
                    params = keys.get("input_schema") or keys.get("parameters")
                    props = []
                    if isinstance(params, ast.Dict):
                        inner = {k.value: v for k, v in zip(params.keys, params.values) if isinstance(k, ast.Constant)}
                        if isinstance(inner.get("properties"), ast.Dict):
                            props = [k.value for k in inner["properties"].keys if isinstance(k, ast.Constant)]
                    out.append(Surface("tool_schema", name, rel, node.lineno, node.end_lineno, "", _src(lines, node),
                                       {"params": props}))
        # Function-call style schemas (e.g. google genai FunctionDeclaration(name=...)).
        if isinstance(node, ast.Call) and _call_name(node).endswith("FunctionDeclaration"):
            kw = {k.arg: k.value for k in node.keywords}
            name = _const_str(kw.get("name")) if "name" in kw else None
            if name:
                schema_names.add(name)
                out.append(Surface("tool_schema", name, rel, node.lineno, node.end_lineno, "", _src(lines, node)))
        # model=... / system_instruction=... arguments at call sites.
        if isinstance(node, ast.Call):
            for k in node.keywords:
                if k.arg == "model":
                    s = _const_str(k.value)
                    if s:
                        out.append(Surface("model_config", s, rel, node.lineno, node.end_lineno, "",
                                           _src(lines, node), {"value": s}))

    # Functions: tool handlers (named like a tool, or traced as a TOOL span) and clients.
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        tool_name = None
        for d in node.decorator_list:
            if isinstance(d, ast.Call):
                kw = {k.arg: _const_str(k.value) for k in d.keywords}
                if kw.get("kind") == "TOOL":
                    tool_name = kw.get("tool_name") or kw.get("name") or node.name
        if tool_name or node.name in schema_names:
            out.append(Surface("tool_handler", tool_name or node.name, rel, node.lineno, node.end_lineno, node.name,
                               _src(lines, node)))
        calls = {_call_name(c) for c in ast.walk(node) if isinstance(c, ast.Call)}
        if any(CLIENT_CALL.search(c) for c in calls):
            out.append(Surface("client", node.name, rel, node.lineno, node.end_lineno, node.name, _src(lines, node),
                               {"calls": sorted(c for c in calls if CLIENT_CALL.search(c))[:6]}))

    # Handler registries: {"tool_name": lambda args: fn(args["x"])}.
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict) and node.keys and all(isinstance(k, ast.Constant) for k in node.keys):
            for k, v in zip(node.keys, node.values):
                if isinstance(k.value, str) and isinstance(v, ast.Lambda) and k.value in schema_names | {
                        s.name for s in out if s.kind == "tool_handler"}:
                    out.append(Surface("tool_handler", k.value, rel, v.lineno, v.end_lineno, "", _src(lines, v),
                                       {"registry": True}))
    return out


def scan(root: Path, files: list[str] | None = None) -> list[Surface]:
    root = Path(root)
    paths = [root / f for f in files] if files else [p for p in root.rglob("*.py")
                                                      if not any(part.startswith(".") or part in ("tests", "venv")
                                                                 for part in p.relative_to(root).parts)]
    out = []
    for p in paths:
        if p.exists():
            out.extend(scan_file(p, p.relative_to(root).as_posix()))
    return out


def resolve(surfaces: list[Surface], suspects: list[dict]) -> list[dict]:
    """Map localized suspects ({kind, name}) to concrete surfaces, keeping suspect order."""
    seen, out = set(), []
    for s in suspects:
        matches = [x for x in surfaces if x.kind == s["kind"] and (x.name == s["name"] or s["kind"] in ("prompt", "loop_limit"))]
        if s["kind"] == "model_config" and not matches:
            matches = [x for x in surfaces if x.kind == "model_config"]
        if s["kind"] == "client" and not matches:
            # The client a tool goes through: a client function its handler calls.
            handler_src = " ".join(x.snippet for x in surfaces if x.kind == "tool_handler" and x.name == s["name"])
            matches = [x for x in surfaces if x.kind == "client" and re.search(rf"\b{re.escape(x.name)}\(", handler_src)]
        for m in matches:
            key = (m.kind, m.name, m.file, m.start)
            if key not in seen:
                seen.add(key)
                out.append({**m.to_dict(), "suspect_score": s.get("score")})
    return out


def changed_surfaces(repo: Path, sha: str, surfaces: list[Surface]) -> dict:
    """Which surfaces a commit touched: diff hunks mapped onto surface line ranges, plus Entire
    graph's entity-level change list (with dependent counts) when the plugin is available."""
    diff = subprocess.run(["git", "show", "--format=", "--unified=0", sha], cwd=repo, capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout
    touched: dict[str, list[tuple[int, int]]] = {}
    current = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else None
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if m and current:
            start, count = int(m.group(1)), int(m.group(2) or 1)
            touched.setdefault(current, []).append((start, start + max(count, 1) - 1))
    hit = [s.to_dict() for s in surfaces
           if any(a <= s.end and b >= s.start for a, b in touched.get(s.file, []))]
    return {"surfaces": hit, "hunks": touched, "graph": graph_commit(repo, sha)}


ENTIRE_TIMEOUT_S = 60


def _entire(repo: Path) -> str | None:
    return shutil.which("entire")


def _run_entire(cmd: list[str]) -> subprocess.CompletedProcess | None:
    """Entire's graph adds context; it must never sink a finding. Slow, missing or failing: None
    (seen live: `entire graph commit` ran past its timeout and the regression finding was lost)."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=ENTIRE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None


def graph_commit(repo: Path, sha: str) -> list[dict]:
    exe = _entire(repo)
    if not exe:
        return []
    proc = _run_entire([exe, "graph", "commit", sha, "--json", "--repo", str(repo), "--max-seconds", "45"])
    try:
        data = json.loads(proc.stdout) if proc else {}
    except ValueError:
        return []
    return [{"file": f["path"], **c} for f in data.get("files", []) for c in f.get("changes", [])]


def graph_impact(repo: Path, symbol: str, file: str | None = None) -> str:
    """Entire graph's blast radius for a function, as compact text for the fixer's context."""
    exe = _entire(repo)
    if not exe or not symbol:
        return ""
    cmd = [exe, "graph", "impact", "--symbol", symbol, "--repo", str(repo), "--depth", "1", "--limit", "8",
           "--format", "text"]
    if file:
        cmd += ["--file", file]
    proc = _run_entire(cmd)
    return proc.stdout.strip()[:2500] if proc and proc.returncode == 0 else ""


def describe(surfaces: list[dict]) -> str:
    """Suspected surfaces as a prompt section."""
    out = []
    for s in surfaces:
        out.append(f'<surface kind="{s["kind"]}" name="{s["name"]}" file="{s["file"]}" lines="{s["start"]}-{s["end"]}">\n'
                   f'{s["snippet"]}\n</surface>')
    return "\n".join(out)
