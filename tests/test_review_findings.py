"""One test per Codex review finding on pull requests #1 and #2.

Each test reproduces the scenario the reviewer described and states the correct
behaviour. They were written to fail before the fix.
"""

from __future__ import annotations

import pytest

from chase.channels.fakes import FakeEmail
from chase.domain.models import (
    HOUR_MS,
    MINUTE_MS,
    Channel,
    ChaseState,
    EscalationReason,
    Load,
    OutboxMessage,
    OutboxStatus,
)
from chase.domain.tokens import make_upload_token
from chase.outbox import OutboxDispatcher
from tests.conftest import World, at, pod

DAY = 24 * HOUR_MS


def upload(world: World, load: Load, content: bytes) -> None:
    token = make_upload_token(b"test-secret", load.id, world.clock.now_ms() + DAY)
    world.engine.handle_upload(token, "pod.jpg", content)
    world.settle()


# ------------------------------------------------- PR #1, P1: grouped reroutes


@pytest.mark.parametrize("order", ["clean_first", "damaged_first"])
def test_rerouted_documents_for_one_load_are_judged_together(world: World, order):
    load_a = world.make_load()
    load_b = world.make_load()
    world.tick()
    clean = ("clean.pdf", pod(load_b))
    damaged = ("damaged.pdf", pod(load_b, exception_notes=["3 cartons crushed"]))
    attachments = [clean, damaged] if order == "clean_first" else [damaged, clean]

    world.reply(load_a, "Paperwork attached.", attachments)

    chase_b = world.chase(load_b)
    assert chase_b.state is ChaseState.ESCALATED
    assert [e.reason for e in world.escalations(load_b)] == [EscalationReason.DOCUMENT_EXCEPTION]
    assert world.tms.documents_on(load_b.load_number) == []
    assert world.store.get(Load, load_b.id).ready_to_invoice_at_ms is None
    # Both documents were recorded on B, and A was asked again.
    received = [e for e in world.store.evidence(load_b.id) if e.kind == "document.received"]
    assert len(received) == 2
    assert world.chase(load_a).state is ChaseState.ASKED


def test_reroutes_to_two_different_loads_are_each_judged_on_their_own(world: World):
    load_a, load_b, load_c = world.make_load(), world.make_load(), world.make_load()
    world.tick()
    world.reply(
        load_a,
        "Two PODs.",
        [("b.pdf", pod(load_b)), ("c.pdf", pod(load_c, exception_notes=["short 1 pallet"]))],
    )
    assert world.chase(load_b).state is ChaseState.CLOSED
    assert world.chase(load_c).state is ChaseState.ESCALATED


# ------------------------------- PR #1, P2: corrected document during a pending re-ask


def test_corrected_upload_in_quiet_hours_is_accepted():
    world = World(start=at(2026, 11, 10, 21))
    load = world.make_load()
    upload(world, load, pod(load, signature_present=False))
    assert world.chase(load).state is ChaseState.FAILED_CHECK  # re-ask waits for 07:00

    world.clock.advance(30 * MINUTE_MS)
    upload(world, load, pod(load))
    assert world.chase(load).state is ChaseState.CLOSED
    assert len(world.tms.documents_on(load.load_number)) == 1

    world.run(12 * HOUR_MS)
    assert world.email.sent == [] and world.sms.sent == []  # never asked for it again


def test_corrected_email_reply_while_outbound_is_off_is_accepted(world: World):
    load = world.make_load()
    world.tick()
    world.engine.set_global_outbound(False, by="jermaine")
    world.send_pod(load, signature_present=False)
    assert world.chase(load).state is ChaseState.FAILED_CHECK

    world.clock.advance(HOUR_MS)
    world.send_pod(load)
    assert world.chase(load).state is ChaseState.PASSED  # filing waits for the switch
    world.engine.set_global_outbound(True, by="jermaine")
    world.tick()
    assert world.chase(load).state is ChaseState.CLOSED
    reasks = [m for m in world.messages(load) if m.subject.startswith("Please resend")]
    assert reasks == []


def test_second_bad_document_during_pending_reask_updates_the_reasons():
    world = World(start=at(2026, 11, 10, 21))
    load = world.make_load(expected_pages=2)
    upload(world, load, pod(load, signature_present=False, page_count=2))
    world.clock.advance(30 * MINUTE_MS)
    upload(world, load, pod(load, page_count=1))
    chase = world.chase(load)
    assert chase.state is ChaseState.FAILED_CHECK and chase.fail_reasons == ["missing_pages"]
    world.run(11 * HOUR_MS)
    (reask,) = [m for m in world.email.sent if m.subject.startswith("Please resend")]
    assert "pages are missing" in reask.body and "signature" not in reask.body


# ------------------------------------------------- PR #2, P1: STOP with a queued text


def test_text_queued_before_stop_is_not_sent(world: World):
    load = world.make_load()
    world.tick()
    world.clock.advance(3 * HOUR_MS)
    world.engine.tick()  # the driver text is queued, not yet delivered
    (text,) = [m for m in world.messages(load) if m.channel is Channel.SMS]
    assert text.status is OutboxStatus.PENDING

    world.inbound(broker_id="brk-1", channel=Channel.SMS, from_addr="3125550142", body="STOP")

    assert world.sms.sent == []
    text = world.store.get(OutboxMessage, text.id)
    assert text.status is OutboxStatus.CANCELLED


def test_text_waiting_for_a_retry_is_not_sent_after_stop(world: World):
    load = world.make_load()
    world.tick()
    world.sms.fail_next = 1
    world.run(3 * HOUR_MS, step_ms=MINUTE_MS)  # text fails once; retry due in a minute
    (text,) = [m for m in world.messages(load) if m.channel is Channel.SMS]
    assert text.status is OutboxStatus.PENDING and text.tries == 1

    world.inbound(broker_id="brk-1", channel=Channel.SMS, from_addr="+13125550142", body="stop")
    world.run(HOUR_MS, step_ms=MINUTE_MS)
    assert world.sms.sent == []
    assert world.store.get(OutboxMessage, text.id).status is OutboxStatus.CANCELLED


def test_text_is_not_sent_once_consent_is_withdrawn_in_a_contact_sync(world: World):
    load = world.make_load()
    world.tick()
    world.clock.advance(3 * HOUR_MS)
    world.engine.tick()
    world.driver.sms_consent = False
    world.store.put(world.driver)
    world.settle()
    assert world.sms.sent == []
    texts = [m for m in world.messages(load) if m.channel is Channel.SMS]
    assert [m.status for m in texts] == [OutboxStatus.CANCELLED]


# -------------------------------- PR #2, P2 and P1: long batches, changes mid-batch


class SlowEmail(FakeEmail):
    """Each provider call takes a minute; an optional hook runs during a given call."""

    def __init__(self, world: World, hook_on_call: int = 0, hook=None) -> None:
        super().__init__(honors_idempotency=False)
        self.world, self.hook_on_call, self.hook = world, hook_on_call, hook

    def send(self, message):
        result = super().send(message)
        self.world.clock.advance(MINUTE_MS)
        if self.hook and self.calls == self.hook_on_call:
            self.hook()
        return result


def queued_world(n: int, **kwargs) -> tuple[World, list[Load]]:
    world = World(**kwargs)
    loads = [world.make_load() for _ in range(n)]
    world.engine.tick()  # n first requests queued, none delivered
    return world, loads


def test_a_long_batch_does_not_let_leases_expire_before_sending():
    world, loads = queued_world(8)
    second = OutboxDispatcher(world.engine)
    world.engine.email = SlowEmail(world, hook_on_call=6, hook=second.dispatch)
    world.outbox.dispatch()
    world.outbox.dispatch()
    sent = world.engine.email.sent
    assert len(sent) == 8  # every message delivered exactly once
    assert len({m.idempotency_key for m in sent}) == 8
    assert "message.duplicate" not in {
        e.kind for load in loads for e in world.store.evidence(load.id)
    }


def test_kill_switch_stops_the_rest_of_a_batch_already_running():
    world, loads = queued_world(3)
    world.engine.email = SlowEmail(
        world, hook_on_call=1, hook=lambda: world.engine.set_global_outbound(False, by="ops")
    )
    world.outbox.dispatch()
    assert world.engine.email.calls == 1
    statuses = sorted(m.status.value for m in world.messages())
    assert statuses == ["PENDING", "PENDING", "SENT"]


def test_document_arriving_mid_batch_cancels_that_loads_request():
    world, loads = queued_world(3)
    last = loads[-1]

    def carrier_uploads():
        token = make_upload_token(b"test-secret", last.id, world.clock.now_ms() + DAY)
        world.engine.handle_upload(token, "pod.jpg", pod(last))

    world.engine.email = SlowEmail(world, hook_on_call=1, hook=carrier_uploads)
    world.outbox.dispatch()
    assert world.chase(last).state is ChaseState.CLOSED
    (request,) = world.messages(last)
    assert request.status is OutboxStatus.CANCELLED
    assert world.engine.email.calls == 2


def test_quiet_hours_starting_mid_batch_defer_the_rest():
    world, loads = queued_world(3, start=at(2026, 11, 10, 19, 59))
    world.engine.email = SlowEmail(world)  # 19:59 -> 20:00 during the first call
    world.outbox.dispatch()
    assert world.engine.email.calls == 1
    pending = [m for m in world.messages() if m.status is OutboxStatus.PENDING]
    assert len(pending) == 2
    assert {m.next_attempt_at_ms for m in pending} == {at(2026, 11, 11, 7)}
