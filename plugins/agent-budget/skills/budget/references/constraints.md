# Constraint schema

The `set` command reads one JSON object on stdin and rejects unknown fields.
No additional model or API key is used; the host agent interprets the sentence.

```json
{
  "duration_minutes": 120,
  "deadline": "2026-09-25T07:00:00-07:00",
  "usage": [{
    "window": "weekly",
    "max_additional_percent": 10,
    "min_remaining_percent": 30
  }]
}
```

Every field is optional, but at least one time or usage constraint is required.
Dates above are illustrative; calculate the user's actual deadline at invocation.

| Field | Meaning |
| --- | --- |
| `duration_minutes` | Elapsed wall time, from activation; includes idle time. Positive, at most 31 days. |
| `deadline` | Absolute ISO 8601 timestamp, with timezone. Earliest time limit wins. |
| `usage` | At most two rules, one per window. |
| `window` | `weekly` (default) or `five_hour`. |
| `max_additional_percent` | Additional **percentage points**, from the activation baseline. |
| `remaining_fraction_percent` | Percentage of the allowance remaining at activation. Mutually exclusive with `max_additional_percent`. |
| `min_remaining_percent` | Account reserve. May combine with either spending constraint. |
| `buffer_percent` | Optional stopping buffer, in percentage points. Default: the smaller of 0.5 points and 10% of the available budget. |

Percentages are finite numbers from 0 through 100. Impossible headroom is rejected.

At 40% already used:

- “Use another 10%” → `max_additional_percent: 10`; ceiling 50%, cutoff 49.5%.
- “Use 10% of what remains” → `remaining_fraction_percent: 10`; ceiling 46%, cutoff 45.5%.
- “Leave 30%” → `min_remaining_percent: 30`; ceiling 70%, cutoff 69.5%.
- “Stop in two hours” → `{"duration_minutes":120}`; no quota reader needed.

The helper freezes baselines and thresholds when armed. A reset, changed account,
decreasing counter, missing required window, or stale telemetry stops the selected
sessions rather than refilling the budget. Polling is every 30 seconds, with a
90-second freshness ceiling. Reports and in-flight requests can cause overshoot.
This is a local stop mechanism, not a provider-enforced spending guarantee.

Selected sessions share account-level measurements. Do not sum duplicate readings
or promise attribution to individual sessions. Separate providers need separate
budgets. Cross-account groups are unsupported; never imply that one meter covers them.
