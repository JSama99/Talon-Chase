"""Unit tests for the pure rules: schedule, risk, document checks."""

from __future__ import annotations

import pytest

from chase.domain.checks import Extraction, check_document
from chase.domain.models import HOUR_MS, BrokerConfig, CheckResult, DocType, Load
from chase.domain.risk import (
    domain_of,
    find_risk_terms,
    is_stop_message,
    normalize_email,
    normalize_phone,
    same_company_domain,
)
from chase.domain.schedule import build_plan, in_quiet_hours, next_allowed, to_local
from tests.conftest import at

CFG = BrokerConfig()
TZ = "America/Chicago"


def local(ms: int) -> str:
    return to_local(ms, TZ).strftime("%a %H:%M")


def test_plan_for_a_weekday_afternoon():
    plan = build_plan(at(2026, 11, 10, 14), TZ, CFG)
    assert [local(v) for v in plan.to_dict().values()] == [
        "Tue 14:00", "Tue 17:00", "Wed 08:00", "Thu 14:00", "Fri 14:00",
    ]  # fmt: skip


def test_plan_for_a_friday_night_skips_the_weekend_morning():
    plan = build_plan(at(2026, 11, 13, 22, 30), TZ, CFG)
    assert [local(v) for v in plan.to_dict().values()] == [
        "Sat 07:00", "Sat 10:00", "Mon 08:00", "Mon 12:00", "Tue 12:00",
    ]  # fmt: skip


def test_driver_text_is_dropped_when_it_would_land_after_the_second_ask():
    plan = build_plan(at(2026, 11, 10, 19, 30), TZ, BrokerConfig(sms_delay_ms=14 * HOUR_MS))
    assert plan.sms_ms == 0


@pytest.mark.parametrize(
    "hour,minute,expected",
    [(6, 59, "Tue 07:00"), (7, 0, "Tue 07:00"), (19, 59, "Tue 19:59"), (20, 0, "Wed 07:00")],
)
def test_quiet_hours_boundaries(hour, minute, expected):
    ts = at(2026, 11, 10, hour, minute)
    assert local(next_allowed(ts, TZ, CFG)) == expected
    assert in_quiet_hours(ts, TZ, CFG) == (expected != local(ts))


def test_quiet_hours_across_the_daylight_saving_change():
    # Clocks go back on 1 November 2026. 21:00 on the 31st still resumes at 07:00 local.
    resumed = next_allowed(at(2026, 10, 31, 21), TZ, CFG)
    assert to_local(resumed, TZ).strftime("%d %H:%M") == "01 07:00"
    assert resumed - at(2026, 10, 31, 21) == 11 * HOUR_MS  # ten hours on the clock, eleven real


@pytest.mark.parametrize(
    "text",
    [
        "Our bank changed",
        "new routing number is 0210",
        "please update acct # on file",
        "we now use a factoring company",
        "see attached Notice of Assignment",
        "NOA attached",
        "send payment by ACH",
        "wire it to us",
        "remit to the address below",
        "updated remittance info",
        "new payment instructions attached",
        "can you Zelle the driver",
    ],
)
def test_money_language_is_flagged(text):
    assert find_risk_terms(text)


@pytest.mark.parametrize(
    "text",
    [
        "POD attached, thanks",
        "Driver delivered at 10am, receiver signed",
        "Will send tomorrow when he reaches the yard",
        "Unloaded at dock 4",
        "Reached the consignee late",
    ],
)
def test_ordinary_replies_are_not_flagged(text):
    assert find_risk_terms(text) == []


def test_address_helpers():
    assert normalize_email(" Dana <Dispatch@Redline.EXAMPLE> ") == "dispatch@redline.example"
    assert domain_of("a@b.example") == "b.example" and domain_of("nonsense") == ""
    assert normalize_phone("+1 (312) 555-0142") == normalize_phone("312.555.0142") == "3125550142"
    assert same_company_domain("new@redline.example", ["dispatch@redline.example"])
    assert not same_company_domain("new@redIine.example", ["dispatch@redline.example"])
    assert not same_company_domain("x@gmail.com", ["y@gmail.com"])
    assert is_stop_message("Stop.") and is_stop_message(" UNSUBSCRIBE ")
    assert not is_stop_message("stop asking me") and not is_stop_message("")


LOAD = Load("L1", "b", "c", "TC-100001", delivery_appt_ms=1_000_000_000, bol_number="BOL 55-001")
OTHER = Load("L2", "b", "c", "TC-100002", delivery_appt_ms=1_000_000_000)


def extraction(**overrides) -> Extraction:
    base = dict(
        doc_type=DocType.POD,
        readable=True,
        reference_numbers=["tc 100001"],
        signature_present=True,
        delivery_date_ms=1_000_000_000,
        page_count=1,
        exception_notes=[],
        confidence=95,
    )
    base.update(overrides)
    return Extraction(**base)


@pytest.mark.parametrize(
    "overrides,result,reasons",
    [
        ({}, CheckResult.PASS, []),
        ({"reference_numbers": ["BOL55001"]}, CheckResult.PASS, []),
        ({"confidence": 79}, CheckResult.EXCEPTION, ["low_confidence"]),
        ({"readable": False}, CheckResult.FAIL, ["unreadable"]),
        ({"doc_type": DocType.RATE_CONFIRMATION}, CheckResult.FAIL, ["not_a_pod"]),
        ({"reference_numbers": ["TC-999999"]}, CheckResult.FAIL, ["wrong_load"]),
        ({"reference_numbers": []}, CheckResult.FAIL, ["wrong_load"]),
        ({"reference_numbers": ["TC-100002"]}, CheckResult.REROUTE, ["rerouted"]),
        ({"signature_present": False}, CheckResult.FAIL, ["unsigned"]),
        ({"delivery_date_ms": None}, CheckResult.FAIL, ["no_delivery_date"]),
        ({"page_count": 0}, CheckResult.FAIL, ["missing_pages"]),
        (
            {"signature_present": False, "delivery_date_ms": None, "page_count": 0},
            CheckResult.FAIL,
            ["unsigned", "no_delivery_date", "missing_pages"],
        ),
        ({"exception_notes": ["damaged"]}, CheckResult.EXCEPTION, ["exception_notes"]),
        (
            {"exception_notes": ["damaged"], "signature_present": False},
            CheckResult.EXCEPTION,
            ["exception_notes"],
        ),
        (
            {"delivery_date_ms": 1_000_000_000 + 25 * HOUR_MS},
            CheckResult.EXCEPTION,
            ["date_mismatch"],
        ),
        ({"delivery_date_ms": 1_000_000_000 - 23 * HOUR_MS}, CheckResult.PASS, []),
    ],
)
def test_document_check_table(overrides, result, reasons):
    outcome = check_document(extraction(**overrides), LOAD, [OTHER], CFG)
    assert (outcome.result, outcome.reasons) == (result, reasons)
    assert outcome.reroute_load_id == ("L2" if result is CheckResult.REROUTE else "")


def test_extraction_round_trip_and_validation():
    e = extraction(exception_notes=["short 2"])
    assert Extraction.from_dict(e.to_dict()) == e
    with pytest.raises(ValueError):
        Extraction.from_dict({"delivery_date_ms": 1.5})
    with pytest.raises(ValueError):
        Extraction.from_dict({"doc_type": "PASSPORT"})
