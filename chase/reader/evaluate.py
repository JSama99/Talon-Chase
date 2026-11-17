"""Measure a reader against documents a person has labelled.

    python -m chase.reader.evaluate <folder> [--floor 80] [--json report.json]

``<folder>`` holds the documents and a ``labels.json`` saying, for each file name,
what a careful person sees on it:

    {
      "pod-0001.jpg": {
        "doc_type": "POD",
        "readable": true,
        "reference_numbers": ["TC-104417", "BOL5591207"],
        "signature_present": true,
        "delivery_date": "2026-11-14",
        "exception_notes": ["2 PLT crushed"],
        "page_count": 1
      }
    }

``reference_numbers`` lists the load's own numbers, the load number first.
``delivery_date`` is "" when none is written. ``needs_person: true`` marks a
document no reader should settle alone, whatever is on it. ``read_on`` (a date) is
the day the document would have been read; it matters only for a date written
without a year, which the reader takes as the one nearest to that day. Left out, it
is today.

The score is not how many fields match. It is what would have happened to the
document: each one is routed twice through the real checks (``check_document``),
once from the label and once from the reader's answer, and the two routes are
compared. The numbers that matter are the first two lines of the report:

* filed when it should not have been (must be zero);
* damage, shortage or refusal notes that did not reach a person (must be zero).

Exits 1 when either is above zero, so it can gate a model or prompt change.
Needs ANTHROPIC_API_KEY (and optionally CHASE_READER_MODEL), read the same way the
service reads them. Each document costs one model call.
"""

from __future__ import annotations

import argparse
import calendar
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from chase.domain.checks import Extraction, check_document
from chase.domain.models import BrokerConfig, CheckResult, DocType, Load
from chase.ports import DocumentReader, ReaderError

FILE, REASK, PERSON = "file", "ask again", "person"
_ROUTE = {CheckResult.PASS: FILE, CheckResult.FAIL: REASK, CheckResult.EXCEPTION: PERSON}


@dataclass
class Row:
    name: str
    wanted: str  # what a careful person's reading leads to
    got: str  # what the reader's reading leads to
    reasons: list[str]
    confidence: int
    doubts: list[str]
    notes_wanted: int
    notes_reached_a_person: bool
    fields_wrong: list[str]
    error: str = ""

    @property
    def verdict(self) -> str:
        if self.error:
            return "error"
        if self.got == self.wanted:
            return "right"
        if self.got == FILE:
            return "unsafe"
        if self.got == REASK:
            return "wrong ask"
        return "extra work"


@dataclass
class Report:
    reader: str
    floor: int
    rows: list[Row] = field(default_factory=list)

    def count(self, verdict: str) -> int:
        return sum(1 for row in self.rows if row.verdict == verdict)

    @property
    def notes_missed(self) -> int:
        return sum(1 for r in self.rows if r.notes_wanted and not r.notes_reached_a_person)

    @property
    def with_notes(self) -> int:
        return sum(1 for r in self.rows if r.notes_wanted)

    @property
    def safe(self) -> bool:
        return self.count("unsafe") == 0 and self.notes_missed == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "reader": self.reader,
            "confidence_floor": self.floor,
            "documents": len(self.rows),
            "right": self.count("right"),
            "extra_work": self.count("extra work"),
            "wrong_ask": self.count("wrong ask"),
            "unsafe": self.count("unsafe"),
            "errors": self.count("error"),
            "notes_missed": self.notes_missed,
            "documents_with_notes": self.with_notes,
            "safe": self.safe,
            "rows": [asdict(row) | {"verdict": row.verdict} for row in self.rows],
        }


def _noon(day: str) -> int | None:
    if not day:
        return None
    written = date.fromisoformat(day)
    return calendar.timegm((written.year, written.month, written.day, 12, 0, 0)) * 1000


def _truth(label: dict[str, Any]) -> Extraction:
    return Extraction(
        doc_type=DocType(label.get("doc_type", "POD")),
        readable=bool(label.get("readable", True)),
        reference_numbers=[str(r) for r in label.get("reference_numbers", [])],
        signature_present=bool(label.get("signature_present", False)),
        delivery_date_ms=_noon(label.get("delivery_date", "")),
        page_count=int(label.get("page_count", 1)),
        exception_notes=[str(n) for n in label.get("exception_notes", [])],
        confidence=100,
    )


def _load(label: dict[str, Any], now_ms: int) -> Load:
    """The load this document was sent for, as far as the label tells."""
    numbers = [str(r) for r in label.get("reference_numbers", [])] + ["", "", ""]
    return Load(
        id="load-eval",
        broker_id="brk-eval",
        carrier_id="car-eval",
        load_number=str(label.get("load") or numbers[0] or "NOT-ON-THIS-DOCUMENT"),
        delivery_appt_ms=_noon(label.get("delivery_date", "")) or now_ms,
        bol_number=numbers[1],
        po_number=numbers[2],
        expected_pages=int(label.get("page_count", 1)),
    )


def _fields_wrong(truth: Extraction, read: Extraction) -> list[str]:
    wrong = []
    if read.doc_type is not truth.doc_type:
        wrong.append("doc_type")
    if read.readable != truth.readable:
        wrong.append("readable")
    if not truth.readable:
        return wrong  # nothing else can be expected of an unreadable page
    have = {r.upper().replace(" ", "") for r in read.reference_numbers}
    if any(r.upper().replace(" ", "") not in have for r in truth.reference_numbers):
        wrong.append("reference_numbers")
    if read.signature_present != truth.signature_present:
        wrong.append("signature_present")
    if read.delivery_date_ms != truth.delivery_date_ms:
        wrong.append("delivery_date")
    if bool(read.exception_notes) != bool(truth.exception_notes):
        wrong.append("exception_notes")
    if read.page_count != truth.page_count:
        wrong.append("page_count")
    return wrong


def score(name: str, label: dict[str, Any], read: Extraction, floor: int, now_ms: int) -> Row:
    """Route one document from its label and from the reader's answer, and compare."""
    cfg = BrokerConfig(confidence_floor=floor)
    load = _load(label, now_ms)
    truth = _truth(label)
    wanted = (
        PERSON if label.get("needs_person") else _ROUTE[check_document(truth, load, [], cfg).result]
    )
    outcome = check_document(read, load, [], cfg)
    got = _ROUTE[outcome.result]
    return Row(
        name=name,
        wanted=wanted,
        got=got,
        reasons=list(outcome.reasons),
        confidence=read.confidence,
        doubts=list(read.doubts),
        notes_wanted=len(truth.exception_notes),
        notes_reached_a_person=got == PERSON,
        fields_wrong=_fields_wrong(truth, read),
    )


class DayClock:
    """The clock the reader is given here: set to each document's ``read_on`` day."""

    def __init__(self, now_ms: int) -> None:
        self.real_now_ms = self.ms = now_ms

    def now_ms(self) -> int:
        return self.ms


def evaluate(folder: Path, reader: DocumentReader, clock: DayClock, floor: int = 80) -> Report:
    labels = json.loads((folder / "labels.json").read_text())
    report = Report(reader=reader.name, floor=floor)
    for name in sorted(labels):
        label = labels[name]
        clock.ms = now_ms = _noon(label.get("read_on", "")) or clock.real_now_ms
        try:
            read = reader.read(name, (folder / name).read_bytes())
        except ReaderError as exc:
            # In the service this is retried, then handed to a person: not unsafe.
            report.rows.append(
                Row(
                    name,
                    "",
                    PERSON,
                    [],
                    0,
                    [],
                    len(label.get("exception_notes", [])),
                    True,
                    [],
                    error=str(exc),
                )  # fmt: skip
            )
            continue
        report.rows.append(score(name, label, read, floor, now_ms))
    return report


def render(report: Report) -> str:
    total = len(report.rows)
    lines = [
        f"{total} documents read by {report.reader}, confidence floor {report.floor}",
        "",
        f"  filed when it should not have been        {report.count('unsafe'):>4}   must be 0",
        f"  notes that did not reach a person         {report.notes_missed:>4}   must be 0"
        f"   (of {report.with_notes} documents with a note)",
        f"  carrier asked again for no reason         {report.count('wrong ask'):>4}",
        f"  handed to a person, could have been done  {report.count('extra work'):>4}",
        f"  handled as a careful person would         {report.count('right'):>4}",
        f"  reader did not answer                     {report.count('error'):>4}",
        "",
    ]
    width = max([len(r.name) for r in report.rows] + [8])
    lines.append(f"  {'document':<{width}}  {'verdict':<10}  {'wanted':<9}  {'got':<9}  detail")
    for row in report.rows:
        detail = row.error or ", ".join(row.reasons + [f"unsure: {d}" for d in row.doubts])
        if row.fields_wrong and not row.error:
            detail = (detail + "; " if detail else "") + "misread: " + ", ".join(row.fields_wrong)
        lines.append(
            f"  {row.name:<{width}}  {row.verdict:<10}  {row.wanted:<9}  {row.got:<9}  {detail}"
        )
    lines += ["", "SAFE" if report.safe else "NOT SAFE: do not use this reader as it is"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m chase.reader.evaluate",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("folder", type=Path)
    parser.add_argument("--floor", type=int, default=BrokerConfig().confidence_floor)
    parser.add_argument("--json", type=Path, help="also write the full report here")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if not (args.folder / "labels.json").is_file():
        print(f"{args.folder}/labels.json not found")
        return 2

    from chase.api.main import document_reader
    from chase.domain.clock import SystemClock

    clock = DayClock(SystemClock().now_ms())
    try:
        reader = document_reader(clock)
    except RuntimeError as exc:
        print(exc)
        return 2
    if getattr(reader, "inline", False):
        print("ANTHROPIC_API_KEY is not set: there is no real reader to measure")
        return 2
    report = evaluate(args.folder, reader, clock, args.floor)
    print(render(report))
    if args.json:
        args.json.write_text(json.dumps(report.to_dict(), indent=2) + "\n")
    return 0 if report.safe else 1


if __name__ == "__main__":
    raise SystemExit(main())
