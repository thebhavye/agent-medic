"""`agent-medic init` on the app shapes people write: a method, a web handler, a query engine."""

import importlib
import sys

import pytest

from medic import init as init_mod
from medic_runtime.core import question_of

CLASS_APP = '''MODEL = "small"


class Reply:
    def __init__(self, text):
        self.content = [type("Block", (), {"text": text})()]


class Messages:
    def create(self, model, messages, **kw):
        return Reply("Each project stores 10 GB. (" + messages[0]["content"] + ")")


class Client:
    messages = Messages()


class SupportBot:
    def __init__(self):
        self.client = Client()

    def search(self, query):
        return ["notes"]

    def ask(self, question: str) -> str:
        msg = self.client.messages.create(model=MODEL, messages=[{"role": "user", "content": question}])
        return msg.content[0].text
'''

HANDLER_APP = '''from dataclasses import dataclass

ROUTES = []


def post(path):
    def register(fn):
        ROUTES.append(fn)              # the framework keeps whatever function it was given
        return fn
    return register


class Completions:
    def create(self, model, messages):
        return {"choices": [{"text": "Pro costs 99 dollars."}]}


class Chat:
    completions = Completions()


class Client:
    chat = Chat()


client = Client()
SYSTEM_PROMPT = "Answer from the docs only."


@dataclass
class ChatRequest:
    message: str


def retrieve(query):
    return ["Pro costs 99 dollars."]


@post("/chat")
def chat(req: ChatRequest):
    docs = retrieve(req.message)
    out = client.chat.completions.create(model="m", messages=[SYSTEM_PROMPT, docs, req.message])
    return {"answer": out["choices"][0]["text"]}
'''

ENGINE_APP = '''class Engine:
    def query(self, question):
        return "The office is in Narvik."


query_engine = Engine()


def ask_docs(question: str) -> str:
    return str(query_engine.query(question))
'''


def load(tmp_path, monkeypatch, module):
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("MEDIC_RUNNER", "1")              # the tracing init adds stands down
    sys.modules.pop(module, None)
    return importlib.import_module(module)


def test_a_method_is_the_entrypoint_and_medic_asks_through_an_adapter(tmp_path, monkeypatch):
    (tmp_path / "bot.py").write_text(CLASS_APP)
    plan, edits, proj = init_mod.run(tmp_path, write=True)
    assert plan.adapter == ("method", "SupportBot") and proj.entrypoint == "bot:medic_answer"
    assert "    @medic.entrypoint\n    def ask(" in edits["bot.py"]
    bot = load(tmp_path, monkeypatch, "bot")
    assert "10 GB" in bot.medic_answer("How much can a project store?")
    assert bot.SupportBot().ask("again").startswith("Each project")         # production path unchanged


def test_a_web_handler_keeps_its_route_and_medic_builds_the_request(tmp_path, monkeypatch):
    (tmp_path / "api.py").write_text(HANDLER_APP)
    plan, edits, proj = init_mod.run(tmp_path, write=True)
    assert plan.adapter == ("request", "ChatRequest", "message") and proj.entrypoint == "api:medic_answer"
    assert '@post("/chat")\n@medic.entrypoint\ndef chat(' in edits["api.py"]           # route decorator outermost
    api = load(tmp_path, monkeypatch, "api")
    assert getattr(api.ROUTES[0], "__medic_entrypoint__", False)                   # the route serves Medic's wrapper
    assert api.medic_answer("How much is Pro?") == {"answer": "Pro costs 99 dollars."}


def test_a_query_engine_call_counts_as_the_model_call(tmp_path):
    (tmp_path / "rag.py").write_text(ENGINE_APP)
    plan, _, proj = init_mod.run(tmp_path)
    assert proj.entrypoint == "rag:ask_docs" and plan.adapter is None


def test_a_class_that_needs_arguments_gets_a_clear_next_step(tmp_path):
    (tmp_path / "bot.py").write_text(CLASS_APP.replace("def __init__(self):", "def __init__(self, api_key):"))
    plan, _, proj = init_mod.run(tmp_path)
    assert proj is None and "medic_answer" in init_mod.report(plan)


@pytest.mark.parametrize("args,expected", [
    ((object(), "Where is it?"), "Where is it?"),                                  # a method: self skipped
    ((type("Req", (), {"message": "How much?"})(),), "How much?"),                # a request body
    (({"query": "Which plan?"},), "Which plan?"),                                  # a dict body
])
def test_the_runtime_records_the_real_question(args, expected):
    assert question_of(list(args)) == expected
