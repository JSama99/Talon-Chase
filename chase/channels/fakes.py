"""In-memory email and text channels. They record what would have been sent.

``honors_idempotency`` says whether the fake behaves like a provider that drops a
second message with an idempotency key it has already accepted. Real providers
differ; the outbox is tested both ways.
"""

from __future__ import annotations

from chase.ports import EmailMessage, PermanentSendError, SmsMessage


class _FakeChannel:
    def __init__(self, prefix: str, honors_idempotency: bool) -> None:
        self._prefix = prefix
        self.honors_idempotency = honors_idempotency
        self.sent: list = []
        self.calls = 0
        self.fail_next = 0  # raise a temporary error on this many calls
        self.reject_next = 0  # raise PermanentSendError on this many calls
        self._accepted: dict[str, str] = {}

    def _send(self, message, key: str) -> str:
        self.calls += 1
        if self.reject_next > 0:
            self.reject_next -= 1
            raise PermanentSendError(f"fake {self._prefix} rejected the recipient")
        if self.fail_next > 0:
            self.fail_next -= 1
            raise ConnectionError(f"fake {self._prefix} outage")
        if key and self.honors_idempotency and key in self._accepted:
            return self._accepted[key]
        self.sent.append(message)
        provider_id = f"{self._prefix}-{len(self.sent)}"
        if key:
            self._accepted[key] = provider_id
        return provider_id


class FakeEmail(_FakeChannel):
    def __init__(self, honors_idempotency: bool = True) -> None:
        super().__init__("email", honors_idempotency)

    def send(self, message: EmailMessage) -> str:
        return self._send(message, message.idempotency_key)


class FakeSms(_FakeChannel):
    def __init__(self, honors_idempotency: bool = True) -> None:
        super().__init__("sms", honors_idempotency)

    def send(self, message: SmsMessage) -> str:
        return self._send(message, message.idempotency_key)
