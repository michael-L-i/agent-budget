"""Private SQLite state shared by plugin commands and independent watchers."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path

from .policy import BudgetError


def state_dir() -> Path:
    path = Path(os.environ.get("AGENT_BUDGET_HOME", "~/.local/state/agent-budget")).expanduser()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise BudgetError("Budget state must be a real directory owned by you.")
    path.chmod(0o700)
    return path


class Store:
    def __init__(self):
        os.umask(0o077)
        path = state_dir() / "state.sqlite3"
        if path.is_symlink():
            raise BudgetError("The state database cannot be a symlink.")
        self.db = sqlite3.connect(path, timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS budgets (
                id TEXT PRIMARY KEY, state TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, budget_id TEXT NOT NULL,
                at REAL NOT NULL, message TEXT NOT NULL
            );
        """)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def sessions(self) -> list[dict]:
        return [json.loads(row[0]) for row in self.db.execute("SELECT data FROM sessions")]

    def session(self, session: dict):
        self.db.execute(
            "INSERT OR REPLACE INTO sessions VALUES (?, ?)",
            (session["id"], json.dumps(session)),
        )

    def budgets(self) -> list[dict]:
        return [json.loads(row[0]) for row in self.db.execute("SELECT data FROM budgets")]

    def get(self, budget_id: str) -> dict:
        row = self.db.execute("SELECT data FROM budgets WHERE id = ?", (budget_id,)).fetchone()
        if row is None:
            raise BudgetError("Unknown budget. Run status to see budget IDs.")
        return json.loads(row[0])

    def save(self, budget: dict):
        self.db.execute(
            "INSERT OR REPLACE INTO budgets VALUES (?, ?, ?)",
            (budget["id"], budget["state"], json.dumps(budget)),
        )

    def event(self, budget_id: str, now: float, message: str):
        self.db.execute(
            "INSERT INTO events (budget_id, at, message) VALUES (?, ?, ?)",
            (budget_id, now, message),
        )

    def history(self, budget_id: str) -> list[dict]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT at, message FROM events WHERE budget_id = ? ORDER BY id DESC LIMIT 20",
                (budget_id,),
            )
        ]
