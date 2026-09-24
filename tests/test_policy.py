import copy

import pytest

from agent_budget.policy import BudgetError, compile_policy, evaluate, validate


def snapshot(used=40, reset=1000, identity="account"):
    return {
        "identity": identity,
        "updated_at": 100,
        "windows": {"weekly": {"used": used, "resets_at": reset}},
    }


def test_additional_points_and_fraction_are_distinct():
    additional = compile_policy({"usage": [{"max_additional_percent": 10}]}, snapshot(), 100)
    fraction = compile_policy({"usage": [{"remaining_fraction_percent": 10}]}, snapshot(), 100)
    assert additional["usage"][0]["ceiling"] == 50
    assert fraction["usage"][0]["ceiling"] == 46


def test_first_constraint_wins_and_buffer_is_explicit():
    policy = compile_policy(
        {
            "duration_minutes": 2,
            "usage": [
                {
                    "max_additional_percent": 10,
                    "min_remaining_percent": 55,
                }
            ],
        },
        snapshot(),
        100,
    )
    assert policy["usage"][0]["stop_at"] == 44.5
    assert evaluate(copy.deepcopy(policy), snapshot(44.5), 101).startswith("weekly")
    assert evaluate(policy, snapshot(40), 221) == "Time limit reached."


@pytest.mark.parametrize(
    "reading", [snapshot(1, 2000), snapshot(39), snapshot(41, identity="other")]
)
def test_uncertain_accounting_stops(reading):
    policy = compile_policy({"usage": [{"max_additional_percent": 10}]}, snapshot(), 100)
    assert evaluate(policy, reading, 101)


def test_reset_stops_without_refilling_even_if_meter_is_offline():
    policy = compile_policy({"usage": [{"max_additional_percent": 10}]}, snapshot(), 100)
    assert "not refilled" in evaluate(policy, None, 1000)


@pytest.mark.parametrize(
    "spec",
    [
        {},
        {"duration_minutes": True},
        {"duration_minutes": float("nan")},
        {"duration_minutes": float("inf")},
        {"duration_minutes": -2},
        {"typo": 5},
        {"deadline": "2026-09-24T07:00:00"},
        {"usage": {}},
        {"usage": [{"max_additional_percent": 110}]},
        {"usage": [{"max_additional_percent": 10, "remaining_fraction_percent": 10}]},
        {"usage": [{"max_additional_percent": 10, "window": "monthly"}]},
        {"usage": [{"buffer_percent": 1}]},
    ],
)
def test_invalid_specs_fail_closed(spec):
    with pytest.raises(BudgetError):
        validate(spec, 100)


def test_missing_usage_cannot_arm():
    with pytest.raises(BudgetError, match="not armed"):
        compile_policy({"usage": [{"max_additional_percent": 10}]}, None, 100)


def test_empty_headroom_cannot_arm():
    with pytest.raises(BudgetError, match="No usable"):
        compile_policy({"usage": [{"min_remaining_percent": 80}]}, snapshot(), 100)


def test_time_only_needs_no_usage():
    policy = compile_policy({"duration_minutes": 60}, None, 100)
    assert policy["deadline"] == 3700
    assert not policy["usage"]
