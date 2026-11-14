"""Time and identifiers are injected so the engine is deterministic under test."""

from __future__ import annotations

import time
import uuid
from typing import Protocol


class Clock(Protocol):
    def now_ms(self) -> int: ...


class SystemClock:
    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000


class FakeClock:
    def __init__(self, start_ms: int) -> None:
        self._now = start_ms

    def now_ms(self) -> int:
        return self._now

    def set(self, ms: int) -> None:
        if ms < self._now:
            raise ValueError("the fake clock does not go backwards")
        self._now = ms

    def advance(self, ms: int) -> None:
        self.set(self._now + ms)


class IdGen(Protocol):
    def new(self, prefix: str) -> str: ...


class SequentialIds:
    """Predictable ids (att-000001). Used in tests and the synthetic pack."""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}

    def new(self, prefix: str) -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}-{self._counters[prefix]:06d}"


class UuidIds:
    def new(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex}"
