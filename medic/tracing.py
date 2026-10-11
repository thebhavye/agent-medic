"""Which neatlogs instrumentations an app needs, from what it imports.

`neatlogs.init()` without `instrumentations=` records the spans the app opens itself, but not its
model calls: on a LangChain RAG app the traces had no LLM span at all, so no tokens, no model
errors and no step-level evidence for Medic or for the neatlogs dashboard. With
`instrumentations=["openai"]` the same call showed up as an LLM span with its tokens. Medic names
the instrumentations explicitly, from the libraries the app imports (directly or through
LangChain), both in the tracing `agent-medic init` adds and in its own eval runs.
"""

import ast
import importlib.util
from pathlib import Path

# imported module (or prefix) -> (neatlogs instrumentation, the package it instruments)
IMPORTS = {
    "openai": ("openai", "openai"),
    "langchain_openai": ("openai", "openai"),
    "llama_index.llms.openai": ("openai", "openai"),
    "anthropic": ("anthropic", "anthropic"),
    "langchain_anthropic": ("anthropic", "anthropic"),
    "google.genai": ("google_genai", "google.genai"),
    "google.generativeai": ("google_generativeai", "google.generativeai"),
    "langchain_google_genai": ("google_generativeai", "google.generativeai"),
    "mistralai": ("mistralai", "mistralai"),
    "langchain_mistralai": ("mistralai", "mistralai"),
    "cohere": ("cohere", "cohere"),
    "langchain_cohere": ("cohere", "cohere"),
    "groq": ("groq", "groq"),
    "langchain_groq": ("groq", "groq"),
    "ollama": ("ollama", "ollama"),
    "langchain_ollama": ("ollama", "ollama"),
    "litellm": ("litellm", "litellm"),
    "together": ("together", "together"),
    "boto3": ("bedrock", "boto3"),
    "langchain_aws": ("bedrock", "boto3"),
    "chromadb": ("chromadb", "chromadb"),
    "langchain_chroma": ("chromadb", "chromadb"),
    "langchain_community.vectorstores": ("chromadb", "chromadb"),
    "pinecone": ("pinecone", "pinecone"),
    "qdrant_client": ("qdrant", "qdrant_client"),
}
SKIP_DIRS = {"node_modules", "site-packages", "__pycache__", "tests", "test"}


def _imports(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return set()
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
    return found


def _installed(package: str) -> bool:
    try:
        return importlib.util.find_spec(package) is not None
    except (ImportError, ValueError):
        return False


def detect(root: Path, files: list[str] | None = None, check_installed: bool = True) -> list[str]:
    """neatlogs instrumentation names for the libraries the app's code imports (sorted)."""
    root = Path(root)
    paths = [root / f for f in files] if files else [
        p for p in root.rglob("*.py")
        if not any(part.startswith(".") or part in SKIP_DIRS or part.startswith("venv")
                   for part in p.relative_to(root).parts[:-1])]
    imported = set().union(*(_imports(p) for p in paths if p.is_file())) if paths else set()
    names = set()
    for module in imported:
        for prefix, (name, package) in IMPORTS.items():
            if module == prefix or module.startswith(prefix + "."):
                if not check_installed or _installed(package):
                    names.add(name)
    return sorted(names)
