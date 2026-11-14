"""Every word the agent sends to a carrier comes from this file.

Messages carry the load number and the request. They never carry rates, customer
or consignee names, pickup numbers or addresses. ``assert_safe`` enforces that as
a second line of defence in case a template is ever changed carelessly.
"""

from __future__ import annotations

import re

from chase.domain.models import Broker, Load, Step

REASON_TEXT: dict[str, str] = {
    "unreadable": "the image is too blurry or cropped to read",
    "wrong_load": "the reference number on it does not match this load",
    "rerouted": "it belongs to a different load, and we have filed it there",
    "unsigned": "the receiver's signature or stamp is missing",
    "no_delivery_date": "the delivery date is missing",
    "missing_pages": "one or more pages are missing",
    "not_a_pod": "it is not a proof of delivery (we need the signed bill of lading)",
}


class OutboundBlocked(Exception):
    def __init__(self, term_kind: str) -> None:
        super().__init__(f"outbound message blocked: it contains the load's {term_kind}")
        self.term_kind = term_kind


def _footer(broker: Broker) -> str:
    return (
        f"This message was sent by the automated assistant for {broker.name}. "
        f"To reach a person, write to {broker.rep_email}."
    )


def subject_for(load: Load, step: Step) -> str:
    prefix = {
        Step.SECOND: "Second request: ",
        Step.THIRD: "Third request: ",
        Step.REASK: "Please resend: ",
    }.get(step, "")
    return f"{prefix}proof of delivery for load {load.load_number}"


def email_body(broker: Broker, load: Load, step: Step, link: str, reasons: list[str]) -> str:
    n = load.load_number
    if step is Step.REASK:
        lines = "\n".join(f"  - {REASON_TEXT[r]}" for r in reasons)
        opening = (
            f"Thank you for sending paperwork for load {n}. We could not accept it because:\n"
            f"{lines}\n\nPlease send the signed proof of delivery again."
        )
    elif step is Step.SECOND:
        opening = f"We still need the signed proof of delivery for load {n}."
    elif step is Step.THIRD:
        opening = (
            f"This is our third request for the signed proof of delivery for load {n}. "
            "A member of our team is copied and will follow up by phone."
        )
    else:
        opening = f"Please send the signed proof of delivery for load {n}."
    return (
        f"Hello,\n\n{opening}\n\n"
        f"Reply to this email with the document attached, or upload it here:\n{link}\n\n"
        f"{_footer(broker)}\n"
    )


def sms_body(broker: Broker, load: Load, link: str) -> str:
    return (
        f"{broker.name}: please send a photo of the signed POD for load {load.load_number}. "
        f"Upload: {link} Automated message. Reply STOP to opt out."
    )


def packet_subject(load: Load) -> str:
    return f"POD ready to file: load {load.load_number}"


def packet_body(load: Load, filename: str, sha256: str, delivered_at_ms: int | None) -> str:
    delivered = "not stated" if delivered_at_ms is None else str(delivered_at_ms)
    return (
        f"Load {load.load_number}: the proof of delivery passed every check.\n\n"
        f"Attached file: {filename}\nSHA-256: {sha256}\n"
        f"Delivery time on the document (epoch ms): {delivered}\n\n"
        "File it on the load and mark the load ready to invoice.\n"
    )


def _digits_pattern(amount: int) -> re.Pattern[str]:
    return re.compile(rf"(?<![\d.]){amount}(?:\.\d{{2}})?(?!\d)")


def assert_safe(text: str, load: Load) -> None:
    """Raise OutboundBlocked if ``text`` carries any sensitive detail of ``load``."""
    lowered = text.lower()
    for kind, value in (
        ("customer name", load.customer_name),
        ("consignee name", load.consignee_name),
        ("consignee address", load.consignee_address),
        ("pickup number", load.pickup_number),
    ):
        if value and value.strip().lower() in lowered:
            raise OutboundBlocked(kind)
    if load.rate_cents > 0:
        without_commas = re.sub(r"(?<=\d),(?=\d{3})", "", text)
        if _digits_pattern(load.rate_cents // 100).search(without_commas):
            raise OutboundBlocked("rate")
