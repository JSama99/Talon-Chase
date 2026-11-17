"""The broker screens, driven through a browser-like client.

Covers sign-in end to end, every screen, every action, and the rules that keep
one broker's people out of another broker's data.
"""

from __future__ import annotations

import dataclasses
import re

import pytest
from fastapi.testclient import TestClient

from chase.api.app import create_app
from chase.app import timeline, wording
from chase.app.auth import BadToken, Signer, make_link_token
from chase.app.views import message_ref
from chase.domain.models import (
    HOUR_MS,
    MINUTE_MS,
    Broker,
    Carrier,
    Channel,
    ChaseState,
    ConsentSource,
    Contact,
    Escalation,
    EscalationReason,
    EscalationResolution,
    Load,
    Operator,
    OperatorRole,
    Role,
)
from chase.ontology.events import EVENT_PAYLOADS, RETIRED_PAYLOADS
from chase.reader.rules import StandInReader
from tests.conftest import World, pod

KEY = "test-api-key-0123456789"
OWNER_EMAIL = "jermaine@peachtreelogistics.example"
STAFF_EMAIL = "dana@peachtreelogistics.example"


def busy(world: World) -> dict[str, Load]:
    """A morning's worth of work: filed, still chasing, held, a new address, a note."""
    world.store.put(dataclasses.replace(world.owner, email=OWNER_EMAIL))
    world.store.put(Operator("op-dana", "brk-1", "Dana Ops", OperatorRole.STAFF, STAFF_EMAIL))
    filed, chasing, risky, newcomer, noted = (world.make_load() for _ in range(5))
    world.tick()
    world.send_pod(filed)
    world.reply(risky, "POD attached. Also our bank details changed, update the remit-to.")
    world.reply(
        newcomer,
        "Here it is",
        [("pod.pdf", pod(newcomer))],
        sender="nights@redlinetrucking.example",
    )
    world.send_pod(noted, exception_notes=["2 cartons damaged"])
    world.inbound(
        broker_id="brk-1",
        channel=Channel.EMAIL,
        from_addr="ops@cedarcarriers.example",
        subject="Any loads out of Dallas this week",
        body="",
        attachments=[],
    )
    world.clock.advance(2 * HOUR_MS)
    return {
        "filed": filed,
        "chasing": chasing,
        "risky": risky,
        "newcomer": newcomer,
        "noted": noted,
    }


@pytest.fixture
def app_world():
    world = World()
    loads = busy(world)
    client = TestClient(create_app(world.engine, KEY), base_url="https://testserver")
    return world, client, loads


def sign_in(world: World, client: TestClient, email: str = OWNER_EMAIL) -> None:
    world.clock.advance(2 * MINUTE_MS)  # step past the resend throttle
    sent = len(world.email.sent)
    page = client.post("/app/login", data={"email": email, "next": "/app"})
    assert page.status_code == 200 and "Check your email" in page.text
    assert len(world.email.sent) == sent + 1, "no sign-in email was sent"
    link = re.search(r"https://\S+/app/login/(\S+)\?next=", world.email.sent[-1].body)
    token = link.group(1)
    confirm = client.get(f"/app/login/{token}")
    assert confirm.status_code == 200 and "Continue" in confirm.text
    done = client.post(f"/app/login/{token}", data={"next": "/app"}, follow_redirects=False)
    assert done.status_code == 303 and done.headers["location"] == "/app"


def csrf(client: TestClient, path: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


# ------------------------------------------------------------------ sign-in


def test_pages_need_a_session(app_world):
    _, client, _ = app_world
    for path in ("/app", "/app/b/brk-1", "/app/b/brk-1/settings", "/app/b/brk-1/chases"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/app/login?next=")


def test_sign_in_with_an_emailed_link(app_world):
    world, client, _ = app_world
    sign_in(world, client)
    home = client.get("/app/b/brk-1")
    assert home.status_code == 200 and "loads need you" in home.text
    signed = [e for e in world.store.evidence("broker:brk-1") if e.kind == "operator.signed_in"]
    assert signed[-1].payload == {"operator_id": "op-jermaine"}
    cookie = client.cookies.jar
    session = next(c for c in cookie if c.name == "tc_session")
    assert session.secure and session.path == "/app"


def test_a_sign_in_link_works_once_and_expires(app_world):
    world, client, _ = app_world
    sign_in(world, client)
    token = re.search(r"/app/login/(\S+)\?next=", world.email.sent[-1].body).group(1)
    again = client.post(f"/app/login/{token}", data={"next": "/app"})
    assert again.status_code == 400 and "already used" in again.text

    fresh = make_link_token(Signer(world.engine.secret), "op-jermaine", world.clock.now_ms())
    world.clock.advance(16 * MINUTE_MS)
    assert client.get(f"/app/login/{fresh}").status_code == 400
    assert client.get("/app/login/not-a-token").status_code == 400


def test_unknown_addresses_get_the_same_answer_and_no_email(app_world):
    world, client, _ = app_world
    sent = len(world.email.sent)
    page = client.post("/app/login", data={"email": "stranger@example.com"})
    assert page.status_code == 200 and "Check your email" in page.text
    assert len(world.email.sent) == sent


def test_links_are_throttled_per_address(app_world):
    world, client, _ = app_world
    sent = len(world.email.sent)
    client.post("/app/login", data={"email": OWNER_EMAIL})
    client.post("/app/login", data={"email": OWNER_EMAIL})
    assert len(world.email.sent) == sent + 1
    world.clock.advance(61_000)
    client.post("/app/login", data={"email": OWNER_EMAIL})
    assert len(world.email.sent) == sent + 2


def test_next_only_goes_inside_the_app(app_world):
    world, client, _ = app_world
    world.clock.advance(2 * MINUTE_MS)
    client.post("/app/login", data={"email": OWNER_EMAIL, "next": "https://evil.example/x"})
    assert "next=/app" in world.email.sent[-1].body
    assert "evil" not in world.email.sent[-1].body
    token = re.search(r"/app/login/(\S+)\?next=", world.email.sent[-1].body).group(1)
    done = client.post(
        f"/app/login/{token}", data={"next": "//evil.example"}, follow_redirects=False
    )
    assert done.headers["location"] == "/app"


def test_a_turned_off_operator_is_signed_out_at_once(app_world):
    world, client, _ = app_world
    sign_in(world, client, STAFF_EMAIL)
    assert client.get("/app/b/brk-1").status_code == 200
    world.engine.save_operator(
        dataclasses.replace(world.store.get(Operator, "op-dana"), active=False), by="op-jermaine"
    )
    gone = client.get("/app/b/brk-1", follow_redirects=False)
    assert gone.status_code == 303 and "/app/login" in gone.headers["location"]


def test_a_forged_or_expired_session_is_refused(app_world):
    world, client, _ = app_world
    client.cookies.set("tc_session", "eyJvcCI6Im9wLWplcm1haW5lIn0.forged", path="/app")
    assert client.get("/app/b/brk-1", follow_redirects=False).status_code == 303
    sign_in(world, client)
    world.clock.advance(13 * HOUR_MS)
    assert client.get("/app/b/brk-1", follow_redirects=False).status_code == 303


def test_sign_out(app_world):
    world, client, _ = app_world
    sign_in(world, client)
    token = csrf(client, "/app/b/brk-1")
    out = client.post("/app/logout", data={"csrf": token}, follow_redirects=False)
    assert out.status_code == 303
    assert client.get("/app/b/brk-1", follow_redirects=False).status_code == 303


# ------------------------------------------------------------------ screens


def test_needs_you_lists_every_open_escalation_oldest_first(app_world):
    world, client, loads = app_world
    sign_in(world, client)
    page = client.get("/app/b/brk-1").text
    numbers = re.findall(r'class="row-load">([^<]+)<', page)
    assert numbers == [loads["risky"].load_number, loads["newcomer"].load_number,
                       loads["noted"].load_number]  # fmt: skip
    assert "Held: mentions payment details" in page and 'class="bar red"' in page
    assert "No reply was sent." in page
    assert "New address at this carrier. Approve it?" in page
    assert "(312) 555-0142" in page  # the driver's number, formatted
    assert "ops@cedarcarriers.example" in page and "Any loads out of Dallas" in page
    assert "(message inb-" not in page  # internal ids stay out of the wording
    assert '<span class="count">3</span>' in page


def test_nothing_needs_you(app_world):
    world, client, _ = app_world
    for escalation in world.store.find(Escalation):
        world.engine.acknowledge_escalation(escalation.id, by="op-jermaine", resolution="WAIVED")
    sign_in(world, client)
    page = client.get("/app/b/brk-1").text
    assert "Nothing needs you." in page and 'class="count"' not in page


def test_load_page_tells_the_story_and_offers_the_call(app_world):
    world, client, loads = app_world
    sign_in(world, client)
    page = client.get(f"/app/b/brk-1/loads/{loads['noted'].id}").text
    assert "Asked the dispatcher by email" in page
    assert "Carrier replied with a file" in page
    assert "Checked the document: note found" in page and "2 cartons damaged" in page
    assert "Stopped and handed to you" in page
    assert "Needs you: POD has a note" in page
    assert "Call the carrier, then take this load." in page
    assert "chain verifies" in page and "Export evidence" in page
    assert "Acme Foods" in page  # the broker sees the customer
    for resolution in EscalationResolution:
        assert wording.RESOLUTIONS[resolution] in page


def test_resolving_closes_the_chase_and_records_the_operator(app_world):
    world, client, loads = app_world
    sign_in(world, client)
    load = loads["noted"]
    escalation = world.escalations(load)[0]
    path = f"/app/b/brk-1/loads/{load.id}"
    token = csrf(client, path)

    no_note = client.post(
        f"/app/b/brk-1/escalations/{escalation.id}/resolve",
        data={"csrf": token, "resolution": "OTHER", "note": ""},
        follow_redirects=False,
    )
    assert no_note.status_code == 303 and no_note.headers["location"] == path
    assert "say what was done" in client.get(path).text.lower()

    done = client.post(
        f"/app/b/brk-1/escalations/{escalation.id}/resolve",
        data={"csrf": token, "resolution": "HANDLED_BY_HAND", "note": "Called, refused 2"},
        follow_redirects=False,
    )
    assert done.headers["location"] == "/app/b/brk-1"
    assert f"{load.load_number} is settled." in client.get("/app/b/brk-1").text
    settled = world.store.get(Escalation, escalation.id)
    assert settled.acknowledged_by == "op-jermaine"
    assert settled.resolution is EscalationResolution.HANDLED_BY_HAND
    assert world.chase(load).state is ChaseState.CLOSED
    after = client.get(path).text
    assert "Jermaine: got it myself" in after and "Settled by a person" in after


def test_approving_a_new_address_adds_the_contact(app_world):
    world, client, loads = app_world
    sign_in(world, client)
    load = loads["newcomer"]
    path = f"/app/b/brk-1/loads/{load.id}"
    page = client.get(path).text
    assert "Approve this address" in page and "nights@redlinetrucking.example" in page
    inbound_id = re.search(r"/messages/([^/]+)/approve-sender", page).group(1)
    attachment = client.get(f"/app/b/brk-1/messages/{inbound_id}/files/0")
    assert (
        attachment.status_code == 200 and "sandbox" in attachment.headers["content-security-policy"]
    )
    client.post(
        f"/app/b/brk-1/messages/{inbound_id}/approve-sender",
        data={"csrf": csrf(client, path), "role": "DISPATCHER"},
    )
    added = [c for c in world.store.find(Contact) if c.email == "nights@redlinetrucking.example"]
    assert added and added[0].role is Role.DISPATCHER and added[0].source == "approved:op-jermaine"


def test_documents_and_evidence_download(app_world):
    world, client, loads = app_world
    sign_in(world, client)
    page = client.get(f"/app/b/brk-1/loads/{loads['filed'].id}").text
    assert "Ready to invoice" in page
    doc_id = re.search(r"/documents/([^\"]+)\"", page).group(1)
    file = client.get(f"/app/b/brk-1/documents/{doc_id}")
    assert file.status_code == 200 and file.headers["content-type"] == "application/pdf"
    assert file.headers["content-disposition"].startswith("inline")
    evidence = client.get(f"/app/b/brk-1/loads/{loads['filed'].id}/evidence.json")
    assert evidence.status_code == 200 and evidence.json()["verification"]["ok"] is True
    assert "attachment" in evidence.headers["content-disposition"]


def test_all_chases_filters_by_state(app_world):
    world, client, loads = app_world
    sign_in(world, client)
    everything = client.get("/app/b/brk-1/chases").text
    assert all(load.load_number in everything for load in loads.values())
    escalated = client.get("/app/b/brk-1/chases?state=ESCALATED").text
    assert loads["risky"].load_number in escalated
    assert loads["filed"].load_number not in escalated
    assert client.get("/app/b/brk-1/chases?state=NONSENSE").status_code == 200


# ------------------------------------------------------------------ settings


def test_anyone_can_stop_messages_but_only_the_owner_starts_them(app_world):
    world, client, _ = app_world
    sign_in(world, client, STAFF_EMAIL)
    token = csrf(client, "/app/b/brk-1/settings")
    client.post("/app/b/brk-1/outbound", data={"csrf": token, "enabled": "0"})
    assert world.store.get(Broker, "brk-1").outbound_enabled is False
    page = client.get("/app/b/brk-1/settings").text
    assert "Messages to carriers are off" in page and "Only the owner" in page
    refused = client.post("/app/b/brk-1/outbound", data={"csrf": token, "enabled": "1"})
    assert refused.status_code == 403
    assert world.store.get(Broker, "brk-1").outbound_enabled is False
    home = client.get("/app/b/brk-1").text
    assert "Messages to carriers are off." in home

    client.cookies.clear()
    sign_in(world, client)
    client.post(
        "/app/b/brk-1/outbound",
        data={"csrf": csrf(client, "/app/b/brk-1/settings"), "enabled": "1"},
    )
    assert world.store.get(Broker, "brk-1").outbound_enabled is True


def test_owner_changes_the_schedule_staff_cannot(app_world):
    world, client, _ = app_world
    form = {
        "sms_delay_hours": "4", "third_attempt_hours": "36", "escalate_wait_hours": "12",
        "morning_hour": "9", "quiet_start_hour": "19", "quiet_end_hour": "6",
        "confidence_floor": "85", "max_reasks": "2", "timezone": "America/Chicago",
    }  # fmt: skip
    sign_in(world, client, STAFF_EMAIL)
    page = client.get("/app/b/brk-1/settings").text
    assert "disabled" in page and "Only the owner can change these." in page
    token = csrf(client, "/app/b/brk-1/settings")
    assert (
        client.post("/app/b/brk-1/settings/schedule", data=form | {"csrf": token}).status_code
        == 403
    )

    client.cookies.clear()
    sign_in(world, client)
    token = csrf(client, "/app/b/brk-1/settings")
    client.post("/app/b/brk-1/settings/schedule", data=form | {"csrf": token})
    cfg = world.store.get(Broker, "brk-1").config
    assert (cfg.sms_delay_ms, cfg.third_attempt_ms, cfg.escalate_wait_ms) == (
        4 * HOUR_MS, 36 * HOUR_MS, 12 * HOUR_MS,
    )  # fmt: skip
    assert (cfg.morning_hour, cfg.confidence_floor, cfg.max_reasks, cfg.timezone) == (
        9, 85, 2, "America/Chicago",
    )  # fmt: skip
    change = [e for e in world.store.evidence("broker:brk-1") if e.kind == "config.changed"][-1]
    assert change.payload["by"] == "op-jermaine" and "timezone" in change.payload["fields"]

    bad = form | {"csrf": token, "morning_hour": "22"}
    assert "hours must satisfy" in client.post("/app/b/brk-1/settings/schedule", data=bad).text
    assert world.store.get(Broker, "brk-1").config.morning_hour == 9


def test_owner_adds_and_turns_off_people(app_world):
    world, client, _ = app_world
    sign_in(world, client)
    token = csrf(client, "/app/b/brk-1/settings")
    client.post(
        "/app/b/brk-1/people",
        data={"csrf": token, "name": "Kim", "email": "Kim@Peachtreelogistics.example",
              "role": "STAFF"},
    )  # fmt: skip
    kim = next(o for o in world.store.find(Operator) if o.name == "Kim")
    assert kim.email == "kim@peachtreelogistics.example" and kim.broker_id == "brk-1"

    duplicate = {"csrf": token, "name": "Kim 2", "email": "kim@peachtreelogistics.example",
                 "role": "STAFF"}  # fmt: skip
    assert "already signs in" in client.post("/app/b/brk-1/people", data=duplicate).text

    client.post("/app/b/brk-1/people", data={"csrf": token, "id": kim.id, "action": "deactivate"})
    assert world.store.get(Operator, kim.id).active is False


def test_the_last_owner_cannot_be_turned_off():
    world = World()
    with pytest.raises(ValueError, match="at least one active owner"):
        world.engine.save_operator(dataclasses.replace(world.owner, active=False))
    with pytest.raises(ValueError, match="at least one active owner"):
        world.engine.save_operator(
            dataclasses.replace(world.owner, role=OperatorRole.STAFF), by="op-platform"
        )


def test_staff_cannot_manage_people(app_world):
    world, client, _ = app_world
    sign_in(world, client, STAFF_EMAIL)
    token = csrf(client, "/app/b/brk-1/settings")
    refused = client.post(
        "/app/b/brk-1/people",
        data={"csrf": token, "name": "Me Too", "email": "me@x.example", "role": "OWNER"},
    )
    assert refused.status_code == 403


def test_texts_after_stop_come_back_only_with_written_consent(app_world):
    world, client, _ = app_world
    world.inbound(
        broker_id="brk-1", channel=Channel.SMS, from_addr=world.driver.phone, subject="",
        body="STOP", attachments=[],
    )  # fmt: skip
    sign_in(world, client)
    settings = client.get("/app/b/brk-1/settings").text
    assert "Replied STOP" in settings
    path = f"/app/b/brk-1/contacts/{world.driver.id}"
    page = client.get(path).text
    assert "Texts stay off until you record new written consent" in page
    assert "Replied STOP" in page
    token = csrf(client, path)
    empty = client.post(f"{path}/consent", data={"csrf": token, "granted": "1", "note": ""})
    assert "Say where the written consent is kept." in empty.text
    assert world.engine.sms_allowed(world.driver.id) is False
    client.post(
        f"{path}/consent", data={"csrf": token, "granted": "1", "note": "Signed form, 15 Nov"}
    )
    assert world.engine.sms_allowed(world.driver.id) is True
    latest = world.engine.latest_consent(world.driver.id)
    assert latest.source is ConsentSource.WRITTEN and latest.recorded_by == "op-jermaine"


# ------------------------------------------------------------------ security


def test_every_form_needs_its_session_token(app_world):
    world, client, loads = app_world
    sign_in(world, client)
    escalation = world.escalations(loads["noted"])[0]
    for path, data in (
        ("/app/b/brk-1/outbound", {"enabled": "0"}),
        (f"/app/b/brk-1/escalations/{escalation.id}/resolve", {"resolution": "WAIVED"}),
        ("/app/b/brk-1/settings/schedule", {}),
        ("/app/b/brk-1/people", {"name": "X", "email": "x@x.example", "role": "STAFF"}),
        (f"/app/b/brk-1/contacts/{world.driver.id}/consent", {"granted": "0"}),
        ("/app/logout", {}),
    ):
        for token in (None, "wrong"):
            body = data | ({"csrf": token} if token else {})
            assert client.post(path, data=body).status_code == 403, path
    assert world.store.get(Broker, "brk-1").outbound_enabled is True
    assert world.store.get(Escalation, escalation.id).resolution is None


def test_people_cannot_see_another_brokers_work(app_world):
    world, client, loads = app_world
    world.store.put(Broker("brk-2", "Other Co", "pods@other.example", "ops@other.example"))
    world.store.put(Carrier("car-2", "brk-2", "Far Away", "MC9", "America/Denver"))
    world.engine.save_operator(
        Operator("op-other", "brk-2", "Olga", OperatorRole.OWNER, "olga@other.example")
    )
    sign_in(world, client, "olga@other.example")
    load = loads["noted"]
    assert client.get("/app/b/brk-1").status_code == 403
    assert client.get(f"/app/b/brk-1/loads/{load.id}").status_code == 403
    assert client.get(f"/app/b/brk-2/loads/{load.id}").status_code == 404
    assert client.get(f"/app/b/brk-1/loads/{load.id}/evidence.json").status_code == 403
    assert client.get(f"/app/b/brk-2/contacts/{world.driver.id}").status_code == 404
    escalation = world.escalations(load)[0]
    token = csrf(client, "/app/b/brk-2")
    refused = client.post(
        f"/app/b/brk-2/escalations/{escalation.id}/resolve",
        data={"csrf": token, "resolution": "WAIVED"},
    )
    assert refused.status_code == 404
    assert world.store.get(Escalation, escalation.id).resolution is None


def test_platform_operators_choose_a_broker(app_world):
    world, client, _ = app_world
    world.store.put(dataclasses.replace(world.platform, email="oncall@talonsight.example"))
    sign_in(world, client, "oncall@talonsight.example")
    home = client.get("/app", follow_redirects=False)
    assert home.headers["location"] == "/app/brokers"
    assert "Peachtree Logistics" in client.get("/app/brokers").text
    assert client.get("/app/b/brk-1").status_code == 200


def test_screens_send_strict_security_headers(app_world):
    world, client, _ = app_world
    sign_in(world, client)
    response = client.get("/app/b/brk-1")
    csp = response.headers["content-security-policy"]
    assert "script-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"


def test_carrier_text_is_escaped(app_world):
    world, client, loads = app_world
    world.inbound(
        broker_id="brk-1", channel=Channel.EMAIL, from_addr="x@cedarcarriers.example",
        subject="<script>alert(1)</script>", body="", attachments=[],
    )  # fmt: skip
    sign_in(world, client)
    page = client.get("/app/b/brk-1").text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


# ------------------------------------------------------------------ wording


def test_every_code_has_words():
    for reason in EscalationReason:
        for table in (
            wording.REASON_TITLES,
            wording.REASON_BADGES,
            wording.REASON_ADVICE,
        ):
            assert reason in table, reason
    assert set(wording.RESOLUTIONS) == set(EscalationResolution)
    assert set(wording.STATE_WORDS) == set(ChaseState)


def test_every_load_evidence_kind_is_told_or_deliberately_quiet():
    broker_only = {
        "inbound.unmatched", "contact.approved", "consent.recorded", "outbound.switch",
        "config.changed", "operator.saved", "operator.signed_in", "inbound.unrouted",
    }  # fmt: skip
    on_loads = set(EVENT_PAYLOADS) - broker_only
    # Retired kinds stay told, so a chain written before they were retired still reads.
    assert on_loads == (timeline.TOLD | timeline.QUIET) - set(RETIRED_PAYLOADS)
    assert not timeline.TOLD & timeline.QUIET


def test_times_durations_and_phones():
    tz = "America/New_York"
    now = 1_794_340_800_000  # Tue 10 Nov 2026, 15:00 New York
    assert wording.when(now - 30 * MINUTE_MS, now, tz) == "Today 2:30 pm"
    assert wording.when(now - 2 * 24 * HOUR_MS, now, tz) == "Sun 3:00 pm"
    assert (
        wording.when(now - 10 * 24 * HOUR_MS, now, tz) == "Sat 31 Oct, 4:00 pm"
    )  # still daylight time then
    assert [wording.waited(x) for x in (0, 40 * MINUTE_MS, HOUR_MS, 6 * HOUR_MS, 50 * HOUR_MS)] == [
        "1 min", "40 min", "1 hour", "6 hours", "2 days",
    ]  # fmt: skip
    assert wording.short_duration(5 * HOUR_MS) == "5 h" and wording.short_duration(None) == "–"
    assert wording.phone("+1 (312) 555-0142") == "(312) 555-0142"
    assert wording.phone("+44 20 7946 0958") == "+44 20 7946 0958"
    assert wording.tel("+1 (312) 555-0142") == "tel:+13125550142"


def test_message_reference_is_split_from_the_detail():
    assert message_ref("message mentions bank (message inb-000004)") == (
        "message mentions bank",
        "inb-000004",
    )
    assert message_ref("no document after 3 attempts") == ("no document after 3 attempts", "")


def test_tokens_cannot_cross_purposes():
    signer = Signer(b"x" * 32)
    link = make_link_token(signer, "op-1", 0)
    with pytest.raises(BadToken):
        signer.read("session", link, 1)
    assert signer.read("link", link, 1)["op"] == "op-1"
    with pytest.raises(BadToken):
        Signer(b"y" * 32).read("link", link, 1)


def test_the_preview_renders_every_screen(tmp_path):
    from chase.app.preview import render

    written = render(tmp_path)
    assert {p.name for p in written} == {
        "needs-you.html", "load.html", "load-held.html", "load-filed.html",
        "chases.html", "settings.html", "contact.html", "login.html",
    }  # fmt: skip
    home = (tmp_path / "needs-you.html").read_text()
    assert "Redline Trucking wrote about bank, remit. No reply was sent." in home
    assert "POD has a note: 2 cartons damaged" in home
    assert "Last asked" in home and "with you copied" in home
    load = (tmp_path / "load.html").read_text()
    assert "Receiver wrote “2 cartons damaged”" in load and "pod.pdf" in load
    assert not re.search(r"[0-9a-f]{40}", load.split("head ")[0])  # no hashes in the story


# --- PR #4 review -----------------------------------------------------------------


def test_the_story_says_asked_only_once_a_message_is_delivered():
    """Codex: a queued request must not read as sent, and a late delivery shows the
    time it went out, not when it was queued."""
    world = World()
    load = world.make_load()
    world.engine.tick()  # decided and queued...
    world.engine.set_broker_outbound("brk-1", False, by="op-jermaine")
    world.outbox.dispatch()  # ...then held by the off switch before delivery
    told = timeline.tell(world.store.evidence(load.id))
    titles = [e.title for e in told]
    assert not any(t.startswith("Asked") for t in titles), titles
    waiting = next(e for e in told if e.title.startswith("Waiting to send"))
    assert "dispatcher by email" in waiting.title

    world.clock.advance(2 * HOUR_MS)
    world.engine.set_broker_outbound("brk-1", True, by="op-jermaine")
    world.settle()
    told = timeline.tell(world.store.evidence(load.id))
    asked = [e for e in told if e.title == "Asked the dispatcher by email"]
    assert len(asked) == 1 and asked[0].at_ms == world.clock.now_ms()
    assert not any(e.title.startswith("Waiting to send") for e in told)


def test_saving_settings_keeps_sub_hour_values_it_did_not_change(app_world):
    """Codex: a 30-minute delay set through the API survives an unrelated save."""
    world, client, _ = app_world
    world.engine.update_broker_config("brk-1", {"sms_delay_ms": 30 * MINUTE_MS})
    sign_in(world, client)
    page = client.get("/app/b/brk-1/settings").text
    shown = re.search(r'name="sms_delay_hours" value="([^"]+)"', page).group(1)
    assert shown == "0.5"
    form = {
        k: re.search(rf'name="{k}" value="([^"]+)"', page).group(1)
        for k in (
            "sms_delay_hours", "third_attempt_hours", "escalate_wait_hours", "morning_hour",
            "quiet_start_hour", "quiet_end_hour", "confidence_floor", "max_reasks",
        )
    }  # fmt: skip
    form |= {"timezone": "America/Chicago", "csrf": csrf(client, "/app/b/brk-1/settings")}
    client.post("/app/b/brk-1/settings/schedule", data=form)
    cfg = world.store.get(Broker, "brk-1").config
    assert cfg.timezone == "America/Chicago" and cfg.sms_delay_ms == 30 * MINUTE_MS
    change = [e for e in world.store.evidence("broker:brk-1") if e.kind == "config.changed"][-1]
    assert change.payload["fields"] == ["timezone"]

    # A fractional value typed in is kept to the minute.
    form |= {"sms_delay_hours": "1.25"}
    client.post("/app/b/brk-1/settings/schedule", data=form)
    assert world.store.get(Broker, "brk-1").config.sms_delay_ms == 75 * MINUTE_MS


# ------------------------------------------------------------------ reading


class _QueuedReader(StandInReader):
    """The stand-in, read through the reading desk as a real reader is."""

    inline = False


def test_a_load_whose_file_is_still_being_read_says_so():
    world = World(reader=_QueuedReader())
    world.store.put(dataclasses.replace(world.owner, email=OWNER_EMAIL))
    load = world.make_load()
    world.tick()
    world.inbound(
        settle=False,
        broker_id="brk-1",
        channel=Channel.EMAIL,
        from_addr=world.dispatcher.email,
        subject=f"POD {load.load_number}",
        body="attached",
        attachments=[("pod.pdf", pod(load))],
    )
    client = TestClient(create_app(world.engine, KEY), base_url="https://testserver")
    sign_in(world, client)
    page = client.get(f"/app/b/brk-1/loads/{load.id}").text
    assert "Carrier replied with a file" in page and "Reading" in page
    assert "Checked the document" not in page
    chases = client.get("/app/b/brk-1/chases?state=RECEIVED").text
    assert load.load_number in chases


def test_a_reading_in_doubt_tells_the_broker_what_to_look_at(app_world):
    world, client, _ = app_world
    load = world.make_load()
    world.tick()
    world.send_pod(
        load,
        confidence=60,
        doubts=["whether the receiver signed", "the delivery date, read as 11/1?"],
    )
    sign_in(world, client)
    page = client.get("/app/b/brk-1").text
    assert "Could not read the POD reliably" in page
    assert "Not sure about: whether the receiver signed; the delivery date, read as 11/1?." in page
    assert "% sure" not in page


def test_a_file_the_reader_never_answered_for_is_told_plainly():
    from chase.ports import ReaderError

    class Down(_QueuedReader):
        def read(self, filename, content):
            raise ReaderError("Anthropic 400/invalid_request_error", transient=False)

    world = World(reader=Down())
    load = world.make_load()
    world.tick()
    world.send_pod(load)
    told = timeline.tell(world.store.evidence(load.id))
    failed = next(e for e in told if e.title == "Could not read the document")
    assert failed.tone == "bad" and "kept for you to look at" in failed.detail
    assert "Anthropic" not in failed.detail  # provider errors stay in the evidence
    assert told[-1].title == "Stopped and handed to you"
