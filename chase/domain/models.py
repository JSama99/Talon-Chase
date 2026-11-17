"""Entities of the Talon Chase ontology.

Plain dataclasses. Every time is integer epoch milliseconds. Each field carries
metadata read by ``chase.ontology``:

  doc        what the field means (becomes the schema description)
  ref        the entity type an id points at ("" allowed only if ``optional``)
  sensitive  must never appear in a message to a carrier (drives the outbound guard)

The JSON Schema, JSON-LD context and docs/ONTOLOGY.md are generated from these
definitions. Change them here, then run ``python -m chase.ontology.build``.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

HOUR_MS = 3_600_000
MINUTE_MS = 60_000


def f(
    doc: str,
    *,
    ref: str = "",
    optional: bool = False,
    sensitive: bool = False,
    shape: str = "",
    default: Any = dataclasses.MISSING,
    default_factory: Any = dataclasses.MISSING,
) -> Any:
    """A field with ontology metadata."""
    metadata = {
        "doc": doc,
        "ref": ref,
        "optional_ref": optional,
        "sensitive": sensitive,
        "shape": shape,  # a value type the dict must match, e.g. "Plan"
    }
    if default_factory is not dataclasses.MISSING:
        return dataclasses.field(default_factory=default_factory, metadata=metadata)
    if default is not dataclasses.MISSING:
        return dataclasses.field(default=default, metadata=metadata)
    return dataclasses.field(metadata=metadata)


# ---------------------------------------------------------------------- enums


class ChaseState(StrEnum):
    """Where one chase for paperwork stands."""

    OPEN = "OPEN"
    ASKED = "ASKED"
    RECEIVED = "RECEIVED"
    CHECKING = "CHECKING"
    PASSED = "PASSED"
    FAILED_CHECK = "FAILED_CHECK"
    FILED = "FILED"
    CLOSED = "CLOSED"
    ESCALATED = "ESCALATED"


class Step(StrEnum):
    """The next thing a chase will do."""

    FIRST_EMAIL = "FIRST_EMAIL"
    DRIVER_SMS = "DRIVER_SMS"
    SECOND = "SECOND"
    THIRD = "THIRD"
    ESCALATE = "ESCALATE"
    REASK = "REASK"
    READ = "READ"  # waiting for the reader; the reading desk moves it on, not the tick
    WRITEBACK = "WRITEBACK"
    NONE = "NONE"


class Channel(StrEnum):
    """How a message travels."""

    EMAIL = "EMAIL"
    SMS = "SMS"
    UPLOAD = "UPLOAD"


class Role(StrEnum):
    """A carrier contact's job."""

    DISPATCHER = "DISPATCHER"
    DRIVER = "DRIVER"


class OperatorRole(StrEnum):
    """What a person at the broker, or at TalonSight, may do."""

    OWNER = "OWNER"
    STAFF = "STAFF"
    PLATFORM = "PLATFORM"  # runs the service; the only role that may use the global switch


class PartyRole(StrEnum):
    """A business on a load other than the broker and the carrier."""

    CUSTOMER = "CUSTOMER"
    SHIPPER = "SHIPPER"
    CONSIGNEE = "CONSIGNEE"


class DocType(StrEnum):
    """What a document is."""

    POD = "POD"
    LUMPER_RECEIPT = "LUMPER_RECEIPT"
    DETENTION_RECEIPT = "DETENTION_RECEIPT"
    SCALE_TICKET = "SCALE_TICKET"
    CARRIER_INVOICE = "CARRIER_INVOICE"
    RATE_CONFIRMATION = "RATE_CONFIRMATION"
    OTHER = "OTHER"


class RequirementStatus(StrEnum):
    """Whether a load still needs a document."""

    OPEN = "OPEN"
    MET = "MET"  # a checked document was filed
    RESOLVED_BY_PERSON = "RESOLVED_BY_PERSON"  # a person closed it; see the escalation


class CheckResult(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    EXCEPTION = "EXCEPTION"
    REROUTE = "REROUTE"


class EscalationReason(StrEnum):
    NO_RESPONSE = "NO_RESPONSE"
    RISK = "RISK"
    UNKNOWN_SENDER = "UNKNOWN_SENDER"
    DOCUMENT_EXCEPTION = "DOCUMENT_EXCEPTION"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    WRONG_PERSON = "WRONG_PERSON"
    WRITEBACK_FAILED = "WRITEBACK_FAILED"
    NO_CONTACT = "NO_CONTACT"
    REASK_LIMIT = "REASK_LIMIT"
    OUTBOUND_BLOCKED = "OUTBOUND_BLOCKED"
    SEND_FAILED = "SEND_FAILED"


class EscalationResolution(StrEnum):
    """How a person finished an escalation."""

    HANDLED_BY_HAND = "HANDLED_BY_HAND"  # got and filed the paperwork outside Talon Chase
    WAIVED = "WAIVED"  # the paperwork is not needed
    DISPUTED = "DISPUTED"  # a claim or dispute with the carrier is open
    OTHER = "OTHER"  # see the note


class InboundStatus(StrEnum):
    NEW = "NEW"
    PROCESSED = "PROCESSED"
    HELD = "HELD"
    UNMATCHED = "UNMATCHED"


class OutboxStatus(StrEnum):
    PENDING = "PENDING"  # waiting to be sent, or waiting to be retried
    SENDING = "SENDING"  # claimed by a dispatcher until lease_until_ms
    SENT = "SENT"
    FAILED = "FAILED"  # gave up; a person was told
    CANCELLED = "CANCELLED"  # no longer needed, never sent


class OutboxPurpose(StrEnum):
    CARRIER_REQUEST = "CARRIER_REQUEST"  # asks a carrier for paperwork
    PACKET = "PACKET"  # a ready-to-file packet for the broker


class ReadingStatus(StrEnum):
    PENDING = "PENDING"  # waiting to be read, or waiting to be tried again
    READING = "READING"  # claimed by a reading desk until lease_until_ms
    DONE = "DONE"  # the reader answered
    FAILED = "FAILED"  # gave up; the document goes to a person unread


class WriteBackStatus(StrEnum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ConsentStatus(StrEnum):
    GRANTED = "GRANTED"
    WITHDRAWN = "WITHDRAWN"


class ConsentSource(StrEnum):
    """Where a consent record came from."""

    RATE_CONFIRMATION = "RATE_CONFIRMATION"  # a consent clause the carrier accepted
    CARRIER_PACKET = "CARRIER_PACKET"  # the carrier's onboarding packet
    WRITTEN = "WRITTEN"  # recorded by a named operator from a written request
    TMS_SYNC = "TMS_SYNC"  # the broker's system said so
    STOP_REPLY = "STOP_REPLY"  # the contact replied STOP; only an operator can undo it


# --------------------------------------------------------------------- Policy


@dataclass
class BrokerConfig:
    """Per-broker schedule and policy. Every value can be changed by the broker."""

    no_status_grace_ms: int = f(
        "Open a chase this long after the delivery appointment if the load has no status.",
        default=2 * HOUR_MS,
    )
    sms_delay_ms: int = f("Text the driver this long after the first email.", default=3 * HOUR_MS)
    third_attempt_ms: int = f("Third request this long after the first.", default=48 * HOUR_MS)
    escalate_wait_ms: int = f(
        "Hand to a person this long after the third request.", default=24 * HOUR_MS
    )
    min_gap_ms: int = f("Shortest gap between two requests.", default=1 * HOUR_MS)
    third_min_gap_ms: int = f(
        "Shortest gap between the second and third requests.", default=4 * HOUR_MS
    )
    promise_grace_ms: int = f(
        "How long a carrier's promise ('will send tomorrow') pushes the next ask back.",
        default=24 * HOUR_MS,
    )
    reask_wait_ms: int = f(
        "After a re-ask, wait this long before the plan continues.", default=4 * HOUR_MS
    )
    quiet_start_hour: int = f("Carrier-local hour when quiet hours begin.", default=20)
    quiet_end_hour: int = f("Carrier-local hour when quiet hours end.", default=7)
    morning_hour: int = f("Carrier-local hour of the second request.", default=8)
    max_reasks: int = f("Re-asks for a failing document before a person decides.", default=3)
    date_tolerance_ms: int = f(
        "How far the POD's delivery date may be from the appointment.", default=24 * HOUR_MS
    )
    confidence_floor: int = f(
        "Below this reading confidence (0 to 100), a person decides.", default=80
    )
    writeback_retry_backoff_ms: list[int] = f(
        "Waits between filing retries; when it runs out, the next rung is tried.",
        default_factory=lambda: [MINUTE_MS, 5 * MINUTE_MS, 15 * MINUTE_MS],
    )
    upload_link_ttl_ms: int = f("How long an upload link stays valid.", default=14 * 24 * HOUR_MS)
    timezone: str = f(
        "IANA time zone the broker works in; the broker screens show times in it.",
        default="America/New_York",
    )
    send_retry_backoff_ms: list[int] = f(
        "Waits between delivery retries; when it runs out, the message fails.",
        default_factory=lambda: [
            MINUTE_MS,
            5 * MINUTE_MS,
            15 * MINUTE_MS,
            HOUR_MS,
            4 * HOUR_MS,
        ],  # fmt: skip
    )


@dataclass
class Consent:
    """One recorded change to whether a contact may be texted. The record with the
    highest ``seq`` is the contact's current state."""

    id: str = f("Identifier.")
    contact_id: str = f("The contact this applies to.", ref="Contact")
    channel: Channel = f("Always SMS for now.")
    status: ConsentStatus = f("Granted or withdrawn.")
    source: ConsentSource = f("Where this record came from.")
    recorded_at_ms: int = f("When it was recorded.")
    seq: int = f(
        "Position in this contact's consent history, from 1, in the order recorded. "
        "Decides which record is latest; times can tie and ids carry no order."
    )
    recorded_by: str = f(
        "The operator who recorded it; empty when it came from the carrier or a sync.",
        ref="Operator",
        optional=True,
        default="",
    )
    inbound_id: str = f(
        "The carrier message it came from, for a STOP reply.",
        ref="Inbound",
        optional=True,
        default="",
    )
    note: str = f("Free text from the operator.", default="")


# ------------------------------------------------------------------------ Who


@dataclass
class Broker:
    """A freight brokerage using Talon Chase."""

    id: str = f("Identifier.")
    name: str = f("Trading name, shown to carriers in every message.")
    chase_mailbox: str = f("The broker's own address that requests come from, e.g. pods@.")
    rep_email: str = f("Where packets and copied third requests go.")
    sms_number: str = f("The number texts come from; empty means no texts.", default="")
    adapter: str = f("Where checked PODs are filed: an adapter name.", default="packet_email")
    outbound_enabled: bool = f("This broker's kill switch.", default=True)
    config: BrokerConfig = f("Schedule and policy.", default_factory=BrokerConfig)


@dataclass
class Operator:
    """A person who acts in Talon Chase: acknowledges, approves, switches off."""

    id: str = f("Identifier.")
    broker_id: str = f(
        "The broker they work for; empty for a platform operator.",
        ref="Broker",
        optional=True,
    )
    name: str = f("Display name.")
    role: OperatorRole = f("What they may do.")
    email: str = f("Sign-in address.", default="")
    active: bool = f("Inactive operators cannot act.", default=True)


@dataclass
class Carrier:
    """A trucking company a broker gives loads to."""

    id: str = f("Identifier.")
    broker_id: str = f("The broker whose carrier record this is.", ref="Broker")
    name: str = f("Carrier name.")
    mc_number: str = f("FMCSA motor carrier number.")
    timezone: str = f(
        "IANA time zone; quiet hours and the schedule use it.", default="America/New_York"
    )


@dataclass
class Contact:
    """A person at a carrier the agent may write to. Only verified contacts are used."""

    id: str = f("Identifier.")
    carrier_id: str = f("Their carrier.", ref="Carrier")
    role: Role = f("Dispatcher or driver.")
    name: str = f("Name.", default="")
    email: str = f("Email address.", default="")
    phone: str = f("Mobile number.", default="")
    verified: bool = f("From the carrier record or approved by an operator.", default=True)
    source: str = f("'tms', or 'approved:<operator id>'.", default="tms")
    email_undeliverable_at_ms: int | None = f(
        "When mail to this address bounced for good; it is not emailed again.", default=None
    )


# -------------------------------------------------------------------- Freight


@dataclass
class Party:
    """A customer, shipper or consignee. Names and addresses never go to carriers."""

    id: str = f("Identifier.")
    broker_id: str = f("The broker whose record this is.", ref="Broker")
    role: PartyRole = f("Customer, shipper or consignee.")
    name: str = f("Business name.", sensitive=True)
    address: str = f("Street address.", sensitive=True, default="")


@dataclass
class Load:
    """One shipment the broker arranged."""

    id: str = f("Identifier.")
    broker_id: str = f("The broker.", ref="Broker")
    carrier_id: str = f("The carrier hauling it.", ref="Carrier")
    load_number: str = f("The broker's load number; the only load detail carriers see.")
    delivery_appt_ms: int = f("Delivery appointment.")
    bol_number: str = f("Bill of lading number.", default="")
    po_number: str = f("Purchase order number.", default="")
    expected_pages: int = f("Pages a complete POD has.", default=1)
    delivered_at_ms: int | None = f("When the TMS marked it delivered.", default=None)
    contact_ids: list[str] = f(
        "Carrier contacts for this load; empty means all verified contacts.",
        ref="Contact",
        default_factory=list,
    )
    customer_party_id: str = f("Who the broker bills.", ref="Party", optional=True, default="")
    consignee_party_id: str = f("Who receives the freight.", ref="Party", optional=True, default="")
    pickup_number: str = f("Pickup reference.", sensitive=True, default="")
    rate_cents: int = f("What the broker pays the carrier, in cents.", sensitive=True, default=0)
    ready_to_invoice_at_ms: int | None = f(
        "When a checked POD was filed and the load became billable.", default=None
    )


# ----------------------------------------------------------------------- Need


@dataclass
class Requirement:
    """A document a load needs before it can be billed."""

    id: str = f("Identifier.")
    load_id: str = f("The load.", ref="Load")
    doc_type: DocType = f("Which document.", default=DocType.POD)
    status: RequirementStatus = f(
        "Open, met, or resolved by a person.", default=RequirementStatus.OPEN
    )
    met_by_document_id: str = f(
        "The filed document that met it.", ref="Document", optional=True, default=""
    )
    closed_at_ms: int | None = f("When it stopped being open.", default=None)


@dataclass
class Chase:
    """The agent's effort to obtain one requirement."""

    id: str = f("Identifier.")
    load_id: str = f("The load.", ref="Load")
    broker_id: str = f("The load's broker.", ref="Broker")
    requirement_id: str = f("What is being chased.", ref="Requirement")
    opened_at_ms: int = f("When the chase opened.")
    state: ChaseState = f("Where it stands.", default=ChaseState.OPEN)
    next_step: Step = f("What it will do next.", default=Step.FIRST_EMAIL)
    next_action_at_ms: int = f("When the next step is due.", default=0)
    attempts_sent: int = f("Requests made in the schedule (first, second, third).", default=0)
    reasks_sent: int = f("Re-asks made after a failed check.", default=0)
    plan: dict[str, int] = f(
        "The schedule worked out when it opened.", shape="Plan", default_factory=dict
    )
    fail_reasons: list[str] = f("Why the last document failed.", default_factory=list)
    reply_to_contact_id: str = f(
        "Who sent the last document; re-asks go to them.",
        ref="Contact",
        optional=True,
        default="",
    )
    passed_document_id: str = f(
        "The document that passed.", ref="Document", optional=True, default=""
    )
    writeback_id: str = f("The current filing.", ref="WriteBack", optional=True, default="")
    reading_inbound_id: str = f(
        "The carrier message whose files are being read; set while RECEIVED.",
        ref="Inbound",
        optional=True,
        default="",
    )
    closed_at_ms: int | None = f("When it closed.", default=None)
    closed_reason: str = f("'filed' or 'escalation_acknowledged'.", default="")
    kill_noted: bool = f("A blocked step was already recorded.", default=False)


# ------------------------------------------------------------------- Exchange


@dataclass
class Attempt:
    """A request the agent decided to make. Its delivery is the outbox message."""

    id: str = f("Identifier.")
    chase_id: str = f("The chase.", ref="Chase")
    step: Step = f("Which request in the schedule.")
    channel: Channel = f("Email or text.")
    to: list[str] = f("Recipients.")
    cc: list[str] = f("Copied, e.g. the broker's rep on the third request.")
    queued_at_ms: int = f("When it was decided and queued.")
    outbox_id: str = f("The message that delivers it.", ref="OutboxMessage", default="")


@dataclass
class Attachment:
    """A file referred to by its content hash."""

    filename: str = f("Name as sent.")
    content_sha256: str = f("SHA-256 of the bytes; the file is stored under it.")


@dataclass
class Inbound:
    """A message or upload from a carrier."""

    id: str = f("Identifier.")
    broker_id: str = f("The broker it was sent to.", ref="Broker")
    channel: Channel = f("Email, text or upload link.")
    from_addr: str = f("Sender address or number.")
    received_at_ms: int = f("When it arrived.")
    subject: str = f("Email subject.", default="")
    body: str = f("Text of the message.", default="")
    attachments: list[Attachment] = f("Files attached.", default_factory=list)
    external_id: str = f("Provider's message id; repeats are ignored.", default="")
    status: InboundStatus = f("What happened to it.", default=InboundStatus.NEW)
    chase_id: str = f("The chase it belongs to.", ref="Chase", optional=True, default="")
    classification: str = f("What the reply said, as labelled.", default="")
    hold_reason: str = f("Why it was held for a person.", default="")
    trusted: bool = f("Came through a signed upload link.", default=False)


@dataclass
class OutboxMessage:
    """One email or text to deliver. Written in the same transaction as the decision
    to send it; delivered afterwards by the dispatcher, at least once."""

    id: str = f("Identifier.")
    broker_id: str = f("The broker sending it.", ref="Broker")
    load_id: str = f("The load it is about.", ref="Load")
    chase_id: str = f("The chase it serves.", ref="Chase")
    purpose: OutboxPurpose = f("A request to a carrier, or a packet to the broker.")
    channel: Channel = f("Email or text.")
    idempotency_key: str = f("Unique; the same on every try.")
    from_addr: str = f("Sender address or number.")
    to: list[str] = f("Recipients.")
    created_at_ms: int = f("When it was queued.")
    next_attempt_at_ms: int = f("When it may next be tried.")
    cc: list[str] = f("Copied recipients.", default_factory=list)
    subject: str = f("Email subject.", default="")
    body: str = f("Message text.", default="")
    attachments: list[Attachment] = f("Files attached.", default_factory=list)
    quiet_tz: str = f(
        "Carrier time zone for quiet hours; empty for the broker's own inbox.", default=""
    )
    status: OutboxStatus = f("Delivery state.", default=OutboxStatus.PENDING)
    tries: int = f("Provider calls made.", default=0)
    claim_token: str = f("Which dispatcher holds it while SENDING.", default="")
    lease_until_ms: int = f("When that hold expires.", default=0)
    last_error: str = f("The last provider error.", default="")
    provider_id: str = f("The provider's id for the delivered message.", default="")
    sent_at_ms: int | None = f("When the provider accepted it.", default=None)
    finished_at_ms: int | None = f("When it reached SENT, FAILED or CANCELLED.", default=None)
    deferred_noted: bool = f("A quiet-hours deferral was already recorded.", default=False)


# ---------------------------------------------------------------------- Proof


@dataclass
class Document:
    """A file received for a load."""

    id: str = f("Identifier.")
    load_id: str = f("The load it is filed under.", ref="Load")
    inbound_id: str = f("The message it came in.", ref="Inbound")
    filename: str = f("Name as sent.")
    content_sha256: str = f("SHA-256 of the bytes.")
    received_at_ms: int = f("When it arrived.")
    doc_type: DocType = f("What the reader says it is.", default=DocType.OTHER)
    extraction: dict[str, Any] = f(
        "What the reader read from it.", shape="Extraction", default_factory=dict
    )
    read_at_ms: int | None = f(
        "When the reading was settled: the reader answered, or gave up. Null while unread.",
        default=None,
    )
    reader: str = f("Which reader read it; empty while unread or when none could.", default="")


@dataclass
class Reading:
    """One file to be read. Written in the same transaction as its document; read
    afterwards by the reading desk, outside the engine, with retries."""

    id: str = f("Identifier.")
    broker_id: str = f("The broker the file was sent to.", ref="Broker")
    load_id: str = f("The load the document is filed under.", ref="Load")
    document_id: str = f("The document to read.", ref="Document")
    created_at_ms: int = f("When it was queued.")
    next_attempt_at_ms: int = f("When it may next be tried.")
    status: ReadingStatus = f("Where the reading stands.", default=ReadingStatus.PENDING)
    tries: int = f("Reader calls started.", default=0)
    claim_token: str = f("Which desk holds it while READING.", default="")
    lease_until_ms: int = f("When that hold expires.", default=0)
    last_error: str = f("The last reader error.", default="")
    finished_at_ms: int | None = f("When it reached DONE or FAILED.", default=None)


@dataclass
class Check:
    """The verdict on one document."""

    id: str = f("Identifier.")
    chase_id: str = f("The chase.", ref="Chase")
    document_id: str = f("The document.", ref="Document")
    result: CheckResult = f("Pass, fail, exception or reroute.")
    reasons: list[str] = f("Why, in short codes.")
    checked_at_ms: int = f("When.")
    reroute_load_id: str = f(
        "The load it belongs to instead, for REROUTE.", ref="Load", optional=True, default=""
    )


# -------------------------------------------------------------------- Outcome


@dataclass
class WriteBack:
    """One attempt to file a checked document in the broker's system."""

    id: str = f("Identifier.")
    chase_id: str = f("The chase.", ref="Chase")
    document_id: str = f("The document being filed.", ref="Document")
    adapter: str = f("Which rung of the filing ladder.")
    idempotency_key: str = f("SHA-256 over (adapter, document hash, load).")
    status: WriteBackStatus = f("Pending, succeeded or failed.", default=WriteBackStatus.PENDING)
    attempts: int = f("Adapter calls that returned an answer.", default=0)
    receipt: str = f("The adapter's receipt.", default="")
    detail: str = f("Last error, or the pending reference.", default="")
    completed_at_ms: int | None = f("When it succeeded.", default=None)


@dataclass
class Escalation:
    """A load handed to a person, and how they finished it."""

    id: str = f("Identifier.")
    chase_id: str = f("The chase.", ref="Chase")
    load_id: str = f("The load.", ref="Load")
    broker_id: str = f("The broker.", ref="Broker")
    reason: EscalationReason = f("Why a person is needed.")
    detail: str = f("What happened, in a sentence.")
    created_at_ms: int = f("When.")
    acknowledged_by: str = f(
        "The operator who finished it.", ref="Operator", optional=True, default=""
    )
    acknowledged_at_ms: int | None = f("When they finished it.", default=None)
    resolution: EscalationResolution | None = f("How they finished it.", default=None)
    note: str = f("What they did, in their words.", default="")


ENTITY_TYPES = (
    Broker, Operator, Carrier, Contact, Party, Load, Requirement, Chase, Attempt,
    Inbound, Document, Reading, Check, Escalation, WriteBack, OutboxMessage, Consent,
)  # fmt: skip

# Value types: stored inside an entity, never on their own.
VALUE_TYPES = (BrokerConfig, Attachment)


def sensitive_text(obj: Any) -> list[tuple[str, str]]:
    """(field, value) for every non-empty sensitive text field of ``obj``."""
    out = []
    for fld in dataclasses.fields(obj):
        value = getattr(obj, fld.name)
        if fld.metadata.get("sensitive") and isinstance(value, str) and value.strip():
            out.append((fld.name, value))
    return out
