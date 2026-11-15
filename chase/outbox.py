"""Transactional outbox.

The engine never talks to an email or text provider. When it decides to send, it
writes an ``OutboxMessage`` in the same database transaction as the decision, so a
message exists if and only if the decision was committed. ``OutboxDispatcher``
delivers those messages afterwards, outside the engine's transaction:

1. **Claim** (one short transaction). Take the next due message. Hold it while the
   kill switch is off; cancel it if its chase no longer needs it or, for a text, if
   the recipient opted out or lost consent; push it out of the carrier's quiet hours;
   otherwise mark it SENDING with a lease.
2. **Send** (no transaction, no lock held). Call the provider with the message's
   idempotency key and a stable Message-ID.
3. **Complete** (one short transaction). Record SENT, or schedule a retry, or give up
   after the broker's backoff list runs out and hand the load to a person.

Messages are claimed **one at a time**, right before their own provider call. Every
check in step 1 therefore sees the state at that moment, even late in a long batch,
and a lease starts only when its message is about to be sent.

Delivery is at least once. If a dispatcher dies after the provider accepted a
message and before step 3 commits, the lease expires and the message is sent again
with the same idempotency key. A provider that honours that key drops the repeat;
one that does not delivers a duplicate. That window is the time between the
provider's reply and one database commit. See docs/OUTBOX.md.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from chase.domain.models import (
    MINUTE_MS,
    Attachment,
    Attempt,
    Broker,
    Channel,
    Chase,
    ChaseState,
    Contact,
    EscalationReason,
    Load,
    OutboxMessage,
    OutboxPurpose,
    OutboxStatus,
    Step,
)
from chase.domain.risk import normalize_email, normalize_phone
from chase.domain.schedule import in_quiet_hours, next_allowed
from chase.domain.states import ACTIVE_STATES
from chase.evidence import sha256_hex
from chase.ports import (
    EmailMessage,
    OutgoingAttachment,
    PermanentSendError,
    SmsMessage,
    Store,
)

if TYPE_CHECKING:
    from chase.engine import ChaseEngine

DEFAULT_LEASE_MS = 5 * MINUTE_MS
DEFAULT_BATCH = 50


def enqueue(
    store: Store,
    *,
    message_id: str,
    now_ms: int,
    broker_id: str,
    load_id: str,
    chase_id: str,
    purpose: OutboxPurpose,
    channel: Channel,
    idempotency_key: str,
    from_addr: str,
    to: list[str],
    cc: list[str] | None = None,
    subject: str = "",
    body: str = "",
    attachments: list[Attachment] | None = None,
    quiet_tz: str = "",
) -> OutboxMessage:
    """Write one message to the outbox. Returns the existing one if the key is taken."""
    existing = store.find(OutboxMessage, idempotency_key=idempotency_key)
    if existing:
        return existing[0]
    message = OutboxMessage(
        id=message_id,
        broker_id=broker_id,
        load_id=load_id,
        chase_id=chase_id,
        purpose=purpose,
        channel=channel,
        idempotency_key=idempotency_key,
        from_addr=from_addr,
        to=list(to),
        cc=list(cc or []),
        subject=subject,
        body=body,
        attachments=list(attachments or []),
        quiet_tz=quiet_tz,
        created_at_ms=now_ms,
        next_attempt_at_ms=now_ms,
    )
    store.put(message)
    return message


@dataclass
class DispatchReport:
    sent: int = 0
    retried: int = 0
    failed: int = 0
    cancelled: int = 0
    deferred: int = 0
    duplicates: int = 0
    held_ids: set[str] = field(default_factory=set)

    @property
    def held(self) -> int:
        """Messages left waiting because outbound is switched off, counted once each."""
        return len(self.held_ids)

    @property
    def changed(self) -> int:
        return self.sent + self.retried + self.failed + self.cancelled + self.deferred

    def to_dict(self) -> dict[str, int]:
        return {
            "sent": self.sent,
            "retried": self.retried,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "deferred": self.deferred,
            "held": self.held,
            "duplicates": self.duplicates,
        }


@dataclass(frozen=True)
class _Outcome:
    ok: bool
    provider_id: str = ""
    error: str = ""
    permanent: bool = False


class OutboxDispatcher:
    """Delivers outbox messages. Safe to run in several processes at once on Postgres:
    every claim and completion takes the store's transaction lock."""

    def __init__(
        self,
        engine: ChaseEngine,
        *,
        guard: Callable[[], contextlib.AbstractContextManager[Any]] | None = None,
        lease_ms: int = DEFAULT_LEASE_MS,
        batch: int = DEFAULT_BATCH,
    ) -> None:
        self.engine = engine
        self.store = engine.store
        self.clock = engine.clock
        self.guard = guard or engine.store.transaction
        self.lease_ms = lease_ms
        self.batch = batch

    # ------------------------------------------------------------------ public

    def dispatch(self) -> DispatchReport:
        """Deliver up to ``batch`` due messages, claiming each one just before sending it."""
        report = DispatchReport()
        for _ in range(self.batch):
            claimed = self.claim(report, limit=1)
            if not claimed:
                break
            outcome = self._send(claimed[0])
            with self.guard():
                self.complete(claimed[0], outcome, report)
        return report

    def claim(
        self, report: DispatchReport | None = None, limit: int | None = None
    ) -> list[OutboxMessage]:
        """Step 1. Returns copies of the messages this call now holds a lease on.

        ``dispatch`` claims one at a time. A larger ``limit`` is for tests and tools;
        anything claimed that way should be sent promptly, before its lease runs out.
        """
        report = report or DispatchReport()
        limit = self.batch if limit is None else limit
        claimed: list[OutboxMessage] = []
        with self.guard():
            now = self.clock.now_ms()
            for message in self._due(now):
                if len(claimed) >= limit:
                    break
                broker = self.store.get(Broker, message.broker_id)
                if broker is None or not self.engine._outbound_enabled(broker):
                    report.held_ids.add(message.id)
                    continue
                stale = self._stale_reason(message)
                if stale:
                    self._cancel(message, stale, now)
                    report.cancelled += 1
                    continue
                undeliverable = self._drop_bounced(message)
                if undeliverable:
                    self._cancel(message, undeliverable, now)
                    chase = self.store.get(Chase, message.chase_id)
                    if chase is not None:  # the stale check above means it still awaits a reply
                        self.engine._escalate(chase, EscalationReason.SEND_FAILED, undeliverable)
                    report.cancelled += 1
                    continue
                if message.quiet_tz and in_quiet_hours(now, message.quiet_tz, broker.config):
                    self._defer(message, next_allowed(now, message.quiet_tz, broker.config), now)
                    report.deferred += 1
                    continue
                if message.status is OutboxStatus.SENDING:
                    self.engine._record(
                        message.load_id,
                        "message.reclaimed",
                        {"outbox_id": message.id, "after_try": message.tries},
                    )
                message.status = OutboxStatus.SENDING
                message.tries += 1
                message.claim_token = self.engine.ids.new("claim")
                message.lease_until_ms = now + self.lease_ms
                self.store.put(message)
                claimed.append(message)
        return claimed

    def complete(
        self, message: OutboxMessage, outcome: _Outcome, report: DispatchReport | None = None
    ) -> None:
        """Step 3. Must run inside ``self.guard()``."""
        report = report or DispatchReport()
        now = self.clock.now_ms()
        current = self.store.get(OutboxMessage, message.id)
        if current is None:
            return
        if outcome.ok:
            if current.status is OutboxStatus.SENT:
                # Our lease ran out and another dispatcher delivered it too.
                report.duplicates += 1
                self.engine._record(
                    current.load_id,
                    "message.duplicate",
                    {"outbox_id": current.id, "provider_id": outcome.provider_id},
                )
                return
            current.status = OutboxStatus.SENT
            current.provider_id = outcome.provider_id
            current.sent_at_ms = now
            current.finished_at_ms = now
            current.claim_token = ""
            current.last_error = ""
            self.store.put(current)
            self.engine._record(
                current.load_id,
                "message.sent",
                {
                    "outbox_id": current.id,
                    "channel": current.channel,
                    "to": current.to,
                    "provider_id": outcome.provider_id,
                    "try": message.tries,
                },
            )
            report.sent += 1
            if current.purpose is OutboxPurpose.PACKET:
                self._wake(current.chase_id, now)
            return

        if current.status is not OutboxStatus.SENDING or current.claim_token != message.claim_token:
            return  # someone else holds it now; their result stands
        broker = self.store.get(Broker, current.broker_id)
        backoff = broker.config.send_retry_backoff_ms if broker else []
        current.last_error = outcome.error
        current.claim_token = ""
        if not outcome.permanent and current.tries <= len(backoff):
            current.status = OutboxStatus.PENDING
            current.next_attempt_at_ms = now + backoff[current.tries - 1]
            self.store.put(current)
            self.engine._record(
                current.load_id,
                "message.retry",
                {
                    "outbox_id": current.id,
                    "try": current.tries,
                    "error": outcome.error,
                    "next_attempt_at_ms": current.next_attempt_at_ms,
                },
            )
            report.retried += 1
            return

        current.status = OutboxStatus.FAILED
        current.finished_at_ms = now
        self.store.put(current)
        self.engine._record(
            current.load_id,
            "message.failed",
            {
                "outbox_id": current.id,
                "tries": current.tries,
                "permanent": outcome.permanent,
                "error": outcome.error,
            },
        )
        report.failed += 1
        if current.purpose is OutboxPurpose.PACKET:
            self._wake(current.chase_id, now)  # the engine then fails the write-back
            return
        chase = self.store.get(Chase, current.chase_id)
        if chase is not None and chase.state in ACTIVE_STATES:
            self.engine._escalate(
                chase,
                EscalationReason.SEND_FAILED,
                f"could not deliver {current.channel.lower()} to {', '.join(current.to)}: "
                f"{outcome.error}",
            )

    # ---------------------------------------------------------------- internals

    def _due(self, now: int) -> list[OutboxMessage]:
        pending = [
            m
            for m in self.store.find(OutboxMessage, status=OutboxStatus.PENDING)
            if m.next_attempt_at_ms <= now
        ]
        expired = [
            m
            for m in self.store.find(OutboxMessage, status=OutboxStatus.SENDING)
            if m.lease_until_ms <= now
        ]
        return sorted(pending + expired, key=lambda m: (m.next_attempt_at_ms, m.id))

    def _stale_reason(self, message: OutboxMessage) -> str:
        """Why a carrier request must not go out now, or an empty string."""
        if message.purpose is not OutboxPurpose.CARRIER_REQUEST:
            return ""
        chase = self.store.get(Chase, message.chase_id)
        if chase is None:
            return "chase not found"
        if chase.state is not ChaseState.ASKED:
            return f"chase is {chase.state.value}"
        if message.channel is Channel.SMS and not self._may_text(message):
            return "recipient opted out or has no consent on file"
        return ""

    def _drop_bounced(self, message: OutboxMessage) -> str:
        """Take addresses that bounced for good off a queued or retrying carrier request,
        checked again at send time because a bounce can land after the request was queued.
        Returns why the request cannot go out when no recipient is left, else an empty
        string."""
        if (
            message.purpose is not OutboxPurpose.CARRIER_REQUEST
            or message.channel is not Channel.EMAIL
        ):
            return ""
        load = self.store.get(Load, message.load_id)
        if load is None:
            return ""
        dead = {
            normalize_email(c.email)
            for c in self.store.find(Contact, carrier_id=load.carrier_id)
            if c.email and c.email_undeliverable_at_ms is not None
        }
        to = [a for a in message.to if normalize_email(a) not in dead]
        cc = [a for a in message.cc if normalize_email(a) not in dead]
        if len(to) == len(message.to) and len(cc) == len(message.cc):
            return ""
        if not to:
            return f"email to {', '.join(message.to)} bounced earlier and no other address is left"
        message.to, message.cc = to, cc
        self.store.put(message)
        # The request and its delivery list the same recipients (ontology rule TC-11).
        for attempt in self.store.find(Attempt, outbox_id=message.id):
            attempt.to, attempt.cc = list(to), list(cc)
            self.store.put(attempt)
        return ""

    def _may_text(self, message: OutboxMessage) -> bool:
        """Consent is checked again at send time: a STOP or a contact sync may have
        landed after the text was queued."""
        load = self.store.get(Load, message.load_id)
        if load is None or not message.to:
            return False
        phone = normalize_phone(message.to[0])
        return any(
            c.verified and self.engine.sms_allowed(c.id)
            for c in self.store.find(Contact, carrier_id=load.carrier_id)
            if c.phone and normalize_phone(c.phone) == phone
        )

    def _cancel(self, message: OutboxMessage, reason: str, now: int) -> None:
        message.status = OutboxStatus.CANCELLED
        message.finished_at_ms = now
        message.claim_token = ""
        self.store.put(message)
        self.engine._record(
            message.load_id, "message.cancelled", {"outbox_id": message.id, "reason": reason}
        )

    def _defer(self, message: OutboxMessage, until_ms: int, now: int) -> None:
        message.status = OutboxStatus.PENDING
        message.claim_token = ""
        message.next_attempt_at_ms = until_ms
        if not message.deferred_noted:
            message.deferred_noted = True
            self.engine._record(
                message.load_id,
                "message.deferred",
                {"outbox_id": message.id, "reason": "quiet_hours", "until_ms": until_ms},
            )
        self.store.put(message)

    def _wake(self, chase_id: str, now: int) -> None:
        chase = self.store.get(Chase, chase_id)
        if chase is not None and chase.next_step is Step.WRITEBACK:
            chase.next_action_at_ms = min(chase.next_action_at_ms, now)
            self.store.put(chase)

    def _send(self, message: OutboxMessage) -> _Outcome:
        """Step 2. Talks to the provider; holds no lock and no transaction."""
        try:
            if message.channel is Channel.EMAIL:
                provider_id = self.engine.email.send(
                    EmailMessage(
                        from_addr=message.from_addr,
                        to=message.to,
                        subject=message.subject,
                        body=message.body,
                        cc=message.cc,
                        attachments=[
                            OutgoingAttachment(a.filename, a.content_sha256)
                            for a in message.attachments
                        ],
                        idempotency_key=message.idempotency_key,
                        message_id=f"<{sha256_hex(message.idempotency_key.encode())[:40]}"
                        "@talon-chase>",
                    )
                )
            else:
                provider_id = self.engine.sms.send(
                    SmsMessage(
                        from_number=message.from_addr,
                        to=message.to[0],
                        body=message.body,
                        idempotency_key=message.idempotency_key,
                    )
                )
        except PermanentSendError as exc:
            return _Outcome(False, error=str(exc) or "rejected", permanent=True)
        except Exception as exc:  # anything else is treated as temporary
            return _Outcome(False, error=f"{type(exc).__name__}: {exc}")
        return _Outcome(True, provider_id=str(provider_id))
