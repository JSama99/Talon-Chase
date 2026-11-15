"""A small world to run the engine in: one broker, one carrier, fake everything."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from chase.channels.fakes import FakeEmail, FakeSms
from chase.domain.clock import FakeClock, SequentialIds
from chase.domain.models import (
    HOUR_MS,
    MINUTE_MS,
    Broker,
    Carrier,
    Channel,
    Chase,
    ConsentSource,
    Contact,
    Escalation,
    Load,
    Operator,
    OperatorRole,
    Party,
    PartyRole,
    Role,
)
from chase.engine import ChaseEngine
from chase.ontology.constraints import check_store
from chase.outbox import OutboxDispatcher
from chase.reader.rules import RuleBasedClassifier, StandInReader
from chase.store.memory import MemoryStore
from chase.writeback.adapters import FakeApiAdapter

TZ = "America/Chicago"


def at(year, month, day, hour, minute=0, tz=TZ) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz)).timestamp()) * 1000


# Tuesday 10 November 2026, 14:00 in Chicago.
START = at(2026, 11, 10, 14)


def pod(load: Load, **overrides) -> bytes:
    """A stand-in POD: JSON describing what a reader would extract from the image."""
    data = {
        "doc_type": "POD",
        "readable": True,
        "reference_numbers": [load.load_number],
        "signature_present": True,
        "delivery_date_ms": load.delivery_appt_ms,
        "page_count": load.expected_pages,
        "exception_notes": [],
        "confidence": 97,
    }
    data.update(overrides)
    return json.dumps(data).encode()


class World:
    # Every World made during a test is checked against all ontology rules at the end
    # of the test (see the autouse fixture below). Set ``check_rules = False`` on a
    # world that a test breaks on purpose.
    created: list[World] = []

    def __init__(
        self,
        store=None,
        start: int = START,
        carrier_tz: str = TZ,
        honors_idempotency: bool = True,
    ) -> None:
        World.created.append(self)
        self.check_rules = True
        self.store = store or MemoryStore()
        self.clock = FakeClock(start)
        self.ids = SequentialIds()
        self.email = FakeEmail(honors_idempotency)
        self.sms = FakeSms(honors_idempotency)
        self.tms = FakeApiAdapter()
        self.engine = ChaseEngine(
            store=self.store,
            clock=self.clock,
            ids=self.ids,
            email=self.email,
            sms=self.sms,
            classifier=RuleBasedClassifier(),
            reader=StandInReader(),
            adapters=[self.tms],
            secret=b"test-secret",
        )
        self.outbox = OutboxDispatcher(self.engine)
        self.broker = Broker(
            id="brk-1",
            name="Peachtree Logistics",
            chase_mailbox="pods@peachtreelogistics.example",
            rep_email="ops@peachtreelogistics.example",
            sms_number="+14045550100",
            adapter="fake_api",
        )
        self.carrier = Carrier("car-1", "brk-1", "Redline Trucking", "MC482910", carrier_tz)
        self.dispatcher = Contact(
            "con-disp", "car-1", Role.DISPATCHER, "Dana", "dispatch@redlinetrucking.example"
        )
        self.driver = Contact("con-drv", "car-1", Role.DRIVER, "Sam", phone="+1 (312) 555-0142")
        self.owner = Operator("op-jermaine", "brk-1", "Jermaine", OperatorRole.OWNER)
        self.platform = Operator("op-platform", "", "TalonSight on call", OperatorRole.PLATFORM)
        for obj in (
            self.broker, self.carrier, self.dispatcher, self.driver, self.owner, self.platform,
        ):  # fmt: skip
            self.store.put(obj)
        # The driver agreed to texts in the rate confirmation.
        self.engine.record_consent(self.driver.id, True, ConsentSource.RATE_CONFIRMATION)
        self._load_seq = 0

    # -- building blocks -------------------------------------------------------

    def make_load(self, delivered: bool = True, **overrides) -> Load:
        self._load_seq += 1
        n = self._load_seq
        fields = dict(
            id=f"load-{n}",
            broker_id="brk-1",
            carrier_id="car-1",
            load_number=f"TC-10{n:04d}",
            delivery_appt_ms=self.clock.now_ms() - HOUR_MS,
            bol_number=f"BOL{55000 + n}",
            delivered_at_ms=self.clock.now_ms() if delivered else None,
            pickup_number="PU-77812",
            rate_cents=185_000,
        )
        customer = overrides.pop("customer_name", "Acme Foods")
        consignee = overrides.pop("consignee_name", "Riverside Distribution")
        address = overrides.pop("consignee_address", "4100 Fulton Industrial Blvd, Atlanta, GA")
        fields.update(overrides)
        parties = [
            Party(f"{fields['id']}-customer", "brk-1", PartyRole.CUSTOMER, customer),
            Party(f"{fields['id']}-consignee", "brk-1", PartyRole.CONSIGNEE, consignee, address),
        ]
        load = Load(**fields, customer_party_id=parties[0].id, consignee_party_id=parties[1].id)
        self.engine.intake_load(load, parties)
        return load

    def set_sms_consent(self, contact: Contact, granted: bool) -> None:
        """Consent changed in the broker's system."""
        self.engine.record_consent(contact.id, granted, ConsentSource.TMS_SYNC)

    def tick(self) -> int:
        """What the scheduler does every few minutes: engine tick, then deliver."""
        steps = self.engine.tick()
        self.settle()
        return steps

    def settle(self) -> None:
        """Deliver what is queued and let the engine react, at the current time."""
        for _ in range(10):
            report = self.outbox.dispatch()
            steps = self.engine.tick()
            if not report.changed and not steps:
                return

    def chase(self, load: Load) -> Chase:
        return self.store.find(Chase, load_id=load.id)[0]

    def reply(self, load: Load, body: str = "", attachments=None, sender: str | None = None):
        inbound = self.inbound(
            broker_id="brk-1",
            channel=Channel.EMAIL,
            from_addr=sender or self.dispatcher.email,
            subject=f"Re: proof of delivery for load {load.load_number}",
            body=body,
            attachments=attachments or [],
        )
        return inbound

    def inbound(self, **kwargs):
        """A carrier message arrives, then whatever it triggers is delivered."""
        inbound = self.engine.handle_inbound(**kwargs)
        self.settle()
        return self.store.get(type(inbound), inbound.id)

    def send_pod(self, load: Load, **overrides):
        return self.reply(load, "Attached.", [("pod.pdf", pod(load, **overrides))])

    def run(self, duration_ms: int, step_ms: int = 15 * MINUTE_MS) -> None:
        """Advance the clock in steps, ticking the engine each time."""
        end = self.clock.now_ms() + duration_ms
        while self.clock.now_ms() < end:
            self.clock.advance(min(step_ms, end - self.clock.now_ms()))
            self.tick()

    def escalations(self, load: Load) -> list[Escalation]:
        return sorted(self.store.find(Escalation, load_id=load.id), key=lambda e: e.id)

    def messages(self, load: Load | None = None):
        from chase.domain.models import OutboxMessage

        found = (
            self.store.find(OutboxMessage, load_id=load.id)
            if load
            else self.store.find(OutboxMessage)
        )
        return sorted(found, key=lambda m: (m.created_at_ms, m.id))

    def carrier_emails(self):
        """Emails sent to anyone other than the broker's own rep alone."""
        return [m for m in self.email.sent if m.to != [self.broker.rep_email]]

    def all_outbound_text(self) -> str:
        parts = [f"{m.subject}\n{m.body}" for m in self.email.sent]
        parts += [m.body for m in self.sms.sent]
        return "\n".join(parts)


@pytest.fixture
def world() -> World:
    return World()


def check_worlds() -> None:
    """Check every World made so far against every ontology rule, then forget them.

    Fixtures that tear down a database call this first, while the data still exists.
    """
    made, World.created[:] = list(World.created), []
    for world in made:
        if world.check_rules:
            violations = check_store(world.store)
            assert violations == [], "\n".join(str(v) for v in violations)


@pytest.fixture(autouse=True)
def every_world_keeps_the_rules():
    """After each test, every World it made must satisfy every ontology rule."""
    World.created.clear()
    yield
    check_worlds()
