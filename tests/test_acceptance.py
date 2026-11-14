"""The sixteen acceptance tests named in the build prompt. Each one is a rule."""

from __future__ import annotations

import dataclasses
import itertools

import pytest

from chase.domain import outbound
from chase.domain.models import (
    HOUR_MS,
    MINUTE_MS,
    Attempt,
    Broker,
    Channel,
    ChaseState,
    Check,
    CheckResult,
    Contact,
    EscalationReason,
    InboundStatus,
    Step,
    WriteBack,
    WriteBackStatus,
)
from chase.domain.schedule import to_local
from chase.domain.states import ALLOWED, IllegalTransition, transition
from chase.evidence import EvidenceEvent, verify_chain
from tests.conftest import TZ, World, at, pod

DAY = 24 * HOUR_MS


def test_happy_path_closes_with_passed_check_and_writeback(world: World):
    load = world.make_load()
    world.engine.tick()
    assert world.chase(load).state is ChaseState.ASKED
    assert [m.to for m in world.email.sent] == [[world.dispatcher.email]]

    world.clock.advance(40 * MINUTE_MS)
    world.send_pod(load)

    chase = world.chase(load)
    assert chase.state is ChaseState.CLOSED and chase.closed_reason == "filed"
    checks = world.store.find(Check, chase_id=chase.id)
    assert [c.result for c in checks] == [CheckResult.PASS]
    writebacks = world.store.find(WriteBack, chase_id=chase.id)
    assert [(w.adapter, w.status) for w in writebacks] == [("fake_api", WriteBackStatus.SUCCEEDED)]
    assert len(world.tms.documents_on(load.load_number)) == 1
    assert world.store.get(type(load), load.id).ready_to_invoice_at_ms == world.clock.now_ms()
    assert world.engine.export_evidence(load.id)["verification"]["ok"]
    assert world.escalations(load) == []


def test_three_attempts_then_escalation_and_no_fourth_send(world: World):
    load = world.make_load()
    world.engine.tick()
    world.run(4 * DAY)

    emails = world.carrier_emails()
    assert [m.subject.split(":")[0] for m in emails] == [
        f"proof of delivery for load {load.load_number}",
        "Second request",
        "Third request",
    ]
    assert emails[2].cc == [world.broker.rep_email] and emails[0].cc == emails[1].cc == []
    assert len(world.sms.sent) == 1  # the driver, three hours after the first ask

    chase = world.chase(load)
    assert chase.state is ChaseState.ESCALATED and chase.attempts_sent == 3
    assert [e.reason for e in world.escalations(load)] == [EscalationReason.NO_RESPONSE]

    sent_before = (len(world.email.sent), len(world.sms.sent))
    world.run(10 * DAY, step_ms=HOUR_MS)
    assert (len(world.email.sent), len(world.sms.sent)) == sent_before


def test_quiet_hours_respected_in_carrier_timezone():
    # 22:30 on a Tuesday in Los Angeles, which is already Wednesday morning in New York.
    tz = "America/Los_Angeles"
    world = World(start=at(2026, 11, 10, 22, 30, tz=tz), carrier_tz=tz)
    load = world.make_load()
    assert world.engine.tick() == 0 and world.email.sent == []

    world.run(5 * DAY)
    attempts = world.store.find(Attempt, chase_id=world.chase(load).id)
    assert len(attempts) >= 4
    for attempt in attempts:
        hour = to_local(attempt.sent_at_ms, tz).hour
        assert 7 <= hour < 20, f"{attempt.step} sent at {hour}:00 carrier-local"
    first = min(attempts, key=lambda a: a.sent_at_ms)
    assert first.sent_at_ms == at(2026, 11, 11, 7, tz=tz)


def test_failed_check_reasks_with_specific_reason(world: World):
    load = world.make_load(expected_pages=2)
    world.engine.tick()
    world.send_pod(load, signature_present=False, page_count=1)

    chase = world.chase(load)
    assert chase.state is ChaseState.ASKED and chase.reasks_sent == 1
    reask = world.email.sent[-1]
    assert reask.to == [world.dispatcher.email]
    assert reask.subject.startswith("Please resend")
    assert outbound.REASON_TEXT["unsigned"] in reask.body
    assert outbound.REASON_TEXT["missing_pages"] in reask.body
    assert outbound.REASON_TEXT["unreadable"] not in reask.body
    assert world.store.find(WriteBack, chase_id=chase.id) == []

    world.clock.advance(HOUR_MS)
    world.send_pod(load, page_count=2)
    assert world.chase(load).state is ChaseState.CLOSED


@pytest.mark.parametrize(
    "overrides",
    [
        {"exception_notes": ["2 cartons damaged"]},
        {"exception_notes": ["short 1 pallet"], "signature_present": False},
        {"exception_notes": ["seal broken on arrival"], "page_count": 0},
    ],
)
def test_exception_notes_never_filed_clean(world: World, overrides):
    load = world.make_load()
    world.engine.tick()
    sent_before = len(world.email.sent)
    world.send_pod(load, **overrides)

    chase = world.chase(load)
    assert chase.state is ChaseState.ESCALATED
    assert [e.reason for e in world.escalations(load)] == [EscalationReason.DOCUMENT_EXCEPTION]
    assert world.tms.filed == {} and world.store.find(WriteBack, chase_id=chase.id) == []
    assert world.store.get(type(load), load.id).ready_to_invoice_at_ms is None
    assert len(world.email.sent) == sent_before  # no reply, no packet
    world.run(5 * DAY, step_ms=HOUR_MS)
    assert world.tms.filed == {}


def test_document_for_other_open_load_is_rerouted(world: World):
    load_a = world.make_load()
    load_b = world.make_load()
    world.engine.tick()

    # The dispatcher answers the thread for load A with load B's paperwork.
    world.reply(load_a, "Here you go.", [("pod.pdf", pod(load_b))])

    chase_b = world.chase(load_b)
    assert chase_b.state is ChaseState.CLOSED and chase_b.closed_reason == "filed"
    assert len(world.tms.documents_on(load_b.load_number)) == 1
    assert world.tms.documents_on(load_a.load_number) == []

    chase_a = world.chase(load_a)
    assert chase_a.state is ChaseState.ASKED and chase_a.reasks_sent == 1
    reask = world.email.sent[-1]
    assert load_a.load_number in reask.subject
    assert outbound.REASON_TEXT["rerouted"] in reask.body
    kinds = [e.kind for e in world.store.evidence(load_a.id)]
    assert "document.rerouted" in kinds


def test_new_reply_address_is_not_adopted(world: World):
    load = world.make_load()
    world.engine.tick()
    contacts_before = {c.id for c in world.store.find(Contact)}
    newcomer = "new.dispatcher@redlinetrucking.example"

    inbound = world.reply(load, "I handle PODs now, send them to me.", sender=newcomer)
    assert inbound.status is InboundStatus.HELD
    assert [e.reason for e in world.escalations(load)] == [EscalationReason.UNKNOWN_SENDER]
    assert {c.id for c in world.store.find(Contact)} == contacts_before

    # A second load for the same carrier: the newcomer is still not written to.
    load_2 = world.make_load()
    world.run(4 * DAY)
    recipients = {addr for m in world.email.sent for addr in m.to + m.cc}
    assert newcomer not in recipients
    assert recipients <= {world.dispatcher.email, world.broker.rep_email}
    assert world.chase(load_2).attempts_sent == 3


def test_bank_change_message_is_held_without_reply(world: World):
    load = world.make_load()
    world.engine.tick()
    sent_before = (len(world.email.sent), len(world.sms.sent))

    inbound = world.reply(
        load,
        "POD attached. Also our bank details changed, please update the remit-to on file.",
        [("pod.pdf", pod(load))],
    )

    assert inbound.status is InboundStatus.HELD and inbound.hold_reason == EscalationReason.RISK
    escalations = world.escalations(load)
    assert [e.reason for e in escalations] == [EscalationReason.RISK]
    assert "bank" in escalations[0].detail and "remit" in escalations[0].detail
    assert world.chase(load).state is ChaseState.ESCALATED
    assert world.tms.filed == {}  # the attached POD is not filed either
    world.run(5 * DAY, step_ms=HOUR_MS)
    assert (len(world.email.sent), len(world.sms.sent)) == sent_before


def test_outbound_never_contains_rate_or_customer(world: World, monkeypatch):
    world.dispatcher.phone, world.dispatcher.sms_consent = "+13125550199", True
    world.store.put(world.dispatcher)
    load = world.make_load()
    world.engine.tick()
    world.run(4 * HOUR_MS)  # first email, then the driver text
    world.send_pod(load, readable=False)  # forces a re-ask as well
    world.run(4 * DAY)
    assert len(world.carrier_emails()) >= 4 and len(world.sms.sent) >= 2

    text = world.all_outbound_text().lower()
    for secret in ("acme foods", "riverside distribution", "fulton industrial", "pu-77812"):
        assert secret not in text
    for rate in ("1850", "1,850", "$1"):
        assert rate not in text.replace(load.load_number.lower(), "")

    # The guard itself refuses each kind of leak.
    for leak in (
        "Rate is $1,850.00",
        "pays 1850",
        "for Acme Foods",
        "PU-77812",
        "at Riverside Distribution",
    ):
        with pytest.raises(outbound.OutboundBlocked):
            outbound.assert_safe(f"Please send the POD. {leak}", load)
    outbound.assert_safe("Please send the POD for load 18500, ref 21850.", load)  # not the rate

    # And if a template is ever changed to leak, nothing is sent and a person is told.
    leaky = World()
    load_2 = leaky.make_load()
    monkeypatch.setattr(
        outbound, "email_body", lambda broker, load, step, link, reasons: "Load for Acme Foods"
    )
    leaky.engine.tick()
    assert leaky.email.sent == []
    assert [e.reason for e in leaky.escalations(load_2)] == [EscalationReason.OUTBOUND_BLOCKED]


def test_writeback_is_idempotent_after_transient_failure(world: World):
    load = world.make_load()
    world.engine.tick()
    world.tms.fail_after_commit_next = 1  # the TMS writes the file, then the response is lost
    world.send_pod(load)

    chase = world.chase(load)
    assert chase.state is ChaseState.PASSED and world.tms.calls == 1

    world.run(2 * MINUTE_MS, step_ms=MINUTE_MS)
    assert world.chase(load).state is ChaseState.CLOSED
    assert world.tms.calls == 2
    assert len(world.tms.documents_on(load.load_number)) == 1  # attached once, not twice
    writebacks = world.store.find(WriteBack, chase_id=chase.id)
    assert [(w.status, w.attempts) for w in writebacks] == [(WriteBackStatus.SUCCEEDED, 2)]


def test_adapter_outage_falls_back_to_packet_email(world: World):
    load = world.make_load()
    world.engine.tick()
    world.tms.down = True
    world.send_pod(load)
    assert world.chase(load).state is ChaseState.PASSED

    world.run(HOUR_MS, step_ms=MINUTE_MS)
    chase = world.chase(load)
    assert chase.state is ChaseState.CLOSED
    writebacks = sorted(world.store.find(WriteBack, chase_id=chase.id), key=lambda w: w.id)
    assert [(w.adapter, w.status) for w in writebacks] == [
        ("fake_api", WriteBackStatus.FAILED),
        ("packet_email", WriteBackStatus.SUCCEEDED),
    ]
    assert world.tms.calls == 4  # one try and three retries with backoff
    packet = world.email.sent[-1]
    assert packet.to == [world.broker.rep_email] and load.load_number in packet.subject
    assert len(packet.attachments) == 1
    assert world.escalations(load) == []


def test_sms_requires_consent_and_honors_stop(world: World):
    # No consent on file: no text, and the evidence says why.
    world.driver.sms_consent = False
    world.store.put(world.driver)
    load_1 = world.make_load()
    world.engine.tick()
    world.run(4 * HOUR_MS)
    assert world.sms.sent == []
    assert "sms.skipped" in [e.kind for e in world.store.evidence(load_1.id)]

    # Consent on file: one text to the driver.
    world.driver.sms_consent = True
    world.store.put(world.driver)
    load_2 = world.make_load()
    world.engine.tick()
    world.run(16 * HOUR_MS)  # past the quiet hours, to 07:00 the next morning
    assert [m.to for m in world.sms.sent] == [world.driver.phone]
    assert "STOP" in world.sms.sent[0].body

    # The driver replies STOP: never texted again, on this load or the next.
    world.engine.handle_inbound(
        broker_id="brk-1", channel=Channel.SMS, from_addr="3125550142", body=" stop "
    )
    assert world.store.get(Contact, world.driver.id).sms_stopped is True
    load_3 = world.make_load()
    world.engine.tick()
    world.run(4 * DAY)
    assert len(world.sms.sent) == 1
    assert world.chase(load_2).attempts_sent == world.chase(load_3).attempts_sent == 3


def test_kill_switch_stops_all_outbound(world: World):
    world.engine.set_global_outbound(False, by="jermaine")
    load = world.make_load()
    world.engine.tick()
    world.run(3 * DAY, step_ms=HOUR_MS)
    assert world.email.sent == [] and world.sms.sent == []
    assert world.chase(load).state is ChaseState.OPEN

    # A document that arrives anyway is kept and checked, but nothing is sent or filed.
    world.send_pod(load)
    assert world.chase(load).state is ChaseState.PASSED
    assert world.tms.filed == {} and world.email.sent == []

    world.engine.set_global_outbound(True, by="jermaine")
    world.engine.tick()
    assert world.chase(load).state is ChaseState.CLOSED and len(world.tms.filed) == 1

    # The per-broker switch stops one broker and leaves the other running.
    other = dataclasses.replace(world.broker, id="brk-2", name="Second Broker")
    world.store.put(other)
    world.engine.set_broker_outbound("brk-1", False, by="jermaine")
    assert world.store.get(Broker, "brk-1").outbound_enabled is False
    load_2 = world.make_load()
    world.run(DAY, step_ms=HOUR_MS)
    assert world.email.sent == [] and world.chase(load_2).state is ChaseState.OPEN
    assert "outbound.blocked" in [e.kind for e in world.store.evidence(load_2.id)]


def test_evidence_chain_verifies_and_detects_tamper_at_index(world: World):
    load = world.make_load()
    world.engine.tick()
    world.send_pod(load, signature_present=False)
    world.clock.advance(HOUR_MS)
    world.send_pod(load)

    export = world.engine.export_evidence(load.id)
    events = [EvidenceEvent.from_dict(e) for e in export["events"]]
    assert export["verification"]["ok"] and len(events) > 20
    assert verify_chain(events).head_hash == export["verification"]["head_hash"]

    for index in (0, 7, len(events) - 1):
        forged = list(events)
        forged[index] = dataclasses.replace(
            forged[index], payload={**forged[index].payload, "forged": True}
        )
        result = verify_chain(forged)
        assert not result.ok and result.failed_index == index

    shortened = events[:5] + events[6:]
    assert verify_chain(shortened).failed_index == 5


def _scenario(variant: int = 0) -> dict[str, str]:
    world = World()
    loads = [world.make_load() for _ in range(3)]
    world.engine.tick()
    world.send_pod(loads[0])
    world.send_pod(loads[1], signature_present=bool(variant))
    world.run(4 * 24 * HOUR_MS)
    chains = [load.id for load in loads] + ["broker:brk-1", "system"]
    return {c: verify_chain(world.store.evidence(c)).head_hash for c in chains}


def test_same_fixture_and_clock_produce_identical_hashes():
    first, second = _scenario(), _scenario()
    assert first == second
    assert len(set(first.values())) >= 3
    assert _scenario(variant=1)["load-2"] != first["load-2"]


def test_illegal_state_transition_raises(world: World):
    load = world.make_load()
    for current, new in itertools.product(ChaseState, ChaseState):
        chase = dataclasses.replace(world.chase(load), state=current)
        if new in ALLOWED[current]:
            assert transition(chase, new) == (current, new) and chase.state is new
        else:
            with pytest.raises(IllegalTransition):
                transition(chase, new)
            assert chase.state is current
    assert ALLOWED[ChaseState.CLOSED] == frozenset()
    assert ALLOWED[ChaseState.ESCALATED] == frozenset({ChaseState.CLOSED})

    # The engine cannot be talked out of a terminal state either.
    world.engine.tick()
    world.send_pod(load)
    world.send_pod(load, signature_present=False)
    world.run(5 * 24 * HOUR_MS, step_ms=HOUR_MS)
    assert world.chase(load).state is ChaseState.CLOSED
    assert len(world.tms.filed) == 1
    assert Step.NONE is world.chase(load).next_step
    assert TZ  # carrier-local time zone used throughout
