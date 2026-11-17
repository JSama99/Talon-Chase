"""Rules that hold across entities. The schema checks each record on its own; these
check that records agree with each other.

    from chase.ontology.constraints import check_store
    violations = check_store(store)      # [] when everything holds

Each rule has a stable id (TC-01 ...). The test suite runs every rule after every
test, and has one test per rule that breaks it on purpose and expects it to fire.
"""

from __future__ import annotations

import dataclasses
import re
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from chase.domain import models as m
from chase.domain.models import (
    ChaseState,
    CheckResult,
    ConsentSource,
    ConsentStatus,
    OperatorRole,
    OutboxPurpose,
    OutboxStatus,
    PartyRole,
    ReadingStatus,
    RequirementStatus,
    Step,
    WriteBackStatus,
)
from chase.domain.risk import normalize_phone
from chase.domain.states import ACTIVE_STATES
from chase.evidence import canonical_json, sha256_hex, verify_chain


@dataclass(frozen=True)
class Violation:
    rule: str
    entity: str
    entity_id: str
    message: str

    def __str__(self) -> str:
        return f"{self.rule} {self.entity} {self.entity_id}: {self.message}"


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    check: Callable[[Snapshot], Iterator[Violation]]


RULES: list[Rule] = []
_BY_NAME = {cls.__name__: cls for cls in m.ENTITY_TYPES}


def rule(rule_id: str, title: str):
    def register(fn: Callable[[Snapshot], Iterator[Violation]]):
        RULES.append(Rule(rule_id, title, fn))
        return fn

    return register


class Snapshot:
    """Every entity in a store, indexed, so rules can look across them."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.of: dict[type, dict[str, Any]] = {
            cls: {obj.id: obj for obj in store.find(cls)} for cls in m.ENTITY_TYPES
        }
        self.by: dict[tuple[type, str], dict[str, list[Any]]] = {}

    def all(self, cls: type) -> list[Any]:
        return sorted(self.of[cls].values(), key=lambda o: o.id)

    def get(self, cls: type, obj_id: str) -> Any:
        return self.of[cls].get(obj_id)

    def where(self, cls: type, field_name: str, value: str) -> list[Any]:
        key = (cls, field_name)
        if key not in self.by:
            index: dict[str, list[Any]] = defaultdict(list)
            for obj in self.all(cls):
                index[getattr(obj, field_name)].append(obj)
            self.by[key] = index
        return self.by[key].get(value, [])


def _v(rule_id: str, obj: Any, message: str) -> Violation:
    return Violation(rule_id, type(obj).__name__, getattr(obj, "id", ""), message)


# ------------------------------------------------------------------- integrity


@rule("TC-01", "Every reference points at an existing entity of the named type.")
def _references(s: Snapshot) -> Iterator[Violation]:
    for cls in m.ENTITY_TYPES:
        refs = [f for f in dataclasses.fields(cls) if f.metadata.get("ref")]
        for obj in s.all(cls):
            for fld in refs:
                target = _BY_NAME[fld.metadata["ref"]]
                value = getattr(obj, fld.name)
                values = value if isinstance(value, list) else [value]
                if not isinstance(value, list) and value == "":
                    if not fld.metadata.get("optional_ref"):
                        yield _v("TC-01", obj, f"{fld.name} is required")
                    continue
                for item in values:
                    if s.get(target, item) is None:
                        yield _v("TC-01", obj, f"{fld.name} -> {target.__name__} {item} missing")


@rule("TC-02", "Records only point at records of the same broker, in the right role.")
def _tenancy(s: Snapshot) -> Iterator[Violation]:
    for load in s.all(m.Load):
        carrier = s.get(m.Carrier, load.carrier_id)
        if carrier and carrier.broker_id != load.broker_id:
            yield _v("TC-02", load, "carrier belongs to another broker")
        for field_name, role in (
            ("customer_party_id", PartyRole.CUSTOMER),
            ("consignee_party_id", PartyRole.CONSIGNEE),
        ):
            party = s.get(m.Party, getattr(load, field_name))
            if party and (party.broker_id != load.broker_id or party.role is not role):
                yield _v("TC-02", load, f"{field_name} is not a {role.value} of this broker")
        for contact_id in load.contact_ids:
            contact = s.get(m.Contact, contact_id)
            if contact and contact.carrier_id != load.carrier_id:
                yield _v("TC-02", load, f"contact {contact_id} works for another carrier")
    for cls in (m.Chase, m.Escalation, m.OutboxMessage, m.Reading):
        for obj in s.all(cls):
            load = s.get(m.Load, obj.load_id)
            if load and load.broker_id != obj.broker_id:
                yield _v("TC-02", obj, "broker differs from its load's broker")
    for cls in (m.Escalation, m.OutboxMessage):
        for obj in s.all(cls):
            chase = s.get(m.Chase, obj.chase_id)
            if chase and chase.load_id != obj.load_id:
                yield _v("TC-02", obj, "load differs from its chase's load")
    for inbound in s.all(m.Inbound):
        chase = s.get(m.Chase, inbound.chase_id)
        if chase and chase.broker_id != inbound.broker_id:
            yield _v("TC-02", inbound, "matched to another broker's chase")
    for escalation in s.all(m.Escalation):
        operator = s.get(m.Operator, escalation.acknowledged_by)
        if operator and not _may_act(operator, escalation.broker_id):
            yield _v("TC-02", escalation, "acknowledged by an operator of another broker")
    for consent in s.all(m.Consent):
        operator = s.get(m.Operator, consent.recorded_by)
        contact = s.get(m.Contact, consent.contact_id)
        carrier = s.get(m.Carrier, contact.carrier_id) if contact else None
        if operator and carrier and not _may_act(operator, carrier.broker_id):
            yield _v("TC-02", consent, "recorded by an operator of another broker")


def _may_act(operator: m.Operator, broker_id: str) -> bool:
    return operator.role is OperatorRole.PLATFORM or operator.broker_id == broker_id


# ----------------------------------------------------------------------- chase


@rule("TC-03", "A load has at most one chase, and a chase chases its own load's requirement.")
def _one_chase(s: Snapshot) -> Iterator[Violation]:
    for load in s.all(m.Load):
        chases = s.where(m.Chase, "load_id", load.id)
        if len(chases) > 1:
            yield _v("TC-03", load, f"{len(chases)} chases")
    for requirement in s.all(m.Requirement):
        if len(s.where(m.Chase, "requirement_id", requirement.id)) > 1:
            yield _v("TC-03", requirement, "chased by more than one chase")
    for chase in s.all(m.Chase):
        requirement = s.get(m.Requirement, chase.requirement_id)
        if requirement and requirement.load_id != chase.load_id:
            yield _v("TC-03", chase, "its requirement is for another load")


_STEPS_FOR = {
    ChaseState.OPEN: {Step.FIRST_EMAIL},
    ChaseState.ASKED: {Step.DRIVER_SMS, Step.SECOND, Step.THIRD, Step.ESCALATE},
    ChaseState.RECEIVED: {Step.READ},  # rests here while the reading desk reads the files
    ChaseState.FAILED_CHECK: {Step.REASK},
    ChaseState.PASSED: {Step.WRITEBACK},
    ChaseState.ESCALATED: {Step.NONE},
    ChaseState.CLOSED: {Step.NONE},
}


@rule("TC-04", "A chase at rest is in a resting state whose next step fits it.")
def _next_step(s: Snapshot) -> Iterator[Violation]:
    for chase in s.all(m.Chase):
        allowed = _STEPS_FOR.get(chase.state)
        if allowed is None:
            yield _v("TC-04", chase, f"{chase.state} only exists inside a transaction")
        elif chase.next_step not in allowed:
            yield _v("TC-04", chase, f"{chase.state} with next step {chase.next_step}")
        if (chase.state is ChaseState.CLOSED) != (chase.closed_at_ms is not None):
            yield _v("TC-04", chase, "closed_at_ms is set if and only if CLOSED")


@rule("TC-05", "Request and re-ask counters stay within the schedule and the broker's limit.")
def _counters(s: Snapshot) -> Iterator[Violation]:
    for chase in s.all(m.Chase):
        broker = s.get(m.Broker, chase.broker_id)
        if chase.attempts_sent > 3:
            yield _v("TC-05", chase, f"{chase.attempts_sent} scheduled requests")
        if broker and chase.reasks_sent > broker.config.max_reasks:
            yield _v("TC-05", chase, f"{chase.reasks_sent} re-asks")


@rule(
    "TC-06",
    "A chase closed as filed has a passed document, a successful filing of it, "
    "a met requirement and a billable load.",
)
def _filed(s: Snapshot) -> Iterator[Violation]:
    for chase in s.all(m.Chase):
        if chase.state is not ChaseState.CLOSED or chase.closed_reason != "filed":
            continue
        doc_id = chase.passed_document_id
        checks = [c for c in s.where(m.Check, "document_id", doc_id) if c.chase_id == chase.id]
        if not any(c.result is CheckResult.PASS for c in checks):
            yield _v("TC-06", chase, "passed document has no PASS check")
        writeback = s.get(m.WriteBack, chase.writeback_id)
        if not writeback or writeback.status is not WriteBackStatus.SUCCEEDED:
            yield _v("TC-06", chase, "no successful filing")
        elif writeback.document_id != doc_id:
            yield _v("TC-06", chase, "filed a different document from the one that passed")
        requirement = s.get(m.Requirement, chase.requirement_id)
        if not requirement or requirement.status is not RequirementStatus.MET:
            yield _v("TC-06", chase, "requirement not MET")
        elif requirement.met_by_document_id != doc_id:
            yield _v("TC-06", chase, "requirement met by another document")
        load = s.get(m.Load, chase.load_id)
        if load and load.ready_to_invoice_at_ms is None:
            yield _v("TC-06", chase, "load not marked ready to invoice")


@rule(
    "TC-07",
    "A chase closed by a person has escalations, all resolved, and a requirement "
    "resolved by a person. No other closing reason exists.",
)
def _closed_by_person(s: Snapshot) -> Iterator[Violation]:
    for chase in s.all(m.Chase):
        if chase.state is not ChaseState.CLOSED:
            continue
        if chase.closed_reason not in ("filed", "escalation_acknowledged"):
            yield _v("TC-07", chase, f"unknown closing reason {chase.closed_reason!r}")
            continue
        if chase.closed_reason != "escalation_acknowledged":
            continue
        escalations = s.where(m.Escalation, "chase_id", chase.id)
        if not escalations:
            yield _v("TC-07", chase, "closed by a person without an escalation")
        if any(e.resolution is None for e in escalations):
            yield _v("TC-07", chase, "closed while an escalation is unresolved")
        requirement = s.get(m.Requirement, chase.requirement_id)
        if requirement and requirement.status is not RequirementStatus.RESOLVED_BY_PERSON:
            yield _v("TC-07", chase, "requirement not RESOLVED_BY_PERSON")


@rule(
    "TC-08",
    "An escalated chase has an open escalation; a chase still working has none.",
)
def _escalation_state(s: Snapshot) -> Iterator[Violation]:
    for chase in s.all(m.Chase):
        open_ = [e for e in s.where(m.Escalation, "chase_id", chase.id) if e.resolution is None]
        if chase.state is ChaseState.ESCALATED and not open_:
            yield _v("TC-08", chase, "ESCALATED with no open escalation")
        if chase.state in ACTIVE_STATES and open_:
            yield _v("TC-08", chase, f"{chase.state} with an open escalation")


@rule(
    "TC-09",
    "An escalation is resolved all at once: by an operator, at a time, with a "
    "resolution; OTHER needs a note.",
)
def _resolution(s: Snapshot) -> Iterator[Violation]:
    for escalation in s.all(m.Escalation):
        parts = (
            bool(escalation.acknowledged_by),
            escalation.acknowledged_at_ms is not None,
            escalation.resolution is not None,
        )
        if len(set(parts)) > 1:
            yield _v("TC-09", escalation, "partly resolved")
        if escalation.resolution is m.EscalationResolution.OTHER and not escalation.note.strip():
            yield _v("TC-09", escalation, "OTHER without a note")


@rule(
    "TC-10",
    "A requirement's status matches its evidence: MET names a passed document on "
    "the same load; closed requirements have a closing time; open ones have neither.",
)
def _requirement(s: Snapshot) -> Iterator[Violation]:
    for req in s.all(m.Requirement):
        closed = req.closed_at_ms is not None
        if req.status is RequirementStatus.OPEN:
            if closed or req.met_by_document_id:
                yield _v("TC-10", req, "OPEN but closed or met")
            continue
        if not closed:
            yield _v("TC-10", req, f"{req.status} without closed_at_ms")
        if req.status is RequirementStatus.RESOLVED_BY_PERSON and req.met_by_document_id:
            yield _v("TC-10", req, "resolved by a person but names a document")
        if req.status is RequirementStatus.MET:
            doc = s.get(m.Document, req.met_by_document_id)
            if not doc or doc.load_id != req.load_id:
                yield _v("TC-10", req, "MET by a document of another load, or none")
            elif not any(
                c.result is CheckResult.PASS for c in s.where(m.Check, "document_id", doc.id)
            ):
                yield _v("TC-10", req, "MET by a document that never passed")


# -------------------------------------------------------------------- exchange


@rule(
    "TC-11",
    "Every request is delivered by exactly one outbox message that matches it, "
    "and every carrier request in the outbox comes from a request.",
)
def _attempt_message(s: Snapshot) -> Iterator[Violation]:
    for attempt in s.all(m.Attempt):
        message = s.get(m.OutboxMessage, attempt.outbox_id)
        if not message:
            continue  # TC-01
        if (
            message.chase_id != attempt.chase_id
            or message.purpose is not OutboxPurpose.CARRIER_REQUEST
            or message.idempotency_key != f"attempt:{attempt.id}"
            or message.channel is not attempt.channel
            or message.to != attempt.to
        ):
            yield _v("TC-11", attempt, "does not match its outbox message")
    for message in s.all(m.OutboxMessage):
        if message.purpose is OutboxPurpose.CARRIER_REQUEST:
            if len(s.where(m.Attempt, "outbox_id", message.id)) != 1:
                yield _v("TC-11", message, "carrier request without exactly one attempt")


@rule("TC-12", "An outbox message's fields match its delivery state.")
def _outbox_state(s: Snapshot) -> Iterator[Violation]:
    for msg in s.all(m.OutboxMessage):
        done = msg.status in (OutboxStatus.SENT, OutboxStatus.FAILED, OutboxStatus.CANCELLED)
        if done != (msg.finished_at_ms is not None):
            yield _v("TC-12", msg, "finished_at_ms is set if and only if finished")
        if (msg.status is OutboxStatus.SENT) != (msg.sent_at_ms is not None):
            yield _v("TC-12", msg, "sent_at_ms is set if and only if SENT")
        if msg.status is OutboxStatus.SENT and (not msg.provider_id or msg.tries < 1):
            yield _v("TC-12", msg, "SENT without a provider id or a try")
        if msg.status is OutboxStatus.FAILED and msg.tries < 1:
            yield _v("TC-12", msg, "FAILED without a try")
        if (msg.status is OutboxStatus.SENDING) != bool(msg.claim_token):
            yield _v("TC-12", msg, "a claim token is held if and only if SENDING")
        if msg.status is OutboxStatus.SENDING and msg.lease_until_ms <= 0:
            yield _v("TC-12", msg, "SENDING without a lease")


@rule("TC-13", "A packet goes by email to the broker's own rep, with no quiet hours.")
def _packet(s: Snapshot) -> Iterator[Violation]:
    for msg in s.all(m.OutboxMessage):
        if msg.purpose is not OutboxPurpose.PACKET:
            continue
        broker = s.get(m.Broker, msg.broker_id)
        if broker and (
            msg.to != [broker.rep_email] or msg.channel is not m.Channel.EMAIL or msg.quiet_tz
        ):
            yield _v("TC-13", msg, "packet not addressed to the broker's rep by email")


def _history(s: Snapshot, contact_id: str) -> list[m.Consent]:
    return sorted(s.where(m.Consent, "contact_id", contact_id), key=lambda c: c.seq)


def _consent_at(s: Snapshot, contact_id: str, at_ms: int) -> bool:
    """Whether texts were allowed at ``at_ms``, by the last record made by then. A
    withdrawal recorded in the same millisecond as a send is taken to have come after
    it, so the send is not blamed for it."""
    records = [
        c
        for c in _history(s, contact_id)
        if c.recorded_at_ms < at_ms
        or (c.recorded_at_ms == at_ms and c.status is ConsentStatus.GRANTED)
    ]
    return bool(records) and records[-1].status is ConsentStatus.GRANTED


@rule("TC-14", "Every delivered text went to a contact who had consent at that moment.")
def _texts_had_consent(s: Snapshot) -> Iterator[Violation]:
    for msg in s.all(m.OutboxMessage):
        if (
            msg.channel is not m.Channel.SMS
            or msg.purpose is not OutboxPurpose.CARRIER_REQUEST
            or msg.status is not OutboxStatus.SENT
        ):
            continue
        load = s.get(m.Load, msg.load_id)
        phone = normalize_phone(msg.to[0]) if msg.to else ""
        contacts = [
            c
            for c in (s.where(m.Contact, "carrier_id", load.carrier_id) if load else [])
            if c.phone and normalize_phone(c.phone) == phone
        ]
        if not any(_consent_at(s, c.id, msg.sent_at_ms or 0) for c in contacts):
            yield _v("TC-14", msg, f"text to {msg.to} without consent at the time")


@rule("TC-15", "A carrier message's status agrees with what was done with it.")
def _inbound(s: Snapshot) -> Iterator[Violation]:
    for inbound in s.all(m.Inbound):
        if inbound.status is m.InboundStatus.NEW:
            yield _v("TC-15", inbound, "left NEW; every message is processed in one step")
        if inbound.status is m.InboundStatus.UNMATCHED and inbound.chase_id:
            yield _v("TC-15", inbound, "UNMATCHED but tied to a chase")
        if inbound.status is m.InboundStatus.HELD:
            escalated = any(
                inbound.id in e.detail for e in s.where(m.Escalation, "chase_id", inbound.chase_id)
            )
            if not inbound.hold_reason or not escalated:
                yield _v("TC-15", inbound, "HELD without a reason and an escalation naming it")
        if inbound.trusted != (inbound.channel is m.Channel.UPLOAD):
            yield _v("TC-15", inbound, "only upload-link messages are trusted")


# ----------------------------------------------------------------------- proof


@rule(
    "TC-16",
    "Every file is stored under its hash, and every check judges a document of its "
    "own chase's load with reasons that fit the result.",
)
def _documents(s: Snapshot) -> Iterator[Violation]:
    hashes = {d.content_sha256 for d in s.all(m.Document)}
    for inbound in s.all(m.Inbound):
        hashes |= {a.content_sha256 for a in inbound.attachments}
    for digest in sorted(hashes):
        if s.store.get_blob(digest) is None:
            yield Violation("TC-16", "Blob", digest, "file referred to but not stored")
    for check in s.all(m.Check):
        doc = s.get(m.Document, check.document_id)
        chase = s.get(m.Chase, check.chase_id)
        if doc and chase and doc.load_id != chase.load_id:
            yield _v("TC-16", check, "judges a document of another load")
        is_reroute = check.result is CheckResult.REROUTE
        if is_reroute != bool(check.reroute_load_id):
            yield _v("TC-16", check, "reroute_load_id is set if and only if REROUTE")
        if chase and check.reroute_load_id == chase.load_id:
            yield _v("TC-16", check, "rerouted to its own load")
        if (check.result is CheckResult.PASS) == bool(check.reasons):
            yield _v("TC-16", check, "PASS has no reasons; every other result has some")


# --------------------------------------------------------------------- outcome


@rule(
    "TC-17",
    "A filing names a document of its chase's load, carries the agreed idempotency "
    "key, and a successful one has a receipt and a time.",
)
def _writebacks(s: Snapshot) -> Iterator[Violation]:
    for wb in s.all(m.WriteBack):
        doc = s.get(m.Document, wb.document_id)
        chase = s.get(m.Chase, wb.chase_id)
        if not doc or not chase:
            continue  # TC-01
        if doc.load_id != chase.load_id:
            yield _v("TC-17", wb, "files a document of another load")
        key = sha256_hex(
            canonical_json(
                {
                    "adapter": wb.adapter,
                    "document_sha256": doc.content_sha256,
                    "load_id": chase.load_id,
                }
            )
        )
        if wb.idempotency_key != key:
            yield _v("TC-17", wb, "idempotency key does not match its parts")
        succeeded = wb.status is WriteBackStatus.SUCCEEDED
        if succeeded != (wb.completed_at_ms is not None) or (succeeded and not wb.receipt):
            yield _v("TC-17", wb, "a success has a receipt and a time; nothing else has a time")
    for chase in s.all(m.Chase):
        wb = s.get(m.WriteBack, chase.writeback_id)
        if wb and wb.chase_id != chase.id:
            yield _v("TC-17", chase, "points at another chase's filing")


# ---------------------------------------------------------------------- policy


@rule(
    "TC-18",
    "Consent history is coherent: numbered 1, 2, 3... in the order recorded, with "
    "time never going backwards; STOP withdraws and names its message; written "
    "consent names an operator; after a STOP only written consent grants again.",
)
def _consent_history(s: Snapshot) -> Iterator[Violation]:
    for contact in s.all(m.Contact):
        history = _history(s, contact.id)
        if [c.seq for c in history] != list(range(1, len(history) + 1)):
            yield _v("TC-18", contact, "consent history is not numbered 1, 2, 3...")
        times = [c.recorded_at_ms for c in history]
        if times != sorted(times):
            yield _v("TC-18", contact, "a later consent record has an earlier time")
        stopped = False
        for consent in history:
            if consent.channel is not m.Channel.SMS:
                yield _v("TC-18", consent, "consent is recorded for texts only")
            if consent.source is ConsentSource.STOP_REPLY:
                if consent.status is not ConsentStatus.WITHDRAWN or not consent.inbound_id:
                    yield _v("TC-18", consent, "STOP must withdraw and name its message")
                stopped = True
            if consent.source is ConsentSource.WRITTEN and not consent.recorded_by:
                yield _v("TC-18", consent, "written consent without an operator")
            if consent.status is ConsentStatus.GRANTED:
                if stopped and consent.source is not ConsentSource.WRITTEN:
                    yield _v("TC-18", consent, "re-granted after STOP without written consent")
                stopped = False


@rule(
    "TC-19",
    "Every evidence chain verifies, every load's chain starts with load.received, "
    "and every chase is on its load's chain.",
)
def _evidence(s: Snapshot) -> Iterator[Violation]:
    chains = [load.id for load in s.all(m.Load)]
    chains += [f"broker:{b.id}" for b in s.all(m.Broker)] + ["system"]
    for chain_id in chains:
        events = s.store.evidence(chain_id)
        result = verify_chain(events)
        if not result.ok:
            yield Violation(
                "TC-19", "Chain", chain_id, f"fails at {result.failed_index}: {result.reason}"
            )
    for load in s.all(m.Load):
        events = s.store.evidence(load.id)
        if not events or events[0].kind != "load.received":
            yield _v("TC-19", load, "chain does not start with load.received")
        opened = {e.payload.get("chase_id") for e in events if e.kind == "chase.opened"}
        for chase in s.where(m.Chase, "load_id", load.id):
            if chase.id not in opened:
                yield _v("TC-19", chase, "no chase.opened on its load's chain")


_LINK = re.compile(r"\S+/u/\S+")


@rule("TC-20", "No carrier request contains anything the ontology marks sensitive.")
def _nothing_sensitive_sent(s: Snapshot) -> Iterator[Violation]:
    from chase.domain import outbound

    for msg in s.all(m.OutboxMessage):
        if msg.purpose is not OutboxPurpose.CARRIER_REQUEST:
            continue
        load = s.get(m.Load, msg.load_id)
        if not load:
            continue
        parties = [
            p
            for p in (
                s.get(m.Party, load.customer_party_id),
                s.get(m.Party, load.consignee_party_id),
            )
            if p
        ]
        text = _LINK.sub("", f"{msg.subject}\n{msg.body}").replace(load.load_number, "")
        try:
            outbound.assert_safe(text, load, parties)
        except outbound.OutboundBlocked as exc:
            yield _v("TC-20", msg, str(exc))


@rule("TC-21", "Active operators sign in with distinct email addresses.")
def _operator_emails(s: Snapshot) -> Iterator[Violation]:
    seen: dict[str, str] = {}
    for operator in s.all(m.Operator):
        email = operator.email.strip().lower()
        if not operator.active or not email:
            continue
        if email in seen:
            yield _v("TC-21", operator, f"shares {email} with operator {seen[email]}")
        else:
            seen[email] = operator.id


@rule(
    "TC-22",
    "A document is read once: an unread document has exactly one reading in hand, a "
    "settled one has none, and a chase waits in RECEIVED only for a file still unread.",
)
def _readings(s: Snapshot) -> Iterator[Violation]:
    in_hand = (ReadingStatus.PENDING, ReadingStatus.READING)
    for document in s.all(m.Document):
        mine = s.where(m.Reading, "document_id", document.id)
        waiting = [r for r in mine if r.status in in_hand]
        if len(mine) > 1:
            yield _v("TC-22", document, f"has {len(mine)} readings")
        if document.read_at_ms is None:
            if len(waiting) != 1:
                yield _v("TC-22", document, "unread, with no reading in hand")
            if document.reader:
                yield _v("TC-22", document, "names a reader though it is unread")
        elif waiting:
            yield _v("TC-22", document, "settled, yet a reading is still in hand")
    for reading in s.all(m.Reading):
        document = s.get(m.Document, reading.document_id)
        if document and document.load_id != reading.load_id:
            yield _v("TC-22", reading, "load differs from its document's load")
        if (reading.status in in_hand) == (reading.finished_at_ms is not None):
            yield _v("TC-22", reading, "finished_at_ms is set if and only if DONE or FAILED")
        if (reading.status is ReadingStatus.READING) != bool(reading.claim_token):
            yield _v("TC-22", reading, "a claim token is held if and only if READING")
        if reading.status is ReadingStatus.DONE and document and not document.reader:
            yield _v("TC-22", reading, "DONE, but its document names no reader")
    for chase in s.all(m.Chase):
        resting = chase.state is ChaseState.RECEIVED
        if resting != bool(chase.reading_inbound_id):
            yield _v("TC-22", chase, "names a message being read if and only if RECEIVED")
        elif resting and not any(
            d.read_at_ms is None and d.load_id == chase.load_id
            for d in s.where(m.Document, "inbound_id", chase.reading_inbound_id)
        ):
            yield _v("TC-22", chase, "waits in RECEIVED though every file has been read")


def check_store(store: Any) -> list[Violation]:
    """Run every rule. Returns all violations, in rule order."""
    snapshot = Snapshot(store)
    return [violation for r in RULES for violation in r.check(snapshot)]
