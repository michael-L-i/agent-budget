---
name: budget
description: Set, inspect, cancel, or stop usage and time budgets for this Claude Code or Codex terminal session, or a selected group of registered sessions. Use when the user asks to limit agent usage or runtime.
---

# Budget

Translate the user's sentence into concrete limits, then let the local helper
enforce them. Do not enforce a budget by remembering to stop yourself.

The helper is `../../scripts/budget` relative to this SKILL.md's directory.
Resolve that to an absolute path and quote it in shell commands. It uses uv;
first use installs the locked Python dependencies. Never interpolate user text
into shell code. Pass specifications as JSON on stdin, using a quoted heredoc.

## Set a budget

1. Read [constraints.md](references/constraints.md) for the supported fields.
2. Interpret the user's intent. Default to **this session**, **weekly allowance**,
   and **additional percentage points**. Preserve an explicit five-hour window,
   fraction of remaining, reserve, duration, or deadline. Multiple limits use OR.
   Resolve local deadlines to a timezone-aware ISO timestamp using the system's
   current time. Ask one short question only if a material ambiguity remains.
   “While I sleep” alone is not a numeric duration; a stated usage limit is sufficient.
3. For Claude percentage budgets, run `setup claude` first (idempotent). If it
   reports a project override, use `setup claude --project` to preserve it. This
   enables native usage reports while forwarding the existing status line.
   Claude needs version 2.1.251+, an interactive subscription session, and a fresh
   report. If no report exists yet, explain that the user must finish a response
   and retry; do not claim a budget is armed. Codex needs no usage setup.
4. For this session, run the helper's `set` command with the JSON on stdin. For
   others, run `sessions`, select only the explicitly requested IDs, and repeat
   `--session ID`. A group must use one provider and the same authenticated account.
   If the intended workers are ambiguous, ask which ones; do not select all by default.
5. Only say **Budget set** after the helper reports `active`. Briefly state the
   actual cutoff (including buffer), deadline, selected scope, and that other
   activity on the same account counts. Explain that enforcement ends the selected
   CLI sessions; conversations can be resumed with the provider's normal resume command.
   Do not start unrelated work just because a budget was set.

Example implementation (replace the absolute helper path with the installed one):

```sh
/absolute/plugin/path/scripts/budget set <<'BUDGET_JSON'
{"duration_minutes":120,"usage":[{"window":"weekly","max_additional_percent":10}]}
BUDGET_JSON
```

If arming fails, state that **no new budget is active** and give the corrective
step from the helper. Never substitute token costs for quota percentages, drop a
requested constraint, bypass the sandbox, or silently switch to a timer.
Percentage budgets read native provider telemetry for the default local account.
A missing or unidentifiable account must remain an error.

## Manage budgets

- `status` or `status ID`: limits, state, and watcher health; `--json` adds structured detail.
- `sessions`: live, registered terminal sessions, with their IDs and working directories.
- `cancel ID`: remove enforcement and allow sessions to continue.
- `stop ID`: request stopping every session in that budget; check status for completion.
- `setup claude --restore`: restore the previous status line before uninstalling;
  add `--project` when the bridge was installed for this project.
- `doctor`: local prerequisites, without making model calls.

Cancel and stop are different actions. Change an existing budget only when the
user requests it: cancel that specific budget, then set its replacement. Report
if replacement fails and leaves the session unbudgeted. Never loosen a budget
because a task needs more time or usage. The local registry only includes sessions
that loaded this plugin or invoked its helper; it is not an inventory of every agent.

If a sandbox or host prevents the helper or detached watcher from running, report
that limitation. Desktop app servers, remote agents, and Windows are unsupported
in this release. Do not kill an arbitrary PID or a shared app server as a workaround.
