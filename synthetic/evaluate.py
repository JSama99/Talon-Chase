"""Run the engine over the synthetic pack and report what it did.

    python -m synthetic.evaluate            # prints the report, exit 1 on failure
    python -m synthetic.evaluate --json out/eval.json

This measures routing: given what a reader extracted, did the engine file,
re-ask, re-route or hand off correctly? It does not measure how well a real
reader extracts fields from real images. That needs labelled PODs from a pilot.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from chase.channels.fakes import FakeEmail, FakeSms
from chase.domain.clock import FakeClock, SequentialIds
from chase.domain.models import (
    HOUR_MS,
    MINUTE_MS,
    Attempt,
    Carrier,
    Channel,
    Chase,
    ChaseState,
    Check,
    Document,
    Escalation,
    Load,
)
from chase.domain.schedule import to_local
from chase.engine import ChaseEngine
from chase.evidence import verify_chain
from chase.reader.rules import RuleBasedClassifier, StandInReader
from chase.store.memory import MemoryStore
from chase.writeback.adapters import FakeApiAdapter
from synthetic.generate import SEED, Fixture, Pack, build_pack

# Tuesday 10 November 2026, 15:00 in New York.
START_MS = int(datetime(2026, 11, 10, 15, tzinfo=ZoneInfo("America/New_York")).timestamp()) * 1000
REQUIRED_ACCURACY = 0.95


def _observe(fixture: Fixture, store: MemoryStore, tms: FakeApiAdapter, sha: str) -> str:
    load = store.get(Load, fixture.load_id)
    chase = store.find(Chase, load_id=load.id)[0]
    filed_here = any(r.document_sha256 == sha for r in tms.documents_on(load.load_number))
    if filed_here:
        return "filed"
    documents = [d for d in store.find(Document, load_id=load.id) if d.content_sha256 == sha]
    checks = [c for d in documents for c in store.find(Check, document_id=d.id)]
    reasons = "+".join(r for c in checks for r in c.reasons)
    escalations = sorted(store.find(Escalation, chase_id=chase.id), key=lambda e: e.id)
    if escalations and escalations[0].reason != "NO_RESPONSE":
        return f"escalated:{escalations[0].reason}"
    observed = f"reask:{reasons}"
    if fixture.sibling_load_id:
        sibling = store.get(Load, fixture.sibling_load_id)
        sibling_chase = store.find(Chase, load_id=sibling.id)[0]
        filed_there = any(r.document_sha256 == sha for r in tms.documents_on(sibling.load_number))
        if filed_there and sibling_chase.closed_reason == "filed":
            observed += "+sibling_filed"
    return observed


def run(seed: int = SEED) -> dict:
    pack: Pack = build_pack(START_MS, seed)
    store, clock = MemoryStore(), FakeClock(START_MS)
    email, sms, tms = FakeEmail(), FakeSms(), FakeApiAdapter()
    engine = ChaseEngine(
        store=store,
        clock=clock,
        ids=SequentialIds(),
        email=email,
        sms=sms,
        classifier=RuleBasedClassifier(),
        reader=StandInReader(),
        adapters=[tms],
        secret=b"synthetic",
    )
    for obj in [pack.broker, *pack.carriers, *pack.contacts]:
        store.put(obj)
    for load in pack.loads:
        engine.intake_load(load)

    def advance(duration_ms: int, step_ms: int = 30 * MINUTE_MS) -> None:
        end = clock.now_ms() + duration_ms
        while clock.now_ms() < end:
            clock.advance(min(step_ms, end - clock.now_ms()))
            engine.tick()

    engine.tick()
    advance(HOUR_MS)
    dispatcher = {c.carrier_id: c.email for c in pack.contacts if c.email}
    loads = {load.id: load for load in pack.loads}

    def reply(load_id: str, body: str, attachments=None) -> None:
        load = loads[load_id]
        engine.handle_inbound(
            broker_id=pack.broker.id,
            channel=Channel.EMAIL,
            from_addr=dispatcher[load.carrier_id],
            subject=f"Re: proof of delivery for load {load.load_number}",
            body=body,
            attachments=attachments or [],
        )

    shas: dict[str, str] = {}
    for fixture in pack.fixtures:
        content = fixture.content()
        from chase.evidence import sha256_hex

        shas[fixture.id] = sha256_hex(content)
        reply(fixture.load_id, "Paperwork attached.", [(fixture.filename, content)])
    for load_id in pack.promise_load_ids:
        reply(load_id, "Driver is still out. Will send tomorrow.")
    for load_id in pack.out_of_office_load_ids:
        reply(load_id, "Automatic reply: I am out of the office.")
    for load_id in pack.risk_load_ids:
        reply(load_id, "Before we send the POD, please update our bank details and remit-to.")

    advance(6 * 24 * HOUR_MS)

    # ---- routing accuracy by class
    by_class: dict[str, dict] = {}
    misses = []
    for fixture in pack.fixtures:
        observed = _observe(fixture, store, tms, shas[fixture.id])
        row = by_class.setdefault(fixture.klass, {"total": 0, "correct": 0})
        row["total"] += 1
        if observed == fixture.expected:
            row["correct"] += 1
        else:
            misses.append(
                {"fixture": fixture.id, "expected": fixture.expected, "observed": observed}
            )
    total = sum(r["total"] for r in by_class.values())
    correct = sum(r["correct"] for r in by_class.values())

    # ---- hard requirements
    exception_shas = {shas[f.id] for f in pack.fixtures if f.klass == "exception_noted"}
    exception_filed = sum(1 for r in tms.filed.values() if r.document_sha256 in exception_shas)
    carriers = {c.id: c for c in store.find(Carrier)}
    chase_load = {c.id: loads[c.load_id] for c in store.find(Chase)}
    quiet_violations = 0
    for attempt in store.find(Attempt):
        tz = carriers[chase_load[attempt.chase_id].carrier_id].timezone
        if not 7 <= to_local(attempt.sent_at_ms, tz).hour < 20:
            quiet_violations += 1
    chains_ok = sum(1 for load in pack.loads if verify_chain(store.evidence(load.id)).ok)
    leaked = 0
    for load in pack.loads:
        for message in email.sent:
            if load.load_number in message.subject and message.to != [pack.broker.rep_email]:
                text = message.body.lower()
                if load.customer_name.lower() in text or load.pickup_number.lower() in text:
                    leaked += 1
    risk_held = sum(
        1
        for load_id in pack.risk_load_ids
        if store.find(Chase, load_id=load_id)[0].state is ChaseState.ESCALATED
        and any(e.reason == "RISK" for e in store.find(Escalation, load_id=load_id))
    )
    states = Counter(c.state.value for c in store.find(Chase))
    reasons = Counter(e.reason.value for e in store.find(Escalation))
    accuracy = correct / total
    requirements = {
        "routing_accuracy_at_least_95_percent": accuracy >= REQUIRED_ACCURACY,
        "zero_exception_noted_documents_filed": exception_filed == 0,
        "zero_sends_in_quiet_hours": quiet_violations == 0,
        "every_evidence_chain_verifies": chains_ok == len(pack.loads),
        "zero_sensitive_details_sent": leaked == 0,
        "every_money_message_held": risk_held == len(pack.risk_load_ids),
    }
    return {
        "seed": seed,
        "loads": len(pack.loads),
        "fixtures": total,
        "routing": {"correct": correct, "total": total, "accuracy": f"{accuracy:.1%}"},
        "by_class": by_class,
        "misses": misses,
        "exception_noted_filed": exception_filed,
        "quiet_hour_sends": quiet_violations,
        "evidence_chains_verified": f"{chains_ok}/{len(pack.loads)}",
        "money_messages_held": f"{risk_held}/{len(pack.risk_load_ids)}",
        "final_states": dict(sorted(states.items())),
        "escalation_reasons": dict(sorted(reasons.items())),
        "messages": {
            "emails_to_carriers": sum(1 for m in email.sent if m.to != [pack.broker.rep_email]),
            "texts": len(sms.sent),
            "documents_filed": len(tms.filed),
        },
        "requirements": requirements,
        "passed": all(requirements.values()),
    }


def render(report: dict) -> str:
    lines = [
        f"Synthetic evaluation (seed {report['seed']}): "
        f"{report['loads']} loads, {report['fixtures']} document fixtures",
        "",
        f"{'class':<18}{'correct':>8}{'total':>7}",
    ]
    for klass, row in report["by_class"].items():
        lines.append(f"{klass:<18}{row['correct']:>8}{row['total']:>7}")
    routing = report["routing"]
    lines += [
        f"{'all':<18}{routing['correct']:>8}{routing['total']:>7}   {routing['accuracy']}",
        "",
        f"exception-noted documents filed clean: {report['exception_noted_filed']}",
        f"sends during quiet hours:              {report['quiet_hour_sends']}",
        f"evidence chains verified:              {report['evidence_chains_verified']}",
        f"money messages held:                   {report['money_messages_held']}",
        f"final chase states:                    {report['final_states']}",
        f"escalation reasons:                    {report['escalation_reasons']}",
        f"messages:                              {report['messages']}",
        "",
    ]
    for name, ok in report["requirements"].items():
        lines.append(f"[{'PASS' if ok else 'FAIL'}] {name}")
    for miss in report["misses"]:
        lines.append(f"  miss: {miss}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()
    report = run(args.seed)
    print(render(report))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2))
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
