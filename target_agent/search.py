"""Keyword ranking over the corpus. Runs inside the knowledge-base service."""

import re

from target_agent.corpus import DOCS

STOPWORDS = {"the", "a", "an", "of", "and", "is", "what", "who", "when", "where", "in", "to",
             "for", "did", "does", "how", "its", "it", "was", "which", "by", "on", "at", "halcyon"}

SNIPPET_CHARS = 120


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOPWORDS]


def rank(query: str, limit: int = 3) -> list[dict]:
    terms = set(_tokens(query))
    scored = []
    for doc_id, doc in DOCS.items():
        title_tokens = _tokens(doc["title"] + " " + doc_id)
        body_tokens = _tokens(doc["body"])
        score = sum(3 for t in title_tokens if t in terms) + sum(1 for t in body_tokens if t in terms)
        if score:
            scored.append((score, doc_id))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [
        {"id": doc_id, "title": DOCS[doc_id]["title"], "snippet": DOCS[doc_id]["body"][:SNIPPET_CHARS]}
        for _, doc_id in scored[:limit]
    ]
