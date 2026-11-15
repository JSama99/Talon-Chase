"""Walk a few synthetic loads through the engine and print what happened.

    python -m chase.demo [--out out/demo]

Nothing is sent anywhere: the channels and the TMS are in-memory stand-ins.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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
    Load,
    Party,
    PartyRole,
    Role,
)
from chase.domain.schedule import to_local
from chase.engine import ChaseEngine
from chase.outbox import OutboxDispatcher
from chase.reader.rules import RuleBasedClassifier, StandInReader
from chase.store.memory import MemoryStore
from chase.writeback.adapters import FakeApiAdapter

TZ = "America/Chicago"
START = int(datetime(2026, 11, 10, 14, tzinfo=ZoneInfo(TZ)).timestamp()) * 1000


def stand_in_pod(load: Load, **overrides) -> bytes:
    data = {
        "doc_type": "POD",
        "readable": True,
        "reference_numbers": [load.load_number],
        "signature_present": True,
        "delivery_date_ms": load.delivery_appt_ms,
        "page_count": 1,
        "exception_notes": [],
        "confidence": 96,
    }
    data.update(overrides)
    return json.dumps(data).encode()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("out/demo"))
    args = parser.parse_args()

    store, clock = MemoryStore(), FakeClock(START)
    email, sms, tms = FakeEmail(), FakeSms(), FakeApiAdapter()
    engine = ChaseEngine(
        store=store, clock=clock, ids=SequentialIds(), email=email, sms=sms,
        classifier=RuleBasedClassifier(), reader=StandInReader(), adapters=[tms],
        secret=b"demo-secret",
    )  # fmt: skip
    broker = Broker(
        "brk-demo", "Peachtree Logistics", "pods@peachtreelogistics.example",
        "ops@peachtreelogistics.example", "+14045550100", "fake_api",
    )  # fmt: skip
    store.put(broker)
    store.put(Carrier("car-demo", broker.id, "Redline Trucking", "MC482910", TZ))
    dispatch = "dispatch@redlinetrucking.example"
    store.put(Contact("con-d", "car-demo", Role.DISPATCHER, "Dana", dispatch))
    store.put(Contact("con-v", "car-demo", Role.DRIVER, "Sam", phone="+13125550142"))
    engine.record_consent("con-v", True, ConsentSource.RATE_CONFIRMATION)
    customer = Party("party-acme", broker.id, PartyRole.CUSTOMER, "Acme Foods")

    stories = {
        "TC-300001": "carrier sends a clean POD after the first email",
        "TC-300002": "first POD is unsigned; the agent says why and the carrier resends",
        "TC-300003": "POD notes damage; never filed, goes to a person",
        "TC-300004": "carrier asks to change bank details; held, no reply",
        "TC-300005": "carrier never answers; three attempts, then a person",
    }
    loads = {}
    for i, number in enumerate(stories, start=1):
        load = Load(
            id=f"demo-{i}", broker_id=broker.id, carrier_id="car-demo", load_number=number,
            delivery_appt_ms=START - HOUR_MS, delivered_at_ms=START,
            customer_party_id=customer.id, pickup_number="PU-77812", rate_cents=185_000,
        )  # fmt: skip
        loads[number] = load
        engine.intake_load(load, [customer])

    def reply(number: str, body: str, attachment: bytes | None = None) -> None:
        engine.handle_inbound(
            broker_id=broker.id, channel=Channel.EMAIL, from_addr=dispatch,
            subject=f"Re: proof of delivery for load {number}", body=body,
            attachments=[("pod.pdf", attachment)] if attachment else [],
        )  # fmt: skip
        settle()

    outbox = OutboxDispatcher(engine)

    def settle() -> None:
        """What a scheduler does: deliver queued messages, let the engine react."""
        for _ in range(10):
            if not outbox.dispatch().changed and not engine.tick():
                return

    def advance(duration_ms: int) -> None:
        end = clock.now_ms() + duration_ms
        while clock.now_ms() < end:
            clock.advance(15 * MINUTE_MS)
            engine.tick()
            settle()

    engine.tick()
    settle()
    advance(40 * MINUTE_MS)
    reply("TC-300001", "Attached.", stand_in_pod(loads["TC-300001"]))
    reply("TC-300002", "Here.", stand_in_pod(loads["TC-300002"], signature_present=False))
    reply(
        "TC-300003", "POD.", stand_in_pod(loads["TC-300003"], exception_notes=["2 cartons damaged"])
    )
    reply("TC-300004", "Before we send it, please update our bank details and remit-to address.")
    advance(HOUR_MS)
    reply("TC-300002", "Signed copy.", stand_in_pod(loads["TC-300002"]))
    advance(4 * 24 * HOUR_MS)

    args.out.mkdir(parents=True, exist_ok=True)
    for number, story in stories.items():
        load = loads[number]
        chase = store.find(Chase, load_id=load.id)[0]
        print(f"\n{number}: {story}")
        print(
            f"  result: {chase.state.value}"
            + (f" ({chase.closed_reason})" if chase.closed_reason else "")
        )
        for event in store.evidence(load.id):
            when = to_local(event.at_ms, TZ).strftime("%a %H:%M")
            note = ""
            p = event.payload
            if event.kind == "attempt.queued":
                note = f"{p['step']} by {p['channel']}"
            elif event.kind == "message.sent":
                note = f"{p['channel']} to {', '.join(p['to'])}"
            elif event.kind == "chase.state":
                note = f"{p['from']} -> {p['to']}"
            elif event.kind == "check.completed":
                note = f"{p['result']} {', '.join(p['reasons'])}".strip()
            elif event.kind in ("escalation.opened", "inbound.held"):
                note = f"{p['reason']}: {p['detail']}"
            elif event.kind == "writeback.succeeded":
                note = f"{p['adapter']} {p['receipt']}"
            print(f"  {when}  {event.kind:<24}{note}")
        export = engine.export_evidence(load.id)
        path = args.out / f"evidence-{number}.json"
        path.write_text(json.dumps(export, indent=1, ensure_ascii=False))
        print(f"  evidence: {path}  head {export['verification']['head_hash'][:16]}...")

    queue = engine.queue(broker.id)
    print(f"\nQueue for a person: {len(queue['escalations'])} items")
    for item in queue["escalations"]:
        print(f"  {item['load_number']}  {item['reason']:<20}{item['detail']}")
    print(
        f"\nSent {len(email.sent)} emails and {len(sms.sent)} texts (to stand-ins). "
        f"Filed {len(tms.filed)} documents."
    )
    from chase.ontology import ONTOLOGY_VERSION
    from chase.ontology.constraints import RULES, check_store

    violations = check_store(store)
    print(f"Ontology {ONTOLOGY_VERSION}: {len(RULES)} rules checked, {len(violations)} violations.")
    for violation in violations:
        print(f"  {violation}")
    if violations:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
