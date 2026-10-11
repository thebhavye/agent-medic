"""Medic names neatlogs instrumentations for the libraries an app uses, or its model calls go unrecorded."""

from medic import doctor, init as init_mod, tracing


def test_libraries_map_to_neatlogs_instrumentations(tmp_path):
    (tmp_path / "rag.py").write_text("from langchain_openai import ChatOpenAI\n"
                                     "from langchain_google_genai import GoogleGenerativeAIEmbeddings\n"
                                     "from langchain_community.vectorstores import Chroma\n")
    (tmp_path / "bot.py").write_text("import anthropic\nimport os\n")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "x.py").write_text("import cohere\n")                  # not the app's code
    assert tracing.detect(tmp_path, check_installed=False) == ["anthropic", "chromadb", "google_generativeai", "openai"]


def test_only_installed_libraries_are_instrumented(tmp_path):
    (tmp_path / "a.py").write_text("import openai\nimport some_llm_sdk_that_is_not_installed\nimport qdrant_client\n")
    names = tracing.detect(tmp_path)
    assert "openai" in names and "qdrant" not in names                         # qdrant_client isn't installed here


def test_init_writes_the_instrumentations_into_the_tracing(tmp_path):
    (tmp_path / "bot.py").write_text("from openai import OpenAI\n\nclient = OpenAI()\n\n\n"
                                     "def answer(question: str) -> str:\n"
                                     "    return client.chat.completions.create(model='m', messages=[]).choices[0].message.content\n")
    plan, edits, _ = init_mod.run(tmp_path)
    assert 'instrumentations=["openai"]' in edits["bot.py"]
    compile(edits["bot.py"], "bot.py", "exec")


def test_doctor_warns_when_tracing_records_no_model_calls(tmp_path, monkeypatch):
    (tmp_path / "bot.py").write_text("import neatlogs\nfrom openai import OpenAI\n"
                                     'neatlogs.init(workflow_name="x", tags=["release:" + "abc"])\n'
                                     "def answer(question):\n    return 'ok'\n")
    (tmp_path / "medic.toml").write_text('[agent]\nentrypoint = "bot:answer"\nfiles = ["bot.py"]\n')
    from medic import project
    check = doctor.tracing(project.load(tmp_path / "medic.toml"))
    assert check.status == doctor.WARN and 'instrumentations=["openai"]' in check.fix
