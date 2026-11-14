"""In-memory email and text channels. They record what would have been sent."""

from __future__ import annotations

from chase.ports import EmailMessage, SmsMessage


class FakeEmail:
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []
        self.fail_next = 0

    def send(self, message: EmailMessage) -> str:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise ConnectionError("fake email outage")
        self.sent.append(message)
        return f"email-{len(self.sent)}"


class FakeSms:
    def __init__(self) -> None:
        self.sent: list[SmsMessage] = []

    def send(self, message: SmsMessage) -> str:
        self.sent.append(message)
        return f"sms-{len(self.sent)}"
