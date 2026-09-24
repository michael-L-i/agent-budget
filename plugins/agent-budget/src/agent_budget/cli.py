"""Small command surface for the skill and for humans."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
import uuid
from datetime import datetime

import psutil

from . import meter, processes, watch
from .policy import BudgetError, compile_policy, validate
from .store import Store


def register(store: Store) -> dict:
    session = processes.current()
    store.session(session)
    return session


def select_targets(store: Store, selectors: list[str] | None) -> list[dict]:
    selectors = selectors or ["current"]
    sessions = {session["id"]: session for session in store.sessions()}
    targets = []
    for name in dict.fromkeys(selectors):
        if name == "current":
            target = register(store)
        else:
            target = sessions.get(name)
            if target is None:
                raise BudgetError(f"Unknown session {name}. Run sessions for registered IDs.")
        if processes.resolve(target) is None:
            raise BudgetError(f"Session {target['id']} has exited; open it and set a new budget.")
        if target["id"] not in {item["id"] for item in targets}:
            targets.append(target)
    if len({target["provider"] for target in targets}) != 1:
        raise BudgetError(
            "A shared budget must use one provider. Set separate budgets per provider."
        )
    return targets


def arm(store: Store, spec: dict, targets: list[dict]) -> dict:
    now = time.time()
    normalized = validate(spec, now)
    if normalized["usage"] and any(not target.get("quota_eligible", True) for target in targets):
        raise BudgetError(
            "Percentage budgets require the default local subscription profile. "
            "API keys, custom config roots, and profile overrides are unsupported. "
            "Time-only budgets are still available."
        )
    snapshot = meter.fetch(targets[0]["provider"], targets=targets) if normalized["usage"] else None
    policy = compile_policy(spec, snapshot, now)
    if policy["deadline"] is not None and policy["deadline"] <= time.time():
        raise BudgetError("The deadline elapsed during setup; budget not armed.")
    budget = {
        "id": uuid.uuid4().hex[:12],
        "state": "starting",
        "created_at": now,
        "provider": targets[0]["provider"],
        "targets": targets,
        "policy": policy,
        "snapshot": snapshot,
        "heartbeat": None,
        "watcher": None,
    }
    with store.transaction():
        target_ids = {target["id"] for target in targets}
        for previous in store.budgets():
            if previous["state"] in ("starting", "active", "stopping") and target_ids & {
                target["id"] for target in previous["targets"]
            }:
                raise BudgetError(
                    f"Session already belongs to budget {previous['id']}. "
                    "Cancel that budget explicitly before replacing it."
                )
        store.save(budget)
        store.event(budget["id"], now, "Budget created.")
    return watch.start(store, budget)


def change_state(store: Store, budget_id: str, action: str) -> dict:
    with store.transaction():
        budget = store.get(budget_id)
        if budget["state"] not in ("starting", "active"):
            raise BudgetError(f"Budget is {budget['state']}; it cannot be {action}.")
        if action == "cancelled":
            budget.update(state="cancelled", reason="Budget removed; sessions may continue.")
        else:
            budget.update(state="stopping", reason="Stopped by request.")
        store.save(budget)
        store.event(budget_id, time.time(), budget["reason"])
    return budget


def describe(budget: dict) -> str:
    lines = [f"Budget {budget['id']} · {budget['state']} · {budget['provider']}"]
    lines.append("Sessions: " + ", ".join(target["id"] for target in budget["targets"]))
    policy = budget["policy"]
    for rule in policy["usage"]:
        lines.append(
            f"{rule['window']}: {rule['baseline']:g}% at start → stop at "
            f"{rule['stop_at']:g}% used (ceiling {rule['ceiling']:g}%, "
            f"buffer {rule['buffer_percent']:g} points)."
        )
        lines.append(f"Last reported usage: {rule['last_used']:g}%.")
    if policy["deadline"] is not None:
        deadline = (
            datetime.fromtimestamp(policy["deadline"]).astimezone().isoformat(timespec="seconds")
        )
        lines.append(f"Stop by {deadline}.")
    if budget.get("reason"):
        lines.append(budget["reason"])
    if budget.get("health"):
        lines.append(budget["health"])
    if policy["usage"]:
        lines.append("Account-wide usage; other sessions count. First limit wins.")
    return "\n".join(lines)


def status(store: Store, budget_id: str | None) -> list[dict]:
    budgets = [store.get(budget_id)] if budget_id else store.budgets()
    for budget in budgets:
        if budget["state"] in ("active", "stopping"):
            watcher = budget.get("watcher")
            if not watcher or processes.resolve(watcher) is None:
                budget["health"] = (
                    "UNPROTECTED: watcher is not running. Cancel and set a new budget."
                )
            elif time.time() - (budget.get("heartbeat") or 0) > 10:
                budget["health"] = (
                    "UNHEALTHY: watcher heartbeat is stale. Inspect before continuing."
                )
        if budget_id:
            budget["events"] = store.history(budget_id)
    return budgets


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Plain-English agent budgets, enforced locally.")
    root.add_argument("--version", action="version", version="agent-budget 0.2.0")
    sub = root.add_subparsers(dest="command", required=True)
    reg = sub.add_parser("register", help="Register the current Claude/Codex terminal session")
    reg.add_argument("--hook", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("sessions", help="List live, registered sessions")
    set_cmd = sub.add_parser("set", help="Arm a validated budget; read JSON from stdin")
    set_cmd.add_argument(
        "--session", action="append", help="Registered ID; repeat to share a budget"
    )
    set_cmd.add_argument("--json", action="store_true", help="Return structured output")
    for command in ("status", "cancel", "stop"):
        cmd = sub.add_parser(
            command,
            help={
                "status": "Show budgets and watcher health",
                "cancel": "Remove a budget and keep sessions running",
                "stop": "Stop all sessions selected by a budget",
            }[command],
        )
        cmd.add_argument("budget_id", nargs="?" if command == "status" else None)
        cmd.add_argument("--json", action="store_true")
    sub.add_parser("doctor", help="Check local prerequisites without using any model tokens")
    setup_cmd = sub.add_parser("setup", help="Enable or restore Claude's native usage bridge")
    setup_cmd.add_argument("provider", choices=["claude"])
    setup_cmd.add_argument("--project", action="store_true")
    setup_cmd.add_argument("--restore", action="store_true")
    bridge = sub.add_parser("_claude-statusline", help=argparse.SUPPRESS)
    bridge.add_argument("--config", required=True)
    internal = sub.add_parser("_watch", help=argparse.SUPPRESS)
    internal.add_argument("budget_id")
    return root


def main() -> int:
    args = parser().parse_args()
    store = None
    try:
        if sys.platform not in ("darwin", "linux"):
            raise BudgetError("Agent Budget currently supports macOS and Linux terminal sessions.")
        if args.command == "_watch":
            # Only UUID-like IDs may become lock file names.
            if len(args.budget_id) != 12 or any(
                c not in "0123456789abcdef" for c in args.budget_id
            ):
                raise BudgetError("Invalid budget ID.")
            return watch.run(args.budget_id)
        if args.command in ("setup", "_claude-statusline"):
            from . import claude

            if args.command == "setup":
                print(claude.setup(project=args.project, restore=args.restore))
            else:
                claude.statusline(args.config)
            return 0
        store = Store()
        if args.command == "doctor":
            print(f"Python {sys.version.split()[0]} · private state ready")
            for name in ("claude", "codex"):
                print(f"{name}: {'available' if shutil.which(name) else 'not installed'}")
            print("Usage comes directly from your agent. No monitoring app or model calls needed.")
        elif args.command == "register":
            try:
                session = register(store)
                if args.hook:
                    print(
                        f"Agent Budget session: {session['id']}. Use the budget skill for limits."
                    )
                else:
                    print(json.dumps(session))
            except BudgetError:
                if not args.hook:
                    raise
        elif args.command == "sessions":
            live = [s for s in store.sessions() if processes.resolve(s)]
            print(json.dumps(live, indent=2))
        elif args.command == "set":
            raw = sys.stdin.read(65537)
            if len(raw) > 65536:
                raise BudgetError("Budget specification is too large.")
            try:
                spec = json.loads(raw)
            except ValueError as exc:
                raise BudgetError(
                    "Send a JSON budget on stdin. See the budget skill for examples."
                ) from exc
            budget = arm(store, spec, select_targets(store, args.session))
            print(json.dumps(budget) if args.json else describe(budget))
        elif args.command == "status":
            budgets = status(store, args.budget_id)
            print(
                json.dumps(budgets, indent=2)
                if args.json
                else "\n\n".join(describe(budget) for budget in budgets) or "No budgets yet."
            )
        else:
            budget = change_state(
                store,
                args.budget_id,
                "cancelled" if args.command == "cancel" else "stopping",
            )
            if args.command == "stop":
                # A dead watcher cannot honor a stop request. Start a dedicated stop helper.
                watcher = budget.get("watcher")
                if not watcher or processes.resolve(watcher) is None:
                    budget = watch.start(store, budget)
                print(
                    json.dumps(budget)
                    if args.json
                    else "Stop requested. Check status for completion."
                )
            else:
                print(
                    json.dumps(budget) if args.json else "Budget cancelled. Sessions may continue."
                )
        return 0
    except (BudgetError, OSError, psutil.Error, sqlite3.Error) as exc:
        print(f"Agent Budget: {exc}", file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
