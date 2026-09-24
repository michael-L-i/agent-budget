import copy
from datetime import UTC, datetime

import pytest

from agent_budget.meter import parse_snapshot
from agent_budget.policy import BudgetError

NOW = 1_800_000_000


def iso(value):
    return datetime.fromtimestamp(value, UTC).isoformat()


@pytest.fixture
def report():
    # Minimal documented CodexBar usage JSON, with synthetic account and timestamps.
    return [
        {
            "provider": "claude",
            "usage": {
                "updatedAt": iso(NOW),
                "identity": {"accountEmail": "test@example.invalid", "accountOrganization": None},
                "primary": {"usedPercent": 23, "windowMinutes": 300, "resetsAt": iso(NOW + 100)},
                "secondary": {
                    "usedPercent": 40,
                    "windowMinutes": 10080,
                    "resetsAt": iso(NOW + 1000),
                },
            },
        }
    ]


def test_real_quota_schema_and_identity_redaction(report):
    result = parse_snapshot(report, "claude", NOW)
    assert result["windows"]["weekly"]["used"] == 40
    assert "test@example.invalid" not in str(result)


def test_window_order_does_not_determine_cadence(report):
    usage = report[0]["usage"]
    usage["primary"], usage["secondary"] = usage["secondary"], usage["primary"]
    assert parse_snapshot(report, "claude", NOW)["windows"]["weekly"]["used"] == 40


@pytest.mark.parametrize("value", [True, -1, 101, float("nan"), None, "40"])
def test_invalid_quota_is_not_zero(report, value):
    report[0]["usage"]["primary"]["usedPercent"] = value
    with pytest.raises(BudgetError):
        parse_snapshot(report, "claude", NOW)


def test_stale_and_future_reports_are_rejected(report):
    for offset in (-91, 6):
        report[0]["usage"]["updatedAt"] = iso(NOW + offset)
        with pytest.raises(BudgetError, match="timestamp"):
            parse_snapshot(report, "claude", NOW)


def test_wrong_provider_multiple_accounts_and_errors_are_rejected(report):
    for payload, provider in (
        (report, "codex"),
        (report * 2, "claude"),
        ([{"provider": "claude", "error": {"message": "secret"}}], "claude"),
    ):
        with pytest.raises(BudgetError):
            parse_snapshot(payload, provider, NOW)


def test_unknown_or_duplicate_windows_are_not_guessed(report):
    usage = report[0]["usage"]
    usage["primary"]["windowMinutes"] = 1
    usage["secondary"]["windowMinutes"] = None
    with pytest.raises(BudgetError, match="No supported"):
        parse_snapshot(report, "claude", NOW)
    usage["primary"]["windowMinutes"] = 300
    usage["secondary"] = copy.deepcopy(usage["primary"])
    with pytest.raises(BudgetError, match="Ambiguous"):
        parse_snapshot(report, "claude", NOW)


def test_missing_identity_and_expired_window_fail_closed(report):
    report[0]["usage"]["identity"] = {}
    with pytest.raises(BudgetError, match="identity"):
        parse_snapshot(report, "claude", NOW)
