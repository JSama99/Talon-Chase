"""In-memory store. Copies on the way in and out, so a caller that forgets to
``put`` a changed object loses the change here exactly as it would in SQL."""

from __future__ import annotations

import copy
from typing import Any, TypeVar

from chase.evidence.chain import EvidenceEvent

T = TypeVar("T")


class MemoryStore:
    def __init__(self) -> None:
        self._objects: dict[type, dict[str, Any]] = {}
        self._blobs: dict[str, bytes] = {}
        self._evidence: dict[str, list[EvidenceEvent]] = {}
        self._settings: dict[str, str] = {}

    def put(self, obj: Any) -> None:
        self._objects.setdefault(type(obj), {})[obj.id] = copy.deepcopy(obj)

    def get(self, cls: type[T], obj_id: str) -> T | None:
        found = self._objects.get(cls, {}).get(obj_id)
        return copy.deepcopy(found) if found is not None else None

    def find(self, cls: type[T], **equals: Any) -> list[T]:
        return [
            copy.deepcopy(obj)
            for obj in self._objects.get(cls, {}).values()
            if all(getattr(obj, key) == value for key, value in equals.items())
        ]

    def put_blob(self, sha256: str, content: bytes) -> None:
        self._blobs.setdefault(sha256, content)

    def get_blob(self, sha256: str) -> bytes | None:
        return self._blobs.get(sha256)

    def append_evidence(self, event: EvidenceEvent) -> None:
        chain = self._evidence.setdefault(event.chain_id, [])
        if event.seq != len(chain):
            raise ValueError(f"evidence seq {event.seq} out of order for chain {event.chain_id}")
        chain.append(event)

    def last_evidence(self, chain_id: str) -> EvidenceEvent | None:
        chain = self._evidence.get(chain_id)
        return chain[-1] if chain else None

    def evidence(self, chain_id: str) -> list[EvidenceEvent]:
        return list(self._evidence.get(chain_id, []))

    def get_setting(self, key: str, default: str) -> str:
        return self._settings.get(key, default)

    def set_setting(self, key: str, value: str) -> None:
        self._settings[key] = value
