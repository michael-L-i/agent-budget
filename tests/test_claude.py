import json
import time

import pytest

from agent_budget import claude
from agent_budget.policy import BudgetError
from agent_budget.store import Store


@pytest.fixture
def settings(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.setattr(claude.Path, "home", lambda: home)
    monkeypatch.chdir(project)
    path = home / ".claude/settings.json"
    path.parent.mkdir()
    return path


def test_setup_preserves_existing_settings_and_is_reversible(settings):
    original = {
        "permissions": {"allow": ["Read"]},
        "statusLine": {
            "type": "command",
            "command": "printf 'my status'",
            "padding": 2,
        },
    }
    settings.write_text(json.dumps(original))
    claude.setup()
    installed = json.loads(settings.read_text())
    assert installed["permissions"] == original["permissions"]
    assert installed["statusLine"]["padding"] == 2
    assert "_claude-statusline" in installed["statusLine"]["command"]
    claude.setup()  # Repeated invocation must not wrap itself recursively.
    assert json.loads(settings.read_text()) == installed
    claude.setup(restore=True)
    assert json.loads(settings.read_text()) == original


def test_restore_never_overwrites_subsequent_user_changes(settings):
    claude.setup()
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "echo new"}}))
    with pytest.raises(BudgetError, match="nothing was overwritten"):
        claude.setup(restore=True)
    assert json.loads(settings.read_text())["statusLine"]["command"] == "echo new"


def test_project_override_wraps_effective_command(settings):
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "echo user"}}))
    project = claude.Path.cwd() / ".claude/settings.json"
    project.parent.mkdir()
    project.write_text(json.dumps({"statusLine": {"type": "command", "command": "echo project"}}))
    with pytest.raises(BudgetError, match="--project"):
        claude.setup()
    claude.setup(project=True)
    metadata = list(claude.state_dir().glob("claude-statusline-*.json"))
    assert json.loads(metadata[0].read_text())["forward"]["command"] == "echo project"
    claude.setup(project=True, restore=True)
    assert "statusLine" not in json.loads((project.parent / "settings.local.json").read_text())


def payload(now, used=40, session="native-session"):
    return {
        "session_id": session,
        "rate_limits": {"seven_day": {"used_percentage": used, "resets_at": now + 1000}},
        "cost": {"total_api_duration_ms": 300},
    }


def test_native_claude_capture_and_group_fetch(monkeypatch):
    now = time.time()
    store = Store()
    try:
        claude.capture(store, {"id": "first"}, payload(now, 40), now)
        claude.capture(store, {"id": "second"}, payload(now, 42, "second"), now)
        monkeypatch.setattr(claude, "auth_identity", lambda _: "account")
        result = claude.fetch_claude([{"id": "first"}, {"id": "second"}])
        assert result["windows"]["weekly"]["used"] == 42
        assert "native-session" not in str(result)
    finally:
        store.close()


def test_identical_repaint_never_renews_freshness(monkeypatch):
    now = time.time()
    store = Store()
    try:
        data = payload(now)
        claude.capture(store, {"id": "one"}, data, now - 100)
        claude.capture(store, {"id": "one"}, data, now)
        assert store.telemetry("one")["updated_at"] == now - 100
        with pytest.raises(BudgetError, match="stale"):
            claude.fetch_claude([{"id": "one"}])
        data["cost"]["total_api_duration_ms"] += 100
        claude.capture(store, {"id": "one"}, data, now)
        assert store.telemetry("one")["updated_at"] == now
    finally:
        store.close()


def test_missing_claude_telemetry_is_actionable():
    with pytest.raises(BudgetError, match="setup claude"):
        claude.fetch_claude([{"id": "not-recorded"}])


def test_missing_window_clears_old_reading(monkeypatch):
    now = time.time()
    store = Store()
    try:
        claude.capture(store, {"id": "one"}, payload(now), now)
        claude.capture(store, {"id": "one"}, {"session_id": "native-session"}, now + 1)
        monkeypatch.setattr(claude, "auth_identity", lambda _: "account")
        assert claude.fetch_claude([{"id": "one"}])["windows"] == {}
    finally:
        store.close()
