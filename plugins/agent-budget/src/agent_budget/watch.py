"""Detached enforcement: no model calls, no prompts, no tool-hook timing dependency."""

from __future__ import annotations

import fcntl
import os
import queue
import signal
import sqlite3
import threading
import time

import psutil

from . import meter, processes
from .policy import BudgetError, evaluate
from .store import Store, state_dir


def _read(provider: str, inbox: queue.Queue, cancelled: threading.Event):
    try:
        inbox.put((meter.fetch(provider, cancelled), None))
    except Exception:
        # Do not persist provider stderr, credentials, or arbitrary exception strings.
        inbox.put((None, "Usage reader unavailable."))


def run(budget_id: str) -> int:
    # Kernel lock prevents duplicate enforcers, including after an interrupted startup.
    with (state_dir() / f"{budget_id}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        store = Store()
        budget = store.get(budget_id)
        if budget["state"] not in ("starting", "active", "stopping"):
            store.close()
            return 0
        me = psutil.Process()
        with store.transaction():
            budget = store.get(budget_id)
            if budget["state"] not in ("starting", "active", "stopping"):
                return 0
            budget.update(
                state="stopping" if budget["state"] == "stopping" else "active",
                watcher={"pid": me.pid, "born": me.create_time()},
                heartbeat=time.time(),
            )
            store.save(budget)
            store.event(budget_id, time.time(), "Budget armed; independent watcher running.")
        interrupted = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, lambda *_: interrupted.set())
        inbox: queue.Queue = queue.Queue(maxsize=1)
        cancel_reader = threading.Event()
        reader = None
        policy = budget["policy"]
        last_good = budget.get("snapshot")
        next_poll, pending = 0.0, False
        wall_start, mono_start = time.time(), time.monotonic()
        reason = None
        try:
            while True:
                now, monotonic = time.time(), time.monotonic()
                with store.transaction():
                    budget = store.get(budget_id)
                    if budget["state"] == "cancelled":
                        return 0
                    if budget["state"] not in ("active", "stopping"):
                        return 0
                    budget["heartbeat"] = now
                    budget["policy"] = policy
                    store.save(budget)
                if not any(processes.resolve(target) for target in budget["targets"]):
                    with store.transaction():
                        budget = store.get(budget_id)
                        if budget["state"] != "cancelled":
                            budget.update(state="completed", reason="All selected sessions exited.")
                            store.save(budget)
                    return 0
                reason = evaluate(policy, None, now)
                if interrupted.is_set():
                    reason = "Watcher was interrupted; stopping selected sessions for safety."
                if abs((now - wall_start) - (monotonic - mono_start)) > 5:
                    reason = "System clock changed; time accounting is uncertain."
                if budget["state"] == "stopping":
                    reason = budget.get("reason", "Stopped by request.")
                if policy["usage"]:
                    try:
                        snapshot, error = inbox.get_nowait()
                        pending = False
                        if snapshot is not None:
                            last_good = snapshot
                            reason = reason or evaluate(policy, snapshot, now)
                            with store.transaction():
                                latest = store.get(budget_id)
                                latest["snapshot"] = snapshot
                                store.save(latest)
                    except queue.Empty:
                        pass
                    if last_good is None or now - last_good["updated_at"] > meter.MAX_AGE:
                        reason = reason or "Usage data is stale; stopping selected sessions."
                    if not reason and not pending and monotonic >= next_poll:
                        pending = True
                        next_poll = monotonic + meter.POLL_SECONDS
                        reader = threading.Thread(
                            target=_read,
                            args=(budget["provider"], inbox, cancel_reader),
                            daemon=True,
                        )
                        reader.start()
                if reason:
                    break
                time.sleep(0.25)
        except Exception:
            reason = "Watcher encountered an internal error; stopping selected sessions."
        finally:
            cancel_reader.set()
            if reader is not None:
                reader.join(timeout=2)
            if reason:
                # Linearize cancellation against stopping before sending any signal.
                try:
                    with store.transaction():
                        latest = store.get(budget_id)
                        if latest["state"] == "cancelled":
                            reason = None
                        else:
                            latest.update(state="stopping", reason=reason)
                            store.save(latest)
                            store.event(budget_id, time.time(), reason)
                except (sqlite3.Error, OSError, BudgetError):
                    # A broken ledger must not prevent the independently cached targets stopping.
                    pass
                if reason:
                    errors = processes.stop_targets(budget["targets"])
                    try:
                        with store.transaction():
                            latest = store.get(budget_id)
                            latest.update(
                                state="stop_failed" if errors else "stopped",
                                errors=errors,
                                heartbeat=time.time(),
                            )
                            store.save(latest)
                    except (sqlite3.Error, OSError, BudgetError):
                        pass
            store.close()
        return 0


def start(store: Store, budget: dict):
    import subprocess
    import sys

    with open(os.devnull, "wb") as null:
        proc = subprocess.Popen(
            [sys.executable, "-m", "agent_budget.cli", "_watch", budget["id"]],
            stdin=subprocess.DEVNULL,
            stdout=null,
            stderr=null,
            start_new_session=True,
            close_fds=True,
        )
    until = time.monotonic() + 5
    while time.monotonic() < until:
        latest = store.get(budget["id"])
        if latest["state"] in ("active", "stopping", "stopped", "stop_failed") and latest.get(
            "watcher"
        ):
            return latest
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    with store.transaction():
        latest = store.get(budget["id"])
        if latest["state"] in ("starting", "active"):
            latest.update(state="cancelled", reason="Watcher failed to acknowledge startup.")
            store.save(latest)
    raise BudgetError("Watcher did not start. No budget was armed; run doctor before continuing.")
