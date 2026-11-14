"""Rules that decide whether an inbound message is held for a person.

These are plain code on purpose: a security rule must not depend on a language model.
"""

from __future__ import annotations

import re

# Any of these in a carrier message means money or payment routing is being discussed.
_RISK_PATTERNS: list[tuple[str, str]] = [
    ("bank", r"\bbank(?:ing)?\b"),
    ("routing number", r"\brouting\s*(?:number|no\.?|#)"),
    ("account number", r"\b(?:account|acct)\.?\s*(?:number|no\.?|#)"),
    ("ach", r"\bach\b"),
    ("wire", r"\bwir(?:e|ing)\b"),
    ("remit", r"\bremit(?:-?to|tance)?\b"),
    ("factoring", r"\bfactor(?:ing|ed|s)?\b"),
    ("notice of assignment", r"\bnotice\s+of\s+assignment\b|\bnoa\b"),
    ("payment details", r"\bpay(?:ment)?\s+(?:details|info(?:rmation)?|instructions)\b"),
    ("voided check", r"\bvoid(?:ed)?\s+che(?:ck|que)\b"),
    ("zelle or similar", r"\b(?:zelle|cash\s*app|venmo|paypal)\b"),
]
_COMPILED = [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in _RISK_PATTERNS]

# A shared mail provider's domain says nothing about which company a sender belongs to.
FREE_MAIL_DOMAINS = frozenset(
    {
        "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "aol.com", "icloud.com",
        "live.com", "msn.com", "proton.me", "protonmail.com", "ymail.com", "me.com",
    }
)  # fmt: skip


def find_risk_terms(text: str) -> list[str]:
    return [name for name, pattern in _COMPILED if pattern.search(text)]


def normalize_email(address: str) -> str:
    address = address.strip()
    match = re.search(r"<([^>]+)>", address)
    if match:
        address = match.group(1)
    return address.strip().lower()


def domain_of(address: str) -> str:
    address = normalize_email(address)
    return address.rsplit("@", 1)[1] if "@" in address else ""


def normalize_phone(number: str) -> str:
    digits = re.sub(r"\D", "", number)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def same_company_domain(address: str, known_addresses: list[str]) -> bool:
    """True when ``address`` shares a non-free-mail domain with a known contact."""
    domain = domain_of(address)
    if not domain or domain in FREE_MAIL_DOMAINS:
        return False
    return any(domain_of(known) == domain for known in known_addresses)


STOP_WORDS = frozenset({"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"})


def is_stop_message(body: str) -> bool:
    return body.strip().upper().rstrip(".!") in STOP_WORDS
