"""The reading desk: where files are read, outside the engine.

The engine never calls a reader that talks to a service. When a file arrives it
writes a ``Reading`` in the same database transaction as the document, and the chase
waits in RECEIVED. ``ReadingDesk`` does the reading afterwards, the same way the
outbox dispatcher does the sending:

1. **Claim** (one short transaction). Take the next due reading, mark it READING
   with a lease, and load the file.
2. **Read** (no transaction, no lock held). Call the reader. This is the slow part:
   a model can take half a minute on a long PDF.
3. **Complete** (one short transaction). Hand the answer to the engine, which writes
   it on the document and, once every file of the message has been read, checks them
   and moves the chase on. A temporary failure is tried again after a wait; when the
   waits run out, or the failure is for good, the document goes to a person unread.

A reading is settled once. If a desk dies between steps 2 and 3, the lease expires
and the file is read again; the first answer to be recorded stands and a later one
is dropped, so the evidence never holds two readings of one document.

Reading is not outbound. The kill switches stop messages and filings, not this: a
file that arrived is still read while the switch is off, and what follows from the
reading (a re-ask, a filing) is what the switch holds.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from chase.domain.checks import Extraction
from chase.domain.models import MINUTE_MS, Document, Reading, ReadingStatus
from chase.ports import DocumentReader, ReaderError

if TYPE_CHECKING:
    from chase.engine import ChaseEngine

# Long enough for the slowest reading; a desk that died is noticed after this.
DEFAULT_LEASE_MS = 10 * MINUTE_MS
DEFAULT_BATCH = 20
# Waits between tries. After the last one fails, a person gets the document: about
# an hour and twenty minutes into an outage.
DEFAULT_BACKOFF_MS = (MINUTE_MS, 5 * MINUTE_MS, 15 * MINUTE_MS, 60 * MINUTE_MS)
MAX_ERROR_CHARS = 300


@dataclass
class ReadReport:
    read: int = 0
    retried: int = 0
    failed: int = 0
    duplicates: int = 0

    @property
    def changed(self) -> int:
        return self.read + self.retried + self.failed

    def to_dict(self) -> dict[str, int]:
        return {
            "read": self.read,
            "retried": self.retried,
            "failed": self.failed,
            "duplicates": self.duplicates,
        }


@dataclass(frozen=True)
class Claimed:
    reading: Reading
    filename: str
    content: bytes


@dataclass(frozen=True)
class _Outcome:
    extraction: Extraction | None = None
    error: str = ""
    permanent: bool = False


class ReadingDesk:
    """Reads queued files. Safe to run in several processes at once on Postgres:
    every claim and completion takes the store's transaction lock."""

    def __init__(
        self,
        engine: ChaseEngine,
        reader: DocumentReader | None = None,
        *,
        guard: Callable[[], contextlib.AbstractContextManager[Any]] | None = None,
        lease_ms: int = DEFAULT_LEASE_MS,
        batch: int = DEFAULT_BATCH,
        backoff_ms: Sequence[int] = DEFAULT_BACKOFF_MS,
    ) -> None:
        self.engine = engine
        self.reader = reader or engine.reader
        self.store = engine.store
        self.clock = engine.clock
        self.guard = guard or engine.store.transaction
        self.lease_ms = lease_ms
        self.batch = batch
        self.backoff_ms = tuple(backoff_ms)

    # ------------------------------------------------------------------ public

    def work(self) -> ReadReport:
        """Read up to ``batch`` due files, claiming each one just before reading it."""
        report = ReadReport()
        for _ in range(self.batch):
            claimed = self.claim(report, limit=1)
            if not claimed:
                break
            outcome = self._read(claimed[0])
            with self.guard():
                self.complete(claimed[0].reading, outcome, report)
        return report

    def claim(self, report: ReadReport | None = None, limit: int | None = None) -> list[Claimed]:
        """Step 1. Returns the readings this call now holds a lease on, with their files."""
        report = report or ReadReport()
        limit = self.batch if limit is None else limit
        claimed: list[Claimed] = []
        with self.guard():
            now = self.clock.now_ms()
            for reading in self._due(now):
                if len(claimed) >= limit:
                    break
                document = self.store.get(Document, reading.document_id)
                if document is None or document.read_at_ms is not None:
                    # Settled another way (a second desk answered first).
                    self._close(reading, ReadingStatus.DONE, now)
                    continue
                if reading.tries > len(self.backoff_ms):
                    # Every try was started and none was completed: the desk kept
                    # dying on this file. Stop, and let a person look at it.
                    self._give_up(reading, "the reader never answered", False, now, report)
                    continue
                content = self.store.get_blob(document.content_sha256)
                if content is None:
                    self._give_up(reading, "the file is missing from storage", True, now, report)
                    continue
                reading.status = ReadingStatus.READING
                reading.tries += 1
                reading.claim_token = self.engine.ids.new("claim")
                reading.lease_until_ms = now + self.lease_ms
                self.store.put(reading)
                claimed.append(Claimed(reading, document.filename, content))
        return claimed

    def complete(
        self, reading: Reading, outcome: _Outcome, report: ReadReport | None = None
    ) -> None:
        """Step 3. Must run inside ``self.guard()``."""
        report = report or ReadReport()
        now = self.clock.now_ms()
        current = self.store.get(Reading, reading.id)
        if current is None:
            return
        if current.status in (ReadingStatus.DONE, ReadingStatus.FAILED):
            report.duplicates += 1  # our lease ran out and another desk settled it
            return
        if outcome.extraction is not None:
            # An answer is an answer, whoever holds the lease now: the first one stands.
            self._close(current, ReadingStatus.DONE, now)
            self.engine.reading_done(current.document_id, outcome.extraction, self.reader.name)
            report.read += 1
            return
        if (
            current.status is not ReadingStatus.READING
            or current.claim_token != reading.claim_token
        ):
            return  # someone else holds it now; their result stands
        current.last_error = outcome.error
        current.claim_token = ""
        if not outcome.permanent and current.tries <= len(self.backoff_ms):
            current.status = ReadingStatus.PENDING
            current.next_attempt_at_ms = now + self.backoff_ms[current.tries - 1]
            self.store.put(current)
            self.engine._record(
                current.load_id,
                "reading.retry",
                {
                    "reading_id": current.id,
                    "document_id": current.document_id,
                    "try": current.tries,
                    "error": outcome.error,
                    "next_attempt_at_ms": current.next_attempt_at_ms,
                },
            )
            report.retried += 1
            return
        self._give_up(current, outcome.error, outcome.permanent, now, report)

    # ---------------------------------------------------------------- internals

    def _due(self, now: int) -> list[Reading]:
        pending = [
            r
            for r in self.store.find(Reading, status=ReadingStatus.PENDING)
            if r.next_attempt_at_ms <= now
        ]
        expired = [
            r
            for r in self.store.find(Reading, status=ReadingStatus.READING)
            if r.lease_until_ms <= now
        ]
        return sorted(pending + expired, key=lambda r: (r.next_attempt_at_ms, r.id))

    def _close(self, reading: Reading, status: ReadingStatus, now: int) -> None:
        reading.status = status
        reading.claim_token = ""
        reading.finished_at_ms = now
        self.store.put(reading)

    def _give_up(
        self, reading: Reading, error: str, permanent: bool, now: int, report: ReadReport
    ) -> None:
        reading.last_error = error
        self._close(reading, ReadingStatus.FAILED, now)
        self.engine._record(
            reading.load_id,
            "reading.failed",
            {
                "reading_id": reading.id,
                "document_id": reading.document_id,
                "tries": reading.tries,
                "permanent": permanent,
                "error": error,
            },
        )
        why = "the reader could not take this file" if permanent else "the reader was unavailable"
        self.engine.reading_abandoned(reading.document_id, why)
        report.failed += 1

    def _read(self, claimed: Claimed) -> _Outcome:
        """Step 2. Talks to the reader; holds no lock and no transaction."""
        try:
            extraction = self.reader.read(claimed.filename, claimed.content)
        except ReaderError as exc:
            return _Outcome(error=_short(str(exc) or "refused"), permanent=not exc.transient)
        except Exception as exc:  # anything else is treated as temporary
            return _Outcome(error=_short(f"{type(exc).__name__}: {exc}"))
        return _Outcome(extraction=extraction)


def _short(text: str) -> str:
    """One line, bounded: provider errors can be long, and this goes into evidence."""
    text = " ".join(text.split())
    return text if len(text) <= MAX_ERROR_CHARS else text[: MAX_ERROR_CHARS - 1] + "…"
