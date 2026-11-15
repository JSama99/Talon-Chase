"""Write-back adapters: where a checked document ends up.

``FakeApiAdapter`` behaves like a TMS API (rung 1 of the ladder).
``PacketEmailAdapter`` emails the broker a ready-to-file packet (rung 4), through
the outbox. It works with any TMS, so it is also the fallback when another
adapter is down.
"""

from __future__ import annotations

from chase.domain import outbound
from chase.domain.clock import Clock, IdGen
from chase.domain.models import (
    Attachment,
    Broker,
    Channel,
    Load,
    OutboxMessage,
    OutboxPurpose,
    OutboxStatus,
)
from chase.outbox import enqueue
from chase.ports import AdapterError, AdapterPending, Store, WriteBackRequest


class FakeApiAdapter:
    name = "fake_api"

    def __init__(self) -> None:
        self.filed: dict[str, WriteBackRequest] = {}
        self.calls = 0
        self.fail_next = 0  # fail this many calls before doing anything
        self.fail_after_commit_next = 0  # do the write, then lose the response
        self.down = False  # fail every call
        self.permanent = False  # make failures non-transient

    def file(self, request: WriteBackRequest) -> str:
        self.calls += 1
        if self.down:
            raise AdapterError("fake TMS is down", transient=not self.permanent)
        if self.fail_next > 0:
            self.fail_next -= 1
            raise AdapterError("fake TMS timeout", transient=not self.permanent)
        self.filed.setdefault(request.idempotency_key, request)
        if self.fail_after_commit_next > 0:
            self.fail_after_commit_next -= 1
            raise AdapterError("fake TMS wrote the file but the response was lost")
        return f"fake-tms:{request.load_number}:{request.idempotency_key[:12]}"

    def documents_on(self, load_number: str) -> list[WriteBackRequest]:
        return [r for r in self.filed.values() if r.load_number == load_number]


class PacketEmailAdapter:
    """Emails the broker one ready-to-file packet per load, through the outbox.

    The first call queues the email and reports the filing as pending. Later calls
    report it filed once the outbox has delivered it, or failed if delivery gave up.
    """

    name = "packet_email"

    def __init__(self, store: Store, clock: Clock, ids: IdGen) -> None:
        self._store = store
        self._clock = clock
        self._ids = ids

    def file(self, request: WriteBackRequest) -> str:
        key = f"packet:{request.idempotency_key}"
        existing = self._store.find(OutboxMessage, idempotency_key=key)
        if existing:
            message = existing[0]
            if message.status is OutboxStatus.SENT:
                return f"packet:{message.provider_id}"
            if message.status in (OutboxStatus.FAILED, OutboxStatus.CANCELLED):
                reason = message.last_error or message.status.value.lower()
                raise AdapterError(f"packet email not delivered: {reason}", transient=False)
            raise AdapterPending(message.id)
        broker = self._store.get(Broker, request.broker_id)
        load = self._store.get(Load, request.load_id)
        if broker is None or load is None:
            raise AdapterError("broker or load not found", transient=False)
        message = enqueue(
            self._store,
            message_id=self._ids.new("msg"),
            now_ms=self._clock.now_ms(),
            broker_id=broker.id,
            load_id=load.id,
            chase_id=request.chase_id,
            purpose=OutboxPurpose.PACKET,
            channel=Channel.EMAIL,
            idempotency_key=key,
            from_addr=broker.chase_mailbox,
            to=[broker.rep_email],
            subject=outbound.packet_subject(load),
            body=outbound.packet_body(
                load, request.filename, request.document_sha256, request.delivered_at_ms
            ),
            attachments=[Attachment(request.filename, request.document_sha256)],
        )
        raise AdapterPending(message.id)
