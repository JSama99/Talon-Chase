"""The HTTP service, driven end to end through its public routes."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from chase.api.app import create_app
from chase.domain.models import HOUR_MS, Broker, Contact, Load
from chase.evidence import EvidenceEvent, verify_chain
from tests.conftest import World, pod

KEY = "test-api-key-0123456789"
H = {"X-API-Key": KEY}


@pytest.fixture
def api():
    world = World()
    client = TestClient(create_app(world.engine, KEY))
    return world, client


def new_load(world: World, client: TestClient, n: int = 1, **extra) -> dict:
    body = {
        "id": f"api-load-{n}",
        "broker_id": "brk-1",
        "carrier_id": "car-1",
        "load_number": f"TC-20{n:04d}",
        "delivery_appt_ms": world.clock.now_ms() - HOUR_MS,
        "delivered_at_ms": world.clock.now_ms(),
        "customer_name": "Acme Foods",
        "rate_cents": 185000,
    }
    body.update(extra)
    response = client.post("/v1/loads", json=body, headers=H)
    assert response.status_code == 200, response.text
    return body


def test_api_key_is_required_everywhere_except_health_and_upload(api):
    world, client = api
    assert client.get("/healthz").json() == {"ok": True}
    for method, path in [
        ("post", "/v1/loads"),
        ("post", "/v1/inbound"),
        ("post", "/v1/tick"),
        ("post", "/v1/dispatch"),
        ("get", "/v1/brokers/brk-1/queue"),
        ("get", "/v1/brokers/brk-1/summary"),
        ("get", "/v1/brokers/brk-1/chases"),
        ("get", "/v1/loads/x"),
        ("get", "/v1/loads/x/evidence"),
        ("post", "/v1/escalations/x/acknowledge"),
        ("post", "/v1/outbound/switch"),
        ("post", "/v1/contacts/approve"),
        ("put", "/v1/brokers/b"),
        ("put", "/v1/carriers/c"),
        ("put", "/v1/contacts/c"),
        ("patch", "/v1/brokers/brk-1/config"),
    ]:
        assert getattr(client, method)(path).status_code == 401, path
        assert getattr(client, method)(path, headers={"X-API-Key": "wrong"}).status_code == 401
    with pytest.raises(ValueError):
        create_app(world.engine, "short")


def test_full_flow_over_http(api):
    world, client = api
    load = new_load(world, client)
    ticked = client.post("/v1/tick", headers=H).json()
    assert ticked["steps"] == 1 and ticked["dispatch"]["sent"] == 1
    assert client.post("/v1/dispatch", headers=H).json()["sent"] == 0

    content = base64.b64encode(pod(world.store.get(Load, load["id"]))).decode()
    reply = {
        "broker_id": "brk-1",
        "channel": "EMAIL",
        "from_addr": world.dispatcher.email,
        "subject": f"Re: {load['load_number']}",
        "body": "Attached",
        "attachments": [{"filename": "pod.pdf", "content_base64": content}],
        "external_id": "<m1@redline>",
    }
    first = client.post("/v1/inbound", json=reply, headers=H)
    again = client.post("/v1/inbound", json=reply, headers=H)
    assert first.status_code == 200 and first.json()["id"] == again.json()["id"]

    detail = client.get(f"/v1/loads/{load['id']}", headers=H).json()
    assert detail["chase"]["state"] == "CLOSED" and detail["chase"]["closed_reason"] == "filed"
    assert [c["result"] for c in detail["checks"]] == ["PASS"]
    assert [w["status"] for w in detail["writebacks"]] == ["SUCCEEDED"]
    assert detail["load"]["ready_to_invoice_at_ms"] is not None
    assert len(detail["attempts"]) == 1 and len(detail["inbound"]) == 1
    assert [m["status"] for m in detail["messages"]] == ["SENT"]
    assert detail["attempts"][0]["outbox_id"] == detail["messages"][0]["id"]

    export = client.get(f"/v1/loads/{load['id']}/evidence", headers=H).json()
    assert export["verification"]["ok"]
    assert verify_chain([EvidenceEvent.from_dict(e) for e in export["events"]]).ok

    summary = client.get("/v1/brokers/brk-1/summary", headers=H).json()
    assert summary["filed"] == 1 and summary["by_state"]["CLOSED"] == 1
    rows = client.get("/v1/brokers/brk-1/chases?state=CLOSED", headers=H).json()
    assert [r["load_number"] for r in rows] == [load["load_number"]]


def test_upload_page_and_upload(api):
    world, client = api
    load = new_load(world, client)
    client.post("/v1/tick", headers=H)
    link = world.email.sent[0].body.split("upload it here:\n")[1].split("\n")[0]
    path = "/u/" + link.rsplit("/u/", 1)[1]

    page = client.get(path)
    assert page.status_code == 200 and load["load_number"] in page.text
    assert "Acme" not in page.text and "1850" not in page.text

    stored = world.store.get(Load, load["id"])
    assert client.post(path, content=b"").status_code == 422
    sent = client.post(f"{path}?filename=../../etc/IMG 1.jpg", content=pod(stored))
    assert sent.status_code == 200 and sent.json() == {"received": True}
    detail = client.get(f"/v1/loads/{load['id']}", headers=H).json()
    assert detail["chase"]["state"] == "CLOSED"
    assert detail["documents"][0]["filename"] == "....etcIMG 1.jpg"

    assert client.get("/u/not-a-token").status_code == 404
    assert client.post("/u/not-a-token", content=b"x").status_code == 404
    world.clock.advance(15 * 24 * HOUR_MS)
    assert client.get(path).status_code == 404


def test_queue_acknowledge_and_switch(api):
    world, client = api
    load = new_load(world, client)
    client.post("/v1/tick", headers=H)
    message = {
        "broker_id": "brk-1",
        "channel": "EMAIL",
        "from_addr": world.dispatcher.email,
        "subject": f"Re: {load['load_number']}",
        "body": "Please update our bank details first.",
    }
    assert client.post("/v1/inbound", json=message, headers=H).json()["status"] == "HELD"
    queue = client.get("/v1/brokers/brk-1/queue", headers=H).json()
    assert [e["reason"] for e in queue["escalations"]] == ["RISK"]
    escalation_id = queue["escalations"][0]["escalation_id"]

    assert (
        client.post(
            f"/v1/escalations/{escalation_id}/acknowledge", json={"by": ""}, headers=H
        ).status_code
        == 422
    )
    assert (
        client.post("/v1/escalations/nope/acknowledge", json={"by": "J"}, headers=H).status_code
        == 404
    )
    done = client.post(
        f"/v1/escalations/{escalation_id}/acknowledge",
        json={"by": "Jermaine", "note": "called carrier on the number we have"},
        headers=H,
    )
    assert done.json()["acknowledged_by"] == "Jermaine"
    assert client.get("/v1/brokers/brk-1/queue", headers=H).json()["escalations"] == []

    off = client.post("/v1/outbound/switch", json={"enabled": False, "by": "J"}, headers=H)
    assert off.json() == {"enabled": False, "scope": "global"}
    new_load(world, client, n=2)
    sent = len(world.email.sent)
    client.post("/v1/tick", headers=H)
    assert len(world.email.sent) == sent
    assert (
        client.get("/v1/brokers/brk-1/summary", headers=H).json()["global_outbound_enabled"]
        is False
    )
    scoped = client.post(
        "/v1/outbound/switch", json={"enabled": False, "by": "J", "broker_id": "brk-1"}, headers=H
    )
    assert scoped.json()["scope"] == "brk-1"


def test_setup_routes_and_validation(api):
    world, client = api
    broker = {
        "id": "brk-9",
        "name": "New Broker",
        "chase_mailbox": "pods@n.example",
        "rep_email": "o@n.example",
    }
    assert (
        client.put("/v1/brokers/brk-9", json=broker, headers=H).json()["adapter"] == "packet_email"
    )
    assert client.put("/v1/brokers/other", json=broker, headers=H).status_code == 422

    carrier = {
        "id": "car-9",
        "broker_id": "brk-9",
        "name": "C",
        "mc_number": "MC1",
        "timezone": "Mars/Olympus",
    }
    assert client.put("/v1/carriers/car-9", json=carrier, headers=H).status_code == 422
    carrier["timezone"] = "America/Denver"
    assert client.put("/v1/carriers/car-9", json=carrier, headers=H).status_code == 200
    contact = {"id": "con-9", "carrier_id": "car-9", "role": "DISPATCHER", "email": "d@c.example"}
    assert client.put("/v1/contacts/con-9", json=contact, headers=H).json()["verified"] is True
    stopped = world.store.get(Contact, "con-9")
    stopped.sms_stopped = True
    world.store.put(stopped)
    resynced = client.put("/v1/contacts/con-9", json=contact | {"sms_consent": True}, headers=H)
    assert resynced.json()["sms_stopped"] is True  # a STOP survives a re-sync
    assert (
        client.post(
            "/v1/contacts/approve",
            json={"carrier_id": "car-9", "role": "DRIVER", "by": "J"},
            headers=H,
        ).status_code
        == 422
    )

    # A load cannot name another broker's carrier.
    bad = client.post(
        "/v1/loads",
        json={
            "id": "x",
            "broker_id": "brk-1",
            "carrier_id": "car-9",
            "load_number": "X",
            "delivery_appt_ms": 1,
        },
        headers=H,
    )
    assert bad.status_code == 422
    assert (
        client.post(
            "/v1/loads",
            json={
                "id": "x",
                "broker_id": "brk-1",
                "carrier_id": "car-1",
                "load_number": "X",
                "delivery_appt_ms": 1.5,
            },
            headers=H,
        ).status_code
        == 422
    )

    cfg = client.patch("/v1/brokers/brk-9/config", json={"max_reasks": 5}, headers=H)
    assert cfg.json()["max_reasks"] == 5
    for bad_cfg in (
        {"nope": 1},
        {"max_reasks": -1},
        {"max_reasks": "5"},
        {"quiet_start_hour": 3},
        {"confidence_floor": 101},
    ):
        assert (
            client.patch("/v1/brokers/brk-9/config", json=bad_cfg, headers=H).status_code == 422
        ), bad_cfg
    assert world.store.get(Broker, "brk-9").config.max_reasks == 5

    bad_attachment = {
        "broker_id": "brk-1",
        "channel": "EMAIL",
        "from_addr": "a@b.example",
        "attachments": [{"filename": "x", "content_base64": "not base64!!"}],
    }
    assert client.post("/v1/inbound", json=bad_attachment, headers=H).status_code == 422
    assert (
        client.post(
            "/v1/inbound",
            json={"broker_id": "brk-1", "channel": "UPLOAD", "from_addr": "x"},
            headers=H,
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/v1/inbound",
            json={"broker_id": "nope", "channel": "EMAIL", "from_addr": "x"},
            headers=H,
        ).status_code
        == 404
    )
