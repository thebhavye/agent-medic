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
from medic.project import tomllib

SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".medic", "site-packages", "dist",
             "build", ".tox", ".mypy_cache", ".pytest_cache"}
# A first parameter that carries the user's question: question, query_text, user_message, prompt, q, ...
QUESTION_PARAM = re.compile(r"^(q|msg)$|question|query|prompt|message|input|text|ask", re.I)
LLM_SUFFIXES = ("chat.completions.create", "messages.create", "responses.create", "generate_content",
                "ChatCompletion.create", "completion", "acompletion")
LLM_RECEIVERS = {"llm", "model", "chat", "chain", "agent", "client"}
ENGINE_METHODS = {"query", "aquery", "chat", "achat", "stream_chat", "complete", "acomplete"}
ENGINE_RECEIVERS = ("engine", "llm", "agent", "index", "retriever_chat")
ADAPTER = "medic_answer"     # the function init adds when the answer isn't a plain f(question)
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
    cls: "ast.ClassDef | None" = None                     # set for a method


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
    # How Medic calls the answer when it isn't a plain f(question): ("method", class) or
    # ("request", request class, field). init then adds medic_answer(question) next to it.
    adapter: tuple | None = None


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
    parts = name.split(".")
    method, receiver = parts[-1], (parts[-2] if len(parts) > 1 else "").lower()
    # LangChain-style: llm.invoke(...), chain.invoke(...)
    if method in ("invoke", "ainvoke", "predict", "run") and parts[0].lower() in LLM_RECEIVERS:
        return True
    # LlamaIndex-style: query_engine.query(...), chat_engine.chat(...), llm.complete(...)
    return method in ENGINE_METHODS and any(word in receiver for word in ENGINE_RECEIVERS)


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
        defs = [(node, None) for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        defs += [(m, node) for node in tree.body if isinstance(node, ast.ClassDef)
                 for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for node, cls in defs:
            f = Func(node.name, rel, node, cls=cls)
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

    # Request bodies a web handler takes: a class in the same file with a question-like str field
    # (FastAPI/Pydantic `class ChatRequest(BaseModel): message: str`, a dataclass, a TypedDict).
    bodies: dict[tuple, str] = {}
    for rel, (_, tree) in trees.items():
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                fields = [s.target.id for s in node.body if isinstance(s, ast.AnnAssign)
                          and isinstance(s.target, ast.Name) and _dotted(s.annotation) == "str"]
                hit = next((n for n in fields if QUESTION_PARAM.search(n)), None)
                if hit:
                    bodies[(rel, node.name)] = hit

    def body_of(f: Func) -> str | None:
        args = [a for a in f.node.args.args if a.arg not in ("self", "cls")]
        if args and args[0].annotation is not None:
            return _dotted(args[0].annotation) if (f.file, _dotted(args[0].annotation)) in bodies else None
        return None

    candidates = [f for f in funcs if reaches_llm(f) and f.name != "__init__"
                  and (QUESTION_PARAM.search(first_param(f) or '') or body_of(f))]
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
    entry = tops[0]
    if entry.cls is not None:
        init = next((m for m in entry.cls.body if isinstance(m, ast.FunctionDef) and m.name == "__init__"), None)
        required = len(init.args.args) - 1 - len(init.args.defaults) if init else 0
        if required > 0:
            plan.notes.append(f"the answer is {entry.cls.name}.{entry.name}(), and {entry.cls.name}() needs "
                              f"arguments: add a module-level `def {ADAPTER}(question): return "
                              f"<your {entry.cls.name}>.{entry.name}(question)`, then run agent-medic init again")
            return plan
        plan.adapter = ("method", entry.cls.name)
    elif not QUESTION_PARAM.search(first_param(entry) or "") and body_of(entry):
        plan.adapter = ("request", body_of(entry), bodies[(entry.file, body_of(entry))])
    plan.entrypoint = entry
    if len(tops) > 1:
        plan.notes.append("other possible entrypoints: " + ", ".join(f"{f.file}:{f.name}" for f in tops[1:4]))

    # The parts the answer path calls (breadth first, within this repo).
    order, queue = [], [(c, plan.entrypoint) for c in sorted(plan.entrypoint.calls)]
    while queue and len(order) < MAX_SURFACES:
        name, caller = queue.pop(0)
        f = resolve(name, caller)
        if f is None or f is plan.entrypoint or f in order or f.cls is not None:
            continue                  # methods aren't swapped by first aid (only module-level parts)
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
            # Only uses in the function's body: a default argument (`k=TOP_K`) or a decorator is
            # evaluated once, when the module loads, so first aid could never change it there.
            loads = [n for f in path_funcs if f.file == rel and name not in _local_names(f.node)
                     for stmt in f.node.body for n in ast.walk(stmt)
                     if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load)
                     and id(n) not in _already_wrapped(f.node)]
            if loads:
                plan.values.append((rel, name, loads))
    return plan


def _local_names(fn: ast.AST) -> set[str]:
    """Names a function binds itself (parameters, assignments, loop and with targets, imports):
    there `MODEL` is the function's own variable, not the module setting."""
    a = fn.args
    names = {p.arg for p in [*a.posonlyargs, *a.args, *a.kwonlyargs]}
    names |= {p.arg for p in (a.vararg, a.kwarg) if p is not None}
    declared_global = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            names.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            names |= {(alias.asname or alias.name).split(".")[0] for alias in n.names}
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            declared_global |= set(n.names)
    return names - declared_global


def _already_wrapped(fn: ast.AST) -> set[int]:
    """Names that are already the value inside medic.value("...", NAME): a second run leaves them."""
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and _dotted(n.func).endswith("medic.value") and len(n.args) > 1:
            out.add(id(n.args[1]))
    return out


# -- edits --------------------------------------------------------------------------------

def _module(rel: str) -> str:
    module = rel[:-3].replace("/", ".")
    return module[: -len(".__init__")] if module.endswith(".__init__") else module


def _runtime_name(rel: str, name: str) -> str:
    """The name a part is registered under: qualified by its module, so two modules' `search`
    (or two `MODEL` settings) never share first aid."""
    return f"{_module(rel)}.{name}"


def _value_name(rel: str, name: str) -> str:
    return _runtime_name(rel, name.lower())


def _statement_start(tree: ast.Module, lineno: int) -> int:
    """0-based index of the first line (decorators included) of the top-level statement holding `lineno`."""
    for node in tree.body:
        start = min([d.lineno for d in getattr(node, "decorator_list", [])] + [node.lineno])
        if start <= lineno <= node.end_lineno:
            return start - 1
    return lineno - 1


def _import_index(tree: ast.Module) -> int:
    """0-based line index just after the module's top-level imports (or its docstring)."""
    last = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last = node.end_lineno
        elif last:
            break
    if not last and tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(getattr(tree.body[0], "value", None),
                                                                                       ast.Constant):
        last = tree.body[0].end_lineno
    return last


NEATLOGS_SNIPPET = '''
# Traced into neatlogs with the release and environment Medic needs (added by agent-medic init).
if not os.environ.get("MEDIC_RUNNER"):          # Medic's own runs initialize neatlogs themselves
    neatlogs.init(workflow_name="{workflow}",
                  tags=["env:" + os.environ.get("MEDIC_ENV", "prod"),
                        "release:" + os.environ.get("GIT_SHA", "dev")[:12]]{instrumentations})
'''


def _instrumentations_arg(root: Path) -> str:
    """`, instrumentations=[...]` for the model and vector-store libraries the app imports: without
    it neatlogs doesn't record the app's model calls (no LLM spans, no tokens)."""
    from medic import tracing
    names = tracing.detect(root, check_installed=False)
    if not names:
        return ""
    return ",\n                  instrumentations=[" + ", ".join(f'"{n}"' for n in names) + "]"


def _fstring_quote(tree: ast.Module, lines: list[str], lineno: int, col: int) -> str | None:
    """The quote character of the innermost f-string around (lineno, col), or None."""
    best = None
    for n in ast.walk(tree):
        if isinstance(n, ast.JoinedStr) and (n.lineno, n.col_offset) <= (lineno, col) <= (n.end_lineno, n.end_col_offset):
            if best is None or (n.lineno, n.col_offset) > (best.lineno, best.col_offset):
                best = n
    if best is None:
        return None
    opening = lines[best.lineno - 1][best.col_offset:].lstrip("rRbBfFuU")
    return opening[:1] or None

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
        # 1. value loads, replaced in place (line count unchanged), right to left so columns stay valid
        loads = sorted(((n.lineno, n.col_offset, n.end_col_offset, name) for r, name, ns in plan.values if r == rel
                        for n in ns), reverse=True)
        for lineno, start, end, name in loads:
            line = lines[lineno - 1]
            if line[start:end] == name:
                # Inside an f-string, the name's quotes must differ from the f-string's own:
                # reusing them is a SyntaxError before Python 3.12.
                q = "'" if _fstring_quote(tree, lines, lineno, start) == '"' else '"'
                lines[lineno - 1] = line[:start] + f'medic.value({q}{_value_name(rel, name)}{q}, {name})' + line[end:]
        # 2. new lines, all placed by the original line numbers and inserted bottom up
        inserts = []                        # (0-based index, text)
        marks = [(plan.entrypoint.node, "@medic.entrypoint")] if plan.entrypoint.file == rel else []
        marks += [(f.node, f'@medic.surface("{_runtime_name(rel, f.name)}")') for f in plan.surfaces if f.file == rel]
        tops = []
        for node, deco in marks:
            top = min([d.lineno for d in node.decorator_list] + [node.lineno])
            if "medic.entrypoint" in "".join(lines[top - 1:node.lineno]) or \
                    "medic.surface" in "".join(lines[top - 1:node.lineno]):
                continue
            tops.append(top - 1)
            # Right above the def: a framework's own decorators (@app.post(...)) stay outermost and
            # register the wrapped function.
            inserts.append((node.lineno - 1, " " * node.col_offset + deco + "\n"))
        present = {line.strip() for line in lines}
        header = [imp for imp in ("import medic_runtime as medic",) if imp not in present]
        if rel == plan.entrypoint.file and not plan.has_neatlogs_init:
            header += [imp for imp in ("import neatlogs", "import os") if imp not in present]
            header.append(NEATLOGS_SNIPPET.format(workflow=workflow,
                                                  instrumentations=_instrumentations_arg(plan.root)))
        if header:
            # After the imports, but never below a decorator that needs `medic`.
            at = min([_import_index(tree), *(_statement_start(tree, t + 1) for t in tops)])
            inserts.append((at, "".join(h if h.endswith("\n") else h + "\n" for h in header)))
        # Same index: the header goes above the decorator (inserting in reverse keeps that order).
        for index, text_ in sorted(inserts, key=lambda i: (i[0], i[1].lstrip().startswith("@")), reverse=True):
            lines.insert(index, text_)
        new = "".join(lines)
        if rel == plan.entrypoint.file and plan.adapter and not re.search(rf"^(async )?def {ADAPTER}\(", new, re.M):
            new = new.rstrip("\n") + "\n" + _adapter_source(plan) + "\n"
        ast.parse(new)                      # never propose a file that doesn't parse
        if new != text:                     # already connected: nothing to change in this file
            out[rel] = new
    return out


def _adapter_source(plan: Plan) -> str:
    """medic_answer(question): how Medic asks the agent a question when the answer is a method or a
    web handler taking a request body. Production keeps calling the agent as before."""
    entry, kind = plan.entrypoint, plan.adapter[0]
    is_async = isinstance(entry.node, ast.AsyncFunctionDef)
    head = f"{'async ' if is_async else ''}def {ADAPTER}(question: str):"
    await_ = "await " if is_async else ""
    note = "# Added by agent-medic init: Medic asks your agent its known-answer questions through this."
    if kind == "method":
        cls = plan.adapter[1]
        return (f"\n\n{note}\n_medic_{cls.lower()} = None\n\n\n{head}\n"
                f"    global _medic_{cls.lower()}\n"
                f"    if _medic_{cls.lower()} is None:\n"
                f"        _medic_{cls.lower()} = {cls}()\n"
                f"    return {await_}_medic_{cls.lower()}.{entry.name}(question)")
    body, field_name = plan.adapter[1], plan.adapter[2]
    return f"\n\n{note}\n{head}\n    return {await_}{entry.name}({body}({field_name}=question))"


def diff(plan: Plan, edits: dict[str, str]) -> str:
    chunks = []
    for rel, new in edits.items():
        path = plan.root / rel
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        chunks.append("".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                                   fromfile=f"a/{rel}" if path.exists() else "/dev/null",
                                                   tofile=f"b/{rel}")))
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
    e = plan.entrypoint
    lines = [f"entrypoint  {e.file}:{e.cls.name + '.' if e.cls is not None else ''}{e.name}"]
    if plan.adapter:
        how = (f"a {plan.adapter[1]}() instance" if plan.adapter[0] == "method"
               else f"a {plan.adapter[1]}({plan.adapter[2]}=question) request")
        lines.append(f"adapter     {e.file}:{ADAPTER}(question) added: Medic asks questions through {how}")
    lines += [f"surface     {f.file}:{f.name}" for f in plan.surfaces]
    lines += [f"value       {rel}:{name} ({len(loads)} use{'s' if len(loads) != 1 else ''})"
              for rel, name, loads in plan.values]
    lines.append("neatlogs    " + ("already initialized" + ("" if plan.has_release_tag else
                                   "; add tags env:<env> and release:<git sha> so Medic can tell releases apart")
                                   if plan.has_neatlogs_init else "will be added, tagged with env and release"))
    lines += [f"note        {n}" for n in plan.notes]
    return "\n".join(lines)


def run(root: Path, workflow: str | None = None, write: bool = False, mode: str = "pr", branch: str | None = None,
        pr: str = "open"):
    """Scan, propose, and (with write) apply. Returns (plan, edits, project); `edits` holds every
    file init would change (code, medic.toml, dependencies, .gitignore), so a dry run shows it all."""
    root = root.resolve()
    workflow = workflow or root.name
    branch = branch or _current_branch(root) or "main"
    plan = scan(root)
    edits = build_edits(plan, workflow)
    proj = None
    if plan.entrypoint is not None:
        files = sorted({plan.entrypoint.file, *(f.file for f in plan.surfaces), *(r for r, _, _ in plan.values)})
        proj = project_mod.Project(root=root, entrypoint=f"{_module(plan.entrypoint.file)}:{ADAPTER if plan.adapter else plan.entrypoint.name}",
                                   files=files, workflow=workflow, mode=mode, branch=branch, pr=pr,
                                   python=_venv_python(root))
        if not (root / project_mod.FILE).exists():       # an existing medic.toml is the user's: kept
            edits[project_mod.FILE] = project_mod.render(proj)
        deps = _dependencies(root)
        if deps is None:
            plan.notes.append("could not add the dependencies automatically (no requirements.txt, or a "
                              "pyproject.toml init can't edit safely): add " + " and ".join(line for _, line in REQUIREMENTS) + " to your app's dependencies")
        elif deps:
            edits[deps[0]] = deps[1]
        ignore = _gitignore(root)
        if ignore is not None:
            edits[".gitignore"] = ignore
        if write:
            for rel, new in edits.items():
                _write(root / rel, new)
    return plan, edits, proj


def _write(path: Path, text: str) -> None:
    """Write as the file was: a CRLF file stays CRLF, an LF file stays LF (Windows would otherwise
    turn every line of an LF file into CRLF, a whole-file diff in the user's repo)."""
    crlf = path.exists() and b"\r\n" in path.read_bytes()
    with open(path, "w", encoding="utf-8", newline="\r\n" if crlf else "\n") as f:
        f.write(text)


REQUIREMENTS = (("agent-medic", "agent-medic @ git+https://github.com/thebhavye/agent-medic.git"),
                ("neatlogs", "neatlogs"))


def _dependencies(root: Path):
    """(file, new text) adding the runtime and neatlogs to the app's dependencies; () when they're
    already there; None when there's no manifest init knows how to edit."""
    req = root / "requirements.txt"
    if req.exists():
        text = req.read_text(encoding="utf-8")
        names = {re.split(r"[<>=\[ ;@]", line.strip(), maxsplit=1)[0].lower()
                 for line in text.splitlines() if line.strip()}
        add = [line for name, line in REQUIREMENTS if name not in names]
        return ("requirements.txt", text.rstrip("\n") + "\n" + "\n".join(add) + "\n") if add else ()
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        return None
    text = pyproject.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    section = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped
            continue
        if section == "[project]" and re.match(r"dependencies\s*=\s*\[\s*$", stripped):
            # PEP 621, one dependency per line
            end = next((j for j in range(i + 1, len(lines)) if lines[j].strip().startswith("]")), None)
            if end is None:
                return None
            have = "".join(lines[i + 1:end]).lower()
            add = [f'    "{line}",\n' for name, line in REQUIREMENTS if f'"{name}' not in have]
            if not add:
                return ()
            body = lines[i + 1:end]
            last = next((k for k in range(len(body) - 1, -1, -1)
                         if body[k].strip() and not body[k].strip().startswith("#")), None)
            if last is not None and not body[last].rstrip().endswith(","):
                body[last] = body[last].rstrip() + ",\n"    # the old last entry needs its separator
            return _checked_pyproject("".join(lines[:i + 1] + body + add + lines[end:]))
        if section == "[tool.poetry.dependencies]":
            block_end = next((j for j in range(i, len(lines)) if lines[j].strip().startswith("[")), len(lines))
            have = {re.split(r"\s*=", lines[j].strip(), maxsplit=1)[0].strip('"').lower()
                    for j in range(i, block_end) if "=" in lines[j]}
            poetry = {"agent-medic": 'agent-medic = { git = "https://github.com/thebhavye/agent-medic.git" }\n',
                      "neatlogs": 'neatlogs = "*"\n'}
            add = [line for name, line in poetry.items() if name not in have]
            if not add:
                return ()
            at = block_end
            while at > i and not lines[at - 1].strip():
                at -= 1                  # before the blank lines that close the table
            if not lines[at - 1].endswith("\n"):
                lines[at - 1] += "\n"            # a file without a final newline
            return _checked_pyproject("".join(lines[:at] + add + lines[at:]))
    return None


def _checked_pyproject(text: str):
    """The proposed pyproject.toml, only if it still parses and now lists both dependencies; else
    None, and init asks the user to add them by hand rather than break their build."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    listed = " ".join([*data.get("project", {}).get("dependencies", []),
                       *data.get("tool", {}).get("poetry", {}).get("dependencies", {})]).lower()
    return ("pyproject.toml", text) if all(name in listed for name, _ in REQUIREMENTS) else None


def _gitignore(root: Path) -> str | None:
    """.gitignore with Medic's working state kept out of the user's history: .medic/ (incidents,
    evidence, worktrees) and .bisect/ (the regression investigator's runs). None when both are."""
    path = root / ".gitignore"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    have = {line.strip() for line in text.splitlines()}
    add = [entry for entry in (".medic/", ".bisect/") if entry not in have]
    if not add:
        return None
    return text + ("\n" if text and not text.endswith("\n") else "") + "".join(f"{e}\n" for e in add)


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
