"""Read real account quota reports; never infer quota from token costs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import threading
import time

from .policy import BudgetError, number, timestamp

MAX_AGE = 90
POLL_SECONDS = 30


def parse_snapshot(payload: object, provider: str, now: float) -> dict:
    if not isinstance(payload, list) or len(payload) != 1:
        raise BudgetError("Expected exactly one usage account from CodexBar.")
    row = payload[0]
    if not isinstance(row, dict) or row.get("provider") != provider or row.get("error"):
        raise BudgetError("CodexBar could not read this provider's usage.")
    usage = row.get("usage")
    if not isinstance(usage, dict):
        raise BudgetError("CodexBar returned no subscription usage.")
    updated = timestamp(usage.get("updatedAt"))
    if not -5 <= now - updated <= MAX_AGE:
        raise BudgetError("Usage report is stale or has an invalid timestamp.")
    identity = usage.get("identity") or usage
    email = identity.get("accountEmail")
    if not isinstance(email, str) or not email.strip():
        raise BudgetError("Usage account identity is unavailable; cannot arm a percentage budget.")
    organization = identity.get("accountOrganization") or ""
    fingerprint = hashlib.sha256(
        f"{provider}:{email.strip().lower()}:{organization}".encode()
    ).hexdigest()
    windows = {}
    for key in ("primary", "secondary", "tertiary"):
        window = usage.get(key)
        if not isinstance(window, dict):
            continue
        # Cadence, not positional order, determines the meaning of a window.
        name = {300: "five_hour", 10080: "weekly"}.get(window.get("windowMinutes"))
        if name is None:
            continue
        if name in windows:
            raise BudgetError("Ambiguous quota windows in the usage report.")
        used = number(window.get("usedPercent"), "reported usage")
        resets = timestamp(window.get("resetsAt"))
        if resets <= now:
            raise BudgetError("Usage window has expired; waiting for a new report.")
        windows[name] = {"used": used, "resets_at": resets}
    if not windows:
        raise BudgetError("No supported five-hour or weekly quota window is available.")
    return {"identity": fingerprint, "updated_at": updated, "windows": windows}


def fetch(provider: str, cancelled: threading.Event | None = None) -> dict:
    binary = shutil.which("codexbar")
    if binary is None:
        raise BudgetError(
            "Percentage budgets need the CodexBar CLI on PATH. Install CodexBar, then use "
            "its Advanced settings to install the CLI. Time-only budgets work without it."
        )
    # Use the local CLI's authenticated account, not a browser's possibly different account.
    command = [binary, "usage", "--provider", provider, "--source", "cli", "--format", "json"]
    with subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    ) as proc:
        until = time.monotonic() + 15
        while True:
            try:
                output, _ = proc.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= until or (cancelled is not None and cancelled.is_set()):
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.communicate()
                    raise BudgetError("Usage reader timed out or was cancelled.") from None
    if proc.returncode:
        raise BudgetError("Usage reader failed; check CodexBar and your CLI login.")
    if len(output) > 1024 * 1024:
        raise BudgetError("Usage report is unexpectedly large.")
    try:
        return parse_snapshot(json.loads(output), provider, time.time())
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        if isinstance(exc, BudgetError):
            raise
        raise BudgetError("Unrecognized CodexBar usage report; budget cannot be enforced.") from exc
