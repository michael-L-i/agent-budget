import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid

import psutil
import pytest

from agent_budget.cli import arm, change_state, status
from agent_budget.policy import BudgetError, compile_policy
from agent_budget.store import Store
from agent_budget.watch import start


def until(predicate, seconds=8):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("Condition did not become true")


@pytest.fixture
def workers():
    children = []

    def spawn():
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        children.append(proc)
        return {
            "id": uuid.uuid4().hex[:12],
            "pid": proc.pid,
            "born": psutil.Process(proc.pid).create_time(),
            "provider": "claude",
            "cwd": "/tmp",
        }

    yield spawn
    for proc in children:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


@pytest.fixture
def store():
    state = Store()
    yield state
    for budget in state.budgets():
        if budget["state"] in ("starting", "active"):
            change_state(state, budget["id"], "cancelled")
    time.sleep(0.3)
    state.close()


def test_deadline_stops_selected_group_and_leaves_unrelated_worker(store, workers):
    selected = [workers(), workers()]
    unrelated = workers()
    budget = arm(store, {"duration_minutes": 0.015}, selected)
    until(lambda: store.get(budget["id"])["state"] == "stopped")
    assert all(
        not psutil.pid_exists(item["pid"])
        or psutil.Process(item["pid"]).status() == psutil.STATUS_ZOMBIE
        for item in selected
    )
    assert psutil.Process(unrelated["pid"]).is_running()
    assert "Time limit" in store.get(budget["id"])["reason"]


def test_cancel_keeps_worker_running(store, workers):
    target = workers()
    budget = arm(store, {"duration_minutes": 0.015}, [target])
    change_state(store, budget["id"], "cancelled")
    time.sleep(1)
    assert store.get(budget["id"])["state"] == "cancelled"
    assert psutil.Process(target["pid"]).status() != psutil.STATUS_ZOMBIE


def test_overlap_does_not_replace_existing_budget(store, workers):
    target = workers()
    existing = arm(store, {"duration_minutes": 2}, [target])
    with pytest.raises(BudgetError, match="already belongs"):
        arm(store, {"duration_minutes": 1}, [target])
    assert store.get(existing["id"])["state"] == "active"


def test_manual_stop_is_independent_of_agent(store, workers):
    budget = arm(store, {"duration_minutes": 2}, [workers()])
    change_state(store, budget["id"], "stopping")
    until(lambda: store.get(budget["id"])["state"] == "stopped")


def test_stale_meter_stops_instead_of_treating_missing_usage_as_zero(store, workers):
    now = time.time()
    snapshot = {
        "identity": "test",
        "updated_at": now - 91,
        "windows": {"weekly": {"used": 40, "resets_at": now + 1000}},
    }
    budget = {
        "id": uuid.uuid4().hex[:12],
        "state": "starting",
        "provider": "claude",
        "targets": [workers()],
        "created_at": now,
        "snapshot": snapshot,
        "policy": compile_policy({"usage": [{"max_additional_percent": 10}]}, snapshot, now),
    }
    store.save(budget)
    start(store, budget)
    until(lambda: store.get(budget["id"])["state"] == "stopped")
    assert "stale" in store.get(budget["id"])["reason"]


def test_time_limit_remains_live_while_usage_reader_is_hung(store, workers, tmp_path, monkeypatch):
    reader = tmp_path / "codex"
    reader.write_text("#!/bin/sh\nsleep 60\n")
    reader.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    now = time.time()
    snapshot = {
        "identity": "test",
        "updated_at": now,
        "windows": {"weekly": {"used": 40, "resets_at": now + 1000}},
    }
    budget = {
        "id": uuid.uuid4().hex[:12],
        "state": "starting",
        "provider": "codex",
        "targets": [workers()],
        "created_at": now,
        "snapshot": snapshot,
        "policy": compile_policy(
            {"duration_minutes": 0.015, "usage": [{"max_additional_percent": 10}]}, snapshot, now
        ),
    }
    store.save(budget)
    start(store, budget)
    until(lambda: store.get(budget["id"])["state"] == "stopped", seconds=5)


def test_dead_watcher_is_reported_unprotected(store, workers):
    budget = arm(store, {"duration_minutes": 2}, [workers()])
    proc = psutil.Process(budget["watcher"]["pid"])
    proc.kill()
    proc.wait(timeout=3)
    assert "UNPROTECTED" in status(store, budget["id"])[0]["health"]


def test_cli_never_accepts_raw_pids():
    result = subprocess.run(
        [sys.executable, "-m", "agent_budget.cli", "set", "--pid", "1"],
        input=json.dumps({"duration_minutes": 1}),
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "unrecognized arguments" in result.stderr


def test_public_cli_registers_ancestor_and_stops_it(tmp_path):
    # A disposable native executable with the expected CLI entrypoint name.
    # No real Claude/Codex instance is launched or signalled by this test.
    executable = tmp_path / "claude"
    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("Native CLI smoke test needs a C compiler")
    subprocess.run(
        [compiler, "-x", "c", "-", "-o", str(executable)],
        input=(
            "#include <stdlib.h>\nint main(int n, char **v) { return n == 2 ? system(v[1]) : 2; }\n"
        ),
        text=True,
        check=True,
        capture_output=True,
    )
    command = (
        f"{shlex.quote(sys.executable)} -m agent_budget.cli set <<'BUDGET'\n"
        '{"duration_minutes":0.03}\nBUDGET\nsleep 60\n'
    )
    proc = subprocess.Popen(
        [str(executable), command], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        output, error = proc.communicate(timeout=10)
        assert "active" in output, error
        assert "Stop by" in output
        assert proc.returncode != 0
        state = Store()
        try:
            until(lambda: state.budgets()[0]["state"] == "stopped")
        finally:
            state.close()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.parametrize(
    "second_used,second_email,reason",
    [
        (50, "test@example.invalid", "usage cutoff"),
        (41, "changed@example.invalid", "account changed"),
        (39, "test@example.invalid", "decreased unexpectedly"),
    ],
)
def test_quota_reader_drives_real_stopping(
    store,
    workers,
    tmp_path,
    monkeypatch,
    second_used,
    second_email,
    reason,
):
    marker = tmp_path / "sample-count"
    reader = tmp_path / "codex"
    reader.write_text(
        f"#!{sys.executable}\n"
        "import sys,json,time\nfrom pathlib import Path\n"
        f"marker = Path({str(marker)!r})\n"
        "second = marker.exists()\nmarker.touch()\n"
        f"reset = {time.time() + 3600!r}\n"
        "for line in sys.stdin:\n"
        " m=json.loads(line)\n"
        " if 'id' not in m: continue\n"
        " result={}\n"
        " if m['method']=='account/read':\n"
        f"  email={second_email!r} if second else 'test@example.invalid'\n"
        "  result={'account':{'type':'chatgpt','email':email}}\n"
        " if m['method']=='account/rateLimits/read':\n"
        f"  used={second_used} if second else 40\n"
        "  result={'rateLimits':{'secondary':{'usedPercent':used,\n"
        "    'windowDurationMins':10080,'resetsAt':reset}}}\n"
        " print(json.dumps({'id':m['id'],'result':result}),flush=True)\n"
    )
    reader.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    target = workers()
    target["provider"] = "codex"
    budget = arm(store, {"usage": [{"max_additional_percent": 10}]}, [target])
    until(lambda: store.get(budget["id"])["state"] == "stopped")
    assert reason in store.get(budget["id"])["reason"]


def test_native_claude_telemetry_drives_stopping(store, workers, tmp_path, monkeypatch):
    from agent_budget.claude import capture, fetch_claude

    reader = tmp_path / "claude"
    reader.write_text(
        f"#!{sys.executable}\nimport json\n"
        "print(json.dumps({'loggedIn':True,'authMethod':'claude.ai',"
        "'email':'test@example.invalid','orgId':'test'}))\n"
    )
    reader.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    now = time.time()
    target = workers()
    unrelated = workers()
    payload = {
        "session_id": "native-test-session",
        "rate_limits": {"seven_day": {"used_percentage": 40, "resets_at": now + 3600}},
    }
    capture(store, target, payload, now)
    snapshot = fetch_claude([target])
    budget = {
        "id": uuid.uuid4().hex[:12],
        "state": "starting",
        "provider": "claude",
        "targets": [target],
        "created_at": now,
        "snapshot": snapshot,
        "policy": compile_policy({"usage": [{"max_additional_percent": 10}]}, snapshot, now),
    }
    store.save(budget)
    payload["rate_limits"]["seven_day"]["used_percentage"] = 50
    capture(store, target, payload, time.time())
    start(store, budget)
    until(lambda: store.get(budget["id"])["state"] == "stopped")
    assert "usage cutoff" in store.get(budget["id"])["reason"]
    assert psutil.Process(unrelated["pid"]).status() != psutil.STATUS_ZOMBIE
