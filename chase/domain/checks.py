"""Document checks. Deterministic rules over what a reader extracted from the file.

The reader (a language model in production, a stand-in here) only reports what is
on the page. Whether the document is acceptable is decided by this code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from chase.domain.models import BrokerConfig, CheckResult, DocType, Load, f


@dataclass(frozen=True)
class Extraction:
    """What the reader saw on a document."""

    doc_type: DocType = f("What kind of document it is.", default=DocType.OTHER)
    readable: bool = f("Whether the page could be read at all.", default=False)
    reference_numbers: list[str] = f(
        "Load, BOL and PO numbers printed on it.", default_factory=list
    )
    signature_present: bool = f("Whether the receiver signed it.", default=False)
    delivery_date_ms: int | None = f("Delivery date written on it.", default=None)
    page_count: int = f("Pages received.", default=0)
    exception_notes: list[str] = f(
        "Shortage, damage or refusal noted by the receiver.", default_factory=list
    )
    confidence: int = f("How sure the reader is, 0 to 100.", default=0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_type": self.doc_type.value,
            "readable": self.readable,
            "reference_numbers": list(self.reference_numbers),
            "signature_present": self.signature_present,
            "delivery_date_ms": self.delivery_date_ms,
            "page_count": self.page_count,
            "exception_notes": list(self.exception_notes),
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Extraction:
        date = data.get("delivery_date_ms")
        if date is not None and (isinstance(date, bool) or not isinstance(date, int)):
            raise ValueError("delivery_date_ms must be an integer or null")
        return cls(
            doc_type=DocType(data.get("doc_type", "OTHER")),
            readable=bool(data.get("readable", False)),
            reference_numbers=[str(r) for r in data.get("reference_numbers", [])],
            signature_present=bool(data.get("signature_present", False)),
            delivery_date_ms=date,
            page_count=int(data.get("page_count", 0)),
            exception_notes=[str(n) for n in data.get("exception_notes", [])],
            confidence=int(data.get("confidence", 0)),
        )


UNREADABLE = Extraction(doc_type=DocType.OTHER, readable=False, confidence=100)


@dataclass(frozen=True)
class CheckOutcome:
    result: CheckResult
    reasons: list[str]
    reroute_load_id: str = ""


def normalize_reference(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def load_references(load: Load) -> set[str]:
    refs = {normalize_reference(v) for v in (load.load_number, load.bol_number, load.po_number)}
    refs.discard("")
    return refs


def check_document(
    extraction: Extraction, load: Load, other_open_loads: list[Load], cfg: BrokerConfig
) -> CheckOutcome:
    """Decide what to do with one document sent for ``load``.

    ``other_open_loads`` are loads of the same broker and carrier that still need a
    POD; a document that matches one of them is re-routed there.
    """
    if extraction.confidence < cfg.confidence_floor:
        return CheckOutcome(CheckResult.EXCEPTION, ["low_confidence"])
    if not extraction.readable:
        return CheckOutcome(CheckResult.FAIL, ["unreadable"])
    if extraction.doc_type is not DocType.POD:
        return CheckOutcome(CheckResult.FAIL, ["not_a_pod"])

    found = {normalize_reference(r) for r in extraction.reference_numbers}
    found.discard("")
    if not (found & load_references(load)):
        for other in other_open_loads:
            if other.id != load.id and found & load_references(other):
                return CheckOutcome(CheckResult.REROUTE, ["rerouted"], other.id)
        return CheckOutcome(CheckResult.FAIL, ["wrong_load"])

    # A noted shortage, damage, refusal or seal problem always goes to a person,
    # whatever else is right or wrong with the document.
    if extraction.exception_notes:
        return CheckOutcome(CheckResult.EXCEPTION, ["exception_notes"])

    reasons: list[str] = []
    if not extraction.signature_present:
        reasons.append("unsigned")
    if extraction.delivery_date_ms is None:
        reasons.append("no_delivery_date")
    if extraction.page_count < load.expected_pages:
        reasons.append("missing_pages")
    if reasons:
        return CheckOutcome(CheckResult.FAIL, reasons)

    assert extraction.delivery_date_ms is not None
    if abs(extraction.delivery_date_ms - load.delivery_appt_ms) > cfg.date_tolerance_ms:
        return CheckOutcome(CheckResult.EXCEPTION, ["date_mismatch"])
    return CheckOutcome(CheckResult.PASS, [])
