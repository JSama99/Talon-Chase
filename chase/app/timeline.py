"""A load's evidence chain, told as a short story for the broker.

Each evidence kind recorded on a load's chain is either told (``_TELL``) or left
out on purpose (``QUIET``), because another line already says it. A test checks
that every kind is one or the other, so nothing new slips by unexplained.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from chase.app.wording import REASON_TITLES, RESOLUTIONS, check_reason
from chase.domain.models import EscalationReason, EscalationResolution, Step
from chase.evidence import EvidenceEvent


@dataclass
class Entry:
    at_ms: int
    title: str
    detail: str = ""
    tone: str = ""  # "", "good" or "bad"
    kinds: list[str] = field(default_factory=list)


# Kinds that never get a line of their own.
QUIET = frozenset(
    {
        "chase.state",  # the lines around it say what changed
        "chase.rescheduled",  # planning, not something that happened
        "inbound.classified",  # folded into how the reply is described
        "message.retry",  # retried until sent or failed; both are told
        "message.reclaimed",
        "message.duplicate",
        "requirement.closed",  # shown as the load's status
        "writeback.pending",  # followed by succeeded or failed
    }
)

_STEP_WORDS = {
    Step.FIRST_EMAIL: "Asked the dispatcher by email",
    Step.DRIVER_SMS: "Texted the driver",
    Step.SECOND: "Asked again",
    Step.THIRD: "Asked a third time, copying you",
    Step.REASK: "Asked for a better copy",
}

Teller = Callable[[EvidenceEvent, dict[str, Any]], Entry | None]


def _load_received(e: EvidenceEvent, ctx: dict[str, Any]) -> Entry:
    return Entry(e.at_ms, "Load received from your system")


def _delivered(e: EvidenceEvent, ctx: dict[str, Any]) -> Entry:
    return Entry(e.at_ms, "Marked delivered")


def _opened(e: EvidenceEvent, ctx: dict[str, Any]) -> Entry:
    return Entry(e.at_ms, "Started asking for the POD")


def _asked(at_ms: int, attempt: dict[str, Any]) -> Entry:
    step = Step(attempt["step"])
    title = _STEP_WORDS.get(step, "Asked the carrier")
    if step is Step.SECOND and attempt["channel"] == "SMS":
        title = "Texted the dispatcher"
    return Entry(at_ms, title, ", ".join(attempt["to"]))


def _attempt(e: EvidenceEvent, ctx: dict[str, Any]) -> Entry | None:
    """A decided request. It is told when the provider accepts it (``message.sent``);
    until then it reads as waiting, so nothing looks sent that was not."""
    if e.payload["outbox_id"] in ctx["_delivered"]:
        ctx["_attempts"][e.payload["outbox_id"]] = e.payload
        return None
    asked = _asked(e.at_ms, e.payload)
    first, rest = asked.title.split(" ", 1)
    verb = {"Asked": "ask", "Texted": "text"}.get(first, first.lower())
    return Entry(e.at_ms, f"Waiting to send: {verb} {rest}", asked.detail)


def _sent(e: EvidenceEvent, ctx: dict[str, Any]) -> Entry | None:
    attempt = ctx["_attempts"].pop(e.payload["outbox_id"], None)
    if attempt is None:
        return None  # not a carrier request (a filing packet); its filing line says it
    return _asked(e.at_ms, attempt)


def _sms_skipped(e: EvidenceEvent, ctx: dict[str, Any]) -> Entry:
    reason = e.payload["reason"].replace("_", " ")
    return Entry(e.at_ms, "Did not text", reason)


def _inbound(e: EvidenceEvent, ctx: dict[str, Any]) -> Entry:
    names = ctx.get("filenames", {})
    files = [names.get(sha, "a file") for sha in e.payload["attachments"]]
    who = "Carrier"
    title = f"{who} replied with a file" if len(files) == 1 else f"{who} replied"
    if len(files) > 1:
        title = f"{who} replied with {len(files)} files"
    detail = e.payload["from"]
    if files:
        detail += " · " + ", ".join(files)
    return Entry(e.at_ms, title, detail)


def _held(e: EvidenceEvent, ctx: dict[str, Any]) -> Entry:
    reason = EscalationReason(e.payload["reason"])
    return Entry(e.at_ms, "Held the message and handed it to you", REASON_TITLES[reason], "bad")


def _late(e: EvidenceEvent, ctx: dict[str, Any]) -> Entry:
    return Entry(e.at_ms, "Reply came after this load was settled", "Kept, nothing done")


def _document(e: EvidenceEvent, ctx: dict[str, Any]) -> Entry | None:
    moved_from = e.payload["rerouted_from_load"]
    if moved_from:
        return Entry(e.at_ms, "Document arrived on another load and was moved here")
    return None  # the reply line already names the file


def _rerouted(e: EvidenceEvent, ctx: dict[str, Any]) -> Entry:
    target = ctx.get("load_numbers", {}).get(e.payload["to_load"], "another load")
    return Entry(e.at_ms, f"Document belongs to {target}; moved it there")


def _check(e: EvidenceEvent, ctx: dict[str, Any]) -> Entry:
    result = e.payload["result"]
    reasons = [check_reason(r) for r in e.payload["reasons"]]
    if result == "PASS":
        return Entry(e.at_ms, "Checked the document: good", "Right load, signed, dated", "good")
    if result == "REROUTE":
        return Entry(e.at_ms, "Checked the document: belongs to another load")
    if result == "EXCEPTION":
        document = ctx.get("documents", {}).get(e.payload["document_id"])
        notes = (document.extraction.get("exception_notes") or []) if document else []
        if notes:
            quoted = "; ".join(f"“{n}”" for n in notes)
            return Entry(
                e.at_ms, "Checked the document: note found", f"Receiver wrote {quoted}", "bad"
            )
        return Entry(e.at_ms, "Checked the document: needs you", ", ".join(reasons), "bad")
    return Entry(e.at_ms, f"Checked the document: {', '.join(reasons) or 'failed'}", "", "bad")


def _writeback_ok(e: EvidenceEvent, ctx: dict[str, Any]) -> Entry:
    how = (
        "Emailed the packet to you"
        if e.payload["adapter"] == "packet_email"
        else ("Filed in your system")
    )
    return Entry(e.at_ms, how, f"Receipt {e.payload['receipt']}", "good")


def _writeback_failed(e: EvidenceEvent, ctx: dict[str, Any]) -> Entry:
    again = "will try again" if e.payload["transient"] else "gave up"
    return Entry(e.at_ms, "Filing did not go through", f"{e.payload['error']} ({again})", "bad")


def _fallback(e: EvidenceEvent, ctx: dict[str, Any]) -> Entry:
    return Entry(e.at_ms, "Your system was unavailable; emailing the packet instead")


def _invoice(e: EvidenceEvent, ctx: dict[str, Any]) -> Entry:
    return Entry(e.at_ms, "Ready to invoice", tone="good")


def _escalated(e: EvidenceEvent, ctx: dict[str, Any]) -> Entry:
    return Entry(
        e.at_ms,
        "Stopped and handed to you",
        "Not filed. Nothing more will be sent to the carrier until you decide.",
        "bad",
    )


def _acknowledged(e: EvidenceEvent, ctx: dict[str, Any]) -> Entry:
    who = ctx.get("operator_names", {}).get(e.payload["by"], e.payload["by"])
    resolution = RESOLUTIONS[EscalationResolution(e.payload["resolution"])]
    detail = e.payload["note"]
    return Entry(e.at_ms, f"{who}: {resolution.lower()}", detail)


def _closed(e: EvidenceEvent, ctx: dict[str, Any]) -> Entry | None:
    return None if e.payload["reason"] == "filed" else Entry(e.at_ms, "Closed")


def _message_failed(e: EvidenceEvent, ctx: dict[str, Any]) -> Entry:
    return Entry(e.at_ms, "A message could not be delivered", e.payload["error"], "bad")


def _message_cancelled(e: EvidenceEvent, ctx: dict[str, Any]) -> Entry:
    return Entry(e.at_ms, "A queued message was not sent", e.payload["reason"].replace("_", " "))


def _message_deferred(e: EvidenceEvent, ctx: dict[str, Any]) -> Entry:
    return Entry(e.at_ms, "Waiting to send", e.payload["reason"].replace("_", " "))


def _bounced(e: EvidenceEvent, ctx: dict[str, Any]) -> Entry:
    if e.payload["permanent"]:
        title = f"Email to {e.payload['email']} bounced; that address will not be used again"
        return Entry(e.at_ms, title, e.payload["detail"], "bad")
    return Entry(e.at_ms, f"Email to {e.payload['email']} was delayed", e.payload["detail"])


def _blocked(e: EvidenceEvent, ctx: dict[str, Any]) -> Entry:
    return Entry(e.at_ms, "Messages are off; nothing was sent", tone="bad")


def _refused(e: EvidenceEvent, ctx: dict[str, Any]) -> Entry:
    return Entry(e.at_ms, "A message was stopped before sending", e.payload["reason"], "bad")


_TELL: dict[str, Teller] = {
    "load.received": _load_received,
    "load.delivered": _delivered,
    "load.ready_to_invoice": _invoice,
    "chase.opened": _opened,
    "chase.closed": _closed,
    "attempt.queued": _attempt,
    "message.sent": _sent,
    "sms.skipped": _sms_skipped,
    "inbound.received": _inbound,
    "inbound.held": _held,
    "inbound.late": _late,
    "document.received": _document,
    "document.rerouted": _rerouted,
    "check.completed": _check,
    "writeback.succeeded": _writeback_ok,
    "writeback.failed": _writeback_failed,
    "writeback.fallback": _fallback,
    "escalation.opened": _escalated,
    "escalation.acknowledged": _acknowledged,
    "message.failed": _message_failed,
    "message.cancelled": _message_cancelled,
    "message.deferred": _message_deferred,
    "message.bounced": _bounced,
    "outbound.blocked": _blocked,
    "outbound.refused": _refused,
}

TOLD = frozenset(_TELL)


def tell(events: list[EvidenceEvent], ctx: dict[str, Any] | None = None) -> list[Entry]:
    """The story of one load, oldest first."""
    ctx = dict(ctx or {})
    ctx["_delivered"] = {e.payload["outbox_id"] for e in events if e.kind == "message.sent"}
    ctx["_attempts"] = {}
    entries: list[Entry] = []
    for event in events:
        teller = _TELL.get(event.kind)
        if teller is None:
            if event.kind not in QUIET:
                entries.append(Entry(event.at_ms, event.kind, kinds=[event.kind]))
            continue
        entry = teller(event, ctx)
        if entry is None:
            continue
        entry.kinds.append(event.kind)
        previous = entries[-1] if entries else None
        # A held message and the hand-off it causes are one moment, told once.
        if (
            previous
            and event.kind == "escalation.opened"
            and "inbound.held" in previous.kinds
            and previous.at_ms == event.at_ms
        ):
            previous.kinds.append(event.kind)
            continue
        # "Checked: not signed" then a re-ask sent at the same moment reads as one line.
        if (
            previous
            and entry.title == _STEP_WORDS[Step.REASK]
            and "check.completed" in previous.kinds
            and previous.at_ms == entry.at_ms
        ):
            previous.title += ", asked again"
            previous.kinds.append(event.kind)
            continue
        entries.append(entry)
    return entries
