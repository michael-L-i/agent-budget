# Agent Budget

Tell your coding agent how much it can use. A local watchdog enforces the budget.

> Use another 10% of my weekly allowance, or stop in two hours.

The agent turns your sentence into explicit constraints. The watchdog monitors
usage and time independently, and stops the selected terminal sessions when a
limit is reached. No dashboard, API key, or extra model call is required.

Built for Claude Code and Codex **terminal sessions** on macOS and Linux.
Desktop app servers and arbitrary process IDs are deliberately unsupported.

## Budget semantics

- “Use 10%” means **another 10 percentage points** of the selected allowance.
- “Use 10% of what remains” means 10% of the remaining allowance at activation.
- “Leave 30%” sets a ceiling of 70% account usage.
- Weekly is the default window; five-hour budgets are also supported.
- Multiple constraints stop at the **first** limit. Multiple selected sessions
  share a budget; account usage is never multiplied by worker count.
- Usage is account-wide, including other sessions and devices. This is not exact
  per-session attribution or a provider-enforced spending cap.

## Scope

The first release prioritizes predictable stopping, private local state, explicit
failure handling, and resumable provider conversations. Stopping ends the selected
CLI process and its local descendants; use the provider's normal resume command
afterward. It does not promise a clean worktree or interrupt remote jobs.

Percentage budgets use the [CodexBar CLI](https://github.com/steipete/CodexBar)
as the usage reader. Time-only budgets do not need it. Provider reports and polling
can lag, so a stopping buffer reduces overshoot but cannot eliminate it.

## Development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+.

```sh
uv sync --locked
uv run pytest
uv run ruff check .
```

MIT licensed.
