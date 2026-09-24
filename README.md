# Agent Budget

Tell your coding agent how much it can use. A local watchdog enforces the budget.

> Use another 10% of my weekly allowance, or stop in two hours.

The agent turns your sentence into explicit constraints. The watchdog monitors
usage and time independently, and stops the selected terminal sessions when a
limit is reached. No dashboard, API key, or extra model call is required.

Built for Claude Code and Codex **terminal sessions** on macOS and Linux.
Desktop app servers and arbitrary process IDs are deliberately unsupported.

## Install

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) first.
The plugin installs its pinned Python dependencies on first use.

**Claude Code** — run in Claude:

```text
/plugin marketplace add michael-L-i/agent-budget
/plugin install agent-budget@agent-budget
```

Restart Claude, then:

```text
/agent-budget:budget use another 10% of my weekly allowance or stop in two hours
/agent-budget:budget status
/agent-budget:budget cancel my budget
```

**Codex CLI** — run in your terminal:

```sh
codex plugin marketplace add michael-L-i/agent-budget
codex plugin add agent-budget@agent-budget
```

Restart Codex, review/trust the plugin's session-registration hook when prompted,
then invoke `$budget` and describe your limit. The CLI helper still registers the
current session when setting a budget if the optional startup hook is disabled.

**Usage reader** — percentage budgets additionally need the
[CodexBar CLI](https://github.com/steipete/CodexBar/blob/main/docs/cli.md).
On macOS, install CodexBar and enable **Advanced → Install CLI** in its settings.
Verify the CLI source works for your current subscription account:

```sh
codexbar usage --provider claude --source cli --format json
# Or: --provider codex
```

Use one default subscription account per provider. API-key sessions, custom profile
roots, and account switching during a run are unsupported for percentage budgets.
Missing quota or account identity is an error, never a guessed allowance.
Timers do not require CodexBar. The reader makes no generation requests, but may
need you to finish your provider's normal sign-in before it can report usage.

## Local development or skill-only installation

```sh
git clone https://github.com/michael-L-i/agent-budget.git
cd agent-budget
uv sync --locked --project plugins/agent-budget
plugins/agent-budget/scripts/budget doctor
claude --plugin-dir "$PWD/plugins/agent-budget"
```

For Codex without a marketplace, link `plugins/agent-budget/skills/budget` into
your personal `~/.agents/skills/` directory as `budget` (only if that name is free).
Resolve the symlink to the actual plugin directory when running its helper.
Other sessions appear in `sessions` once they load the plugin or invoke the skill.

To share a budget, ask the skill to list sessions and name the ones to include.
Only those local sessions are stopped. An orchestrating agent can use the same
helper commands; no separate orchestration service is required.

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

The watcher polls usage every 30 seconds and checks time every 250 ms. Reports
older than 90 seconds trigger a stop. A quota reset or uncertain accounting also
stops the run; overnight budgets never automatically refill. Graceful termination
gets three seconds before remaining local processes are killed. Already-running
remote work and detached/reparented descendants are outside this mechanism.

`cancel` removes a limit and keeps working; `stop` ends its selected sessions.
Cancel active budgets before uninstalling. Normal watcher termination stops its
selected sessions, but a force-killed watcher cannot enforce anything. `status`
reports missing watchers as **UNPROTECTED**. This is a local convenience guard,
not a security boundary against an agent with your filesystem permissions.

State lives in `~/.local/state/agent-budget` with private permissions. No prompts,
transcripts, raw provider responses, or credentials are stored. Override the state
directory with `AGENT_BUDGET_HOME` for isolated tests. See [SECURITY.md](SECURITY.md).

## Development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+.

```sh
uv sync --locked --project plugins/agent-budget
make check
uv build --project plugins/agent-budget
```

CI exercises macOS and Linux with Python 3.11 and 3.13. Tests use disposable local
workers and synthetic quota reports; they never launch paid model requests or stop
your real agent sessions. The native CLI smoke test needs a C compiler.

Runtime design: a short [skill](plugins/agent-budget/skills/budget/SKILL.md) interprets
language; a strict policy compiler freezes constraints; one detached watcher per
budget enforces them. SQLite transactions coordinate cancellation and group state.
Process birth times prevent stale registry entries from targeting reused PIDs.

Provider references: [Claude plugins](https://code.claude.com/docs/en/plugins-reference),
[Codex hooks](https://learn.chatgpt.com/docs/hooks), and
[CodexBar's JSON contract](https://github.com/steipete/CodexBar/blob/main/docs/cli.md).

MIT licensed.
