"""Postmark: the sending adapter against a fake Postmark API, and both webhooks
end to end through the service. Payload shapes follow Postmark's documentation."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from chase.api.app import create_app
from chase.channels.postmark import METADATA_KEY, PostmarkEmail, PostmarkError, metadata_value
from chase.domain.models import (
    HOUR_MS,
    ChaseState,
    Contact,
    EscalationReason,
    OutboxMessage,
    OutboxStatus,
    Role,
)
from chase.ports import EmailMessage, OutgoingAttachment, PermanentSendError
from tests.conftest import World, pod

KEY = "test-api-key-0123456789"
USER, PASSWORD = "postmark", "p" * 32
AUTH = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()


# ------------------------------------------------------------- a fake Postmark


class FakePostmark:
    """Enough of Postmark's API to test against: /email and the outbound search."""

    def __init__(self) -> None:
        self.accepted: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.next_status: list[tuple[int, dict]] = []
        self.lose_next_response = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["X-Postmark-Server-Token"] == "server-token"
        assert request.headers["Accept"] == "application/json"
        if request.url.path == "/messages/outbound":
            key = request.url.params.get(f"metadata_{METADATA_KEY}")
            found = [m for m in self.accepted if m["Metadata"].get(METADATA_KEY) == key]
            return httpx.Response(
                200,
                json={
                    "TotalCount": len(found),
                    "Messages": [{"MessageID": m["id"]} for m in found],
                },
            )
        if self.next_status:
            status, body = self.next_status.pop(0)
            return httpx.Response(status, json=body)
        body = json.loads(request.content)
        body["id"] = f"pm-{len(self.accepted) + 1}"
        self.accepted.append(body)
        if self.lose_next_response:
            self.lose_next_response = False
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(
            200, json={"To": body["To"], "MessageID": body["id"], "ErrorCode": 0, "Message": "OK"}
        )

    def posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == "/email"]


@pytest.fixture
def postmark():
    return FakePostmark()


def adapter(fake: FakePostmark, blobs=None) -> PostmarkEmail:
    return PostmarkEmail(
        "server-token",
        blobs or (lambda sha: b"%PDF-1.4 pod" if sha == "abc" else None),
        transport=httpx.MockTransport(fake.handler),
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


def test_send_builds_the_documented_request(postmark):
    provider_id = adapter(postmark).send(message())
    assert provider_id == "pm-1"
    (request,) = postmark.posts()
    body = json.loads(request.content)
    assert request.url == "https://api.postmarkapp.com/email"
    assert body["From"] == "pods@peachtreelogistics.example"
    assert body["To"] == "dispatch@redlinetrucking.example"
    assert body["Cc"] == "ops@peachtreelogistics.example"
    assert body["TextBody"] == "Please send the POD." and body["MessageStream"] == "outbound"
    assert body["Metadata"] == {METADATA_KEY: metadata_value("attempt:att-000001")}
    assert body["TrackOpens"] is False and body["TrackLinks"] == "None"
    (attachment,) = body["Attachments"]
    assert attachment["Name"] == "pod.pdf" and attachment["ContentType"] == "application/pdf"
    assert base64.b64decode(attachment["Content"]) == b"%PDF-1.4 pod"


def test_a_retry_after_a_lost_response_does_not_send_twice(postmark):
    email = adapter(postmark)
    postmark.lose_next_response = True
    with pytest.raises(PostmarkError):
        email.send(message())
    assert len(postmark.accepted) == 1  # Postmark has it; we never heard back
    assert email.send(message(first_try=False)) == "pm-1"
    assert len(postmark.posts()) == 1 and len(postmark.accepted) == 1


def test_a_retry_that_finds_nothing_sends(postmark):
    email = adapter(postmark)
    assert email.send(message(first_try=False)) == "pm-1"
    assert len(postmark.posts()) == 1


@pytest.mark.parametrize(
    ("status", "body", "permanent"),
    [
        (422, {"ErrorCode": 406, "Message": "Recipient marked inactive."}, True),
        (422, {"ErrorCode": 300, "Message": "Invalid 'To' address"}, True),
        (401, {"ErrorCode": 10, "Message": "No Account or Server API tokens were supplied"}, False),
        (429, {"Message": "Rate limit exceeded"}, False),
        (500, {"Message": "Internal server error"}, False),
        (503, {"ErrorCode": 100, "Message": "Maintenance"}, False),
    ],
)  # fmt: skip
def test_errors_are_permanent_only_when_retrying_cannot_help(postmark, status, body, permanent):
    postmark.next_status.append((status, body))
    expected = PermanentSendError if permanent else PostmarkError
    with pytest.raises(expected) as caught:
        adapter(postmark).send(message())
    assert str(status) in str(caught.value)


def test_network_failures_are_temporary():
    def broken(request):
        raise httpx.ConnectError("no route", request=request)

    email = PostmarkEmail("t", lambda s: b"", transport=httpx.MockTransport(broken))
    with pytest.raises(PostmarkError):
        email.send(message(attachments=[]))


def test_a_missing_attachment_is_permanent(postmark):
    with pytest.raises(PermanentSendError):
        adapter(postmark, blobs=lambda sha: None).send(message())
    assert postmark.posts() == []


def test_the_outbox_retries_through_postmark_without_duplicates(postmark):
    world = World()
    world.engine.email = adapter(postmark, blobs=world.store.get_blob)
    load = world.make_load()
    postmark.lose_next_response = True
    world.tick()
    first = world.messages(load)[0]
    assert first.status is OutboxStatus.PENDING and first.tries == 1
    world.run(HOUR_MS)
    sent = world.store.get(OutboxMessage, first.id)
    assert sent.status is OutboxStatus.SENT and sent.provider_id == "pm-1"
    assert len(postmark.accepted) == 1, "the carrier must get one email, not two"


# ------------------------------------------------------------------ webhooks


@pytest.fixture
def service():
    world = World()
    client = TestClient(
        create_app(
            world.engine, KEY, postmark_webhook_user=USER, postmark_webhook_password=PASSWORD
        )
    )
    return world, client


def inbound_payload(world: World, load, **overrides) -> dict:
    """An inbound webhook body shaped like Postmark's documented example."""
    data = {
        "FromName": "Dana",
        "MessageStream": "inbound",
        "From": world.dispatcher.email,
        "FromFull": {"Email": world.dispatcher.email, "Name": "Dana", "MailboxHash": ""},
        "To": "pods@peachtreelogistics.example",
        "ToFull": [{"Email": "pods@peachtreelogistics.example", "Name": "", "MailboxHash": ""}],
        "Cc": "",
        "CcFull": [],
        "OriginalRecipient": "pods@peachtreelogistics.example",
        "Subject": f"Re: proof of delivery for load {load.load_number}",
        "MessageID": "73e6d360-66eb-11e1-8e72-a8904824019b",
        "ReplyTo": "",
        "MailboxHash": "",
        "Date": "Tue, 10 Nov 2026 14:45:00 -0600",
        "TextBody": "Attached.\n\n> On Tue, Peachtree wrote:\n> Please send the POD",
        "HtmlBody": "<p>Attached.</p>",
        "StrippedTextReply": "Attached.",
        "Tag": "",
        "Headers": [{"Name": "X-Spam-Status", "Value": "No"}],
        "Attachments": [
            {
                "Name": "pod.pdf",
                "Content": base64.b64encode(pod(load)).decode(),
                "ContentType": "application/pdf",
                "ContentLength": len(pod(load)),
            }
        ],
    }
    data.update(overrides)
    return data


def post(client, path, body, auth=AUTH):
    headers = {"Authorization": auth} if auth else {}
    return client.post(path, json=body, headers=headers)


def test_webhooks_refuse_without_the_right_credentials(service):
    world, client = service
    body = {"RecordType": "Bounce"}
    assert post(client, "/webhooks/postmark/events", body, auth=None).status_code == 401
    wrong = "Basic " + base64.b64encode(b"postmark:nope").decode()
    assert post(client, "/webhooks/postmark/inbound", body, auth=wrong).status_code == 401
    closed = TestClient(create_app(world.engine, KEY))
    assert post(closed, "/webhooks/postmark/inbound", body).status_code == 503


def test_a_reply_with_the_pod_is_filed_and_a_repeat_changes_nothing(service):
    world, client = service
    load = world.make_load()
    world.tick()
    body = inbound_payload(world, load)
    first = post(client, "/webhooks/postmark/inbound", body).json()
    assert first["routed"] is True
    world.settle()
    assert world.chase(load).state is ChaseState.CLOSED
    again = post(client, "/webhooks/postmark/inbound", body).json()
    assert again["inbound_id"] == first["inbound_id"]
    from chase.domain.models import Inbound

    stored = world.store.find(Inbound)
    assert len(stored) == 1 and stored[0].external_id == f"postmark:{body['MessageID']}"
    assert stored[0].body == "Attached."  # the reply, without the quoted request


def test_mail_routes_by_mailbox_hash_when_forwarded(service):
    world, client = service
    load = world.make_load()
    world.tick()
    body = inbound_payload(
        world,
        load,
        To="a1b2c3+brk-1@inbound.postmarkapp.com",
        ToFull=[{"Email": "a1b2c3+brk-1@inbound.postmarkapp.com", "MailboxHash": "brk-1"}],
        OriginalRecipient="a1b2c3+brk-1@inbound.postmarkapp.com",
        MailboxHash="brk-1",
    )
    assert post(client, "/webhooks/postmark/inbound", body).json()["routed"] is True


def test_mail_for_no_broker_is_kept_as_evidence(service):
    world, client = service
    load = world.make_load()
    body = inbound_payload(world, load, To="someone@else.example", ToFull=[], OriginalRecipient="")
    assert post(client, "/webhooks/postmark/inbound", body).json() == {"routed": False}
    (event,) = [e for e in world.store.evidence("system") if e.kind == "inbound.unrouted"]
    assert event.payload["to"] == ["someone@else.example"]


def test_signature_logos_are_dropped_and_inline_photos_kept(service):
    world, client = service
    load = world.make_load()
    world.tick()
    logo = base64.b64encode(b"\x89PNG" + b"0" * 2000).decode()
    photo = base64.b64encode(b"\xff\xd8" + b"1" * 60_000).decode()
    body = inbound_payload(
        world,
        load,
        Attachments=[
            {"Name": "logo.png", "Content": logo, "ContentType": "image/png", "ContentID": "l@x"},
            {"Name": "IMG_0042.jpg", "Content": photo, "ContentType": "image/jpeg",
             "ContentID": "p@x"},
        ],
    )  # fmt: skip
    post(client, "/webhooks/postmark/inbound", body)
    from chase.domain.models import Inbound

    (stored,) = world.store.find(Inbound)
    assert [a.filename for a in stored.attachments] == ["IMG_0042.jpg"]


def bounce(world: World, load, **overrides) -> dict:
    sent = world.messages(load)[0]
    data = {
        "RecordType": "Bounce",
        "MessageStream": "outbound",
        "ID": 692560173,
        "Type": "HardBounce",
        "TypeCode": 1,
        "Name": "Hard bounce",
        "Tag": "",
        "MessageID": sent.provider_id,
        "Metadata": {},
        "ServerID": 23,
        "Description": "The server was unable to deliver your message (ex: unknown user).",
        "Details": "smtp;550 5.1.1 The email account that you tried to reach does not exist.",
        "Email": world.dispatcher.email,
        "From": "pods@peachtreelogistics.example",
        "BouncedAt": "2026-11-10T20:05:00Z",
        "DumpAvailable": True,
        "Inactive": True,
        "CanActivate": True,
        "Subject": sent.subject,
    }
    data.update(overrides)
    return data


def test_a_hard_bounce_stops_that_address_and_hands_the_load_over(service):
    world, client = service
    load = world.make_load()
    world.tick()
    response = post(client, "/webhooks/postmark/events", bounce(world, load)).json()
    assert response == {"handled": True, "recorded": True, "permanent": True}
    contact = world.store.get(Contact, world.dispatcher.id)
    assert contact.email_undeliverable_at_ms is not None
    assert world.chase(load).state is ChaseState.ESCALATED
    (escalation,) = world.escalations(load)
    assert escalation.reason is EscalationReason.SEND_FAILED and "bounced" in escalation.detail
    kinds = [e.kind for e in world.store.evidence(load.id)]
    assert "message.bounced" in kinds

    repeat = post(client, "/webhooks/postmark/events", bounce(world, load)).json()
    assert repeat["recorded"] is False
    assert len(world.escalations(load)) == 1


def test_with_another_dispatcher_left_the_chase_carries_on(service):
    world, client = service
    world.store.put(
        Contact("con-disp2", "car-1", Role.DISPATCHER, "Nights", "nights@redlinetrucking.example")
    )
    load = world.make_load()
    world.tick()
    post(client, "/webhooks/postmark/events", bounce(world, load))
    assert world.chase(load).state is ChaseState.ASKED
    world.run(30 * HOUR_MS)
    later = [m for m in world.messages(load) if m.channel.value == "EMAIL"][1:]
    assert later and all(world.dispatcher.email not in m.to for m in later)
    assert all("nights@redlinetrucking.example" in m.to for m in later)


def test_a_soft_bounce_is_only_recorded(service):
    world, client = service
    load = world.make_load()
    world.tick()
    soft = bounce(world, load, Type="SoftBounce", TypeCode=4096, Inactive=False, ID=7)
    assert post(client, "/webhooks/postmark/events", soft).json()["permanent"] is False
    assert world.store.get(Contact, world.dispatcher.id).email_undeliverable_at_ms is None
    assert world.chase(load).state is ChaseState.ASKED


def test_a_spam_complaint_stops_that_address(service):
    world, client = service
    load = world.make_load()
    world.tick()
    complaint = bounce(world, load, RecordType="SpamComplaint", Type="SpamComplaint",
                       TypeCode=512, ID=42)  # fmt: skip
    assert post(client, "/webhooks/postmark/events", complaint).json()["permanent"] is True
    assert world.store.get(Contact, world.dispatcher.id).email_undeliverable_at_ms is not None


def test_a_bounce_for_a_message_that_is_not_ours_is_kept_on_the_system_chain(service):
    world, client = service
    load = world.make_load()
    world.tick()
    stray = bounce(world, load, MessageID="not-ours", ID=99)
    post(client, "/webhooks/postmark/events", stray)
    (event,) = [e for e in world.store.evidence("system") if e.kind == "message.bounced"]
    assert event.payload["outbox_id"] == "" and event.payload["permanent"] is True


def test_other_events_are_acknowledged_and_ignored(service):
    world, client = service
    response = post(client, "/webhooks/postmark/events", {"RecordType": "Open", "MessageID": "x"})
    assert response.status_code == 200 and response.json()["handled"] is False


def test_the_service_uses_postmark_when_a_token_is_set(monkeypatch):
    from chase.api import main
    from chase.channels.fakes import FakeEmail
    from chase.store.memory import MemoryStore

    monkeypatch.delenv("POSTMARK_SERVER_TOKEN", raising=False)
    assert isinstance(main._email(MemoryStore()), FakeEmail)
    monkeypatch.setenv("POSTMARK_SERVER_TOKEN", "server-token")
    assert isinstance(main._email(MemoryStore()), PostmarkEmail)


def test_settings_lists_addresses_that_stopped_working(service):
    import re

    from tests.test_app import OWNER_EMAIL, sign_in

    world, client = service
    load = world.make_load()
    world.tick()
    post(client, "/webhooks/postmark/events", bounce(world, load))
    world.store.put(dataclass_replace(world.owner, email=OWNER_EMAIL))
    browser = TestClient(client.app, base_url="https://testserver")
    sign_in(world, browser)
    page = browser.get("/app/b/brk-1/settings").text
    assert "Email addresses that stopped working" in page
    assert re.search(rf"<td>{re.escape(world.dispatcher.email)}</td>\s*<td>Redline Trucking", page)
    story = browser.get(f"/app/b/brk-1/loads/{load.id}").text
    assert "bounced; that address will not be used again" in story


def dataclass_replace(obj, **changes):
    import dataclasses

    return dataclasses.replace(obj, **changes)
