"""Entities. Plain dataclasses; every time is integer epoch milliseconds."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

HOUR_MS = 3_600_000
MINUTE_MS = 60_000


class ChaseState(StrEnum):
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
    FIRST_EMAIL = "FIRST_EMAIL"
    DRIVER_SMS = "DRIVER_SMS"
    SECOND = "SECOND"
    THIRD = "THIRD"
    ESCALATE = "ESCALATE"
    REASK = "REASK"
    WRITEBACK = "WRITEBACK"
    NONE = "NONE"


class Channel(StrEnum):
    EMAIL = "EMAIL"
    SMS = "SMS"
    UPLOAD = "UPLOAD"


class Role(StrEnum):
    DISPATCHER = "DISPATCHER"
    DRIVER = "DRIVER"


class DocType(StrEnum):
    POD = "POD"
    CARRIER_INVOICE = "CARRIER_INVOICE"
    RATE_CONFIRMATION = "RATE_CONFIRMATION"
    OTHER = "OTHER"


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


class WriteBackStatus(StrEnum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass
class BrokerConfig:
    """Per-broker schedule and policy. Every value can be changed by the broker."""

    no_status_grace_ms: int = 2 * HOUR_MS
    sms_delay_ms: int = 3 * HOUR_MS
    third_attempt_ms: int = 48 * HOUR_MS
    escalate_wait_ms: int = 24 * HOUR_MS
    min_gap_ms: int = 1 * HOUR_MS
    third_min_gap_ms: int = 4 * HOUR_MS
    promise_grace_ms: int = 24 * HOUR_MS
    reask_wait_ms: int = 4 * HOUR_MS
    quiet_start_hour: int = 20
    quiet_end_hour: int = 7
    morning_hour: int = 8
    max_reasks: int = 3
    date_tolerance_ms: int = 24 * HOUR_MS
    confidence_floor: int = 80
    writeback_retry_backoff_ms: list[int] = field(
        default_factory=lambda: [MINUTE_MS, 5 * MINUTE_MS, 15 * MINUTE_MS]
    )
    upload_link_ttl_ms: int = 14 * 24 * HOUR_MS
    send_retry_backoff_ms: list[int] = field(
        default_factory=lambda: [
            MINUTE_MS, 5 * MINUTE_MS, 15 * MINUTE_MS, HOUR_MS, 4 * HOUR_MS
        ]
    )  # fmt: skip


@dataclass
class Broker:
    id: str
    name: str
    chase_mailbox: str
    rep_email: str
    sms_number: str = ""
    adapter: str = "packet_email"
    outbound_enabled: bool = True
    config: BrokerConfig = field(default_factory=BrokerConfig)


@dataclass
class Carrier:
    id: str
    broker_id: str
    name: str
    mc_number: str
    timezone: str = "America/New_York"


@dataclass
class Contact:
    id: str
    carrier_id: str
    role: Role
    name: str = ""
    email: str = ""
    phone: str = ""
    verified: bool = True
    sms_consent: bool = False
    sms_stopped: bool = False
    source: str = "tms"


@dataclass
class Load:
    id: str
    broker_id: str
    carrier_id: str
    load_number: str
    delivery_appt_ms: int
    bol_number: str = ""
    po_number: str = ""
    expected_pages: int = 1
    delivered_at_ms: int | None = None
    contact_ids: list[str] = field(default_factory=list)
    # Sensitive. Stored so the outbound guard can refuse to send them.
    customer_name: str = ""
    consignee_name: str = ""
    consignee_address: str = ""
    pickup_number: str = ""
    rate_cents: int = 0
    ready_to_invoice_at_ms: int | None = None


@dataclass
class Requirement:
    id: str
    load_id: str
    doc_type: DocType = DocType.POD


@dataclass
class Chase:
    id: str
    load_id: str
    broker_id: str
    requirement_id: str
    opened_at_ms: int
    state: ChaseState = ChaseState.OPEN
    next_step: Step = Step.FIRST_EMAIL
    next_action_at_ms: int = 0
    attempts_sent: int = 0
    reasks_sent: int = 0
    plan: dict[str, int] = field(default_factory=dict)
    fail_reasons: list[str] = field(default_factory=list)
    reply_to_contact_id: str = ""
    passed_document_id: str = ""
    writeback_id: str = ""
    closed_at_ms: int | None = None
    closed_reason: str = ""
    kill_noted: bool = False


@dataclass
class Attempt:
    """A request the agent decided to make. Its delivery lives in the outbox message."""

    id: str
    chase_id: str
    step: Step
    channel: Channel
    to: list[str]
    cc: list[str]
    queued_at_ms: int
    outbox_id: str = ""


@dataclass
class Attachment:
    filename: str
    content_sha256: str


@dataclass
class Inbound:
    id: str
    broker_id: str
    channel: Channel
    from_addr: str
    received_at_ms: int
    subject: str = ""
    body: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    external_id: str = ""
    status: InboundStatus = InboundStatus.NEW
    chase_id: str = ""
    classification: str = ""
    hold_reason: str = ""
    trusted: bool = False


@dataclass
class Document:
    id: str
    load_id: str
    inbound_id: str
    filename: str
    content_sha256: str
    received_at_ms: int
    doc_type: DocType = DocType.OTHER
    extraction: dict[str, Any] = field(default_factory=dict)


@dataclass
class Check:
    id: str
    chase_id: str
    document_id: str
    result: CheckResult
    reasons: list[str]
    checked_at_ms: int
    reroute_load_id: str = ""


@dataclass
class Escalation:
    id: str
    chase_id: str
    load_id: str
    broker_id: str
    reason: EscalationReason
    detail: str
    created_at_ms: int
    acknowledged_by: str = ""
    acknowledged_at_ms: int | None = None
    note: str = ""


@dataclass
class WriteBack:
    id: str
    chase_id: str
    document_id: str
    adapter: str
    idempotency_key: str
    status: WriteBackStatus = WriteBackStatus.PENDING
    attempts: int = 0
    receipt: str = ""
    detail: str = ""
    completed_at_ms: int | None = None


@dataclass
class OutboxMessage:
    """One email or text to deliver. Written in the same transaction as the decision
    to send it; delivered afterwards by the dispatcher, at least once."""

    id: str
    broker_id: str
    load_id: str
    chase_id: str
    purpose: OutboxPurpose
    channel: Channel
    idempotency_key: str
    from_addr: str
    to: list[str]
    created_at_ms: int
    next_attempt_at_ms: int
    cc: list[str] = field(default_factory=list)
    subject: str = ""
    body: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    quiet_tz: str = ""  # carrier time zone; empty means no quiet hours (broker's own inbox)
    status: OutboxStatus = OutboxStatus.PENDING
    tries: int = 0
    claim_token: str = ""
    lease_until_ms: int = 0
    last_error: str = ""
    provider_id: str = ""
    sent_at_ms: int | None = None
    finished_at_ms: int | None = None
    deferred_noted: bool = False


ENTITY_TYPES = (
    Broker, Carrier, Contact, Load, Requirement, Chase, Attempt, Inbound,
    Document, Check, Escalation, WriteBack, OutboxMessage,
)  # fmt: skip
