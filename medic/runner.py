"""Run any agent over a set of questions and write per-case results, for Medic.

The user's agent answers one question through its entrypoint (medic.toml: module:function).
Medic runs this in a checkout of the commit under test (bisection, fix candidates, canary),
the same way it runs its demo agent, and scores each answer against the expected facts.

    python -m medic.runner --entry app.chat:answer --cases .medic/evals.jsonl --out r.json \
        --session canary-abc --tag canary --tag release:abc123
"""

import argparse
import asyncio
import concurrent.futures
import contextlib
import contextvars
import importlib
import inspect
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

CHECKS_SUFFIX = ":medic-checks"     # the workflow Medic's own eval runs log under


def normalize(text: str) -> str:
    """Lowercase, with Unicode dashes and spaces folded to ASCII and Markdown emphasis removed."""
    text = re.sub(r"[‐-―−]", "-", text)
    text = re.sub(r"[  -​  　]", " ", text)
    return text.replace("*", "").lower()


def score(case: dict, result: dict) -> bool:
    """Passes when the agent answered and every expected fact group appears in the answer."""
    if result["status"] != "ok":
        return False
    answer = normalize(result["answer"])
    return all(any(contains(answer, normalize(alt)) for alt in group) for group in case.get("expect", []))


def contains(text: str, fact: str) -> bool:
    """The fact as whole words: an expected 99 is not in 199, nor Ann in Annabelle."""
    return re.search(r"(?<!\w)" + re.escape(fact.strip()) + r"(?!\w)", text) is not None


class Cancelled(RuntimeError):
    """The agent's own async work was cancelled: a failed case, not a stopped evaluation."""


async def awaited(awaitable):
    """Await the agent's work. A CancelledError that comes from inside it (it awaited a task that
    was cancelled) is the case failing; only a cancellation of this run itself still propagates."""
    try:
        return await awaitable
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None and getattr(task, "cancelling", lambda: 0)():
            raise
        raise Cancelled("the answer's async work was cancelled") from None


def load_entry(spec: str):
    module, _, name = spec.partition(":")
    if not module or not name:
        raise ValueError(f"entrypoint must be module:function, got {spec!r}")
    return getattr(importlib.import_module(module), name)


def text_of(result) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return str(result.get("answer") or result.get("output") or result.get("text") or "")
    return str(result or "")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="medic.runner")
    parser.add_argument("--entry", required=True)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--workflow", default=os.environ.get("MEDIC_WORKFLOW", "agent"))
    args = parser.parse_args(argv)

    os.environ["MEDIC_RUNNER"] = "1"        # the project's own neatlogs.init stands down (see medic init)
    sys.path.insert(0, os.getcwd())
    try:
        from dotenv import find_dotenv, load_dotenv
        load_dotenv(find_dotenv(usecwd=True))   # the project's .env
    except ImportError:
        pass
    import neatlogs
    # Medic's own eval runs log under their own workflow, so they never mix with the app's
    # production traffic in neatlogs (dashboards, analytics, search).
    workflow = args.workflow if args.workflow.endswith(CHECKS_SUFFIX) else args.workflow + CHECKS_SUFFIX
    neatlogs.init(workflow_name=workflow, tags=["agent-medic", *args.tag])
    entry = load_entry(args.entry)          # imported after init, so its model clients are traced
    import medic_runtime

    with open(args.cases, encoding="utf-8") as f:
        cases = [json.loads(line) for line in f if line.strip()]

    def record(case: dict, out, exc, started: float) -> dict:
        if exc is None:
            answer, status, error = text_of(out), "ok", ""
        else:                                 # a failing agent is a failed case, not a crashed eval
            answer, status, error = "", "error", f"{type(exc).__name__}: {exc}"
        stats = medic_runtime.last_request()  # the case's own request, which just closed
        result = {"status": status, "answer": answer, "error": error, "tool_calls": stats.get("tool_calls", 0),
                  # The runtime saw this request fail the way code fails (a tool error, a non-answer).
                  "tool_errors": int(bool(stats.get("failed")) and status == "ok"),
                  "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                  "duration_s": round(time.monotonic() - started, 2)}
        return {"id": case["id"], "question": case["question"], "expect": case.get("expect", []),
                "passed": score(case, result), **result}

    # Each case is one runtime request, opened here: everything the agent does for the question
    # (the entrypoint, and any async work it hands back) counts toward it, wherever it runs.

    def run_case(case: dict) -> dict:
        started = time.monotonic()
        with neatlogs.trace(f"case:{case['id']}", session_id=args.session, case_id=case["id"]):
            out, exc = None, None
            with contextlib.suppress(Exception), medic_runtime.request():
                try:
                    out = entry(case["question"])
                    if inspect.isawaitable(out):     # a sync wrapper around async work (return async_answer(q))
                        out = on_shared_loop(out, contextvars.copy_context())
                except Exception as e:  # noqa: BLE001
                    exc = e
                    raise                            # so the request records how it failed
            return record(case, out, exc, started)

    loop_box, loop_lock = {}, threading.Lock()

    def shared_loop():
        """One long-lived event loop for every awaited answer (async clients are bound to theirs)."""
        with loop_lock:                      # worker threads ask at once: still exactly one loop
            if "loop" not in loop_box:
                loop = asyncio.new_event_loop()
                threading.Thread(target=loop.run_forever, daemon=True).start()
                loop_box["loop"] = loop
            return loop_box["loop"]

    def on_shared_loop(awaitable, ctx: contextvars.Context):
        """Await on the shared loop, inside the caller's context (its request and its trace)."""
        loop, done = shared_loop(), concurrent.futures.Future()

        async def settle():
            return await awaited(awaitable)

        def finish(t: asyncio.Task):
            if t.cancelled():            # never leave the worker waiting on a cancelled answer
                done.set_exception(Cancelled("the answer's async work was cancelled"))
            elif t.exception() is not None:
                done.set_exception(t.exception())
            else:
                done.set_result(t.result())

        def start():
            task = ctx.run(loop.create_task, settle())       # the task runs in a copy of ctx
            task.add_done_callback(finish)
        loop.call_soon_threadsafe(start)
        return done.result()

    async def run_async(cases: list[dict]) -> list[dict]:
        # One event loop for every case: async clients (AsyncOpenAI and other pooled ones) are
        # usually shared across calls and bound to the loop they first ran on.
        gate = asyncio.Semaphore(args.workers)

        async def one(case: dict) -> dict:
            async with gate:
                started = time.monotonic()
                with neatlogs.trace(f"case:{case['id']}", session_id=args.session, case_id=case["id"]):
                    out, exc = None, None
                    with contextlib.suppress(Exception), medic_runtime.request():
                        try:
                            out = await awaited(entry(case["question"]))
                        except Exception as e:  # noqa: BLE001
                            exc = e
                            raise
                    return record(case, out, exc, started)   # no await since the request closed
        return list(await asyncio.gather(*(one(c) for c in cases)))

    if inspect.iscoroutinefunction(entry):
        results = asyncio.run(run_async(cases))
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            results = list(pool.map(run_case, cases))
        if "loop" in loop_box:
            loop_box["loop"].call_soon_threadsafe(loop_box["loop"].stop)
    neatlogs.flush()
    neatlogs.shutdown()
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"session": args.session, "provider": "project", "model": args.entry, "cases": results}, f, indent=2)
    print(f"{sum(r['passed'] for r in results)}/{len(results)} cases passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
