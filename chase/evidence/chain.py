"""Hash-chained evidence events.

Each chain belongs to one load. An event's hash covers its own header and the
hash of the event before it, so changing, removing or reordering any event
breaks every hash after it.

    payload_digest = SHA-256(canonical_json(payload))
    header         = {"at_ms", "chain_id", "kind", "payload_digest", "seq"}
    hash           = SHA-256(canonical_json(header) + prev_hash as ASCII hex)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from chase.evidence.canonical import canonical_json, sha256_hex

GENESIS_HASH = "0" * 64


@dataclass(frozen=True)
class EvidenceEvent:
    chain_id: str
    seq: int
    kind: str
    at_ms: int
    payload: dict[str, Any]
    payload_digest: str
    prev_hash: str
    hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "seq": self.seq,
            "kind": self.kind,
            "at_ms": self.at_ms,
            "payload": self.payload,
            "payload_digest": self.payload_digest,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvidenceEvent:
        return cls(
            chain_id=data["chain_id"],
            seq=data["seq"],
            kind=data["kind"],
            at_ms=data["at_ms"],
            payload=data["payload"],
            payload_digest=data["payload_digest"],
            prev_hash=data["prev_hash"],
            hash=data["hash"],
        )


def _header(chain_id: str, seq: int, kind: str, at_ms: int, payload_digest: str) -> dict[str, Any]:
    return {
        "at_ms": at_ms,
        "chain_id": chain_id,
        "kind": kind,
        "payload_digest": payload_digest,
        "seq": seq,
    }


def compute_hash(
    chain_id: str, seq: int, kind: str, at_ms: int, payload_digest: str, prev_hash: str
) -> str:
    header = canonical_json(_header(chain_id, seq, kind, at_ms, payload_digest))
    return sha256_hex(header + prev_hash.encode("ascii"))


def make_event(
    chain_id: str,
    kind: str,
    at_ms: int,
    payload: dict[str, Any],
    previous: EvidenceEvent | None,
) -> EvidenceEvent:
    """Build the next event of a chain. ``previous`` is None for the first event."""
    if previous is not None and previous.chain_id != chain_id:
        raise ValueError("previous event belongs to a different chain")
    seq = 0 if previous is None else previous.seq + 1
    prev_hash = GENESIS_HASH if previous is None else previous.hash
    payload_digest = sha256_hex(canonical_json(payload))
    return EvidenceEvent(
        chain_id=chain_id,
        seq=seq,
        kind=kind,
        at_ms=at_ms,
        payload=payload,
        payload_digest=payload_digest,
        prev_hash=prev_hash,
        hash=compute_hash(chain_id, seq, kind, at_ms, payload_digest, prev_hash),
    )


@dataclass(frozen=True)
class ChainVerification:
    ok: bool
    length: int
    head_hash: str
    failed_index: int | None = None
    reason: str | None = None


def verify_chain(events: list[EvidenceEvent]) -> ChainVerification:
    """Check every event of one chain. Reports the first index that fails."""
    prev_hash = GENESIS_HASH
    chain_id = events[0].chain_id if events else ""

    def fail(index: int, reason: str) -> ChainVerification:
        return ChainVerification(False, len(events), prev_hash, index, reason)

    for index, event in enumerate(events):
        if event.chain_id != chain_id:
            return fail(index, "chain_id differs from the first event")
        if event.seq != index:
            return fail(index, f"seq is {event.seq}, expected {index}")
        if event.prev_hash != prev_hash:
            return fail(index, "prev_hash does not match the previous event's hash")
        try:
            digest = sha256_hex(canonical_json(event.payload))
        except ValueError as exc:
            return fail(index, f"payload is not canonical: {exc}")
        if digest != event.payload_digest:
            return fail(index, "payload does not match payload_digest")
        expected = compute_hash(
            event.chain_id, event.seq, event.kind, event.at_ms, event.payload_digest, prev_hash
        )
        if expected != event.hash:
            return fail(index, "hash does not match the event header")
        prev_hash = event.hash
    return ChainVerification(True, len(events), prev_hash)
