# Security and reliability

Agent Budget runs locally as your OS user. It has no network listener and accepts
no remote requests. It stores session process IDs and birth times, working
directories, constraints, hashed account identities, quota numbers, and stop events
in a private SQLite database. Credentials stay with the provider and CodexBar.

Only known ancestor Claude/Codex CLI entrypoints can register. Public commands use
registered session IDs, never arbitrary PIDs. Shared app-server and remote modes
are rejected. Before signalling, the watcher verifies process ownership and birth
time; psutil also checks PID reuse. Stopping covers local descendants captured at
that point. Processes that daemonize or launch remote work are not guaranteed to stop.

This is not a sandbox, billing firewall, or provider-side reservation. Local agents
with the same user's permissions can alter state or kill the watcher. Force-killing
the watcher removes enforcement; status detects that failure. Polling and provider
latency can allow overshoot. A killed agent may leave partially edited files.

Quota readings must identify a single default subscription account and recognized
windows. Unsupported profiles are rejected. The service hashes the account identity
to detect changes but cannot independently prove which account every model request
used. Do not use percentage budgets with multiple profiles, API keys, or changing
credentials. No schema fallback converts unknown/missing readings into zero.

To report a vulnerability, use GitHub's private vulnerability reporting if available
on this repository. Do not put tokens, account identifiers, transcripts, or private
paths in public issues. For non-sensitive reliability bugs, include the OS, CLI
versions, budget state/reason, and a redacted reproduction.
