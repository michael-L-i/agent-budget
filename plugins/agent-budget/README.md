# Agent Budget plugin

Usage and time budgets for Claude Code and Codex terminal sessions. Invoke the
**budget** skill and describe your limit in plain English. The local watchdog
enforces it independently of the model.

Requires **uv**. Usage tracking is built in for both providers. Claude percentage
limits require interactive Claude Code 2.1.251+ and a supported subscription; the
skill sets up a reversible bridge that preserves your existing status line.

Installation, semantics, limitations, and development:
<https://github.com/michael-L-i/agent-budget>
