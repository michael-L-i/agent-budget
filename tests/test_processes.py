import subprocess
import sys

import psutil
import pytest

from agent_budget.processes import provider_for, resolve, stop_targets


@pytest.mark.parametrize(
    "executable,argv,expected",
    [
        ("/bin/claude", ["claude"], "claude"),
        ("/bin/codex", ["codex", "exec", "task"], "codex"),
        ("/bin/codex", ["codex", "app-server"], None),
        ("/bin/claude", ["claude", "--bg"], None),
        ("/bin/bash", ["bash", "claude"], None),
        ("/bin/node", ["node", "/lib/@anthropic-ai/claude-code/cli.js"], "claude"),
        ("/bin/node", ["node", "/tmp/claude-code-fake.js"], None),
    ],
)
def test_only_known_cli_entrypoints(executable, argv, expected):
    assert provider_for(executable, argv) == expected


def test_reused_pid_identity_is_never_signalled():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        identity = {
            "id": "test",
            "pid": child.pid,
            "born": psutil.Process(child.pid).create_time() - 1,
        }
        assert resolve(identity) is None
        assert stop_targets([identity], grace=0.1) == []
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait()
