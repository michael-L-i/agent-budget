"""Claude Code's native status-line bridge and reversible setup."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import processes
from .meter import MAX_AGE, identity, window
from .policy import BudgetError
from .store import Store, state_dir


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise BudgetError(f"Expected an object in {path.name}; left it unchanged.")
    return value


def write_json(path: Path, value: dict):
    if path.is_symlink():
        raise BudgetError("Refusing to replace a symlinked settings file.")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".budget-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def setup(project: bool = False, restore: bool = False) -> str:
    root = state_dir()
    user = Path.home() / ".claude/settings.json"
    local = Path.cwd() / ".claude/settings.local.json"
    settings_path = local if project else user
    key = hashlib.sha256(str(settings_path).encode()).hexdigest()[:16]
    metadata_path = root / f"claude-statusline-{key}.json"
    with (root / "claude-setup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        settings = read_json(settings_path)
        metadata = read_json(metadata_path)
        old = settings.get("statusLine")
        installed = metadata and old == metadata.get("installed")
        if restore:
            if not installed:
                raise BudgetError(
                    "Status line has changed or is not ours; nothing was overwritten."
                )
            if metadata["previous"] is None:
                settings.pop("statusLine", None)
            else:
                settings["statusLine"] = metadata["previous"]
            write_json(settings_path, settings)
            metadata_path.unlink()
            return "Previous Claude status line restored."
        # Preserve the effective inherited status line when installing a project override.
        effective = read_json(user).get("statusLine")
        for directory in reversed((Path.cwd(), *Path.cwd().parents)):
            for name in ("settings.json", "settings.local.json"):
                path = directory / ".claude" / name
                if path == user:
                    continue
                candidate = read_json(path).get("statusLine")
                if candidate is not None:
                    if not project:
                        raise BudgetError(
                            "A project status line overrides user settings. "
                            "Run setup claude --project to preserve and wrap it here."
                        )
                    effective = candidate
        previous = metadata["previous"] if installed else old
        forward = metadata["forward"] if installed else effective
        if forward is not None and (
            not isinstance(forward, dict)
            or forward.get("type") != "command"
            or not isinstance(forward.get("command"), str)
        ):
            raise BudgetError("Unsupported existing status line; left it unchanged.")
        command = shlex.join(
            [
                "env",
                f"AGENT_BUDGET_HOME={root}",
                sys.executable,
                "-m",
                "agent_budget.cli",
                "_claude-statusline",
                "--config",
                key,
            ]
        )
        replacement = {**(forward or {}), "type": "command", "command": command}
        # Do not add a timer: repeated callbacks are not fresh provider measurements.
        metadata = {"previous": previous, "forward": forward, "installed": replacement}
        write_json(metadata_path, metadata)
        settings["statusLine"] = replacement
        write_json(settings_path, settings)
    return "Claude usage bridge enabled; your existing status line is preserved."


def capture(store: Store, target: dict, payload: dict, now: float) -> dict:
    native_id = payload.get("session_id")
    if not isinstance(native_id, str) or not native_id:
        raise BudgetError("Claude status-line data has no session identity.")
    limits = payload.get("rate_limits") or {}
    windows = {}
    for native, name in (("five_hour", "five_hour"), ("seven_day", "weekly")):
        value = limits.get(native)
        if isinstance(value, dict):
            windows[name] = window(value.get("used_percentage"), value.get("resets_at"), now)
    # A repaint of identical data must not extend freshness indefinitely.
    signature = hashlib.sha256(
        json.dumps(
            {
                "session": native_id,
                "limits": limits,
                "api_ms": (payload.get("cost") or {}).get("total_api_duration_ms"),
                "tokens": payload.get("context_window"),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    with store.transaction():
        previous = store.telemetry(target["id"])
        snapshot = {
            "updated_at": now,
            "windows": windows,
            "signature": signature,
            "native_session": hashlib.sha256(native_id.encode()).hexdigest(),
        }
        if previous and previous["signature"] == signature:
            snapshot["updated_at"] = previous["updated_at"]
        store.save_telemetry(target["id"], snapshot)
    return snapshot


def statusline(config: str):
    if len(config) != 16 or any(c not in "0123456789abcdef" for c in config):
        raise BudgetError("Invalid Claude status-line configuration.")
    raw = sys.stdin.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise BudgetError("Claude status-line input is too large.")
    snapshot = None
    try:
        payload = json.loads(raw)
        target = processes.current()
        if target["provider"] != "claude":
            raise BudgetError("Status-line telemetry must come from a Claude CLI session.")
        store = Store()
        try:
            store.session(target)
            snapshot = capture(store, target, payload, time.time())
        finally:
            store.close()
    except (BudgetError, ValueError, TypeError, AttributeError):
        # Missing quota must not break the user's existing display.
        pass
    previous = read_json(state_dir() / f"claude-statusline-{config}.json").get("forward")
    if previous:
        # This is the user's pre-existing shell command, never generated from provider data.
        with subprocess.Popen(
            previous["command"], shell=True, stdin=subprocess.PIPE, start_new_session=True
        ) as proc:
            try:
                proc.communicate(raw.encode(), timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
    elif snapshot and snapshot["windows"]:
        print(
            " · ".join(
                f"{name}: {value['used']:g}% used" for name, value in snapshot["windows"].items()
            )
        )
    else:
        print("Budget: waiting for Claude usage")


def auth_identity(cancelled: threading.Event | None = None) -> str:
    with subprocess.Popen(
        ["claude", "auth", "status", "--json"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    ) as proc:
        until = time.monotonic() + 10
        while True:
            try:
                raw, _ = proc.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= until or (cancelled and cancelled.is_set()):
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.communicate()
                    raise BudgetError("Claude login check timed out or was cancelled.") from None
    if proc.returncode or len(raw) > 65536:
        raise BudgetError("Claude could not report its signed-in account.")
    auth = json.loads(raw)
    if auth.get("loggedIn") is not True or auth.get("authMethod") != "claude.ai":
        raise BudgetError("Sign in to Claude Code with a subscription for percentage budgets.")
    return identity("claude", auth.get("email"), auth.get("orgId"))


def fetch_claude(targets: list[dict], cancelled: threading.Event | None = None) -> dict:
    if not targets:
        raise BudgetError("Select a Claude terminal session before reading its usage.")
    now = time.time()
    store = Store()
    try:
        samples = [store.telemetry(target["id"]) for target in targets]
    finally:
        store.close()
    if any(sample is None for sample in samples):
        raise BudgetError(
            "No Claude usage received yet. Run setup claude, finish a response, then retry. "
            "Claude Code 2.1.251+ with subscription rate_limits is required; no budget was armed."
        )
    if any(not -5 <= now - sample["updated_at"] <= MAX_AGE for sample in samples):
        raise BudgetError("Claude usage is stale; wait for a fresh response before arming.")
    windows = {}
    for name in ("five_hour", "weekly"):
        readings = [sample["windows"].get(name) for sample in samples]
        if any(reading is None for reading in readings):
            continue
        if len({reading["resets_at"] for reading in readings}) != 1:
            raise BudgetError("Selected Claude sessions disagree about the quota window.")
        windows[name] = max(readings, key=lambda value: value["used"])
    fingerprint = auth_identity(cancelled)
    # Clearing/resuming a different conversation requires a fresh budget.
    fingerprint = hashlib.sha256(
        (
            fingerprint + ":" + ":".join(sorted(sample["native_session"] for sample in samples))
        ).encode()
    ).hexdigest()
    return {
        "identity": fingerprint,
        "updated_at": min(s["updated_at"] for s in samples),
        "windows": windows,
    }
