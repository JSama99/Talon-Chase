"""Plain words for the broker screens: reasons, check results, times and phones.

Every code the engine produces has wording here; tests fail if one is missing.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from chase.domain.models import (
    HOUR_MS,
    MINUTE_MS,
    ChaseState,
    EscalationReason,
    EscalationResolution,
    RequirementStatus,
)

DAY_MS = 24 * HOUR_MS

REASON_TITLES: dict[EscalationReason, str] = {
    EscalationReason.NO_RESPONSE: "No reply after 3 requests",
    EscalationReason.RISK: "Held: mentions payment details, or unknown sender",
    EscalationReason.UNKNOWN_SENDER: "New address at this carrier. Approve it?",
    EscalationReason.DOCUMENT_EXCEPTION: "POD has a note: shortage, damage, refusal or date",
    EscalationReason.LOW_CONFIDENCE: "Could not read the POD reliably",
    EscalationReason.NEEDS_HUMAN: "Carrier asked a question",
    EscalationReason.WRONG_PERSON: "Contact says they are the wrong person",
    EscalationReason.WRITEBACK_FAILED: "Could not file the POD",
    EscalationReason.NO_CONTACT: "No dispatcher email for this carrier",
    EscalationReason.REASK_LIMIT: "Carrier keeps sending a document that fails",
    EscalationReason.OUTBOUND_BLOCKED: "A message was stopped before sending",
    EscalationReason.SEND_FAILED: "Could not deliver a message to the carrier",
}

# Shorter, for the badge on the load screen ("Needs you: ...").
REASON_BADGES: dict[EscalationReason, str] = {
    EscalationReason.NO_RESPONSE: "no reply",
    EscalationReason.RISK: "message held",
    EscalationReason.UNKNOWN_SENDER: "new address",
    EscalationReason.DOCUMENT_EXCEPTION: "POD has a note",
    EscalationReason.LOW_CONFIDENCE: "hard to read",
    EscalationReason.NEEDS_HUMAN: "carrier asked",
    EscalationReason.WRONG_PERSON: "wrong person",
    EscalationReason.WRITEBACK_FAILED: "filing failed",
    EscalationReason.NO_CONTACT: "no contact",
    EscalationReason.REASK_LIMIT: "keeps failing",
    EscalationReason.OUTBOUND_BLOCKED: "message stopped",
    EscalationReason.SEND_FAILED: "could not send",
}

# Red rows: something may be wrong with money or identity. Yellow: a judgment call.
URGENT_REASONS = frozenset({EscalationReason.RISK})

# What the person should do, shown above the resolve form.
REASON_ADVICE: dict[EscalationReason, str] = {
    EscalationReason.NO_RESPONSE: "Call the carrier, then take this load.",
    EscalationReason.RISK: "Call the carrier on the number you already have. Do not reply.",
    EscalationReason.UNKNOWN_SENDER: "Check the address with the carrier before approving it.",
    EscalationReason.DOCUMENT_EXCEPTION: "Call the carrier, then take this load.",
    EscalationReason.LOW_CONFIDENCE: "Look at the document and decide.",
    EscalationReason.NEEDS_HUMAN: "Answer the carrier, then take this load.",
    EscalationReason.WRONG_PERSON: "Find the right contact at the carrier.",
    EscalationReason.WRITEBACK_FAILED: "File the POD by hand.",
    EscalationReason.NO_CONTACT: "Add a dispatcher email for this carrier.",
    EscalationReason.REASK_LIMIT: "Call the carrier about the document.",
    EscalationReason.OUTBOUND_BLOCKED: "Look at what was stopped and why.",
    EscalationReason.SEND_FAILED: "Check the carrier's contact details.",
}

RESOLUTIONS: dict[EscalationResolution, str] = {
    EscalationResolution.HANDLED_BY_HAND: "Got it myself",
    EscalationResolution.WAIVED: "Not needed",
    EscalationResolution.DISPUTED: "In dispute",
    EscalationResolution.OTHER: "Something else",
}

CHECK_REASONS: dict[str, str] = {
    "unreadable": "could not be read",
    "unsigned": "not signed",
    "wrong_load": "for another load",
    "rerouted": "belongs to another load",
    "not_a_pod": "not a POD",
    "missing_pages": "pages missing",
    "no_delivery_date": "no delivery date",
    "date_mismatch": "delivery date does not match",
    "low_confidence": "hard to read",
    "exception_notes": "receiver wrote a note",
}

STATE_WORDS: dict[ChaseState, str] = {
    ChaseState.OPEN: "Starting",
    ChaseState.ASKED: "Asked",
    ChaseState.RECEIVED: "Received",
    ChaseState.CHECKING: "Checking",
    ChaseState.FAILED_CHECK: "Asked again",
    ChaseState.PASSED: "Filing",
    ChaseState.FILED: "Filed",
    ChaseState.ESCALATED: "Needs you",
    ChaseState.CLOSED: "Closed",
}

REQUIREMENT_WORDS: dict[RequirementStatus, str] = {
    RequirementStatus.OPEN: "POD still needed",
    RequirementStatus.MET: "POD on file",
    RequirementStatus.RESOLVED_BY_PERSON: "Settled by a person",
}


def check_reason(code: str) -> str:
    return CHECK_REASONS.get(code, code.replace("_", " "))


def waited(ms: int) -> str:
    """How long something has waited, the way a person would say it."""
    ms = max(ms, 0)
    if ms < HOUR_MS:
        minutes = max(1, ms // MINUTE_MS)
        return f"{minutes} min"
    if ms < DAY_MS:
        hours = ms // HOUR_MS
        return "1 hour" if hours == 1 else f"{hours} hours"
    days = ms // DAY_MS
    return "1 day" if days == 1 else f"{days} days"


def short_duration(ms: int | None) -> str:
    """Median-style figure for the header: 40 min, 5 h, 2 d."""
    if ms is None:
        return "–"
    if ms < HOUR_MS:
        return f"{max(1, ms // MINUTE_MS)} min"
    if ms < 2 * DAY_MS:
        return f"{round(ms / HOUR_MS)} h"
    return f"{round(ms / DAY_MS)} d"


def _clock(dt: datetime) -> str:
    hour = dt.hour % 12 or 12
    return f"{hour}:{dt.minute:02d} {'am' if dt.hour < 12 else 'pm'}"


def when(at_ms: int, now_ms: int, tz: str) -> str:
    """Today 9:14 am · Tue 2:00 pm (this week) · Tue 10 Nov, 1:00 pm (older)."""
    zone = ZoneInfo(tz)
    at = datetime.fromtimestamp(at_ms / 1000, zone)
    now = datetime.fromtimestamp(now_ms / 1000, zone)
    if at.date() == now.date():
        return f"Today {_clock(at)}"
    if 0 <= (now.date() - at.date()).days < 7:
        return f"{at:%a} {_clock(at)}"
    return f"{at:%a} {at.day} {at:%b}, {_clock(at)}"


def phone(number: str) -> str:
    """(312) 555-0142 for a North American number; anything else as given."""
    digits = "".join(c for c in number if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return number


def tel(number: str) -> str:
    digits = "".join(c for c in number if c.isdigit() or c == "+")
    return f"tel:{digits}"
