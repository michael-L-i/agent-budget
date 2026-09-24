"""Read native quota telemetry without a third-party monitor or model requests."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import shutil
import signal
import subprocess
import threading
import time

from .policy import BudgetError, number

MAX_AGE = 90
POLL_SECONDS = 30


def identity(provider: str, email: object, organization: object = "") -> str:
    if not isinstance(email, str) or not email.strip():
        raise BudgetError("The signed-in account could not be identified.")
    return hashlib.sha256(
        f"{provider}:{email.strip().lower()}:{organization or ''}".encode()
    ).hexdigest()


def window(used: object, resets: object, now: float) -> dict:
    used = number(used, "reported usage")
    resets = number(resets, "quota reset timestamp", 0, 1e12)
    if resets <= now:
        raise BudgetError("Usage window has expired; waiting for a new report.")
    return {"used": used, "resets_at": resets}


def parse_codex(account: dict, limits: dict, now: float) -> dict:
    account = account.get("account")
    if not isinstance(account, dict) or account.get("type") != "chatgpt":
        raise BudgetError("Sign in to Codex with a ChatGPT subscription to use percentage budgets.")
    fingerprint = identity("codex", account.get("email"), account.get("chatgptAccountId"))
    buckets = limits.get("rateLimitsByLimitId")
    if buckets:
        # Never silently pick a different model's bucket.
        bucket = buckets.get("codex")
    else:
        bucket = limits.get("rateLimits")
    if not isinstance(bucket, dict) or bucket.get("limitId") not in (None, "codex"):
        raise BudgetError("Codex did not expose an unambiguous Codex quota bucket.")
    windows = {}
    for key in ("primary", "secondary"):
        reading = bucket.get(key)
        if not isinstance(reading, dict):
            continue
        name = {300: "five_hour", 10080: "weekly"}.get(reading.get("windowDurationMins"))
        if name is None:
            continue
        if name in windows:
            raise BudgetError("Codex returned ambiguous quota windows.")
        windows[name] = window(reading.get("usedPercent"), reading.get("resetsAt"), now)
    if not windows:
        raise BudgetError("Codex did not expose a supported five-hour or weekly quota window.")
    return {"identity": fingerprint, "updated_at": now, "windows": windows}


class Rpc:
    """Bounded JSONL client for the local Codex app-server's read-only account API."""

    def __init__(self, cancelled: threading.Event | None = None):
        binary = shutil.which("codex")
        if not binary:
            raise BudgetError("Codex CLI is not installed or is not on PATH.")
        self.proc = subprocess.Popen(
            [binary, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            bufsize=0,
        )
        self.cancelled = cancelled
        self.deadline = time.monotonic() + 15
        self.buffer = b""
        self.total = 0
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.proc.stdout, selectors.EVENT_READ)

    def send(self, message: dict):
        self.proc.stdin.write(json.dumps(message).encode() + b"\n")
        self.proc.stdin.flush()

    def request(self, method: str, request_id: int, params: dict | None = None) -> dict:
        self.send({"id": request_id, "method": method, "params": params or {}})
        while time.monotonic() < self.deadline:
            if self.cancelled is not None and self.cancelled.is_set():
                raise BudgetError("Usage read cancelled.")
            if b"\n" not in self.buffer:
                if not self.selector.select(timeout=0.1):
                    continue
                data = os.read(self.proc.stdout.fileno(), 65536)
                if not data:
                    raise BudgetError("Codex usage connection closed before replying.")
                self.total += len(data)
                if self.total > 1024 * 1024:
                    raise BudgetError("Codex usage response exceeded the size limit.")
                self.buffer += data
                continue
            line, self.buffer = self.buffer.split(b"\n", 1)
            message = json.loads(line)
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise BudgetError("Codex could not read usage. Check your CLI version and login.")
            result = message.get("result")
            if not isinstance(result, dict):
                raise BudgetError("Unexpected Codex usage response.")
            return result
        raise BudgetError("Codex usage read timed out.")

    def close(self):
        self.selector.close()
        # This is our private reader process, never an existing agent/app server.
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            self.proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait()
        self.proc.stdin.close()
        self.proc.stdout.close()


def fetch_codex(cancelled: threading.Event | None = None) -> dict:
    rpc = Rpc(cancelled)
    try:
        rpc.request("initialize", 1, {"clientInfo": {"name": "agent_budget", "version": "0.2.0"}})
        rpc.send({"method": "initialized", "params": {}})
        account = rpc.request("account/read", 2, {"refreshToken": False})
        limits = rpc.request("account/rateLimits/read", 3)
        return parse_codex(account, limits, time.time())
    finally:
        rpc.close()


def fetch(
    provider: str, cancelled: threading.Event | None = None, targets: list[dict] | None = None
) -> dict:
    try:
        if provider == "codex":
            return fetch_codex(cancelled)
        if provider == "claude":
            from .claude import fetch_claude

            return fetch_claude(targets or [], cancelled)
        raise BudgetError("Unsupported usage provider.")
    except BudgetError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
        raise BudgetError(
            f"Could not read native {provider} usage; budget cannot be enforced."
        ) from exc
