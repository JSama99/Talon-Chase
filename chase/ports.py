"""Everything the engine needs from the outside world, as interfaces.

Version 1 ships in-memory and rule-based implementations only. Real mailboxes,
text lines, language models and TMS connections plug in behind these.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from chase.domain.checks import Extraction
from chase.domain.models import Inbound
from chase.evidence.chain import EvidenceEvent

T = TypeVar("T")


@dataclass(frozen=True)
class OutgoingAttachment:
    filename: str
    content_sha256: str


@dataclass(frozen=True)
class EmailMessage:
    from_addr: str
    to: list[str]
    subject: str
    body: str
    cc: list[str] = field(default_factory=list)
    attachments: list[OutgoingAttachment] = field(default_factory=list)
    # The same for every try of one message. Pass it to the provider as its
    # idempotency key, so a retry after a lost response cannot send twice.
    idempotency_key: str = ""
    # Stable Message-ID header, so a repeated delivery is recognisably the same email.
    message_id: str = ""


@dataclass(frozen=True)
class SmsMessage:
    from_number: str
    to: str
    body: str
    idempotency_key: str = ""


class PermanentSendError(Exception):
    """The provider refused the message for good (bad address, blocked number).
    Any other exception from ``send`` is treated as temporary and retried."""


class EmailPort(Protocol):
    def send(self, message: EmailMessage) -> str: ...


class SmsPort(Protocol):
    def send(self, message: SmsMessage) -> str: ...


class ReplyLabel:
    HAS_DOCUMENT = "HAS_DOCUMENT"
    PROMISE = "PROMISE"
    QUESTION = "QUESTION"
    WRONG_PERSON = "WRONG_PERSON"
    OUT_OF_OFFICE = "OUT_OF_OFFICE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Classification:
    label: str
    confidence: int  # 0 to 100


class ReplyClassifier(Protocol):
    def classify(self, inbound: Inbound) -> Classification: ...


class ReaderError(Exception):
    """The reader could not answer. ``transient`` (an outage, a rate limit, a timeout)
    is tried again later; anything else is given up on at once. Either way the
    document ends with a person, never back with the carrier."""

    def __init__(self, message: str, transient: bool = True) -> None:
        super().__init__(message)
        self.transient = transient


class DocumentReader(Protocol):
    """Reports what is on a document. It never decides whether the document is good.

    ``name`` goes into the evidence, so it should say which reader, model and prompt
    answered. ``inline`` is True only for a reader that is a pure function of the
    bytes (the stand-in): the engine then calls it on the spot. Any reader that
    talks to a service is called by the reading desk instead, outside the engine's
    transaction, and may raise ``ReaderError``.
    """

    name: str
    inline: bool

    def read(self, filename: str, content: bytes) -> Extraction: ...


class AdapterError(Exception):
    def __init__(self, message: str, transient: bool = True) -> None:
        super().__init__(message)
        self.transient = transient


class AdapterPending(Exception):
    """The filing is under way but not confirmed yet (for example, the packet email
    is queued). Not a failure: the engine checks again later."""

    def __init__(self, reference: str) -> None:
        super().__init__(f"filing pending: {reference}")
        self.reference = reference


@dataclass(frozen=True)
class WriteBackRequest:
    idempotency_key: str
    broker_id: str
    load_id: str
    chase_id: str
    load_number: str
    document_sha256: str
    filename: str
    delivered_at_ms: int | None


class TmsAdapter(Protocol):
    name: str

    def file(self, request: WriteBackRequest) -> str:
        """Attach the document and mark the load. Must be safe to call twice with the
        same idempotency key. Returns a receipt string; raises AdapterError."""
        ...


class Store(Protocol):
    def transaction(self) -> Any: ...
    def put(self, obj: Any) -> None: ...
    def get(self, cls: type[T], obj_id: str) -> T | None: ...
    def find(self, cls: type[T], **equals: Any) -> list[T]: ...
    def put_blob(self, sha256: str, content: bytes) -> None: ...
    def get_blob(self, sha256: str) -> bytes | None: ...
    def append_evidence(self, event: EvidenceEvent) -> None: ...
    def last_evidence(self, chain_id: str) -> EvidenceEvent | None: ...
    def evidence(self, chain_id: str) -> list[EvidenceEvent]: ...
    def get_setting(self, key: str, default: str) -> str: ...
    def set_setting(self, key: str, value: str) -> None: ...
