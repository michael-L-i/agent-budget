import copy
import json
import sys
import threading
import time

import pytest

from agent_budget import meter
from agent_budget.meter import parse_codex
from agent_budget.policy import BudgetError

NOW = 1_800_000_000


@pytest.fixture
def account():
    return {"account": {"type": "chatgpt", "email": "test@example.invalid"}}


@pytest.fixture
def limits():
    return {
        "rateLimits": {
            "limitId": "codex",
            "primary": {"usedPercent": 23, "windowDurationMins": 300, "resetsAt": NOW + 100},
            "secondary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": NOW + 1000},
        }
    }


def test_native_codex_contract_and_redaction(account, limits):
    result = parse_codex(account, limits, NOW)
    assert result["windows"]["weekly"]["used"] == 40
    assert "test@example.invalid" not in str(result)


def test_cadence_not_position(account, limits):
    bucket = limits["rateLimits"]
    bucket["primary"], bucket["secondary"] = bucket["secondary"], bucket["primary"]
    assert parse_codex(account, limits, NOW)["windows"]["weekly"]["used"] == 40


@pytest.mark.parametrize("value", [True, -1, 101, float("nan"), None, "40"])
def test_invalid_quota_fails_closed(account, limits, value):
    limits["rateLimits"]["primary"]["usedPercent"] = value
    with pytest.raises(BudgetError):
        parse_codex(account, limits, NOW)


def test_unknown_and_ambiguous_windows(account, limits):
    bucket = limits["rateLimits"]
    bucket["primary"]["windowDurationMins"] = 1
    bucket["secondary"]["windowDurationMins"] = None
    with pytest.raises(BudgetError, match="supported"):
        parse_codex(account, limits, NOW)
    bucket["primary"]["windowDurationMins"] = 300
    bucket["secondary"] = copy.deepcopy(bucket["primary"])
    with pytest.raises(BudgetError, match="ambiguous"):
        parse_codex(account, limits, NOW)


def test_never_substitutes_another_bucket(account, limits):
    limits["rateLimitsByLimitId"] = {"other_model": limits["rateLimits"]}
    with pytest.raises(BudgetError, match="bucket"):
        parse_codex(account, limits, NOW)


@pytest.mark.parametrize(
    "account",
    [{"account": None}, {"account": {"type": "apiKey"}}, {"account": {"type": "chatgpt"}}],
)
def test_missing_subscription_identity(account, limits):
    with pytest.raises(BudgetError):
        parse_codex(account, limits, NOW)


def test_expired_window(account, limits):
    limits["rateLimits"]["primary"]["resetsAt"] = NOW
    with pytest.raises(BudgetError, match="expired"):
        parse_codex(account, limits, NOW)


def test_rpc_initialization_order_no_generation_and_cleanup(tmp_path, monkeypatch):
    transcript = tmp_path / "requests.jsonl"
    binary = tmp_path / "codex"
    binary.write_text(
        f"#!{sys.executable}\nimport sys,json,time\n"
        f"log=open({str(transcript)!r}, 'w', buffering=1)\n"
        "for line in sys.stdin:\n"
        " log.write(line); m=json.loads(line)\n"
        " if 'id' not in m: continue\n"
        " result={}\n"
        " if m['method']=='account/read':\n"
        "  result={'account':{'type':'chatgpt','email':'t@example.invalid'}}\n"
        " if m['method']=='account/rateLimits/read':\n"
        "  result={'rateLimits':{'primary':{'usedPercent':4,\n"
        "   'windowDurationMins':300,'resetsAt':time.time()+500}}}\n"
        " print(json.dumps({'id':m['id'],'result':result}),flush=True)\n"
    )
    binary.chmod(0o700)
    monkeypatch.setattr(meter.shutil, "which", lambda _: str(binary))
    result = meter.fetch_codex()
    assert result["windows"]["five_hour"]["used"] == 4
    methods = [json.loads(line)["method"] for line in transcript.read_text().splitlines()]
    assert methods == ["initialize", "initialized", "account/read", "account/rateLimits/read"]


def test_rpc_cancellation_does_not_wait_for_timeout(tmp_path, monkeypatch):
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\nsleep 60\n")
    binary.chmod(0o700)
    monkeypatch.setattr(meter.shutil, "which", lambda _: str(binary))
    cancelled = threading.Event()
    cancelled.set()
    started = time.monotonic()
    with pytest.raises(BudgetError, match="cancelled"):
        meter.fetch_codex(cancelled)
    assert time.monotonic() - started < 3
