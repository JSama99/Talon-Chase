"""Reading happens outside the engine: a file is kept and queued in one transaction,
read afterwards by the reading desk, and only then checked.

These tests use a reader that is not inline, as any real reader is, and drive the
desk by hand the way the service's tick does.
"""

from __future__ import annotations

import json

import pytest

from chase.domain.checks import Extraction
from chase.domain.models import (
    HOUR_MS,
    MINUTE_MS,
    Channel,
    ChaseState,
    Document,
    EscalationReason,
    Inbound,
    InboundStatus,
    Reading,
    ReadingStatus,
    Step,
)
from chase.domain.tokens import make_upload_token
from chase.ontology.constraints import check_store
from chase.ports import ReaderError
from chase.reading import DEFAULT_BACKOFF_MS, ReadingDesk
from tests.conftest import World, pod


class ScriptedReader:
    """A reader that talks to nothing but behaves like one that does: the engine
    must not call it, and it can be made to fail."""

    name = "scripted/v1"
    inline = False

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.failures: list[Exception] = []
        self.before_answer = None

    def read(self, filename: str, content: bytes) -> Extraction:
        self.calls.append(filename)
        if self.failures:
            raise self.failures.pop(0)
        if self.before_answer:
            self.before_answer()
        return Extraction.from_dict(json.loads(content))


@pytest.fixture
def reader() -> ScriptedReader:
    return ScriptedReader()


@pytest.fixture
def world(reader: ScriptedReader) -> World:
    return World(reader=reader)


def kinds(world: World, load) -> list[str]:
    return [e.kind for e in world.store.evidence(load.id)]


def arrive(world: World, load, **overrides) -> Inbound:
    """A POD arrives and the engine takes it in. Nothing has read it yet."""
    return world.reply_unread(load, [("pod.pdf", pod(load, **overrides))])


def reply_unread(self: World, load, attachments, body: str = "Attached."):
    return self.inbound(
        settle=False,
        broker_id="brk-1",
        channel=Channel.EMAIL,
        from_addr=self.dispatcher.email,
        subject=f"Re: proof of delivery for load {load.load_number}",
        body=body,
        attachments=attachments,
    )


World.reply_unread = reply_unread


# ------------------------------------------------------------------ the wait


def test_the_engine_keeps_a_file_and_queues_it_without_calling_the_reader(world, reader):
    load = world.make_load()
    world.tick()
    inbound = arrive(world, load)

    assert reader.calls == []  # rule 10: the engine never calls a provider
    assert inbound.status is InboundStatus.PROCESSED
    chase = world.chase(load)
    assert chase.state is ChaseState.RECEIVED and chase.next_step is Step.READ
    assert chase.reading_inbound_id == inbound.id
    (document,) = world.store.find(Document, load_id=load.id)
    assert document.read_at_ms is None and document.reader == ""
    (reading,) = world.store.find(Reading, document_id=document.id)
    assert reading.status is ReadingStatus.PENDING and reading.tries == 0
    assert kinds(world, load)[-2:] == ["chase.state", "document.arrived"]
    assert check_store(world.store) == []  # a chase may rest in RECEIVED now


def test_nothing_is_sent_to_the_carrier_while_a_file_waits_to_be_read(world, reader):
    """The second request was due. With a POD in hand and unread, it must not go."""
    load = world.make_load()
    world.tick()
    arrive(world, load)
    sent = len(world.email.sent)
    for _ in range(80):  # three days of ticks with the desk switched off
        world.clock.advance(HOUR_MS)
        world.engine.tick()
        world.outbox.dispatch()
    assert len(world.email.sent) == sent and world.sms.sent == []
    assert world.chase(load).state is ChaseState.RECEIVED
    assert world.escalations(load) == []


def test_a_read_file_is_checked_filed_and_recorded(world, reader):
    load = world.make_load()
    world.tick()
    arrive(world, load)
    report = world.desk.work()
    world.settle()

    assert report.to_dict() == {"read": 1, "retried": 0, "failed": 0, "duplicates": 0}
    assert reader.calls == ["pod.pdf"]
    assert world.chase(load).state is ChaseState.CLOSED and len(world.tms.filed) == 1
    (document,) = world.store.find(Document, load_id=load.id)
    assert document.reader == "scripted/v1" and document.read_at_ms == world.clock.now_ms()
    assert document.extraction["confidence"] == 97
    read = [e for e in world.store.evidence(load.id) if e.kind == "document.read"]
    assert [e.payload for e in read] == [
        {
            "document_id": document.id,
            "sha256": document.content_sha256,
            "reader": "scripted/v1",
            "doc_type": "POD",
            "readable": True,
            "confidence": 97,
        }
    ]
    order = kinds(world, load)
    assert order.index("document.arrived") < order.index("document.read")
    assert order.index("document.read") < order.index("check.completed")


def test_the_desk_and_the_inline_reader_leave_the_same_trail():
    """One path, two timings: only the reader's name differs in the evidence."""

    def run(reader) -> list[tuple[str, dict]]:
        world = World(reader=reader)
        load = world.make_load()
        world.tick()
        world.send_pod(load)
        return [
            (e.kind, {k: v for k, v in e.payload.items() if k != "reader"})
            for e in world.store.evidence(load.id)
        ]

    assert run(ScriptedReader()) == run(None)


def test_every_file_of_a_message_is_read_before_any_is_checked(world, reader):
    load = world.make_load()
    world.tick()
    world.reply_unread(
        load, [("page-a.jpg", pod(load, signature_present=False)), ("page-b.jpg", pod(load))]
    )
    desk = ReadingDesk(world.engine, batch=1)
    desk.work()
    assert world.chase(load).state is ChaseState.RECEIVED  # one read, one to go
    assert "check.completed" not in kinds(world, load)
    desk.work()
    world.settle()
    assert reader.calls == ["page-a.jpg", "page-b.jpg"]
    assert world.chase(load).state is ChaseState.CLOSED  # the signed copy passed
    checks = [e.payload["result"] for e in world.store.evidence(load.id) if e.kind[:5] == "check"]
    assert checks == ["FAIL", "PASS"]


def test_a_failing_document_is_asked_for_again_only_after_it_was_read(world, reader):
    load = world.make_load()
    world.tick()
    arrive(world, load, signature_present=False)
    assert "re-ask" not in world.all_outbound_text() and len(world.email.sent) == 1
    world.settle()
    assert world.chase(load).state is ChaseState.ASKED and world.chase(load).reasks_sent == 1
    assert len(world.email.sent) == 2


def test_an_upload_waits_for_the_desk_too(world, reader):
    load = world.make_load()
    world.tick()
    token = make_upload_token(world.engine.secret, load.id, world.clock.now_ms() + HOUR_MS)
    world.engine.handle_upload(token, "photo.jpg", pod(load))
    assert world.chase(load).state is ChaseState.RECEIVED and reader.calls == []
    world.settle()
    assert world.chase(load).state is ChaseState.CLOSED


# ----------------------------------------------------------------- failures


def test_a_reader_outage_is_retried_and_the_carrier_hears_nothing(world, reader):
    load = world.make_load()
    world.tick()
    arrive(world, load)
    reader.failures = [ReaderError("Anthropic 529/overloaded_error"), TimeoutError("slow")]
    sent = len(world.email.sent)

    assert world.desk.work().retried == 1
    (reading,) = world.store.find(Reading)
    assert reading.status is ReadingStatus.PENDING and reading.tries == 1
    assert reading.next_attempt_at_ms == world.clock.now_ms() + DEFAULT_BACKOFF_MS[0]
    assert world.desk.work().changed == 0  # not due yet

    world.clock.advance(DEFAULT_BACKOFF_MS[0])
    assert world.desk.work().retried == 1  # any other exception is temporary too
    world.clock.advance(DEFAULT_BACKOFF_MS[1])
    world.settle()

    assert len(reader.calls) == 3
    assert world.chase(load).state is ChaseState.CLOSED
    assert len(world.email.sent) == sent  # nothing went to the carrier meanwhile
    retries = [e.payload for e in world.store.evidence(load.id) if e.kind == "reading.retry"]
    assert [(r["try"], r["error"]) for r in retries] == [
        (1, "Anthropic 529/overloaded_error"),
        (2, "TimeoutError: slow"),
    ]


def test_when_the_retries_run_out_a_person_gets_the_document_unread(world, reader):
    load = world.make_load()
    world.tick()
    arrive(world, load)
    reader.failures = [ReaderError("down")] * 10
    sent = len(world.email.sent)
    for wait in (0, *DEFAULT_BACKOFF_MS):
        world.clock.advance(wait)
        world.settle()

    assert len(reader.calls) == 1 + len(DEFAULT_BACKOFF_MS)
    (reading,) = world.store.find(Reading)
    assert reading.status is ReadingStatus.FAILED and reading.last_error == "down"
    (document,) = world.store.find(Document, load_id=load.id)
    assert document.reader == "" and document.read_at_ms is not None
    assert document.extraction["confidence"] == 0
    assert document.extraction["doubts"] == ["the reader was unavailable"]
    assert [e.reason for e in world.escalations(load)] == [EscalationReason.LOW_CONFIDENCE]
    assert world.chase(load).state is ChaseState.ESCALATED and world.tms.filed == {}
    # Nobody looked at the file, so nobody is told their copy is bad.
    assert len(world.email.sent) == sent
    failed = [e.payload for e in world.store.evidence(load.id) if e.kind == "reading.failed"]
    assert failed == [
        {
            "reading_id": reading.id,
            "document_id": document.id,
            "tries": 5,
            "permanent": False,
            "error": "down",
        }
    ]
    assert "document.read" not in kinds(world, load)


def test_a_refusal_for_good_goes_to_a_person_at_once(world, reader):
    load = world.make_load()
    world.tick()
    arrive(world, load)
    reader.failures = [ReaderError("Anthropic 400/invalid_request_error", transient=False)]
    assert world.desk.work().failed == 1
    assert len(reader.calls) == 1
    (document,) = world.store.find(Document, load_id=load.id)
    assert document.extraction["doubts"] == ["the reader could not take this file"]
    assert world.chase(load).state is ChaseState.ESCALATED


def test_a_long_error_is_cut_to_one_short_line_in_the_evidence(world, reader):
    load = world.make_load()
    world.tick()
    arrive(world, load)
    reader.failures = [ReaderError("x" * 5000 + "\nsecond line")]
    world.desk.work()
    (retry,) = [e for e in world.store.evidence(load.id) if e.kind == "reading.retry"]
    assert len(retry.payload["error"]) == 300 and "\n" not in retry.payload["error"]


def test_a_desk_that_died_mid_read_is_picked_up_after_its_lease(world, reader):
    load = world.make_load()
    world.tick()
    arrive(world, load)
    (held,) = world.desk.claim()  # claimed, and then the process dies
    assert held.filename == "pod.pdf" and held.content == pod(load)
    assert world.desk.work().changed == 0  # still leased
    world.clock.advance(world.desk.lease_ms)
    world.settle()
    (reading,) = world.store.find(Reading)
    assert reading.status is ReadingStatus.DONE and reading.tries == 2
    assert world.chase(load).state is ChaseState.CLOSED


def test_a_file_that_kills_the_desk_every_time_ends_with_a_person(world, reader):
    load = world.make_load()
    world.tick()
    arrive(world, load)
    for _ in range(1 + len(DEFAULT_BACKOFF_MS)):
        assert len(world.desk.claim()) == 1
        world.clock.advance(world.desk.lease_ms)
    assert world.desk.work().failed == 1
    assert reader.calls == []
    (failed,) = [e.payload for e in world.store.evidence(load.id) if e.kind == "reading.failed"]
    assert failed["error"] == "the reader never answered" and failed["tries"] == 5
    assert world.chase(load).state is ChaseState.ESCALATED


def test_two_desks_reading_one_file_record_one_reading(world, reader):
    """A lease runs out while the first desk is still waiting for the model. Both
    answers arrive; the first to be recorded stands and the second is dropped."""
    load = world.make_load()
    world.tick()
    arrive(world, load)
    slow = ReadingDesk(world.engine)
    (held,) = slow.claim()
    world.clock.advance(slow.lease_ms)
    assert world.desk.work().read == 1  # the second desk reads it

    from chase.reading import ReadReport, _Outcome

    report = ReadReport()
    with world.store.transaction():
        slow.complete(held.reading, _Outcome(extraction=Extraction(confidence=1)), report)
    assert report.duplicates == 1 and report.read == 0
    assert kinds(world, load).count("document.read") == 1
    (document,) = world.store.find(Document, load_id=load.id)
    assert document.extraction["confidence"] == 97


def test_a_late_failure_does_not_undo_another_desks_retry(world, reader):
    load = world.make_load()
    world.tick()
    arrive(world, load)
    slow = ReadingDesk(world.engine)
    (held,) = slow.claim()
    world.clock.advance(slow.lease_ms)
    (again,) = world.desk.claim()  # reclaimed: a new token

    from chase.reading import _Outcome

    with world.store.transaction():
        slow.complete(held.reading, _Outcome(error="late"))
    (reading,) = world.store.find(Reading)
    assert (
        reading.status is ReadingStatus.READING and reading.claim_token == again.reading.claim_token
    )
    assert "reading.retry" not in kinds(world, load)


# ---------------------------------------------------------- while it is read


def test_a_load_a_person_took_over_is_not_touched_by_a_reading_that_finishes(world, reader):
    """Money talk arrives while the POD is being read: the load goes to a person.
    The reading still finishes and is kept, and changes nothing."""
    load = world.make_load()
    world.tick()
    arrive(world, load)
    world.reply_unread(load, [], body="Please update our remit-to bank details")
    assert world.chase(load).state is ChaseState.ESCALATED
    assert world.chase(load).reading_inbound_id == ""

    world.settle()
    (document,) = world.store.find(Document, load_id=load.id)
    assert document.read_at_ms is not None and document.extraction["confidence"] == 97
    assert world.chase(load).state is ChaseState.ESCALATED and world.tms.filed == {}
    assert "check.completed" not in kinds(world, load)
    assert [e.reason for e in world.escalations(load)] == [EscalationReason.RISK]


def test_a_second_reply_while_the_first_is_being_read_is_kept_and_read(world, reader):
    load = world.make_load()
    world.tick()
    first = arrive(world, load)
    second = world.reply_unread(load, [("again.pdf", pod(load, signature_present=False))])
    assert second.classification == "LATE"
    assert world.chase(load).reading_inbound_id == first.id
    world.settle()
    assert reader.calls == ["pod.pdf", "again.pdf"]
    assert all(d.read_at_ms is not None for d in world.store.find(Document, load_id=load.id))
    # Only the first message's file was judged; it passed and was filed.
    assert kinds(world, load).count("check.completed") == 1
    assert world.chase(load).state is ChaseState.CLOSED


def test_a_document_for_another_load_is_moved_with_its_reading(world, reader):
    """Read once: the copy on the right load carries the same reading and reader."""
    load_a = world.make_load()
    load_b = world.make_load()
    world.tick()
    world.reply_unread(load_a, [("pod.pdf", pod(load_b))])
    world.settle()
    assert reader.calls == ["pod.pdf"]
    assert world.chase(load_b).state is ChaseState.CLOSED
    (copy,) = world.store.find(Document, load_id=load_b.id)
    assert copy.reader == "scripted/v1" and copy.read_at_ms is not None
    assert world.store.find(Reading, document_id=copy.id) == []
    on_b = kinds(world, load_b)
    assert (
        on_b.index("document.arrived") < on_b.index("document.read") < on_b.index("check.completed")
    )


def test_reading_goes_on_while_messages_are_switched_off(world, reader):
    """The kill switch stops what goes out. A file that came in is still read; the
    filing it leads to is what waits."""
    load = world.make_load()
    world.tick()
    arrive(world, load)
    world.engine.set_broker_outbound("brk-1", False, by=world.owner.id)
    world.settle()
    assert reader.calls == ["pod.pdf"]
    assert world.chase(load).state is ChaseState.PASSED and world.tms.filed == {}
    world.engine.set_broker_outbound("brk-1", True, by=world.owner.id)
    world.settle()
    assert world.chase(load).state is ChaseState.CLOSED


def test_the_reader_is_called_with_no_transaction_open(world, reader):
    """The slow call must not hold the database. The desk claims, lets go, reads,
    and takes the transaction again to record the answer."""
    load = world.make_load()
    world.tick()
    arrive(world, load)
    depth = {"now": 0, "during_read": None}

    import contextlib

    @contextlib.contextmanager
    def guard():
        depth["now"] += 1
        try:
            with world.store.transaction():
                yield
        finally:
            depth["now"] -= 1

    reader.before_answer = lambda: depth.update(during_read=depth["now"])
    ReadingDesk(world.engine, guard=guard).work()
    assert depth["during_read"] == 0


def test_time_passing_alone_never_settles_a_waiting_chase(world, reader):
    """Only the desk ends the wait. If it never runs, the chase stays visibly
    RECEIVED rather than being escalated or closed by the schedule."""
    load = world.make_load()
    world.tick()
    arrive(world, load)
    world.clock.advance(30 * 24 * HOUR_MS)
    assert world.engine.tick() == 0
    assert world.chase(load).state is ChaseState.RECEIVED
    world.clock.advance(MINUTE_MS)
    world.settle()
    assert world.chase(load).state is ChaseState.CLOSED
