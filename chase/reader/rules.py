"""Rule-based stand-ins for the two language-model jobs.

``RuleBasedClassifier`` labels a carrier's reply from keywords.
``StandInReader`` treats a document's bytes as JSON describing what a real
reader would extract from the image, which is how the synthetic pack models PODs.
Anything else (a real PDF or photo) it cannot read, and says so with zero confidence,
so the document goes to a person and the carrier is not told their copy is bad.
"""

from __future__ import annotations

import json
import re

from chase.domain.checks import Extraction, cannot_read
from chase.domain.models import Inbound
from chase.ports import Classification, ReplyLabel

_OUT_OF_OFFICE = re.compile(
    r"out of (?:the )?office|auto(?:matic)?[- ]?reply|autoreply|on vacation|away from my desk",
    re.IGNORECASE,
)
_WRONG_PERSON = re.compile(
    r"wrong (?:person|number|email|address|company)|no longer (?:with|works|employed)"
    r"|not (?:the|your) (?:dispatcher|driver|carrier)|don'?t (?:have|haul|know) (?:this|that) load"
    r"|not (?:my|our) load",
    re.IGNORECASE,
)
_PROMISE = re.compile(
    r"will (?:send|get|have|forward|upload)|i'?ll (?:send|get|have|forward|upload)"
    r"|tomorrow|later today|this afternoon|in the morning|working on it|as soon as"
    r"|asap|when (?:he|she|the driver|they) (?:gets?|returns?|is back)|waiting on (?:the )?driver",
    re.IGNORECASE,
)


class RuleBasedClassifier:
    def classify(self, inbound: Inbound) -> Classification:
        if inbound.attachments:
            return Classification(ReplyLabel.HAS_DOCUMENT, 100)
        text = f"{inbound.subject}\n{inbound.body}"
        if _OUT_OF_OFFICE.search(text):
            return Classification(ReplyLabel.OUT_OF_OFFICE, 95)
        if _WRONG_PERSON.search(text):
            return Classification(ReplyLabel.WRONG_PERSON, 90)
        if "?" in inbound.body:
            return Classification(ReplyLabel.QUESTION, 85)
        if _PROMISE.search(text):
            return Classification(ReplyLabel.PROMISE, 85)
        return Classification(ReplyLabel.UNKNOWN, 50)


# The stand-in cannot read a real file. It must not claim it did: zero confidence sends
# the document to a person, where "unreadable" would tell the carrier their copy is bad.
CANNOT_READ = cannot_read("the stand-in reader cannot read real files")


class StandInReader:
    name = "stand-in"
    inline = True  # a pure function of the bytes, so the engine calls it on the spot

    def read(self, filename: str, content: bytes) -> Extraction:
        try:
            data = json.loads(content.decode("utf-8"))
            if not isinstance(data, dict):
                return CANNOT_READ
            return Extraction.from_dict(data)
        except (ValueError, UnicodeDecodeError, TypeError):
            return CANNOT_READ
