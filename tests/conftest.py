import pytest


@pytest.fixture(autouse=True)
def private_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BUDGET_HOME", str(tmp_path / "state"))
