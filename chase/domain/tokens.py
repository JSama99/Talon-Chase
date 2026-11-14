"""Upload links: one load, an expiry, and a signature. No account and no app."""

from __future__ import annotations

import base64
import hashlib
import hmac


class InvalidToken(Exception):
    pass


def _sign(secret: bytes, message: str) -> str:
    digest = hmac.new(secret, message.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def make_upload_token(secret: bytes, load_id: str, expires_ms: int) -> str:
    body = base64.urlsafe_b64encode(f"{load_id}|{expires_ms}".encode()).decode("ascii").rstrip("=")
    return f"{body}.{_sign(secret, body)}"


def read_upload_token(secret: bytes, token: str, now_ms: int) -> str:
    """Return the load id the token was issued for, or raise InvalidToken."""
    try:
        body, signature = token.split(".", 1)
        if not hmac.compare_digest(signature, _sign(secret, body)):
            raise InvalidToken("bad signature")
        decoded = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8")
        load_id, expires = decoded.rsplit("|", 1)
        expires_ms = int(expires)
    except InvalidToken:
        raise
    except Exception as exc:  # malformed in any way
        raise InvalidToken("malformed token") from exc
    if now_ms > expires_ms:
        raise InvalidToken("expired")
    return load_id
