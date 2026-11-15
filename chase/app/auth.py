"""Sign-in for the broker screens: an emailed link, then a signed session cookie.

Nothing here keeps passwords. An operator types their email; if it belongs to an
active operator, they get a link that works once, for 15 minutes. Opening the link
shows a button; pressing it starts the session. The extra press means a mail
scanner that fetches every link cannot spend it.

Tokens are ``base64url(json).base64url(hmac)``. Each purpose has its own key,
derived from the service secret, so a token made for one purpose is never
accepted for another.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import Any

from chase.domain.models import HOUR_MS, MINUTE_MS

LINK_TTL_MS = 15 * MINUTE_MS
SESSION_TTL_MS = 12 * HOUR_MS
LINK_THROTTLE_MS = 1 * MINUTE_MS
SESSION_COOKIE = "tc_session"


class BadToken(Exception):
    """A token that is malformed, forged, expired or already used."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class Signer:
    secret: bytes

    def _key(self, purpose: str) -> bytes:
        return hmac.new(self.secret, f"talon-chase/app/{purpose}".encode(), hashlib.sha256).digest()

    def _mac(self, purpose: str, body: str) -> str:
        return _b64(hmac.new(self._key(purpose), body.encode("ascii"), hashlib.sha256).digest())

    def sign(self, purpose: str, payload: dict[str, Any]) -> str:
        body = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
        return f"{body}.{self._mac(purpose, body)}"

    def read(self, purpose: str, token: str, now_ms: int) -> dict[str, Any]:
        try:
            body, mac = token.split(".", 1)
            if not hmac.compare_digest(mac, self._mac(purpose, body)):
                raise BadToken("bad signature")
            payload = json.loads(_unb64(body))
            expires = int(payload["exp"])
        except BadToken:
            raise
        except Exception as exc:  # malformed in any way
            raise BadToken("malformed") from exc
        if now_ms >= expires:
            raise BadToken("expired")
        return payload

    def csrf(self, session_token: str) -> str:
        """A form token bound to one session: forged forms from other sites fail."""
        return self._mac("csrf", session_token)[:32]


def make_link_token(signer: Signer, operator_id: str, now_ms: int) -> str:
    return signer.sign(
        "link", {"op": operator_id, "exp": now_ms + LINK_TTL_MS, "n": secrets.token_hex(8)}
    )


def make_session(signer: Signer, operator_id: str, now_ms: int) -> str:
    return signer.sign(
        "session", {"op": operator_id, "iat": now_ms, "exp": now_ms + SESSION_TTL_MS}
    )


def used_key(token: str) -> str:
    """Settings key marking a sign-in link as spent."""
    return "signin-used:" + hashlib.sha256(token.encode()).hexdigest()[:40]


def throttle_key(email: str) -> str:
    return "signin-sent:" + hashlib.sha256(email.encode()).hexdigest()[:40]


def normalize_email(email: str) -> str:
    return email.strip().lower()
