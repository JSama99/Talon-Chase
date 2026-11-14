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


@dataclass(frozen=True)
class SmsMessage:
    from_number: str
    to: str
    body: str


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


class DocumentReader(Protocol):
    def read(self, filename: str, content: bytes) -> Extraction: ...


class AdapterError(Exception):
    def __init__(self, message: str, transient: bool = True) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True)
class WriteBackRequest:
    idempotency_key: str
    broker_id: str
    load_id: str
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
