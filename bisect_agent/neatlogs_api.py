"""Read traces back from neatlogs' public API.

agent-bisect tags each eval run with a unique session id, then reads that session's traces
to score the commit on what neatlogs recorded: cost, tokens and tool calls per run.
"""

import os
import threading
import time

import httpx

BASE_URL = os.environ.get("NEATLOGS_API_URL", "https://app.neatlogs.com/api/v1/public")


class NeatlogsReader:
    def __init__(self) -> None:
        self.token = os.environ.get("NEATLOGS_TOKEN")
        self.project_id = os.environ.get("NEATLOGS_PROJECT_ID")
        self.api_key = os.environ.get("NEATLOGS_API_KEY")

    @property
    def enabled(self) -> bool:
        return bool((self.token and self.project_id) or self.api_key)

    def _headers(self) -> dict:
        if self.token and self.project_id:
            return {"Authorization": f"Bearer {self.token}", "x-project-id": self.project_id}
        headers = {"x-api-key": self.api_key}
        if self.project_id:
            headers["x-project-id"] = self.project_id
        return headers

    def list_traces(self, max_pages: int = 10, **params) -> list[dict]:
        traces, cursor = [], None
        for _ in range(max_pages):
            data = self._get("/traces", limit=50, **params, **({"cursor": cursor} if cursor else {}))
            traces.extend(data["traces"])
            cursor = data["page"]["nextCursor"]
            if not data["page"]["hasMore"]:
                break
        return traces

    def _get(self, path: str, **params) -> dict:
        if getattr(self, "_client", None) is None:
            self._client = httpx.Client(base_url=BASE_URL, headers=self._headers(), timeout=30)
        last_error: Exception | None = None
        for attempt in range(8):
            self._wait_for_budget()
            try:
                resp = self._client.get(path, params=params)
            except httpx.TransportError as exc:      # dropped connection: back off and retry
                last_error = exc
                time.sleep(1 + attempt * 2)
                continue
            self._note_budget(resp)
            if resp.status_code in (409, 429, 503):   # trace not ready yet, or rate limited
                last_error = httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
                if resp.status_code != 429:
                    time.sleep(int(resp.headers.get("Retry-After", "3")))
                continue
            resp.raise_for_status()
            return resp.json()["data"]
        raise last_error or RuntimeError(f"GET {path} failed")

    # The public API allows a fixed number of requests per window for each credential and
    # reports what's left on every response. All threads share one budget: when it runs low,
    # everyone waits for the window to reset instead of collecting 429s.
    _budget_lock = threading.Lock()
    _blocked_until = 0.0

    def _wait_for_budget(self) -> None:
        delay = NeatlogsReader._blocked_until - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def _note_budget(self, resp: httpx.Response) -> None:
        h = resp.headers
        reset_after = float(h.get("X-NeatLogs-RateLimit-Reset-After") or h.get("Retry-After") or 1)
        remaining = int(h.get("RateLimit-Remaining", "1") or 1)
        if resp.status_code == 429 or remaining <= 2:
            with NeatlogsReader._budget_lock:
                NeatlogsReader._blocked_until = max(NeatlogsReader._blocked_until,
                                                    time.monotonic() + reset_after + 0.2)

    def spans(self, trace_id: str) -> list[dict]:
        return self._get(f"/traces/{trace_id}/spans", limit=50)["spans"]

    def payload(self, trace_id: str, span_id: str, fields: str = "error,input,output") -> dict:
        """{"error"|"input"|"output": str | None} for one span."""
        data = self._get(f"/traces/{trace_id}/spans/{span_id}/payload", fields=fields)
        return {k: (v["value"] if v.get("available") else None) for k, v in data["fields"].items()}

    def session_traces(self, session_id: str, expected: int, timeout_s: float = 120) -> list[dict]:
        """Poll until the session has `expected` finalized case traces, or time runs out."""
        deadline = time.monotonic() + timeout_s
        traces: list[dict] = []
        while time.monotonic() < deadline:
            # Each eval run has its own session, so every trace in it is one case. (neatlogs
            # names a trace after its first named span, not the root, so don't filter on name.)
            traces = self.list_traces(sessionId=session_id)
            if len(traces) >= expected and all(t.get("finalizationStatus") == "finalized" for t in traces):
                return traces
            time.sleep(5)
        return traces


def summarize(traces: list[dict]) -> dict:
    n = len(traces)
    return {
        "cost_per_run": sum(t["totalCostUsd"] for t in traces) / n,
        "tokens_per_run": sum(t["totalTokens"] for t in traces) / n,
        "tool_calls_per_run": sum(t["toolCallsCount"] for t in traces) / n,
        "trace_ids": [t["traceId"] for t in traces],
    }
