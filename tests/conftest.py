import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import host as host_module  # noqa: E402
from host import Host  # noqa: E402

SPY = "python tests/spy_server.py"


@pytest.fixture
def spy_log(tmp_path, monkeypatch):
    path = tmp_path / "spy.jsonl"
    path.touch()
    monkeypatch.setenv("SPY_LOG", str(path))

    def calls():
        return [json.loads(line) for line in path.read_text().splitlines()]
    return calls


@pytest.fixture
async def host(tmp_path, spy_log):
    """A host on a fresh database, with the notes server and the spy server connected."""
    h = Host(tmp_path / "test.sqlite")
    await h.start()
    await h.add_server(SPY, "spy")
    yield h
    await h.stop()


@pytest.fixture
def fast_timeout(monkeypatch):
    monkeypatch.setattr(host_module, "RUN_TIMEOUT", 1.0)
