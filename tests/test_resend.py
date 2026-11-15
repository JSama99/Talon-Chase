"""Resend: the sending adapter against a fake Resend API, signed webhooks end to end
through the service, and the setup rules. Payload shapes follow Resend's documentation."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import json
import re
from itertools import count

import httpx
import pytest
from fastapi.testclient import TestClient

from chase.api import main
from chase.api.app import create_app
from chase.channels.fakes import FakeEmail
from chase.channels.resend import (
    IDEMPOTENCY_WINDOW_MS,
    TAG_NAME,
    Identity,
    ResendClient,
    ResendEmail,
    ResendError,
    ResendInbox,
    html_to_text,
    identity_from_env,
    key_hash,
    mailbox_hashes,
    reply_text,
    verify_signature,
)
from chase.domain.models import (
    HOUR_MS,
    BrokerConfig,
    ChaseState,
    Contact,
    EscalationReason,
    Inbound,
    OutboxMessage,
    OutboxStatus,
    Role,
)
from chase.ports import EmailMessage, OutgoingAttachment, PermanentSendError
from chase.store.memory import MemoryStore
from tests.conftest import World, pod

KEY = "test-api-key-0123456789"
API_KEY = "re_test_key"
SECRET = "whsec_" + base64.b64encode(b"k" * 32).decode()
OWN = "peachtreelogistics.example"
SHARED = "chase@mail.talonchase.example"
INBOUND = "in.talonchase.example"
IDENTITY = Identity(frozenset({"mail.talonchase.example"}), SHARED, INBOUND)
OWN_IDENTITY = Identity(frozenset({"mail.talonchase.example", OWN}), SHARED, INBOUND)


# ------------------------------------------------------------- a fake Resend


class FakeResend:
    """Enough of Resend's API to test against: sending with idempotency keys, received
    mail and its attachments, and signed download links."""

    def __init__(self) -> None:
        self.accepted: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.next_status: list[tuple[int, dict]] = []
        self.lose_next_response = False
        self.keys: dict[str, tuple[dict, str]] = {}
        self.mail: dict[str, dict] = {}
        self.attachments: dict[str, list[dict]] = {}
        self.files: dict[str, bytes] = {}
        self.fetch_failures = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "api.resend.com":
            assert request.headers["Authorization"] == f"Bearer {API_KEY}"
        else:  # a signed download link: the API key must never be sent there
            assert "authorization" not in request.headers
            body = self.files.get(str(request.url))
            return httpx.Response(200, content=body) if body is not None else httpx.Response(403)
        path = request.url.path
        if request.method == "POST" and path == "/emails":
            return self.send(request)
        if self.fetch_failures:
            self.fetch_failures -= 1
            return httpx.Response(500, json={"name": "application_error", "message": "boom"})
        if path.endswith("/attachments"):
            email_id = path.split("/")[-2]
            listing = self.attachments.get(email_id, [])
            return httpx.Response(200, json={"object": "list", "has_more": False, "data": listing})
        email_id = path.rsplit("/", 1)[-1]
        if email_id in self.mail:
            return httpx.Response(200, json=self.mail[email_id])
        return httpx.Response(404, json={"name": "not_found", "message": "no such email"})

    def send(self, request: httpx.Request) -> httpx.Response:
        if self.next_status:
            status, body = self.next_status.pop(0)
            return httpx.Response(status, json=body)
        body = json.loads(request.content)
        key = request.headers.get("Idempotency-Key", "")
        if key in self.keys:  # Resend replays the first answer for the same key
            first, email_id = self.keys[key]
            if first != body:
                return httpx.Response(
                    409,
                    json={"name": "invalid_idempotent_request", "message": "different payload"},
                )
            return httpx.Response(200, json={"id": email_id})
        email_id = f"re-{len(self.accepted) + 1}"
        self.accepted.append(body)
        if key:
            self.keys[key] = (body, email_id)
        if self.lose_next_response:
            self.lose_next_response = False
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(200, json={"id": email_id})

    def posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]

    def client(self) -> ResendClient:
        return ResendClient(API_KEY, transport=httpx.MockTransport(self.handler))

    def receive(
        self,
        world: World,
        load,
        *,
        email_id: str = "rx-1",
        to=("pods@peachtreelogistics.example",),
        text: str | None = "Attached.\n\nOn Tue, Nov 10, 2026 at 2:45 PM Peachtree wrote:\n"
        "> Please send the POD",
        html: str | None = None,
        headers: dict | None = None,
        files: list[tuple[str, str, bytes, str | None]] | None = None,
        sender: str | None = None,
    ) -> dict:
        """Stores a received email the way Resend would and returns the webhook event."""
        if files is None:
            files = [("pod.pdf", "application/pdf", pod(load), None)]
        listing = []
        for i, (name, media, content, content_id) in enumerate(files):
            url = f"https://files.resend.test/{email_id}/{i}?signature=abc"
            self.files[url] = content
            listing.append(
                {
                    "id": f"att-{email_id}-{i}",
                    "filename": name,
                    "content_type": media,
                    "content_disposition": "inline" if content_id else "attachment",
                    "content_id": content_id,
                    "size": len(content),
                    "download_url": url,
                    "expires_at": "2026-11-10T21:00:00Z",
                }
            )
        from_addr = sender or world.dispatcher.email
        self.mail[email_id] = {
            "object": "email",
            "id": email_id,
            "to": list(to),
            "from": f"Dana <{from_addr}>",
            "subject": f"Re: proof of delivery for load {load.load_number}",
            "html": html,
            "text": text,
            "headers": headers or {"from": f"Dana <{from_addr}>", "return-path": from_addr},
            "bcc": [],
            "cc": [],
            "reply_to": [],
            "received_for": [],
            "message_id": f"<{email_id}@mail.redlinetrucking.example>",
            "attachments": [{k: v for k, v in a.items() if k != "download_url"} for a in listing],
        }
        self.attachments[email_id] = listing
        return {
            "type": "email.received",
            "created_at": "2026-11-10T19:45:00.000Z",
            "data": {
                "email_id": email_id,
                "created_at": "2026-11-10T19:45:00.000Z",
                "from": from_addr,
                "to": list(to),
                "bcc": [],
                "cc": [],
                "message_id": f"<{email_id}@mail.redlinetrucking.example>",
                "subject": self.mail[email_id]["subject"],
                "attachments": [
                    {k: v for k, v in a.items() if k not in ("download_url", "expires_at", "size")}
                    for a in listing
                ],
            },
        }


@pytest.fixture
def resend():
    return FakeResend()


def adapter(fake: FakeResend, identity: Identity = OWN_IDENTITY, blobs=None) -> ResendEmail:
    return ResendEmail(
        fake.client(),
        blobs or (lambda sha: b"%PDF-1.4 pod" if sha == "abc" else None),
        identity,
        lambda address: (
            ("brk-1", "Peachtree Logistics")
            if address == "pods@peachtreelogistics.example"
            else None
        ),
    )


def message(**overrides) -> EmailMessage:
    fields = dict(
        from_addr="pods@peachtreelogistics.example",
        to=["dispatch@redlinetrucking.example"],
        cc=["ops@peachtreelogistics.example"],
        subject="Proof of delivery for load TC-100001",
        body="Please send the POD.",
        attachments=[OutgoingAttachment("pod.pdf", "abc")],
        idempotency_key="attempt:att-000001",
    )
    fields.update(overrides)
    return EmailMessage(**fields)


# ------------------------------------------------------------------- sending


def test_send_builds_the_documented_request(resend):
    assert adapter(resend).send(message()) == "re-1"
    (request,) = resend.posts()
    body = json.loads(request.content)
    assert str(request.url) == "https://api.resend.com/emails"
    assert body["from"] == "pods@peachtreelogistics.example"
    assert body["to"] == ["dispatch@redlinetrucking.example"]
    assert body["cc"] == ["ops@peachtreelogistics.example"]
    assert body["subject"] == "Proof of delivery for load TC-100001"
    assert body["text"] == "Please send the POD." and "html" not in body
    assert "reply_to" not in body  # the broker's own verified domain: replies reach it directly
    assert request.headers["Idempotency-Key"] == key_hash("attempt:att-000001")
    assert body["tags"] == [{"name": TAG_NAME, "value": key_hash("attempt:att-000001")[:40]}]
    (attachment,) = body["attachments"]
    assert attachment["filename"] == "pod.pdf" and attachment["content_type"] == "application/pdf"
    assert base64.b64decode(attachment["content"]) == b"%PDF-1.4 pod"


def test_the_idempotency_key_is_a_fixed_length_hash():
    long_key = "x" * 1000
    assert len(key_hash(long_key)) == 64 and key_hash(long_key) == key_hash(long_key)
    assert key_hash("a") != key_hash("b")


def test_a_message_without_a_key_sends_without_one(resend):
    adapter(resend).send(message(idempotency_key="", attachments=[]))
    (request,) = resend.posts()
    assert "idempotency-key" not in request.headers
    assert "tags" not in json.loads(request.content)


def test_a_retry_after_a_lost_response_does_not_send_twice(resend):
    email = adapter(resend)
    resend.lose_next_response = True
    with pytest.raises(ResendError):
        email.send(message())
    assert len(resend.accepted) == 1  # Resend has it; we never heard back
    assert email.send(message()) == "re-1"  # the same key: Resend returns the first answer
    assert len(resend.accepted) == 1
    first, second = resend.posts()
    assert first.headers["Idempotency-Key"] == second.headers["Idempotency-Key"]


def test_the_default_retry_schedule_ends_inside_resends_memory_of_a_key():
    waits = BrokerConfig().send_retry_backoff_ms
    assert sum(waits) < IDEMPOTENCY_WINDOW_MS, "a retry after that could send twice"


def test_a_key_reused_with_other_content_is_permanent(resend):
    adapter(resend).send(message())
    with pytest.raises(PermanentSendError) as caught:
        adapter(resend).send(message(subject="Something else"))
    assert "409" in str(caught.value)


def test_a_broker_without_its_own_domain_sends_through_the_shared_address(resend):
    adapter(resend, identity=IDENTITY).send(message())
    body = json.loads(resend.posts()[0].content)
    assert body["from"] == f"Peachtree Logistics via Talon Chase <{SHARED}>"
    assert body["reply_to"] == [f"chase+brk-1@{INBOUND}"]


def test_a_broker_name_cannot_break_out_of_the_from_header(resend):
    email = ResendEmail(
        resend.client(),
        lambda sha: None,
        IDENTITY,
        lambda address: ("brk-1", 'Evil" <x@y.example>,\r\nBcc: z@y.example'),
    )
    email.send(message(attachments=[]))
    sender = json.loads(resend.posts()[0].content)["from"]
    assert "\r" not in sender and "\n" not in sender
    assert sender.endswith(f"<{SHARED}>") and sender.count("<") == 1


def test_an_address_resend_cannot_send_from_is_refused_before_anything_is_sent(resend):
    with pytest.raises(ResendError, match="not a domain Resend can send from"):
        adapter(resend, identity=Identity(frozenset({OWN}))).send(
            message(from_addr="someone@unverified.example")
        )
    assert resend.posts() == []
    with pytest.raises(ResendError, match="shared sender"):
        adapter(resend, identity=IDENTITY).send(message(from_addr="x@elsewhere.example"))
    assert resend.posts() == []


def test_sign_in_mail_from_a_verified_domain_goes_as_written(resend):
    adapter(resend, identity=IDENTITY).send(
        message(from_addr="Talon Chase <signin@mail.talonchase.example>", cc=[], attachments=[])
    )
    body = json.loads(resend.posts()[0].content)
    assert body["from"] == "Talon Chase <signin@mail.talonchase.example>"
    assert "reply_to" not in body


@pytest.mark.parametrize(
    ("status", "body", "permanent"),
    [
        (422, {"name": "validation_error", "message": "Invalid `to` field."}, True),
        (400, {"name": "missing_required_field", "message": "Missing `subject` field."}, True),
        (400, {"name": "validation_error", "message": "API key is invalid"}, False),
        (401, {"name": "missing_api_key", "message": "Missing API key"}, False),
        (403, {"name": "validation_error", "message": "The x.com domain is not verified."}, False),
        (409, {"name": "concurrent_idempotent_requests", "message": "in progress"}, False),
        (429, {"name": "rate_limit_exceeded", "message": "Too many requests"}, False),
        (429, {"name": "daily_quota_exceeded", "message": "daily quota"}, False),
        (500, {"name": "application_error", "message": "Internal error"}, False),
    ],
)  # fmt: skip
def test_errors_are_permanent_only_when_retrying_cannot_help(resend, status, body, permanent):
    resend.next_status.append((status, body))
    expected = PermanentSendError if permanent else ResendError
    with pytest.raises(expected) as caught:
        adapter(resend).send(message())
    assert str(status) in str(caught.value) and body["message"] in str(caught.value)


def test_a_success_without_an_id_is_not_trusted(resend):
    resend.next_status.append((200, {}))
    with pytest.raises(ResendError):
        adapter(resend).send(message())


def test_network_failures_are_temporary():
    def broken(request):
        raise httpx.ConnectError("no route", request=request)

    client = ResendClient(API_KEY, transport=httpx.MockTransport(broken))
    email = ResendEmail(client, lambda s: b"", OWN_IDENTITY)
    with pytest.raises(ResendError):
        email.send(message(attachments=[]))


def test_a_missing_attachment_is_permanent(resend):
    with pytest.raises(PermanentSendError):
        adapter(resend, blobs=lambda sha: None).send(message())
    assert resend.posts() == []


def test_a_key_is_required_to_build_a_client():
    with pytest.raises(ValueError):
        ResendClient("")


def test_the_outbox_retries_through_resend_without_duplicates(resend):
    world = World()
    world.engine.email = adapter(resend, blobs=world.store.get_blob)
    load = world.make_load()
    resend.lose_next_response = True
    world.tick()
    first = world.messages(load)[0]
    assert first.status is OutboxStatus.PENDING and first.tries == 1
    world.run(HOUR_MS)
    sent = world.store.get(OutboxMessage, first.id)
    assert sent.status is OutboxStatus.SENT and sent.provider_id == "re-1"
    assert len(resend.accepted) == 1, "the carrier must get one email, not two"


# ------------------------------------------------------------ setup and identity


def test_identity_rules():
    env = {"RESEND_VERIFIED_DOMAINS": "Peachtree.example, @other.example"}
    assert identity_from_env(env).verified_domains == {"peachtree.example", "other.example"}
    shared = identity_from_env(
        {
            "RESEND_SHARED_FROM": "Talon Chase <chase@mail.x.example>",
            "RESEND_INBOUND_DOMAIN": "IN.x",
        }
    )
    assert shared.shared_from == "chase@mail.x.example" and shared.inbound_domain == "in.x"
    assert "mail.x.example" in shared.verified_domains
    with pytest.raises(ValueError, match="RESEND_INBOUND_DOMAIN"):
        identity_from_env({"RESEND_SHARED_FROM": "chase@mail.x.example"})
    with pytest.raises(ValueError, match="RESEND_VERIFIED_DOMAINS"):
        identity_from_env({})
    with pytest.raises(ValueError, match="email address"):
        identity_from_env({"RESEND_SHARED_FROM": "nonsense", "RESEND_INBOUND_DOMAIN": "x"})


def test_the_service_uses_resend_when_a_key_is_set():
    store = MemoryStore()
    email, inbox, identity = main._resend(store, {})
    assert isinstance(email, FakeEmail) and inbox is None and identity is None
    env = {
        "RESEND_API_KEY": API_KEY,
        "RESEND_WEBHOOK_SECRET": SECRET,
        "RESEND_SHARED_FROM": SHARED,
        "RESEND_INBOUND_DOMAIN": INBOUND,
    }
    email, inbox, identity = main._resend(store, env)
    assert isinstance(email, ResendEmail) and isinstance(inbox, ResendInbox)
    assert identity.inbound_domain == INBOUND


@pytest.mark.parametrize(
    ("change", "message_part"),
    [
        ({"RESEND_WEBHOOK_SECRET": ""}, "RESEND_WEBHOOK_SECRET"),
        ({"RESEND_WEBHOOK_SECRET": "not-a-secret"}, "whsec_"),
        ({"RESEND_WEBHOOK_SECRET": "whsec_%%%"}, "whsec_"),
        ({"RESEND_SHARED_FROM": "", "RESEND_VERIFIED_DOMAINS": ""}, "RESEND_VERIFIED_DOMAINS"),
        ({"RESEND_INBOUND_DOMAIN": ""}, "RESEND_INBOUND_DOMAIN"),
    ],
)  # fmt: skip
def test_a_half_set_up_resend_stops_the_service_at_start(change, message_part):
    env = {
        "RESEND_API_KEY": API_KEY,
        "RESEND_WEBHOOK_SECRET": SECRET,
        "RESEND_SHARED_FROM": SHARED,
        "RESEND_INBOUND_DOMAIN": INBOUND,
    } | change
    with pytest.raises(RuntimeError, match=message_part):
        main._resend(MemoryStore(), env)


def test_the_broker_lookup_matches_the_chase_mailbox():
    world = World()
    lookup = main._broker_of(world.store)
    assert lookup(" PODS@peachtreelogistics.example ") == ("brk-1", "Peachtree Logistics")
    assert lookup("someone@else.example") is None


# ---------------------------------------------------------------- signatures


def sign(secret: str, msg_id: str, timestamp: str, body: bytes) -> str:
    key = base64.b64decode(secret[6:])
    digest = hmac.new(key, f"{msg_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def test_signature_rules():
    body, now = b'{"type":"email.received"}', 1_800_000_000
    good = sign(SECRET, "msg_1", str(now), body)
    assert verify_signature(SECRET, "msg_1", str(now), good, body, now)
    assert verify_signature(SECRET, "msg_1", str(now), f"v1,AAAA {good}", body, now), "rotation"
    assert not verify_signature(SECRET, "msg_1", str(now), good, body + b" ", now), "tampered"
    assert not verify_signature(SECRET, "msg_2", str(now), good, body, now), "other message id"
    other = "whsec_" + base64.b64encode(b"z" * 32).decode()
    assert not verify_signature(other, "msg_1", str(now), good, body, now), "wrong secret"
    assert not verify_signature(SECRET, "msg_1", str(now), "v2," + good[3:], body, now)
    assert not verify_signature(SECRET, "msg_1", str(now), "", body, now)
    assert not verify_signature(SECRET, "msg_1", "soon", good, body, now)
    assert not verify_signature("", "msg_1", str(now), good, body, now)
    assert not verify_signature("whsec_%%%", "msg_1", str(now), good, body, now)


def test_a_captured_webhook_cannot_be_replayed_later():
    body, then = b"{}", 1_800_000_000
    good = sign(SECRET, "msg_1", str(then), body)
    assert verify_signature(SECRET, "msg_1", str(then), good, body, then + 299)
    assert not verify_signature(SECRET, "msg_1", str(then), good, body, then + 301)
    assert not verify_signature(SECRET, "msg_1", str(then), good, body, then - 301)


# ------------------------------------------------------------------ webhooks

_ids = count(1)


@pytest.fixture
def service(resend):
    world = World()
    inbox = ResendInbox(resend.client())
    client = TestClient(
        create_app(
            world.engine,
            KEY,
            resend_webhook_secret=SECRET,
            resend_inbox=inbox,
            resend_inbound_domain=INBOUND,
        )
    )
    return world, client


def deliver(client, world, event, *, msg_id=None, timestamp=None, signature=None, secret=SECRET):
    body = json.dumps(event).encode()
    msg_id = msg_id or f"msg_{next(_ids)}"
    timestamp = str(int(world.clock.now_ms() / 1000)) if timestamp is None else str(timestamp)
    headers = {
        "svix-id": msg_id,
        "svix-timestamp": timestamp,
        "svix-signature": signature or sign(secret, msg_id, timestamp, body),
        "content-type": "application/json",
    }
    return client.post("/webhooks/resend", content=body, headers=headers)


def test_the_webhook_refuses_anything_unsigned_or_wrongly_signed(service):
    world, client = service
    event = {"type": "email.bounced", "data": {}}
    body = json.dumps(event).encode()
    assert client.post("/webhooks/resend", content=body).status_code == 401
    forged = sign("whsec_" + base64.b64encode(b"z" * 32).decode(), "m", "1", body)
    assert deliver(client, world, event, signature=forged).status_code == 401
    stale = deliver(client, world, event, timestamp=int(world.clock.now_ms() / 1000) - 3600)
    assert stale.status_code == 401
    closed = TestClient(create_app(world.engine, KEY))
    assert deliver(closed, world, event).status_code == 503


def test_the_webhook_refuses_bodies_that_are_not_events(service):
    world, client = service
    for raw, status in ((b"nonsense", 400), (b"[1, 2]", 400)):
        headers = {
            "svix-id": "m1",
            "svix-timestamp": str(int(world.clock.now_ms() / 1000)),
        }
        headers["svix-signature"] = sign(SECRET, "m1", headers["svix-timestamp"], raw)
        assert client.post("/webhooks/resend", content=raw, headers=headers).status_code == status
    huge = {"type": "x", "data": {"pad": "x" * (1024 * 1024)}}
    assert deliver(client, world, huge).status_code == 413


def test_other_events_are_acknowledged_and_ignored(service):
    world, client = service
    response = deliver(client, world, {"type": "email.delivered", "data": {"email_id": "x"}})
    assert response.status_code == 200 and response.json() == {
        "handled": False,
        "type": "email.delivered",
    }


def test_a_reply_with_the_pod_is_filed_and_a_repeat_changes_nothing(service, resend):
    world, client = service
    load = world.make_load()
    world.tick()
    event = resend.receive(world, load)
    first = deliver(client, world, event).json()
    assert first["routed"] is True
    world.settle()
    assert world.chase(load).state is ChaseState.CLOSED
    again = deliver(client, world, event).json()
    assert again["inbound_id"] == first["inbound_id"]
    (stored,) = world.store.find(Inbound)
    assert stored.external_id == "resend:rx-1"
    assert stored.body == "Attached."  # the reply, without the quoted request
    assert stored.from_addr == world.dispatcher.email
    assert [a.filename for a in stored.attachments] == ["pod.pdf"]


def test_the_api_key_never_goes_to_the_download_host(service, resend):
    world, client = service
    load = world.make_load()
    world.tick()
    deliver(client, world, resend.receive(world, load))
    downloads = [r for r in resend.requests if r.url.host != "api.resend.com"]
    assert downloads and all("authorization" not in r.headers for r in downloads)


def test_mail_routes_by_the_hash_in_the_reply_to_address(service, resend):
    world, client = service
    load = world.make_load()
    world.tick()
    event = resend.receive(world, load, to=(f"chase+brk-1@{INBOUND}",))
    assert deliver(client, world, event).json()["routed"] is True


def test_forwarded_mail_routes_by_the_original_recipient(service, resend):
    world, client = service
    load = world.make_load()
    world.tick()
    event = resend.receive(
        world,
        load,
        to=("forwarding@in.talonchase.example",),
        headers={
            "from": world.dispatcher.email,
            "x-forwarded-to": "pods@peachtreelogistics.example",
        },
    )
    assert deliver(client, world, event).json()["routed"] is True


def test_a_hash_on_a_foreign_domain_is_not_trusted(service, resend):
    world, client = service
    load = world.make_load()
    event = resend.receive(world, load, to=("chase+brk-1@someone-else.example",))
    assert deliver(client, world, event).json() == {"routed": False}


def test_mail_for_no_broker_is_kept_as_evidence(service, resend):
    world, client = service
    load = world.make_load()
    event = resend.receive(world, load, to=("someone@else.example",))
    assert deliver(client, world, event).json() == {"routed": False}
    (kept,) = [e for e in world.store.evidence("system") if e.kind == "inbound.unrouted"]
    assert kept.payload["to"] == ["someone@else.example"]
    assert kept.payload["external_id"] == "resend:rx-1"


def test_a_reply_that_is_html_only_is_read(service, resend):
    world, client = service
    load = world.make_load()
    world.tick()
    event = resend.receive(
        world,
        load,
        text=None,
        html="<div>Here you go &amp; thanks<br>Dana</div><blockquote>old</blockquote>",
        files=[],
    )
    deliver(client, world, event)
    (stored,) = world.store.find(Inbound)
    assert stored.body.startswith("Here you go & thanks")


def test_signature_logos_are_dropped_and_inline_photos_kept(service, resend):
    world, client = service
    load = world.make_load()
    world.tick()
    logo = b"\x89PNG" + b"0" * 2000
    photo = b"\xff\xd8" + b"1" * 60_000
    event = resend.receive(
        world,
        load,
        files=[
            ("logo.png", "image/png", logo, "l@x"),
            ("IMG_0042.jpg", "image/jpeg", photo, "p@x"),
            ("../../etc/passwd.pdf", "application/pdf", pod(load), None),
        ],
    )
    deliver(client, world, event)
    (stored,) = world.store.find(Inbound)
    assert [a.filename for a in stored.attachments] == ["IMG_0042.jpg", "etcpasswd.pdf"]


def test_a_fetch_that_fails_is_retried_by_resend_and_stored_once(service, resend):
    world, client = service
    load = world.make_load()
    world.tick()
    event = resend.receive(world, load)
    resend.fetch_failures = 1
    failed = deliver(client, world, event)
    assert failed.status_code == 502 and "could not fetch" in failed.json()["detail"]
    assert world.store.find(Inbound) == []
    assert deliver(client, world, event).json()["routed"] is True
    assert len(world.store.find(Inbound)) == 1


def test_an_attachment_link_that_fails_is_a_retry_not_a_loss(service, resend):
    world, client = service
    load = world.make_load()
    world.tick()
    event = resend.receive(world, load)
    resend.files.clear()  # the signed link answers 403
    assert deliver(client, world, event).status_code == 502
    assert world.store.find(Inbound) == []


def test_received_mail_event_needs_an_email_id(service):
    world, client = service
    assert deliver(client, world, {"type": "email.received", "data": {}}).status_code == 400


def test_receiving_without_a_fetcher_answers_503(resend):
    world = World()
    client = TestClient(create_app(world.engine, KEY, resend_webhook_secret=SECRET))
    load = world.make_load()
    event = resend.receive(world, load)
    assert deliver(client, world, event).status_code == 503


# ------------------------------------------------------------------- bounces


def bounce(world: World, load, **overrides) -> dict:
    sent = world.messages(load)[0]
    data = {
        "email_id": sent.provider_id,
        "created_at": "2026-11-10T20:05:00.000Z",
        "from": "pods@peachtreelogistics.example",
        "to": [world.dispatcher.email],
        "subject": sent.subject,
        "bounce": {
            "message": "The recipient's server rejected the message: user unknown.",
            "subType": "General",
            "type": "Permanent",
        },
    }
    data.update(overrides)
    return {"type": "email.bounced", "created_at": "2026-11-10T20:05:00.000Z", "data": data}


def test_a_hard_bounce_stops_that_address_and_hands_the_load_over(service):
    world, client = service
    load = world.make_load()
    world.tick()
    response = deliver(client, world, bounce(world, load), msg_id="msg_b1").json()
    assert response == {"handled": True, "recorded": True, "permanent": True}
    contact = world.store.get(Contact, world.dispatcher.id)
    assert contact.email_undeliverable_at_ms is not None
    assert world.chase(load).state is ChaseState.ESCALATED
    (escalation,) = world.escalations(load)
    assert escalation.reason is EscalationReason.SEND_FAILED and "bounced" in escalation.detail
    assert "message.bounced" in [e.kind for e in world.store.evidence(load.id)]

    repeat = deliver(client, world, bounce(world, load), msg_id="msg_b1").json()
    assert repeat["recorded"] is False
    assert len(world.escalations(load)) == 1


def test_with_another_dispatcher_left_the_chase_carries_on(service):
    world, client = service
    world.store.put(
        Contact("con-disp2", "car-1", Role.DISPATCHER, "Nights", "nights@redlinetrucking.example")
    )
    load = world.make_load()
    world.tick()
    deliver(client, world, bounce(world, load))
    assert world.chase(load).state is ChaseState.ASKED
    world.run(30 * HOUR_MS)
    later = [m for m in world.messages(load) if m.channel.value == "EMAIL"][1:]
    assert later and all(world.dispatcher.email not in m.to for m in later)
    assert all("nights@redlinetrucking.example" in m.to for m in later)


def test_a_temporary_bounce_is_only_recorded(service):
    world, client = service
    load = world.make_load()
    world.tick()
    soft = bounce(world, load, bounce={"type": "Temporary", "subType": "MailboxFull",
                                       "message": "Mailbox full"})  # fmt: skip
    assert deliver(client, world, soft).json()["permanent"] is False
    assert world.store.get(Contact, world.dispatcher.id).email_undeliverable_at_ms is None
    assert world.chase(load).state is ChaseState.ASKED


def test_a_spam_complaint_stops_that_address(service):
    world, client = service
    load = world.make_load()
    world.tick()
    complaint = bounce(world, load, bounce=None)
    complaint["type"] = "email.complained"
    assert deliver(client, world, complaint).json()["permanent"] is True
    assert world.store.get(Contact, world.dispatcher.id).email_undeliverable_at_ms is not None


def test_a_bounce_for_a_message_that_is_not_ours_is_kept_on_the_system_chain(service):
    world, client = service
    load = world.make_load()
    world.tick()
    deliver(client, world, bounce(world, load, email_id="not-ours"))
    (kept,) = [e for e in world.store.evidence("system") if e.kind == "message.bounced"]
    assert kept.payload["outbox_id"] == "" and kept.payload["permanent"] is True


def test_a_bounce_event_needs_recipients(service):
    world, client = service
    load = world.make_load()
    world.tick()
    assert deliver(client, world, bounce(world, load, to=[])).status_code == 400


def test_settings_lists_addresses_that_stopped_working(service):
    from tests.test_app import OWNER_EMAIL, sign_in

    world, client = service
    load = world.make_load()
    world.tick()
    deliver(client, world, bounce(world, load))
    world.store.put(dataclasses.replace(world.owner, email=OWNER_EMAIL))
    browser = TestClient(client.app, base_url="https://testserver")
    sign_in(world, browser)
    page = browser.get("/app/b/brk-1/settings").text
    assert "Email addresses that stopped working" in page
    assert re.search(rf"<td>{re.escape(world.dispatcher.email)}</td>\s*<td>Redline Trucking", page)
    story = browser.get(f"/app/b/brk-1/loads/{load.id}").text
    assert "bounced; that address will not be used again" in story


# ------------------------------------------------------------ reading a reply


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Attached.\n\nOn Tue, Nov 10, 2026 at 2:45 PM Peachtree <p@x> wrote:\n> hi", "Attached."),
        ("Done\nOn Tue, Nov 10, 2026 at 2:45 PM Peachtree Logistics\n<p@x> wrote:\n> q", "Done"),
        ("Sent it.\n\n-----Original Message-----\nFrom: p@x\nSent: Tue", "Sent it."),
        ("Sent it.\n\nFrom: Peachtree <p@x>\nSent: Tuesday, November 10\nTo: Dana\nSubject: POD",
         "Sent it."),  # Outlook
        ("Yes, tomorrow.\n> what time?\n> please", "Yes, tomorrow."),
        ("Line one\nLine two", "Line one\nLine two"),
        ("From the yard, signed.\nPOD attached", "From the yard, signed.\nPOD attached"),
        ("> only a quote", "> only a quote"),
        ("", ""),
        ("Am 10.11.2026 schrieb P <p@x>:\n> hallo", "Am 10.11.2026 schrieb P <p@x>:\n> hallo"),
    ],
)  # fmt: skip
def test_reply_text_drops_the_quoted_message(raw, expected):
    assert reply_text(raw) == expected


def test_html_to_text():
    markup = (
        "<style>p{}</style><p>Hi&nbsp;Dana</p><p>Line &lt;2&gt;<br/>three</p><script>x</script>"
    )
    assert html_to_text(markup) == "Hi Dana\nLine <2>\nthree"
    assert html_to_text("") == ""


def test_mailbox_hashes():
    to = ["chase+brk-1@in.x.example", "a+b@other.example", "plain@in.x.example", "c+@in.x.example"]
    assert mailbox_hashes(to) == ["brk-1", "b"]
    assert mailbox_hashes(to, "IN.x.example") == ["brk-1"]
    assert mailbox_hashes([]) == []
