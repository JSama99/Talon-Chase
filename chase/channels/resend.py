"""Email through Resend: sending, incoming mail, and bounces.

Sending (``ResendEmail``) implements the engine's email port:

* ``POST https://api.resend.com/emails`` with the API key.
* Every message with an idempotency key carries it as Resend's ``Idempotency-Key``
  header (a hash of ours, so it is a fixed length). If the first try was accepted
  but its response was lost, the retry gets the original answer back and nothing
  is sent twice. Resend remembers a key for 24 hours; the default retry schedule
  ends after about five and a half, and a test holds it to that.
* 400 and 422 (bad address, invalid request) are permanent. 401, 403, 429, 5xx,
  concurrent-key conflicts and network errors are temporary: the outbox retries
  them, and when its schedule runs out a person sees the message failed.

Which address a message goes out as depends on what Resend has verified
(``Identity``). A broker's own domain, once verified, sends as the broker. Until
then (and on Resend's free plan, which allows one domain) a message goes out
from one shared Talon Chase address, named for the broker, with a Reply-To that
carries the broker's id (``chase+<broker id>@<inbound domain>``), so the reply
still finds its broker.

Incoming mail and bounce events arrive as webhooks (see ``mount_resend``).
Resend signs them (Svix: ``svix-id``, ``svix-timestamp``, ``svix-signature``);
``verify_signature`` checks that. A received-mail webhook holds only metadata, so
``ResendInbox`` fetches the body and the attachments.

Docs: https://resend.com/docs/api-reference/emails/send-email,
https://resend.com/docs/dashboard/emails/idempotency-keys,
https://resend.com/docs/dashboard/receiving/introduction,
https://resend.com/docs/webhooks/verify-webhooks-requests
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import html
import mimetypes
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.utils import formataddr, getaddresses, parseaddr
from typing import Any

import httpx

from chase.ports import EmailMessage, PermanentSendError

API = "https://api.resend.com"
TAG_NAME = "tc_key"
MAX_INBOUND_BYTES = 40 * 1024 * 1024
# Images this small carried inline (with a Content-ID) are signature logos, not PODs.
INLINE_LOGO_BYTES = 20 * 1024
# Svix rejects a webhook whose timestamp is further than this from now.
SIGNATURE_TOLERANCE_S = 300
# Resend forgets an idempotency key after this long.
IDEMPOTENCY_WINDOW_MS = 24 * 60 * 60 * 1000


class ResendError(Exception):
    """A temporary Resend failure; the outbox retries it."""


def key_hash(idempotency_key: str) -> str:
    """Our idempotency key as a fixed-length key Resend accepts (limit 256 characters)."""
    return hashlib.sha256(idempotency_key.encode()).hexdigest()


# ------------------------------------------------------------------ identity


def _domain(address: str) -> str:
    return parseaddr(address)[1].rpartition("@")[2].strip().lower()


def _display(name: str) -> str:
    """A name that is safe in a From header: no quotes, brackets, commas or control characters."""
    kept = "".join(c for c in name if c.isprintable() and c not in '"<>,;\\')
    return " ".join(kept.split())[:60]


@dataclass(frozen=True)
class Identity:
    """Which address a message goes out as, and where its replies come back.

    ``verified_domains`` are the domains Resend can send from. A message from one of
    them is sent as written. Any other message goes out from ``shared_from`` (which
    must be on a verified domain) with a Reply-To at ``inbound_domain`` that names
    the broker. ``inbound_domain`` is where Resend receives mail: a Resend-managed
    ``*.resend.app`` address, or a domain you pointed an MX record at.
    """

    verified_domains: frozenset[str] = frozenset()
    shared_from: str = ""
    inbound_domain: str = ""

    def resolve(self, from_addr: str, broker: tuple[str, str] | None) -> tuple[str, str]:
        """(From header, Reply-To) for a message. ``broker`` is (id, name) when the
        message is a broker's, else None."""
        if _domain(from_addr) in self.verified_domains:
            return from_addr, ""
        if not (self.shared_from and self.inbound_domain and broker):
            raise ResendError(
                f"{_domain(from_addr) or from_addr!r} is not a domain Resend can send from, "
                "and no shared sender is set up (RESEND_SHARED_FROM, RESEND_INBOUND_DOMAIN)"
            )
        broker_id, broker_name = broker
        name = f"{_display(broker_name) or 'Your broker'} via Talon Chase"
        return formataddr((name, self.shared_from)), f"chase+{broker_id}@{self.inbound_domain}"


def identity_from_env(env: Mapping[str, str]) -> Identity:
    """The identity set by RESEND_VERIFIED_DOMAINS, RESEND_SHARED_FROM and
    RESEND_INBOUND_DOMAIN. Raises ValueError for a combination that cannot work."""
    verified = {
        d.strip().lower().lstrip("@")
        for d in env.get("RESEND_VERIFIED_DOMAINS", "").split(",")
        if d.strip()
    }
    shared = env.get("RESEND_SHARED_FROM", "").strip()
    inbound = env.get("RESEND_INBOUND_DOMAIN", "").strip().lower().lstrip("@")
    if shared:
        domain = _domain(shared)
        if not domain or "@" not in parseaddr(shared)[1]:
            raise ValueError("RESEND_SHARED_FROM must be an email address")
        verified.add(domain)
        if not inbound:
            raise ValueError(
                "RESEND_SHARED_FROM needs RESEND_INBOUND_DOMAIN, so replies find their broker"
            )
    if not verified:
        raise ValueError(
            "set RESEND_VERIFIED_DOMAINS (a domain verified in Resend) or RESEND_SHARED_FROM"
        )
    return Identity(frozenset(verified), parseaddr(shared)[1] if shared else "", inbound)


# -------------------------------------------------------------------- client


class ResendClient:
    """The HTTP plumbing: one client with the API key, one without it for downloads."""

    def __init__(
        self,
        api_key: str,
        *,
        api: str = API,
        timeout_s: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("a Resend API key is required")
        self.api = httpx.Client(
            base_url=api,
            timeout=timeout_s,
            transport=transport,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "talon-chase",
            },
        )
        # Signed download links are on another host; the API key must never go there.
        self.plain = httpx.Client(timeout=timeout_s, transport=transport, follow_redirects=False)

    def call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self.api.request(method, path, **kwargs)
        except httpx.HTTPError as exc:  # timeouts, resets, DNS: the caller retries
            raise ResendError(f"{type(exc).__name__}: {exc}") from exc


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _error(response: httpx.Response, data: dict[str, Any], *, sending: bool) -> Exception:
    name = str(data.get("name") or "")
    message = str(data.get("message") or response.text[:200])
    status = response.status_code
    text = f"Resend {status}" + (f"/{name}" if name else "") + f": {message}"
    if sending:
        if status == 409 and name == "invalid_idempotent_request":
            return PermanentSendError(text)  # the same key with other content: never retryable
        if status in (400, 422) and "api key" not in message.lower():
            return PermanentSendError(text)  # will not succeed unchanged
    # 401/403 (setup), 429, 5xx, a key still in flight: retried, and a person sees it
    return ResendError(text)


# ------------------------------------------------------------------- sending


class ResendEmail:
    def __init__(
        self,
        client: ResendClient,
        blobs: Callable[[str], bytes | None],
        identity: Identity,
        broker_of: Callable[[str], tuple[str, str] | None] = lambda address: None,
    ) -> None:
        self.client = client
        self.blobs = blobs
        self.identity = identity
        self.broker_of = broker_of

    def send(self, message: EmailMessage) -> str:
        sender, reply_to = self.identity.resolve(
            message.from_addr, self.broker_of(message.from_addr)
        )
        body: dict[str, Any] = {
            "from": sender,
            "to": list(message.to),
            "subject": message.subject,
            "text": message.body,
        }
        if message.cc:
            body["cc"] = list(message.cc)
        if reply_to:
            body["reply_to"] = [reply_to]
        headers: dict[str, str] = {}
        if message.idempotency_key:
            key = key_hash(message.idempotency_key)
            headers["Idempotency-Key"] = key
            body["tags"] = [{"name": TAG_NAME, "value": key[:40]}]
        if message.attachments:
            body["attachments"] = [
                self._attachment(a.filename, a.content_sha256) for a in message.attachments
            ]
        response = self.client.call("POST", "/emails", json=body, headers=headers)
        data = _json(response)
        if response.status_code == 200 and data.get("id"):
            return str(data["id"])
        raise _error(response, data, sending=True)

    def _attachment(self, filename: str, sha256: str) -> dict[str, str]:
        content = self.blobs(sha256)
        if content is None:
            raise PermanentSendError(f"attachment {filename} is not stored")
        return {
            "filename": filename,
            "content": base64.b64encode(content).decode("ascii"),
            "content_type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
        }


# ------------------------------------------------------------------ incoming


@dataclass(frozen=True)
class Received:
    email_id: str
    sender: str
    to: list[str]
    subject: str
    body: str
    attachments: list[tuple[str, bytes]]


class ResendInbox:
    """Fetches what a received-mail webhook leaves out: the text and the files."""

    def __init__(self, client: ResendClient) -> None:
        self.client = client

    def fetch(self, event: Mapping[str, Any]) -> Received:
        email_id = str(event.get("email_id") or "")
        if not email_id:
            raise ValueError("the event has no email_id")
        full = self._get(f"/emails/receiving/{email_id}")
        wants_files = bool(event.get("attachments") or full.get("attachments"))
        listed: list[dict[str, Any]] = []
        if wants_files:
            data = self._get(f"/emails/receiving/{email_id}/attachments").get("data")
            listed = [a for a in data or [] if isinstance(a, dict)]
        headers = {str(k).lower(): str(v) for k, v in (full.get("headers") or {}).items()}
        sender = parseaddr(str(event.get("from") or full.get("from") or headers.get("from", "")))[1]
        text = str(full.get("text") or "").strip() or html_to_text(str(full.get("html") or ""))
        return Received(
            email_id=email_id,
            sender=sender.strip().lower(),
            to=addresses(event, full, headers),
            subject=str(event.get("subject") or full.get("subject") or ""),
            body=reply_text(text),
            attachments=self._files(listed),
        )

    def _get(self, path: str) -> dict[str, Any]:
        response = self.client.call("GET", path)
        if response.status_code != 200:
            raise ResendError(f"Resend {response.status_code} on {path}: {response.text[:200]}")
        return _json(response)

    def _files(self, listed: list[dict[str, Any]]) -> list[tuple[str, bytes]]:
        """Attachments as (name, bytes). Small inline images are signature logos and are
        left out; everything else, inline photos of a POD included, is kept."""
        kept: list[tuple[str, bytes]] = []
        total = 0
        for item in listed:
            url = str(item.get("download_url") or "")
            if not url.startswith("https://"):
                continue
            content = self._download(url, MAX_INBOUND_BYTES - total)
            total += len(content)
            media = str(item.get("content_type", ""))
            inline = bool(item.get("content_id"))
            if inline and media.startswith("image/") and len(content) < INLINE_LOGO_BYTES:
                continue
            name = "".join(
                c for c in str(item.get("filename") or "attachment") if c.isalnum() or c in "-_. "
            )
            kept.append((name.lstrip(". ")[:120] or "attachment", content))
        return kept

    def _download(self, url: str, limit: int) -> bytes:
        try:
            with self.client.plain.stream("GET", url) as response:
                if response.status_code != 200:
                    raise ResendError(f"attachment download answered {response.status_code}")
                data = b""
                for chunk in response.iter_bytes():
                    data += chunk
                    if len(data) > limit:
                        raise ResendError("the attachments are larger than the 40 MB limit")
                return data
        except httpx.HTTPError as exc:
            raise ResendError(f"attachment download failed: {type(exc).__name__}") from exc


_FORWARD_HEADERS = ("x-forwarded-to", "delivered-to", "x-original-to")


def addresses(
    event: Mapping[str, Any], full: Mapping[str, Any], headers: Mapping[str, str]
) -> list[str]:
    """Every address the message was sent to, lowercased, in a stable order. Forwarding
    leaves the original recipient in the headers or the Received ``for`` clause."""
    raw: list[str] = []
    for source in (event, full):
        for field in ("to", "cc", "bcc", "received_for"):
            value = source.get(field)
            raw += [value] if isinstance(value, str) else [str(v) for v in value or []]
    raw += [headers[h] for h in _FORWARD_HEADERS if h in headers]
    seen: dict[str, None] = {}
    for _, address in getaddresses(raw):
        if "@" in address:
            seen.setdefault(address.strip().lower(), None)
    return list(seen)


def mailbox_hashes(to: list[str], inbound_domain: str = "") -> list[str]:
    """The ``+hash`` parts of the addresses a message was sent to (``chase+brk-1@...``).
    With an inbound domain given, only addresses on it count."""
    found: list[str] = []
    for address in to:
        local, _, domain = address.rpartition("@")
        if "+" not in local or (inbound_domain and domain != inbound_domain.lower()):
            continue
        tag = local.partition("+")[2]
        if tag and tag not in found:
            found.append(tag)
    return found


# ------------------------------------------------------------- reading a reply

_ATTRIBUTION = re.compile(
    r"^(on|am|le|el)\s.{3,300}\b(wrote|schrieb|a écrit|escribió)\b[^:]{0,200}:\s*$", re.I
)
_DIVIDER = re.compile(
    r"^\s*(-{2,}\s*(original message|forwarded message|reply above this line)\s*-{2,}|_{8,})\s*$",
    re.I,
)
_HEADER_LINE = re.compile(r"^(sent|date|to|subject|cc):\s", re.I)


def reply_text(text: str) -> str:
    """What the sender wrote, without the quoted message below it. Falls back to the
    whole text when nothing but quotation is found."""
    lines = text.replace("\r\n", "\n").split("\n")
    cut = len(lines)
    for i, line in enumerate(lines):
        pair = f"{line.strip()} {lines[i + 1].strip()}" if i + 1 < len(lines) else line.strip()
        outlook = line.lower().startswith("from:") and any(
            _HEADER_LINE.match(n) for n in lines[i + 1 : i + 4]
        )
        if (
            line.lstrip().startswith(">")
            or _DIVIDER.match(line)
            or _ATTRIBUTION.match(line.strip())
            or _ATTRIBUTION.match(pair)
            or outlook
        ):
            cut = i
            break
    kept = "\n".join(lines[:cut]).strip()
    return kept or text.strip()


def html_to_text(markup: str) -> str:
    """Plain text from an HTML-only email: paragraphs and breaks kept, tags dropped."""
    markup = re.sub(r"(?is)<(script|style).*?</\1>", " ", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", markup)
    text = html.unescape(re.sub(r"<[^>]+>", " ", markup))
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# ----------------------------------------------------------------- signatures


def verify_signature(
    secret: str, msg_id: str, timestamp: str, signature: str, body: bytes, now_s: float
) -> bool:
    """Resend webhooks are signed with Svix: HMAC-SHA256 over ``id.timestamp.body`` with
    the base64 part of the ``whsec_`` secret, sent as space-separated ``v1,<base64>``
    values (several while a secret is being rotated). A timestamp more than five
    minutes from now is refused, which stops a captured webhook being replayed."""
    if not (secret and msg_id and timestamp and signature):
        return False
    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    if abs(now_s - sent_at) > SIGNATURE_TOLERANCE_S:
        return False
    key = secret_key(secret)
    if key is None:
        return False
    signed = msg_id.encode() + b"." + timestamp.encode() + b"." + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest())
    for part in signature.split():
        version, _, value = part.partition(",")
        if version == "v1" and hmac.compare_digest(value.encode(), expected):
            return True
    return False


def secret_key(secret: str) -> bytes | None:
    """The signing key inside a ``whsec_...`` secret, or None if it is not one."""
    if not secret.startswith("whsec_"):
        return None
    try:
        key = base64.b64decode(secret[6:], validate=True)
    except (binascii.Error, ValueError):
        return None
    return key or None
