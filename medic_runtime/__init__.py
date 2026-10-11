"""Medic runtime: first aid inside a running agent, while Medic works on the cure.

The agent opts in by registering the parts Medic may stand in for:

    import medic_runtime as medic

    @medic.surface("read_doc")                 # a function Medic may swap for its healthy version
    def read_doc(doc_id): ...

    system = medic.value("system_prompt", SYSTEM)    # read at call time: Medic may pin a known-good value
    with medic.request():                            # one agent request: loop guard scope
        turn = medic.call_model(lambda m: provider.chat(m, ...), MODEL)
        result = medic.guard_tool("search_docs", args, lambda: handler(args))

When Medic diagnoses an incident it publishes a signed first-aid bundle. The runtime applies
it only to the release it was issued for (MEDIC_RELEASE), only until it expires, and only to
registered names. Without a bundle every call goes straight through.

Standard library only: this module ships inside the user's agent.
"""

from medic_runtime.core import (  # noqa: F401
    FirstAidError, answer, call_model, entrypoint, failed, guard_tool, interventions, last_request, request, reset,
    status, surface, value,
)
