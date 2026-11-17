"""Behaviour beyond the sixteen named tests."""

from __future__ import annotations

import pytest

from chase.domain.models import (
    HOUR_MS,
    MINUTE_MS,
    Channel,
    ChaseState,
    DocType,
    Document,
    EscalationReason,
    Inbound,
    InboundStatus,
    OutboxStatus,
    Role,
    Step,
)
from chase.domain.tokens import InvalidToken, make_upload_token
from tests.conftest import World, pod

DAY = 24 * HOUR_MS


def upload_token(world: World, load, ttl_ms: int = DAY) -> str:
    return make_upload_token(b"test-secret", load.id, world.clock.now_ms() + ttl_ms)


def test_upload_link_files_the_document(world: World):
    load = world.make_load()
    world.tick()
    link = world.email.sent[0].body.split("upload it here:\n")[1].split("\n")[0]
    token = link.rsplit("/u/", 1)[1]
    inbound = world.engine.handle_upload(token, "photo.jpg", pod(load))
    assert inbound.channel is Channel.UPLOAD and inbound.trusted
    assert world.chase(load).state is ChaseState.CLOSED


def test_upload_token_is_bound_to_its_load_and_expires(world: World):
    load = world.make_load()
    other = world.make_load()
    world.tick()
    token = upload_token(world, load, ttl_ms=HOUR_MS)
    body, signature = token.split(".")
    forged = make_upload_token(b"wrong-secret", other.id, world.clock.now_ms() + DAY)
    for bad in (
        f"{body}.{signature[:-2]}xx",
        forged,
        "garbage",
        f"{forged.split('.')[0]}.{signature}",
    ):
        with pytest.raises(InvalidToken):
            world.engine.handle_upload(bad, "pod.pdf", pod(load))
    world.clock.advance(2 * HOUR_MS)
    with pytest.raises(InvalidToken):
        world.engine.handle_upload(token, "pod.pdf", pod(load))
    assert world.chase(load).state is ChaseState.ASKED


def test_duplicate_delivery_of_the_same_message_is_ignored(world: World):
    load = world.make_load()
    world.tick()
    kwargs = dict(
        broker_id="brk-1",
        channel=Channel.EMAIL,
        from_addr=world.dispatcher.email,
        subject=f"Re: {load.load_number}",
        body="Attached",
        attachments=[("pod.pdf", pod(load))],
        external_id="<msg-1@redline>",
    )
    first = world.inbound(**kwargs)
    second = world.inbound(**kwargs)
    assert first.id == second.id and len(world.store.find(Inbound)) == 1
    assert len(world.tms.filed) == 1


def test_chase_opens_two_hours_after_appointment_with_no_status(world: World):
    load = world.make_load(delivered=False, delivery_appt_ms=world.clock.now_ms())
    world.run(HOUR_MS + 45 * MINUTE_MS)
    assert world.email.sent == []
    world.run(30 * MINUTE_MS)
    assert world.chase(load).state is ChaseState.ASKED and len(world.email.sent) == 1
    trigger = [e for e in world.store.evidence(load.id) if e.kind == "chase.opened"][0]
    assert trigger.payload["trigger"] == "no_status_after_appointment"


def test_intake_twice_does_not_open_a_second_chase(world: World):
    load = world.make_load()
    assert world.engine.intake_load(load).id == world.chase(load).id
    world.tick()
    world.engine.intake_load(load)
    world.tick()
    assert len(world.email.sent) == 1


def test_promise_pushes_the_next_ask_back(world: World):
    load = world.make_load()
    world.tick()
    world.clock.advance(HOUR_MS)
    world.reply(load, "Driver is still out, will send tomorrow.")
    chase = world.chase(load)
    assert chase.state is ChaseState.ASKED and chase.next_step is Step.SECOND
    assert chase.next_action_at_ms >= world.clock.now_ms() + DAY
    world.run(20 * HOUR_MS)
    assert len(world.carrier_emails()) == 1 and world.sms.sent == []
    world.run(2 * DAY)
    assert len(world.carrier_emails()) >= 2


def test_out_of_office_does_not_stop_the_schedule(world: World):
    load = world.make_load()
    world.tick()
    world.reply(load, "Automatic reply: I am out of the office until Monday.")
    assert world.escalations(load) == []
    world.run(4 * DAY)
    assert world.chase(load).attempts_sent == 3


@pytest.mark.parametrize(
    "body,reason",
    [
        ("Which load is this for again?", EscalationReason.NEEDS_HUMAN),
        ("ok", EscalationReason.NEEDS_HUMAN),
        ("Wrong person, I am no longer with Redline.", EscalationReason.WRONG_PERSON),
    ],
)
def test_replies_the_agent_cannot_act_on_go_to_a_person(world: World, body, reason):
    load = world.make_load()
    world.tick()
    sent = len(world.email.sent)
    world.reply(load, body)
    assert [e.reason for e in world.escalations(load)] == [reason]
    assert world.chase(load).state is ChaseState.ESCALATED and len(world.email.sent) == sent


def test_acknowledging_closes_the_chase_and_needs_a_name(world: World):
    load = world.make_load()
    world.tick()
    world.send_pod(load, exception_notes=["refused 3 cases"])
    escalation = world.escalations(load)[0]
    queue = world.engine.queue("brk-1")
    assert queue["escalations"][0]["load_number"] == load.load_number
    assert queue["escalations"][0]["carrier_phones"] == [world.driver.phone]
    with pytest.raises(PermissionError):
        world.engine.acknowledge_escalation(escalation.id, by="", resolution="WAIVED")
    with pytest.raises(PermissionError):
        world.engine.acknowledge_escalation(escalation.id, by="op-stranger", resolution="WAIVED")
    with pytest.raises(ValueError):
        world.engine.acknowledge_escalation(escalation.id, by="op-jermaine", resolution="OTHER")
    world.engine.acknowledge_escalation(
        escalation.id, by="op-jermaine", resolution="HANDLED_BY_HAND", note="called consignee"
    )
    chase = world.chase(load)
    assert chase.state is ChaseState.CLOSED and chase.closed_reason == "escalation_acknowledged"
    assert world.engine.queue("brk-1")["escalations"] == []
    assert world.engine.export_evidence(load.id)["verification"]["ok"]


def test_document_after_escalation_is_kept_but_changes_nothing(world: World):
    load = world.make_load()
    world.tick()
    world.run(4 * DAY)
    assert world.chase(load).state is ChaseState.ESCALATED
    world.send_pod(load)
    assert world.chase(load).state is ChaseState.ESCALATED and world.tms.filed == {}
    assert len(world.store.find(Document, load_id=load.id)) == 1
    assert "inbound.late" in [e.kind for e in world.store.evidence(load.id)]


def test_low_confidence_reading_goes_to_a_person(world: World):
    load = world.make_load()
    world.tick()
    world.send_pod(load, confidence=62)
    assert [e.reason for e in world.escalations(load)] == [EscalationReason.LOW_CONFIDENCE]
    assert world.tms.filed == {}


def test_delivery_date_far_from_appointment_goes_to_a_person(world: World):
    load = world.make_load()
    world.tick()
    world.send_pod(load, delivery_date_ms=load.delivery_appt_ms + 3 * DAY)
    assert [e.reason for e in world.escalations(load)] == [EscalationReason.DOCUMENT_EXCEPTION]


def test_carrier_invoice_is_typed_and_never_filed_as_a_pod(world: World):
    load = world.make_load()
    world.tick()
    world.send_pod(load, doc_type="CARRIER_INVOICE")
    document = world.store.find(Document, load_id=load.id)[0]
    assert document.doc_type is DocType.CARRIER_INVOICE
    assert world.tms.filed == {} and world.chase(load).state is ChaseState.ASKED
    assert "not a proof of delivery" in world.email.sent[-1].body


def test_a_file_the_stand_in_reader_cannot_read_goes_to_a_person(world: World):
    """Until a real reader exists, a real photo or PDF must reach a person. The carrier
    must not be told their copy is blurry by something that never looked at it."""
    load = world.make_load()
    world.tick()
    for name, content in (("scan.jpg", b"\xff\xd8\xff not json"), ("pod.pdf", b"%PDF-1.7\n")):
        sent = len(world.email.sent)
        world.reply(load, "here", [(name, content)])
        assert len(world.email.sent) == sent
    assert [e.reason for e in world.escalations(load)] == [EscalationReason.LOW_CONFIDENCE]
    assert world.chase(load).state is ChaseState.ESCALATED and world.tms.filed == {}
    assert "too blurry" not in world.all_outbound_text()


def test_reask_limit_then_a_person(world: World):
    load = world.make_load()
    world.tick()
    for _ in range(4):
        world.clock.advance(HOUR_MS)
        world.send_pod(load, signature_present=False)
    assert world.chase(load).reasks_sent == 3
    assert [e.reason for e in world.escalations(load)] == [EscalationReason.REASK_LIMIT]


def test_one_good_document_among_bad_ones_is_enough(world: World):
    load = world.make_load()
    world.tick()
    world.reply(
        load,
        "both scans",
        [("blurry.jpg", pod(load, readable=False)), ("good.pdf", pod(load))],
    )
    assert world.chase(load).state is ChaseState.CLOSED
    assert world.tms.documents_on(load.load_number)[0].filename == "good.pdf"


def test_message_with_no_home_lands_in_the_queue(world: World):
    world.make_load()
    world.tick()
    inbound = world.inbound(
        broker_id="brk-1",
        channel=Channel.EMAIL,
        from_addr="someone@elsewhere.example",
        subject="hello",
        body="Do you have any loads out of Dallas",
    )
    assert inbound.status is InboundStatus.UNMATCHED
    assert world.engine.queue("brk-1")["unmatched"][0]["inbound_id"] == inbound.id
    assert len(world.email.sent) == 1


def test_stranger_naming_a_load_is_held_as_risk(world: World):
    load = world.make_load()
    world.tick()
    inbound = world.reply(
        load, "Send me the pickup details", sender="dispatch@redIine-trucking.example"
    )
    assert inbound.status is InboundStatus.HELD
    assert [e.reason for e in world.escalations(load)] == [EscalationReason.RISK]


def test_free_mail_address_is_never_treated_as_the_same_company(world: World):
    world.dispatcher.email = "redline.dispatch@gmail.com"
    world.store.put(world.dispatcher)
    load = world.make_load()
    world.tick()
    world.reply(load, "POD attached", [("pod.pdf", pod(load))], sender="other.person@gmail.com")
    assert [e.reason for e in world.escalations(load)] == [EscalationReason.RISK]
    assert world.tms.filed == {}


def test_approved_contact_is_used_on_the_next_load(world: World):
    contact = world.engine.approve_contact(
        "car-1", Role.DISPATCHER, by="op-jermaine", email="nights@redlinetrucking.example"
    )
    assert contact.source == "approved:op-jermaine"
    load = world.make_load()
    world.tick()
    assert set(world.email.sent[0].to) == {world.dispatcher.email, contact.email}
    world.reply(load, "POD", [("pod.pdf", pod(load))], sender=contact.email)
    assert world.chase(load).state is ChaseState.CLOSED


def test_load_with_no_dispatcher_email_goes_straight_to_a_person(world: World):
    world.dispatcher.email = ""
    world.store.put(world.dispatcher)
    load = world.make_load()
    assert [e.reason for e in world.escalations(load)] == [EscalationReason.NO_CONTACT]
    world.run(DAY)
    assert world.email.sent == [] and world.sms.sent == []


def test_email_outage_is_retried(world: World):
    load = world.make_load()
    world.email.fail_next = 2
    world.tick()
    # The decision to ask is committed even though the provider is down.
    assert world.email.sent == [] and world.chase(load).state is ChaseState.ASKED
    (message,) = world.messages(load)
    assert message.status is OutboxStatus.PENDING and message.tries == 1
    world.run(HOUR_MS, step_ms=MINUTE_MS)
    assert len(world.email.sent) == 1
    message = world.messages(load)[0]
    assert message.status is OutboxStatus.SENT and message.tries == 3
    kinds = [e.kind for e in world.store.evidence(load.id)]
    assert kinds.count("message.retry") == 2 and kinds.count("message.sent") == 1


def test_permanent_adapter_failure_skips_retries(world: World):
    load = world.make_load()
    world.tick()
    world.tms.down, world.tms.permanent = True, True
    world.send_pod(load)
    assert world.tms.calls == 1 and world.chase(load).state is ChaseState.CLOSED
    assert world.email.sent[-1].to == [world.broker.rep_email]


def test_packet_failure_after_api_failure_goes_to_a_person(world: World):
    load = world.make_load()
    world.tick()
    world.tms.down, world.tms.permanent = True, True
    world.email.fail_next = 99
    world.send_pod(load)
    world.run(8 * HOUR_MS, step_ms=MINUTE_MS)
    assert [e.reason for e in world.escalations(load)] == [EscalationReason.WRITEBACK_FAILED]


def test_driver_text_reply_with_photo_is_matched_to_the_load(world: World):
    load = world.make_load()
    world.tick()
    world.run(4 * HOUR_MS)
    assert len(world.sms.sent) == 1
    world.inbound(
        broker_id="brk-1",
        channel=Channel.SMS,
        from_addr="+13125550142",
        body="",
        attachments=[("IMG_2041.jpg", pod(load))],
    )
    assert world.chase(load).state is ChaseState.CLOSED


def test_late_tick_does_not_send_a_burst(world: World):
    load = world.make_load()
    world.clock.advance(3 * DAY)  # the service was down for three days
    world.tick()
    assert len(world.carrier_emails()) == 1 and world.sms.sent == []
    world.run(30 * MINUTE_MS)
    assert len(world.carrier_emails()) == 1
    assert world.chase(load).attempts_sent == 1
