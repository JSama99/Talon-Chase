"""Write-back adapters: where a checked document ends up.

``FakeApiAdapter`` behaves like a TMS API (rung 1 of the ladder).
``PacketEmailAdapter`` emails the broker a ready-to-file packet (rung 4). It
works with any TMS, so it is also the fallback when another adapter is down.
"""

from __future__ import annotations

from chase.domain import outbound
from chase.domain.models import Broker, Load
from chase.ports import (
    AdapterError,
    EmailMessage,
    EmailPort,
    OutgoingAttachment,
    Store,
    WriteBackRequest,
)


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
    name = "packet_email"

    def __init__(self, store: Store, email: EmailPort) -> None:
        self._store = store
        self._email = email
        self._sent: dict[str, str] = {}

    def file(self, request: WriteBackRequest) -> str:
        already = self._store.get_setting(f"packet:{request.idempotency_key}", "")
        if already:
            return already
        broker = self._store.get(Broker, request.broker_id)
        load = self._store.get(Load, request.load_id)
        if broker is None or load is None:
            raise AdapterError("broker or load not found", transient=False)
        message = EmailMessage(
            from_addr=broker.chase_mailbox,
            to=[broker.rep_email],
            subject=outbound.packet_subject(load),
            body=outbound.packet_body(
                load, request.filename, request.document_sha256, request.delivered_at_ms
            ),
            attachments=[OutgoingAttachment(request.filename, request.document_sha256)],
        )
        try:
            provider_id = self._email.send(message)
        except Exception as exc:
            raise AdapterError(f"packet email failed: {exc}") from exc
        receipt = f"packet:{provider_id}"
        self._store.set_setting(f"packet:{request.idempotency_key}", receipt)
        return receipt
