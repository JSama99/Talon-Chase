"""The chase state machine. Every legal transition is listed here; nothing else is allowed."""

from __future__ import annotations

from chase.domain.models import Chase, ChaseState

S = ChaseState

ALLOWED: dict[ChaseState, frozenset[ChaseState]] = {
    # OPEN -> RECEIVED covers a carrier who sends paperwork before being asked.
    S.OPEN: frozenset({S.ASKED, S.RECEIVED, S.ESCALATED}),
    S.ASKED: frozenset({S.RECEIVED, S.ESCALATED}),
    S.RECEIVED: frozenset({S.CHECKING, S.ESCALATED}),
    S.CHECKING: frozenset({S.PASSED, S.FAILED_CHECK, S.ESCALATED}),
    # FAILED_CHECK -> RECEIVED: a corrected document arrives before the re-ask is sent
    # (quiet hours, kill switch, provider down).
    S.FAILED_CHECK: frozenset({S.ASKED, S.RECEIVED, S.ESCALATED}),
    S.PASSED: frozenset({S.FILED, S.ESCALATED}),
    S.FILED: frozenset({S.CLOSED}),
    # Only a person's acknowledgement closes an escalated chase.
    S.ESCALATED: frozenset({S.CLOSED}),
    S.CLOSED: frozenset(),
}

ACTIVE_STATES = frozenset(s for s in S if s not in (S.CLOSED, S.ESCALATED))
ACCEPTS_DOCUMENTS = frozenset({S.OPEN, S.ASKED, S.FAILED_CHECK})


class IllegalTransition(Exception):
    def __init__(self, chase_id: str, current: ChaseState, new: ChaseState) -> None:
        super().__init__(f"chase {chase_id}: {current} -> {new} is not allowed")
        self.current = current
        self.new = new


def can_transition(current: ChaseState, new: ChaseState) -> bool:
    return new in ALLOWED[current]


def transition(chase: Chase, new: ChaseState) -> tuple[ChaseState, ChaseState]:
    """Move the chase to ``new`` or raise. Returns (old, new)."""
    old = chase.state
    if not can_transition(old, new):
        raise IllegalTransition(chase.id, old, new)
    chase.state = new
    return old, new
