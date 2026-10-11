"""`agent-medic init`: connect an existing agent to Medic in one reviewable diff.

Reads the repo's Python code (never runs it) and finds:
- the entrypoint: the function that answers one question (it reaches a model call);
- the parts it calls: retrievers, tools, prompt builders. Medic may swap these for their last
  healthy version during an incident;
- the prompts and settings those parts read (system prompt, model, top_k, ...). Medic may pin
  these to their last healthy value.

Then it proposes the edits: `@medic.entrypoint`, `@medic.surface(...)`, `medic.value(...)`,
neatlogs tracing with the release tags Medic needs (if missing), and a medic.toml. Nothing is
written without --write. Run it again after big refactors.
"""

import ast
import difflib
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from medic import project as project_mod

SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".medic", "site-packages", "dist",
             "build", ".tox", ".mypy_cache", ".pytest_cache"}
# A first parameter that carries the user's question: question, query_text, user_message, prompt, q, ...
QUESTION_PARAM = re.compile(r"^(q|msg)$|question|query|prompt|message|input|text|ask", re.I)
LLM_SUFFIXES = ("chat.completions.create", "messages.create", "responses.create", "generate_content",
                "ChatCompletion.create", "completion", "acompletion")
LLM_RECEIVERS = {"llm", "model", "chat", "chain", "agent", "client"}
SETTING_NAME = re.compile(r"(PROMPT|SYSTEM|INSTRUCTION|TEMPLATE|MODEL|TEMPERATURE|TOP_?K|^K$|CHUNK|MAX_TOKENS|TOP_P|"
                          r"THRESHOLD|CUTOFF|RELEVANCE|MIN_SCORE|SIMILARITY|LIMIT|N_RESULTS|NUM_RESULTS)", re.I)
MAX_SURFACES = 12


@dataclass
class Func:
    name: str
    file: str
    node: ast.FunctionDef
    calls: set = field(default_factory=set)
    llm: bool = False


@dataclass
class Plan:
    root: Path
    entrypoint: Func | None = None
    surfaces: list = field(default_factory=list)          # [Func]
    values: list = field(default_factory=list)            # [(file, name, [ast.Name loads])]
    has_neatlogs_init: bool = False
    has_release_tag: bool = False
    notes: list = field(default_factory=list)
    edits: dict = field(default_factory=dict)             # file -> new text
    script: "Func | None" = None                          # answer logic inside a no-argument main()


def _py_files(root: Path) -> list[Path]:
    out = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
            continue
        if rel.parts[0] in ("tests", "test") or rel.name.startswith("test_") or rel.name == "setup.py":
            continue
        out.append(path)
    return out


def _dotted(node) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _is_llm_call(call: ast.Call) -> bool:
    name = _dotted(call.func)
    if any(name == s or name.endswith("." + s) for s in LLM_SUFFIXES):
        return True
    # LangChain-style: llm.invoke(...), chain.invoke(...)
    return name.split(".")[-1] in ("invoke", "ainvoke", "predict", "run") and name.split(".")[0].lower() in LLM_RECEIVERS


def scan(root: Path) -> Plan:
    plan = Plan(root=root.resolve())
    funcs: list[Func] = []
    by_name: dict[str, list] = {}
    trees: dict[str, tuple[str, ast.Module]] = {}
    for path in _py_files(plan.root):
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        rel = path.relative_to(plan.root).as_posix()
        trees[rel] = (text, tree)
        if "neatlogs.init(" in text:
            plan.has_neatlogs_init = True
            plan.has_release_tag = plan.has_release_tag or "release:" in text
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                f = Func(node.name, rel, node)
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        if _is_llm_call(sub):
                            f.llm = True
                        callee = sub.func.id if isinstance(sub.func, ast.Name) else (
                            sub.func.attr if isinstance(sub.func, ast.Attribute) else None)
                        if callee:
                            f.calls.add(callee)
                funcs.append(f)
                by_name.setdefault(node.name, []).append(f)

    def resolve(name: str, caller: Func) -> Func | None:
        """A called name: the function in the caller's own file first (several files may each
        define a main()), else one elsewhere in the repo."""
        options = by_name.get(name) or []
        return next((f for f in options if f.file == caller.file), options[0] if options else None)

    def reaches_llm(f: Func, seen=()) -> bool:
        if f is None or id(f) in seen:
            return False
        return f.llm or any(reaches_llm(resolve(c, f), (*seen, id(f))) for c in f.calls)

    def first_param(f: Func) -> str:
        args = [a.arg for a in f.node.args.args if a.arg not in ("self", "cls")]
        return args[0] if args else ""

    candidates = [f for f in funcs if reaches_llm(f) and QUESTION_PARAM.search(first_param(f) or '')]
    called = {id(resolve(c, f)) for f in candidates for c in f.calls}
    tops = [f for f in candidates if id(f) not in called] or candidates
    # Prefer an obvious name, then the shortest path to the model (the answer function itself).
    tops.sort(key=lambda f: (f.name not in ("answer", "ask", "chat", "respond", "query", "run", "main_answer"),
                             f.name.startswith("_"), f.file, f.node.lineno))
    if not tops:
        script = next((f for f in funcs if reaches_llm(f) and not first_param(f)), None)
        if script is not None:
            plan.script = script
            plan.notes.append(f"the answer logic lives inside {script.file}:{script.name}() (a script), so no "
                              "function answers one question yet; see the refactor prompt below")
        else:
            plan.notes.append("no entrypoint found: no function taking a question reaches a model call; "
                              "set [agent] entrypoint in medic.toml by hand")
        return plan
    plan.entrypoint = tops[0]
    if len(tops) > 1:
        plan.notes.append("other possible entrypoints: " + ", ".join(f"{f.file}:{f.name}" for f in tops[1:4]))

    # The parts the answer path calls (breadth first, within this repo).
    order, queue = [], [(c, plan.entrypoint) for c in sorted(plan.entrypoint.calls)]
    while queue and len(order) < MAX_SURFACES:
        name, caller = queue.pop(0)
        f = resolve(name, caller)
        if f is None or f is plan.entrypoint or f in order:
            continue
        order.append(f)
        queue.extend((c, f) for c in sorted(f.calls))
    plan.surfaces = order

    # Settings those functions read: module-level literals with a telling name (or a long string).
    path_funcs = [plan.entrypoint, *plan.surfaces]
    for rel, (text, tree) in trees.items():
        consts = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    continue
                name = node.targets[0].id
                if isinstance(value, (str, int, float, list, dict)) and (
                        SETTING_NAME.search(name) or (isinstance(value, str) and len(value) >= 80)):
                    consts[name] = node
        if not consts:
            continue
        for name in sorted(consts):
            loads = [n for f in path_funcs if f.file == rel for n in ast.walk(f.node)
                     if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load)
                     and id(n) not in _already_wrapped(f.node)]
            if loads:
                plan.values.append((rel, name, loads))
    return plan


def _already_wrapped(fn: ast.AST) -> set[int]:
    """Names that are already the value inside medic.value("...", NAME): a second run leaves them."""
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and _dotted(n.func).endswith("medic.value") and len(n.args) > 1:
            out.add(id(n.args[1]))
    return out


# -- edits --------------------------------------------------------------------------------

def _value_name(name: str) -> str:
    return name.lower()


def _insert_import(lines: list[str], tree: ast.Module, line: str) -> None:
    """Add an import after the module's last top-level import (or its docstring)."""
    if any(existing.strip() == line for existing in lines):
        return
    last = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last = node.end_lineno
        elif last:
            break
    if not last and tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(getattr(tree.body[0], "value", None),
                                                                                       ast.Constant):
        last = tree.body[0].end_lineno
    lines.insert(last, line + "\n")


NEATLOGS_SNIPPET = '''
# Traced into neatlogs with the release and environment Medic needs (added by agent-medic init).
if not os.environ.get("MEDIC_RUNNER"):          # Medic's own runs initialize neatlogs themselves
    neatlogs.init(workflow_name="{workflow}",
                  tags=["env:" + os.environ.get("MEDIC_ENV", "prod"),
                        "release:" + os.environ.get("GIT_SHA", "dev")[:12]])
'''


def build_edits(plan: Plan, workflow: str) -> dict[str, str]:
    """file -> new text, for every file the plan touches."""
    if plan.entrypoint is None:
        return {}
    by_file: dict[str, list] = {}
    for f in [plan.entrypoint, *plan.surfaces]:
        by_file.setdefault(f.file, [])
    for rel, _, _ in plan.values:
        by_file.setdefault(rel, [])
    out = {}
    for rel in by_file:
        text = (plan.root / rel).read_text(encoding="utf-8")
        tree = ast.parse(text)
        lines = text.splitlines(keepends=True)
        # 1. value loads, replaced right to left on each line so columns stay valid
        loads = sorted(((n.lineno, n.col_offset, n.end_col_offset, name) for r, name, ns in plan.values if r == rel
                        for n in ns), reverse=True)
        for lineno, start, end, name in loads:
            line = lines[lineno - 1]
            if line[start:end] == name:
                lines[lineno - 1] = line[:start] + f'medic.value("{_value_name(name)}", {name})' + line[end:]
        # 2. decorators, bottom up so line numbers stay valid
        marks = [(plan.entrypoint.node, "@medic.entrypoint")] if plan.entrypoint.file == rel else []
        marks += [(f.node, f'@medic.surface("{f.name}")') for f in plan.surfaces if f.file == rel]
        for node, deco in sorted(marks, key=lambda m: -m[0].lineno):
            top = min([d.lineno for d in node.decorator_list] + [node.lineno])
            existing = "".join(lines[top - 1:node.lineno])
            if "medic.entrypoint" in existing or "medic.surface" in existing:
                continue
            lines.insert(top - 1, " " * node.col_offset + deco + "\n")
        # 3. imports (and neatlogs, once, in the entrypoint's module)
        _insert_import(lines, tree, "import medic_runtime as medic")
        if rel == plan.entrypoint.file and not plan.has_neatlogs_init:
            _insert_import(lines, tree, "import neatlogs")
            _insert_import(lines, tree, "import os")
            wanted = {"import medic_runtime as medic", "import neatlogs", "import os"}
            last = max(i for i, line in enumerate(lines) if line.strip() in wanted)
            lines.insert(last + 1, NEATLOGS_SNIPPET.format(workflow=workflow))
        new = "".join(lines)
        ast.parse(new)                      # never propose a file that doesn't parse
        out[rel] = new
    return out


def diff(plan: Plan, edits: dict[str, str]) -> str:
    chunks = []
    for rel, new in edits.items():
        old = (plan.root / rel).read_text(encoding="utf-8")
        chunks.append("".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                                   fromfile=f"a/{rel}", tofile=f"b/{rel}")))
    return "\n".join(c for c in chunks if c)


def refactor_prompt(plan: Plan) -> str | None:
    """For a script-style app: the exact change to ask the user's coding agent for."""
    s = plan.script
    if s is None:
        return None
    text = (plan.root / s.file).read_text(encoding="utf-8", errors="replace")
    src = ast.get_source_segment(text, s.node) or ""
    arg = next(iter(re.findall(r"args\.(\w+)", src)), "question")
    llm_line = next((line.strip() for line in src.splitlines() if _LLM_TEXT.search(line)), "the model call")
    return (f"In {s.file}, the answering logic is inside {s.name}(), which reads `{arg}` from the command line. "
            f"Extract it into a module-level function `def answer({arg}: str) -> str:` that takes the question, does "
            f"the retrieval and the model call (`{llm_line}`), and returns the model's answer text instead of "
            f"printing it. Keep the existing early exits as returns (return a short message such as "
            f"\"Unable to find matching results.\"). Then make {s.name}() parse the arguments, call answer({arg}) "
            f"and print what it printed before, so the command line behaves exactly as it does now. Don't change "
            f"prompts, models, retrieval settings or any other behavior.")


_LLM_TEXT = re.compile(r"\.(predict|invoke|create|generate_content|completion)\(")


def report(plan: Plan) -> str:
    if plan.entrypoint is None:
        prompt = refactor_prompt(plan)
        return "\n".join(plan.notes + ([f"\nPaste this to your coding agent, then run agent-medic init again:\n\n"
                                        f"  {prompt}"] if prompt else []))
    lines = [f"entrypoint  {plan.entrypoint.file}:{plan.entrypoint.name}"]
    lines += [f"surface     {f.file}:{f.name}" for f in plan.surfaces]
    lines += [f"value       {rel}:{name} ({len(loads)} use{'s' if len(loads) != 1 else ''})"
              for rel, name, loads in plan.values]
    lines.append("neatlogs    " + ("already initialized" + ("" if plan.has_release_tag else
                                   "; add tags env:<env> and release:<git sha> so Medic can tell releases apart")
                                   if plan.has_neatlogs_init else "will be added, tagged with env and release"))
    lines += [f"note        {n}" for n in plan.notes]
    return "\n".join(lines)


def run(root: Path, workflow: str | None = None, write: bool = False, mode: str = "pr", branch: str | None = None):
    """Scan, propose, and (with write) apply. Returns (plan, edits, project)."""
    root = root.resolve()
    workflow = workflow or root.name
    branch = branch or _current_branch(root) or "main"
    plan = scan(root)
    edits = build_edits(plan, workflow)
    proj = None
    if plan.entrypoint is not None:
        module = plan.entrypoint.file[:-3].replace("/", ".")
        files = sorted({plan.entrypoint.file, *(f.file for f in plan.surfaces), *(r for r, _, _ in plan.values)})
        proj = project_mod.Project(root=root, entrypoint=f"{module}:{plan.entrypoint.name}", files=files,
                                   workflow=workflow, mode=mode, branch=branch, python=_venv_python(root))
        if write:
            for rel, new in edits.items():
                (root / rel).write_text(new, encoding="utf-8")
            project_mod.write(proj)
            _requirements(root)
            _gitignore(root)
    return plan, edits, proj


def _requirements(root: Path) -> None:
    req = root / "requirements.txt"
    if not req.exists():
        return
    text = req.read_text(encoding="utf-8")
    names = {re.split(r"[<>=\[ ;]", line.strip(), maxsplit=1)[0].lower() for line in text.splitlines() if line.strip()}
    add = [line for name, line in REQUIREMENTS if name not in names]
    if add:
        req.write_text(text.rstrip("\n") + "\n" + "\n".join(add) + "\n", encoding="utf-8")


def _gitignore(root: Path) -> None:
    """Medic's working state (incidents, evidence, worktrees) never belongs in the user's history."""
    path = root / ".gitignore"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if ".medic/" in {line.strip() for line in text.splitlines()}:
        return
    path.write_text(text + ("\n" if text and not text.endswith("\n") else "") + ".medic/\n", encoding="utf-8")


REQUIREMENTS = (("agent-medic", "agent-medic @ git+https://github.com/thebhavye/agent-medic.git"),
                ("neatlogs", "neatlogs"))


def _current_branch(root: Path) -> str:
    proc = subprocess.run(["git", "branch", "--show-current"], cwd=root, capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _venv_python(root: Path) -> str:
    """The app's own virtualenv, so Medic runs it with its dependencies (relative to the repo root)."""
    for venv in sorted(p for p in root.iterdir() if p.is_dir() and (p / "pyvenv.cfg").is_file()):
        for exe in ("Scripts/python.exe", "bin/python"):
            if (venv / exe).is_file():
                return f"{venv.name}/{exe}"
    return ""
