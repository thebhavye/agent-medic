"""Tools the research agent can call. Both go to the knowledge-base API over HTTP."""

import os

import httpx

import medic_runtime as medic

KB_URL = os.environ.get("KB_URL", "http://127.0.0.1:8781")
TIMEOUT_S = 10

TOOL_SCHEMAS = [
    {
        "name": "search_docs",
        "description": "Search the Halcyon knowledge base. Returns up to 3 matching documents "
                       "with their id, title and the opening of the text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords to search for."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_doc",
        "description": "Read the full text of one document by its id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string", "description": "Document id from search_docs."},
            },
            "required": ["doc_id"],
        },
    },
]


# Registered with Medic's runtime: during an incident it may run their last healthy versions.
@medic.surface("kb_client")
def _get(path: str, **params):
    resp = httpx.get(KB_URL + path, params=params, timeout=TIMEOUT_S)
    resp.raise_for_status()
    return resp.json()


@medic.surface("search_docs")
def search_docs(query: str) -> list[dict]:
    return [{"id": h["id"], "title": h["title"], "snippet": h["snippet"]} for h in _get("/search", q=query)]


@medic.surface("read_doc")
def read_doc(doc_id: str) -> str:
    return _get(f"/docs/{doc_id}")["body"]


TOOL_HANDLERS = {
    "search_docs": lambda args: search_docs(args["query"]),
    "read_doc": lambda args: read_doc(args["doc_id"]),
}
