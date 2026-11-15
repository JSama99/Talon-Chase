"""Email through Postmark: sending, incoming mail, and bounces.

Sending (``PostmarkEmail``) implements the engine's email port:

* ``POST https://api.postmarkapp.com/email`` with the server token.
* Postmark has no idempotency key. Every message carries ours as metadata
  (``tc_key``), and on a retry the adapter first searches Postmark's outbound
  messages for it: if the first try was accepted but its response was lost, the
  existing message id is returned and nothing is sent twice.
* 422 responses (bad address, inactive recipient, invalid request) are permanent;
  401, 429, 5xx and network errors are temporary and retried by the outbox.

Incoming mail and bounce events arrive as webhooks (see ``mount_postmark``).
Postmark does not sign webhooks, so both routes require HTTP Basic credentials,
which are configured in the webhook URL on Postmark's side.

Docs: https://postmarkapp.com/developer/api/email-api,
https://postmarkapp.com/developer/webhooks/inbound-webhook,
https://postmarkapp.com/developer/webhooks/bounce-webhook
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import mimetypes
import re
from collections.abc import Callable
from typing import Any

import httpx

from chase.ports import EmailMessage, PermanentSendError

API = "https://api.postmarkapp.com"
METADATA_KEY = "tc_key"
MAX_INBOUND_BYTES = 40 * 1024 * 1024  # Postmark's inbound limit is 35 MB, base64 adds a third
# Images this small carried inline (with a Content-ID) are signature logos, not PODs.
INLINE_LOGO_BYTES = 20 * 1024
# Postmark bounce types that mean the address will not work again.
PERMANENT_BOUNCES = frozenset(
    {"HardBounce", "BadEmailAddress", "SpamComplaint", "ManuallyDeactivated", "Blocked"}
)


class PostmarkError(Exception):
    """A temporary Postmark failure; the outbox retries it."""


def metadata_value(idempotency_key: str) -> str:
    """Our idempotency key as a short metadata value Postmark can search by."""
    return hashlib.sha256(idempotency_key.encode()).hexdigest()[:40]


class PostmarkEmail:
    def __init__(
        self,
        server_token: str,
        blobs: Callable[[str], bytes | None],
        *,
        message_stream: str = "outbound",
        api: str = API,
        timeout_s: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not server_token:
            raise ValueError("a Postmark server token is required")
        self.blobs = blobs
        self.message_stream = message_stream
        self.client = httpx.Client(
            base_url=api,
            timeout=timeout_s,
            transport=transport,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Postmark-Server-Token": server_token,
            },
        )

    # ------------------------------------------------------------------ send

    def send(self, message: EmailMessage) -> str:
        key = metadata_value(message.idempotency_key) if message.idempotency_key else ""
        if key and not message.first_try:
            already = self._find_sent(key)
            if already:
                return already
        body: dict[str, Any] = {
            "From": message.from_addr,
            "To": ", ".join(message.to),
            "Subject": message.subject,
            "TextBody": message.body,
            "MessageStream": self.message_stream,
            "TrackOpens": False,
            "TrackLinks": "None",
        }
        if message.cc:
            body["Cc"] = ", ".join(message.cc)
        if key:
            body["Metadata"] = {METADATA_KEY: key}
        if message.attachments:
            body["Attachments"] = [self._attachment(a.filename, a.content_sha256)
                                   for a in message.attachments]  # fmt: skip
        response = self._call("POST", "/email", json=body)
        data = _json(response)
        if response.status_code == 200 and data.get("ErrorCode", 0) == 0:
            return str(data["MessageID"])
        raise self._error(response, data)

    def _attachment(self, filename: str, sha256: str) -> dict[str, str]:
        content = self.blobs(sha256)
        if content is None:
            raise PermanentSendError(f"attachment {filename} is not stored")
        media = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return {
            "Name": filename,
            "Content": base64.b64encode(content).decode("ascii"),
            "ContentType": media,
        }

    def _find_sent(self, key: str) -> str:
        """The id of a message already accepted with this key, or ""."""
        response = self._call(
            "GET",
            "/messages/outbound",
            params={"count": 1, "offset": 0, f"metadata_{METADATA_KEY}": key},
        )
        if response.status_code != 200:
            raise self._error(response, _json(response))
        found = _json(response).get("Messages") or []
        return str(found[0]["MessageID"]) if found else ""

    def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:  # timeouts, resets, DNS: the outbox retries
            raise PostmarkError(f"{type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _error(response: httpx.Response, data: dict[str, Any]) -> Exception:
        code = data.get("ErrorCode")
        text = f"Postmark {response.status_code}" + (f"/{code}" if code else "")
        text += f": {data.get('Message', response.text[:200])}"
        if response.status_code == 422:
            return PermanentSendError(text)  # will not succeed unchanged
        return PostmarkError(text)  # 401 (config), 429, 5xx: retry, and a person sees it


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# ------------------------------------------------------------------ incoming


_ADDRESS = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


def addresses(payload: dict[str, Any]) -> list[str]:
    """Every address the message was sent to, lowercased, in a stable order."""
    found: list[str] = []
    for full in ("ToFull", "CcFull", "BccFull"):
        for entry in payload.get(full) or []:
            if isinstance(entry, dict) and entry.get("Email"):
                found.append(str(entry["Email"]))
    for plain in ("To", "Cc", "OriginalRecipient"):
        found += _ADDRESS.findall(str(payload.get(plain) or ""))
    for header in payload.get("Headers") or []:
        if isinstance(header, dict) and str(header.get("Name", "")).lower() in (
            "x-forwarded-to",
            "delivered-to",
            "x-original-to",
        ):
            found += _ADDRESS.findall(str(header.get("Value", "")))
    seen: dict[str, None] = {}
    for address in found:
        seen.setdefault(address.strip().lower(), None)
    return list(seen)


def message_text(payload: dict[str, Any]) -> str:
    """The reply itself when Postmark could separate it, else the whole text."""
    stripped = str(payload.get("StrippedTextReply") or "").strip()
    if stripped:
        return stripped
    text = str(payload.get("TextBody") or "").strip()
    if text:
        return text
    html = str(payload.get("HtmlBody") or "")
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def files(payload: dict[str, Any]) -> list[tuple[str, bytes]]:
    """Attachments as (name, bytes). Small inline images are signature logos and are
    left out; everything else, inline photos of a POD included, is kept."""
    kept = []
    for attachment in payload.get("Attachments") or []:
        if not isinstance(attachment, dict):
            continue
        try:
            content = base64.b64decode(str(attachment.get("Content", "")), validate=True)
        except (binascii.Error, ValueError):
            continue
        media = str(attachment.get("ContentType", ""))
        inline = bool(attachment.get("ContentID"))
        if inline and media.startswith("image/") and len(content) < INLINE_LOGO_BYTES:
            continue
        name = "".join(
            c for c in str(attachment.get("Name") or "attachment") if c.isalnum() or c in "-_. "
        )
        kept.append((name[:120] or "attachment", content))
    return kept


def credentials_match(header: str, user: str, password: str) -> bool:
    """Constant-time check of an HTTP Basic Authorization header."""
    if not header.lower().startswith("basic "):
        return False
    try:
        given = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        return False
    expected = f"{user}:{password}"
    return hmac.compare_digest(given.encode(), expected.encode())
