"""The knowledge-base API the research agent depends on, as a separate HTTP service.

It lives outside the agent's code on purpose: Medic has to tell "our code broke" apart from
"a dependency changed under us". Chaos modes, set via POST /admin/chaos {"mode": ...}:

    normal       GET /search?q=  -> [{"id", "title", "snippet"}]     GET /docs/<id> -> {"id", "title", "body"}
    schema_v2    the provider shipped API v2: search -> {"results": [{"doc_id", "title", "summary"}]},
                 docs -> {"doc_id", "title", "content"}
    rate_limit   about half of requests get 429 with Retry-After: 1
    outage       every request gets 503
    stale_data   same API and shapes, but documents come from an outdated snapshot: the figures
                 are wrong. Nothing errors; the agent answers confidently and incorrectly.

    python -m target_agent.kb_service --port 8781
"""

import argparse
import json
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from target_agent.corpus import DOCS
from target_agent.search import rank

MODES = ("normal", "schema_v2", "rate_limit", "outage", "stale_data")

# The outdated snapshot served in stale_data mode: same documents, older figures.
STALE = [("412 MWh", "318 MWh"), ("Narvik", "Bodo"), ("9,200 full cycles", "6,100 full cycles"),
         ("$86 million", "$41 million"), ("Fjord Ventures", "Nordic Seed Partners"), ("11 weeks", "19 weeks"),
         ("1.6 GWh", "0.7 GWh"), ("20 MW", "8 MW"), ("Nordhavn Energi", "Vestkyst Kraft"),
         ("94 percent", "71 percent"), ("UL 9540A", "UL 1973"), ("Kestrel Materials", "Arden Chemicals"),
         ("Bergen-2", "Tromso-1"), ("Ines Okafor", "Lars Vik"), ("March 2024", "March 2021")]


def _body(doc: dict) -> str:
    body = doc["body"]
    if _state["mode"] == "stale_data":
        for new, old in STALE:
            body = body.replace(new, old)
    return body


_state = {"mode": "normal", "requests": 0}
_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):   # keep the console quiet
        pass

    def _send(self, status: int, body, headers: dict | None = None) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-KB-API-Version", "2" if _state["mode"] == "schema_v2" else "1")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != "/admin/chaos":
            return self._send(404, {"error": "not found"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if body.get("mode") not in MODES:
            return self._send(400, {"error": f"mode must be one of {MODES}"})
        with _lock:
            _state["mode"] = body["mode"]
        return self._send(200, {"mode": _state["mode"]})

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/admin/chaos":
            return self._send(200, dict(_state))
        if url.path == "/health":
            return self._send(503 if _state["mode"] == "outage" else 200, {"ok": _state["mode"] != "outage"})
        with _lock:
            _state["requests"] += 1
            mode = _state["mode"]
        if mode == "outage":
            return self._send(503, {"error": "service unavailable"})
        if mode == "rate_limit" and random.random() < 0.5:
            return self._send(429, {"error": "rate limited"}, {"Retry-After": "1"})

        if url.path == "/search":
            query = parse_qs(url.query).get("q", [""])[0]
            hits = rank(query)
            if mode == "schema_v2":
                return self._send(200, {"results": [{"doc_id": h["id"], "title": h["title"], "summary": h["snippet"]}
                                                    for h in hits]})
            return self._send(200, hits)

        if url.path.startswith("/docs/"):
            doc_id = url.path.removeprefix("/docs/")
            if doc_id not in DOCS:
                return self._send(404, {"error": f"unknown doc {doc_id!r}"})
            doc = DOCS[doc_id]
            if mode == "schema_v2":
                return self._send(200, {"doc_id": doc_id, "title": doc["title"], "content": _body(doc)})
            return self._send(200, {"id": doc_id, "title": doc["title"], "body": _body(doc)})

        return self._send(404, {"error": "not found"})


def serve(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8781)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"knowledge base API on http://127.0.0.1:{args.port} (mode: {_state['mode']})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
