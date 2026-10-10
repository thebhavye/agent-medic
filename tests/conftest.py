import socket

import pytest

from target_agent import kb_service, tools


@pytest.fixture(scope="session")
def kb_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = kb_service.serve(port)
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@pytest.fixture(autouse=True)
def _point_tools_at_test_kb(kb_url, monkeypatch):
    monkeypatch.setattr(tools, "KB_URL", kb_url)
