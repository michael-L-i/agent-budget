"""Strict policy compilation and pure, conservative quota evaluation."""

from __future__ import annotations

import math
from datetime import datetime


class BudgetError(ValueError):
    """An actionable, safe-to-display failure."""


def number(value: object, label: str, low: float = 0, high: float = 100) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BudgetError(f"{label} must be a number.")
    if not math.isfinite(value) or not low <= value <= high:
        raise BudgetError(f"{label} must be between {low:g} and {high:g}.")
    return float(value)


def timestamp(value: object) -> float:
    if not isinstance(value, str):
        raise BudgetError("Use an ISO 8601 timestamp with a timezone.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError
        return parsed.timestamp()
    except (ValueError, OverflowError) as exc:
        raise BudgetError("Use an ISO 8601 timestamp with a timezone.") from exc


def validate(spec: object, now: float) -> dict:
    if not isinstance(spec, dict):
        raise BudgetError("A budget must be a JSON object.")
    unknown = spec.keys() - {"duration_minutes", "deadline", "usage"}
    if unknown:
        raise BudgetError(f"Unknown budget fields: {', '.join(sorted(unknown))}.")
    deadlines = []
    if "duration_minutes" in spec:
        minutes = number(spec["duration_minutes"], "duration_minutes", 0.01, 44640)
        deadlines.append(now + minutes * 60)
    if "deadline" in spec:
        deadlines.append(timestamp(spec["deadline"]))
    deadline = min(deadlines) if deadlines else None
    if deadline is not None and deadline <= now:
        raise BudgetError("The deadline must be in the future.")
    usage = spec.get("usage", [])
    if not isinstance(usage, list) or len(usage) > 2:
        raise BudgetError("usage must contain at most two window constraints.")
    rules, windows = [], set()
    for rule in usage:
        if not isinstance(rule, dict) or rule.keys() - {
            "window",
            "max_additional_percent",
            "remaining_fraction_percent",
            "min_remaining_percent",
            "buffer_percent",
        }:
            raise BudgetError("Invalid usage constraint or unknown fields.")
        window = rule.get("window", "weekly")
        if window not in ("weekly", "five_hour") or window in windows:
            raise BudgetError("Use each window at most once: weekly or five_hour.")
        windows.add(window)
        normalized = {"window": window}
        for key in rule.keys() - {"window"}:
            normalized[key] = number(rule[key], key)
        if {"max_additional_percent", "remaining_fraction_percent"} <= normalized.keys():
            raise BudgetError("Choose additional percentage points OR a fraction of remaining.")
        if not normalized.keys() & {
            "max_additional_percent",
            "remaining_fraction_percent",
            "min_remaining_percent",
        }:
            raise BudgetError("Each usage rule needs a spending limit or remaining reserve.")
        rules.append(normalized)
    if deadline is None and not rules:
        raise BudgetError("Specify a time limit, a usage limit, or both.")
    return {"deadline": deadline, "usage": rules}


def compile_policy(spec: object, snapshot: dict | None, now: float) -> dict:
    policy = validate(spec, now)
    compiled = []
    for rule in policy["usage"]:
        if snapshot is None or rule["window"] not in snapshot["windows"]:
            raise BudgetError(f"No fresh {rule['window']} usage is available; budget not armed.")
        reading = snapshot["windows"][rule["window"]]
        used = reading["used"]
        ceilings = [100.0]
        if "max_additional_percent" in rule:
            ceilings.append(used + rule["max_additional_percent"])
        if "remaining_fraction_percent" in rule:
            ceilings.append(used + (100 - used) * rule["remaining_fraction_percent"] / 100)
        if "min_remaining_percent" in rule:
            ceilings.append(100 - rule["min_remaining_percent"])
        ceiling = min(ceilings)
        headroom = ceiling - used
        buffer = rule.get("buffer_percent", min(0.5, max(0, headroom) * 0.1))
        stop_at = ceiling - buffer
        if stop_at <= used:
            raise BudgetError("No usable allowance remains after the reserve and stopping buffer.")
        compiled.append(
            {
                **rule,
                "baseline": used,
                "last_used": used,
                "ceiling": ceiling,
                "stop_at": stop_at,
                "buffer_percent": buffer,
                "resets_at": reading["resets_at"],
            }
        )
    return {
        "deadline": policy["deadline"],
        "usage": compiled,
        "identity": snapshot["identity"] if compiled else None,
    }


def evaluate(policy: dict, snapshot: dict | None, now: float) -> str | None:
    if policy["deadline"] is not None and now >= policy["deadline"]:
        return "Time limit reached."
    for rule in policy["usage"]:
        if now >= rule["resets_at"]:
            return "Quota window reset; the budget was not refilled."
    if snapshot is None:
        return None  # The watcher separately enforces its telemetry freshness deadline.
    if policy["usage"] and snapshot["identity"] != policy["identity"]:
        return "Usage account changed."
    for rule in policy["usage"]:
        reading = snapshot["windows"].get(rule["window"])
        if reading is None:
            return f"{rule['window']} usage became unavailable."
        if reading["resets_at"] != rule["resets_at"]:
            return "Quota window changed; the budget was not refilled."
        if reading["used"] < rule["last_used"]:
            return "Usage decreased unexpectedly; accounting is uncertain."
        rule["last_used"] = reading["used"]
        if reading["used"] >= rule["stop_at"]:
            return f"{rule['window']} usage cutoff reached ({rule['stop_at']:g}% used)."
    return None
