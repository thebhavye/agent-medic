"""Probe the knowledge-base API and describe the shape of what it returns.

A shape is the JSON structure without the values: types and key names. Comparing today's
shape with the one recorded while traffic was healthy shows exactly how a dependency changed.
"""

import httpx


def shape(value, depth: int = 0):
    if depth > 4:
        return "…"
    if isinstance(value, dict):
        return {k: shape(v, depth + 1) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [shape(value[0], depth + 1)] if value else []
    return type(value).__name__


def probe(base_url: str, query: str, attempts: int = 6) -> dict:
    """Hit the search and docs endpoints a few times; record statuses, headers and shapes."""
    statuses, search_shape, doc_shape, version, doc_id = [], None, None, None, None
    with httpx.Client(base_url=base_url, timeout=10) as client:
        for _ in range(attempts):
            try:
                r = client.get("/search", params={"q": query})
            except httpx.HTTPError as exc:
                statuses.append(f"connect-error: {type(exc).__name__}")
                continue
            statuses.append(r.status_code)
            version = r.headers.get("X-KB-API-Version", version)
            if r.status_code == 200 and search_shape is None:
                body = r.json()
                search_shape = shape(body)
                items = body if isinstance(body, list) else next((v for v in body.values() if isinstance(v, list)), [])
                if items:
                    doc_id = items[0].get("id") or items[0].get("doc_id")
        if doc_id:
            r = client.get(f"/docs/{doc_id}")
            statuses.append(r.status_code)
            if r.status_code == 200:
                doc_shape = shape(r.json())
    return {"statuses": statuses, "api_version": version, "search": search_shape, "doc": doc_shape,
            "sample_query": query}


def snapshot(base_url: str, questions: list[str]) -> dict:
    return probe(base_url, next((q for q in questions if q), "pilot plant"))


def diff(old, new, path: str = "$") -> list[str]:
    """Human-readable differences between two shapes."""
    if type(old) is not type(new):
        return [f"{path}: was {_kind(old)}, now {_kind(new)}"]
    if isinstance(old, dict):
        out = []
        for k in sorted(set(old) | set(new)):
            if k not in new:
                out.append(f"{path}.{k}: removed")
            elif k not in old:
                out.append(f"{path}.{k}: added ({_kind(new[k])})")
            else:
                out += diff(old[k], new[k], f"{path}.{k}")
        return out
    if isinstance(old, list):
        if old and new:
            return diff(old[0], new[0], f"{path}[]")
        return []
    return [] if old == new else [f"{path}: was {old}, now {new}"]


def _kind(v) -> str:
    if isinstance(v, dict):
        return "object {" + ", ".join(sorted(v)) + "}"
    if isinstance(v, list):
        return "list"
    return str(v)
