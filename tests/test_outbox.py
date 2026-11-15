"""The outbox: what it guarantees, and where its guarantee ends."""

from __future__ import annotations

import os
import threading

import pytest
import sqlalchemy as sa

from chase.channels.fakes import FakeEmail, FakeSms
from chase.domain.clock import UuidIds
from chase.domain.models import (
    HOUR_MS,
    MINUTE_MS,
    Attempt,
    Channel,
    ChaseState,
    EscalationReason,
    Load,
    OutboxMessage,
    OutboxPurpose,
    OutboxStatus,
)
from chase.domain.tokens import make_upload_token
from chase.engine import ChaseEngine
from chase.outbox import OutboxDispatcher, _Outcome
from chase.reader.rules import RuleBasedClassifier, StandInReader
from chase.store.sql import METADATA, SqlStore
from tests.conftest import World, at, check_worlds, pod

DAY = 24 * HOUR_MS
PG_URL = os.environ.get("CHASE_TEST_POSTGRES_URL")


def kinds(world: World, load: Load) -> list[str]:
    return [e.kind for e in world.store.evidence(load.id)]


# --------------------------------------------------------------- the core promise


def test_engine_never_calls_a_provider(world: World):
    load = world.make_load()
    world.engine.tick()
    world.engine.handle_inbound(
        broker_id="brk-1",
        channel=Channel.EMAIL,
        from_addr=world.dispatcher.email,
        subject=f"Re: {load.load_number}",
        body="Here",
        attachments=[("pod.pdf", pod(load, signature_present=False))],
    )
    assert world.email.calls == 0 and world.sms.calls == 0
    statuses = [m.status for m in world.messages(load)]
    assert statuses == [OutboxStatus.PENDING, OutboxStatus.PENDING]  # first ask, then re-ask
    world.settle()
    assert world.email.calls == 2


def test_a_rolled_back_decision_leaves_no_message():
    store = SqlStore("sqlite+pysqlite:///:memory:")
    store.create_schema()
    world = World(store=store)
    load = world.make_load()
    with pytest.raises(RuntimeError):
        with store.transaction():
            world.engine.tick()
            assert len(store.find(OutboxMessage)) == 1  # visible inside the transaction
            raise RuntimeError("database commit fails")
    assert store.find(OutboxMessage) == [] and store.find(Attempt) == []
    assert world.chase(load).state is ChaseState.OPEN
    assert world.outbox.dispatch().changed == 0 and world.email.calls == 0
    # Next time round the decision is made again, once.
    world.tick()
    assert len(world.email.sent) == 1 and len(store.find(OutboxMessage)) == 1


def test_attempt_and_message_are_linked(world: World):
    load = world.make_load()
    world.tick()
    (attempt,) = world.store.find(Attempt, chase_id=world.chase(load).id)
    (message,) = world.messages(load)
    assert attempt.outbox_id == message.id
    assert message.idempotency_key == f"attempt:{attempt.id}"
    assert message.purpose is OutboxPurpose.CARRIER_REQUEST
    assert message.quiet_tz == world.carrier.timezone
    sent = world.email.sent[0]
    assert sent.idempotency_key == message.idempotency_key
    assert sent.message_id.startswith("<") and sent.message_id.endswith("@talon-chase>")
    queued = [e for e in world.store.evidence(load.id) if e.kind == "attempt.queued"][0]
    assert queued.payload["outbox_id"] == message.id


# --------------------------------------------------------- crashes and lost leases


@pytest.mark.parametrize("honors", [True, False])
def test_crash_after_send_before_commit(honors):
    world = World(honors_idempotency=honors)
    load = world.make_load()
    world.engine.tick()
    (claimed,) = world.outbox.claim()
    world.outbox._send(claimed)  # the provider accepts it; then the process dies

    world.clock.advance(4 * MINUTE_MS)  # lease still held: nobody touches it
    assert world.outbox.dispatch().changed == 0

    world.clock.advance(2 * MINUTE_MS)  # lease expired
    world.outbox.dispatch()
    (message,) = world.messages(load)
    assert message.status is OutboxStatus.SENT and message.tries == 2
    assert "message.reclaimed" in kinds(world, load)
    # The guarantee is at least once. A provider that honours the key drops the repeat.
    assert len(world.email.sent) == (1 if honors else 2)
    assert world.email.sent[0].idempotency_key == world.email.sent[-1].idempotency_key


def test_late_completion_after_reclaim_is_recorded_as_a_duplicate():
    world = World(honors_idempotency=False)
    load = world.make_load()
    world.engine.tick()
    (slow,) = world.outbox.claim()
    slow_result = world.outbox._send(slow)
    world.clock.advance(6 * MINUTE_MS)
    second = OutboxDispatcher(world.engine)
    assert second.dispatch().sent == 1
    with world.store.transaction():
        world.outbox.complete(slow, slow_result)
    (message,) = world.messages(load)
    assert message.status is OutboxStatus.SENT
    assert kinds(world, load).count("message.duplicate") == 1
    assert world.engine.export_evidence(load.id)["verification"]["ok"]


def test_a_stale_worker_cannot_overwrite_a_newer_claim(world: World):
    load = world.make_load()
    world.engine.tick()
    (stale,) = world.outbox.claim()
    world.clock.advance(6 * MINUTE_MS)
    (fresh,) = OutboxDispatcher(world.engine).claim()
    assert fresh.claim_token != stale.claim_token
    world.outbox.complete(stale, _Outcome(False, error="timeout"))
    (message,) = world.messages(load)
    assert message.status is OutboxStatus.SENDING and message.claim_token == fresh.claim_token
    assert "message.retry" not in kinds(world, load)


def test_two_dispatchers_never_claim_the_same_message(world: World):
    for _ in range(5):
        world.make_load()
    world.engine.tick()
    first = OutboxDispatcher(world.engine, batch=3).claim()
    second = OutboxDispatcher(world.engine, batch=3).claim()
    assert len(first) == 3 and len(second) == 2
    assert not {m.id for m in first} & {m.id for m in second}


# ----------------------------------------------------------------- failures


def test_permanent_rejection_fails_at_once_and_goes_to_a_person(world: World):
    load = world.make_load()
    world.email.reject_next = 1
    world.tick()
    (message,) = world.messages(load)
    assert message.status is OutboxStatus.FAILED and message.tries == 1
    assert [e.reason for e in world.escalations(load)] == [EscalationReason.SEND_FAILED]
    assert world.chase(load).state is ChaseState.ESCALATED
    world.run(3 * DAY, step_ms=HOUR_MS)
    assert world.email.sent == [] and world.email.calls == 1


def test_temporary_failures_back_off_then_give_up(world: World):
    load = world.make_load()
    world.email.fail_next = 99
    world.tick()
    world.run(6 * HOUR_MS, step_ms=MINUTE_MS)
    message = [m for m in world.messages(load) if m.channel is Channel.EMAIL][0]
    backoff = world.broker.config.send_retry_backoff_ms
    assert message.status is OutboxStatus.FAILED and message.tries == len(backoff) + 1
    retries = [
        e
        for e in world.store.evidence(load.id)
        if e.kind == "message.retry" and e.payload["outbox_id"] == message.id
    ]
    gaps = [e.payload["next_attempt_at_ms"] - e.at_ms for e in retries]
    assert gaps == backoff
    assert [e.reason for e in world.escalations(load)] == [EscalationReason.SEND_FAILED]


def test_a_failed_text_does_not_stop_the_chase_when_email_is_working(world: World):
    load = world.make_load()
    world.tick()
    world.sms.reject_next = 1
    world.run(4 * HOUR_MS)
    texts = [m for m in world.messages(load) if m.channel.value == "SMS"]
    assert [m.status for m in texts] == [OutboxStatus.FAILED]
    # A delivery failure on any request hands the load to a person.
    assert [e.reason for e in world.escalations(load)] == [EscalationReason.SEND_FAILED]


# ------------------------------------------------------- what the dispatcher checks


def test_request_is_cancelled_when_the_document_arrived_first(world: World):
    load = world.make_load()
    world.engine.tick()  # the first ask is queued, not yet delivered
    token = make_upload_token(b"test-secret", load.id, world.clock.now_ms() + DAY)
    world.engine.handle_upload(token, "pod.jpg", pod(load))
    assert world.chase(load).state is ChaseState.CLOSED
    world.settle()
    (message,) = world.messages(load)
    assert message.status is OutboxStatus.CANCELLED and world.email.sent == []
    cancelled = [e for e in world.store.evidence(load.id) if e.kind == "message.cancelled"]
    assert cancelled[0].payload["reason"] == "chase is CLOSED"


def test_message_that_misses_the_evening_waits_for_morning():
    world = World(start=at(2026, 11, 10, 19, 55))
    load = world.make_load()
    world.engine.tick()  # queued at 19:55 carrier time
    world.clock.advance(15 * MINUTE_MS)  # the dispatcher was down until 20:10
    world.settle()
    assert world.email.sent == []
    (message,) = world.messages(load)
    assert message.next_attempt_at_ms == at(2026, 11, 11, 7)
    world.run(12 * HOUR_MS, step_ms=30 * MINUTE_MS)
    message = world.messages(load)[0]
    # Delivered on the first dispatcher run at or after 07:00 carrier time.
    assert message.status is OutboxStatus.SENT
    assert at(2026, 11, 11, 7) <= message.sent_at_ms < at(2026, 11, 11, 7, 30)
    assert kinds(world, load).count("message.deferred") == 1


def test_packet_to_the_broker_ignores_carrier_quiet_hours():
    world = World(start=at(2026, 11, 10, 19, 30))
    world.broker.adapter = "packet_email"
    world.store.put(world.broker)
    load = world.make_load()
    world.tick()
    world.clock.advance(45 * MINUTE_MS)  # 20:15, carrier quiet hours
    world.send_pod(load)
    assert world.chase(load).state is ChaseState.CLOSED
    assert world.email.sent[-1].to == [world.broker.rep_email]


def test_kill_switch_holds_queued_messages_until_turned_back_on(world: World):
    load = world.make_load()
    world.engine.tick()
    world.engine.set_global_outbound(False, by="op-platform")
    report = world.outbox.dispatch()
    assert report.held == 1 and report.sent == 0 and world.email.sent == []
    world.clock.advance(HOUR_MS)
    world.outbox.dispatch()
    assert world.messages(load)[0].status is OutboxStatus.PENDING
    world.engine.set_global_outbound(True, by="op-platform")
    assert world.outbox.dispatch().sent == 1


# ------------------------------------------------------------- packet filing


def test_load_is_ready_to_invoice_only_after_the_packet_is_delivered(world: World):
    world.broker.adapter = "packet_email"
    world.store.put(world.broker)
    load = world.make_load()
    world.tick()
    world.email.fail_next = 1
    world.send_pod(load)

    chase = world.chase(load)
    assert chase.state is ChaseState.PASSED
    assert world.store.get(Load, load.id).ready_to_invoice_at_ms is None
    packet = [m for m in world.messages(load) if m.purpose is OutboxPurpose.PACKET]
    assert [m.status for m in packet] == [OutboxStatus.PENDING]
    assert "writeback.pending" in kinds(world, load)

    world.run(2 * MINUTE_MS, step_ms=MINUTE_MS)
    packet = [m for m in world.messages(load) if m.purpose is OutboxPurpose.PACKET][0]
    assert packet.status is OutboxStatus.SENT
    assert world.chase(load).state is ChaseState.CLOSED
    assert world.store.get(Load, load.id).ready_to_invoice_at_ms == packet.sent_at_ms
    order = kinds(world, load)
    assert order.index("message.sent", order.index("writeback.pending")) < order.index(
        "writeback.succeeded"
    )


def test_packet_is_queued_once_however_often_the_engine_checks(world: World):
    world.broker.adapter = "packet_email"
    world.store.put(world.broker)
    load = world.make_load()
    world.tick()
    world.engine.set_global_outbound(False, by="op-platform")
    world.send_pod(load)
    world.engine.set_global_outbound(True, by="op-platform")
    for _ in range(5):
        world.engine.tick()
        world.clock.advance(10 * MINUTE_MS)
    packets = [m for m in world.messages(load) if m.purpose is OutboxPurpose.PACKET]
    assert len(packets) == 1
    assert kinds(world, load).count("writeback.pending") == 1


# --------------------------------------------------------------------- SQL


def test_sql_refuses_a_second_message_with_the_same_key():
    store = SqlStore("sqlite+pysqlite:///:memory:")
    store.create_schema()
    world = World(store=store)
    load = world.make_load()
    world.engine.tick()
    (message,) = store.find(OutboxMessage)
    clone = OutboxMessage(**{**message.__dict__, "id": "msg-duplicate"})
    with pytest.raises(sa.exc.IntegrityError):
        store.put(clone)
    assert load.id == message.load_id


@pytest.mark.skipif(not PG_URL, reason="CHASE_TEST_POSTGRES_URL is not set")
def test_concurrent_dispatchers_on_postgres_deliver_each_message_once():
    setup = SqlStore(PG_URL)
    METADATA.drop_all(setup.engine)
    setup.create_schema()
    try:
        world = World(store=setup)
        loads = [world.make_load() for _ in range(40)]
        world.engine.tick()
        assert len(setup.find(OutboxMessage)) == 40

        provider = FakeEmail(honors_idempotency=False)  # counts every delivery
        lock = threading.Lock()

        class SafeEmail:
            def send(self, message):
                with lock:
                    return provider.send(message)

        def worker() -> None:
            store = SqlStore(PG_URL)
            engine = ChaseEngine(
                store=store,
                clock=world.clock,
                ids=UuidIds(),
                email=SafeEmail(),
                sms=FakeSms(),
                classifier=RuleBasedClassifier(),
                reader=StandInReader(),
                secret=b"test-secret",
            )
            dispatcher = OutboxDispatcher(engine, batch=3)
            for _ in range(30):
                if not dispatcher.dispatch().sent:
                    break
            store.engine.dispose()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(provider.sent) == 40
        assert sorted(m.idempotency_key for m in provider.sent) == sorted(
            m.idempotency_key for m in setup.find(OutboxMessage)
        )
        assert {m.status for m in setup.find(OutboxMessage)} == {OutboxStatus.SENT}
        for load in loads:
            assert world.engine.export_evidence(load.id)["verification"]["ok"]
        check_worlds()
    finally:
        METADATA.drop_all(setup.engine)
        setup.engine.dispose()
