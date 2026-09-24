"""Only register actual ancestor CLIs; never accept user-supplied process IDs."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import psutil

from .policy import BudgetError


def provider_for(executable: str, argv: list[str]) -> str | None:
    name = Path(executable).name
    if name in ("claude", "codex"):
        provider = name
    elif name in ("node", "nodejs") and len(argv) > 1:
        script = argv[1].replace("\\", "/")
        if script.endswith("/@anthropic-ai/claude-code/cli.js"):
            provider = "claude"
        elif script.endswith("/@openai/codex/bin/codex.js"):
            provider = "codex"
        else:
            return None
    else:
        return None
    # These processes can host several unrelated conversations. Do not kill them.
    if any(
        arg
        in {
            "app-server",
            "mcp-server",
            "serve",
            "--bg",
            "--background",
            "--remote-control",
            "--cloud",
        }
        for arg in argv[1:]
    ):
        return None
    return provider


def current() -> dict:
    for proc in psutil.Process().parents():
        try:
            provider = provider_for(proc.exe(), proc.cmdline())
            if provider is None:
                continue
            born = proc.create_time()
            return {
                "id": hashlib.sha256(f"{proc.pid}:{born}".encode()).hexdigest()[:12],
                "pid": proc.pid,
                "born": born,
                "provider": provider,
                "cwd": proc.cwd(),
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    raise BudgetError(
        "No supported Claude/Codex terminal session found. Run /budget inside a CLI session; "
        "desktop app servers and arbitrary PIDs are not supported."
    )


def resolve(identity: dict) -> psutil.Process | None:
    try:
        proc = psutil.Process(identity["pid"])
        if proc.uids().real != os.getuid() or proc.create_time() != identity["born"]:
            return None
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return None
        return proc
    except psutil.NoSuchProcess:
        return None


def stop_targets(targets: list[dict], grace: float = 3) -> list[str]:
    """Terminate verified local trees. psutil rechecks PID reuse before signalling."""
    errors = []
    protected = {os.getpid(), *(p.pid for p in psutil.Process().parents())}
    victims: dict[int, psutil.Process] = {}
    for target in targets:
        try:
            proc = resolve(target)
            if proc is None:
                continue
            if proc.pid in protected:
                raise BudgetError("Refusing to stop the watcher's own ancestor.")
            # Capture identities before the parent can exit and orphan its children.
            for child in proc.children(recursive=True):
                if child.pid not in protected and child.uids().real == os.getuid():
                    child.create_time()
                    victims[child.pid] = child
            victims[proc.pid] = proc
        except (psutil.Error, BudgetError):
            errors.append(f"Could not inspect session {target['id']}.")
    for proc in victims.values():
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            errors.append(f"Permission denied stopping process {proc.pid}.")
    _, alive = psutil.wait_procs(list(victims.values()), timeout=grace)
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            errors.append(f"Permission denied killing process {proc.pid}.")
    _, remaining = psutil.wait_procs(alive, timeout=1)
    if remaining:
        errors.append("Some local processes have not exited; inspect them manually.")
    return errors
