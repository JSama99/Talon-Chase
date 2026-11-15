"""Render every broker screen with a believable morning of work, to static HTML.

    python -m chase.app.preview --out out/screens

Writes needs-you.html, load.html, load-held.html, settings.html, contact.html,
chases.html and login.html, plus the stylesheet, so the screens can be opened
straight from disk or screenshotted. Nothing is sent anywhere: the channels are
the in-memory stand-ins.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from chase.api.app import create_app
from chase.app.auth import Signer, make_link_token
from chase.app.views import HERE
from chase.channels.fakes import FakeEmail, FakeSms
from chase.domain.clock import FakeClock, SequentialIds
from chase.domain.models import (
    HOUR_MS,
    MINUTE_MS,
    Broker,
    Carrier,
    Channel,
    ConsentSource,
    Contact,
    Load,
    Operator,
    OperatorRole,
    Party,
    PartyRole,
    Role,
)
from chase.engine import ChaseEngine
from chase.outbox import OutboxDispatcher
from chase.reader.rules import RuleBasedClassifier, StandInReader
from chase.store.memory import MemoryStore
from chase.writeback.adapters import FakeApiAdapter

TZ = "America/New_York"
START = int(datetime(2026, 11, 10, 13, 0, tzinfo=ZoneInfo(TZ)).timestamp() * 1000)
CARRIERS = [
    ("car-redline", "Redline Trucking", "MC482910", "America/Chicago", "+13125550142"),
    ("car-blueridge", "Blue Ridge Freight", "MC400548", "America/New_York", "+14045550117"),
    ("car-ironwood", "Ironwood Transport", "MC377120", "America/Chicago", "+16155550188"),
    ("car-summit", "Summit Haulers", "MC512004", "America/Chicago", "+19015550163"),
]


def _pod(load: Load, **overrides) -> bytes:
    data = {
        "doc_type": "POD",
        "readable": True,
        "reference_numbers": [load.load_number],
        "signature_present": True,
        "delivery_date_ms": load.delivery_appt_ms,
        "page_count": 1,
        "exception_notes": [],
        "confidence": 96,
    }
    data.update(overrides)
    return json.dumps(data).encode()


def build() -> tuple[ChaseEngine, TestClient, dict[str, Load]]:
    store, clock = MemoryStore(), FakeClock(START)
    engine = ChaseEngine(
        store=store, clock=clock, ids=SequentialIds(), email=FakeEmail(), sms=FakeSms(),
        classifier=RuleBasedClassifier(), reader=StandInReader(), adapters=[FakeApiAdapter()],
        secret=b"preview-secret-preview-secret-xx", base_url="https://chase.example",
    )  # fmt: skip
    outbox = OutboxDispatcher(engine)
    broker = Broker(
        "brk-peachtree", "Peachtree Logistics", "pods@peachtreelogistics.example",
        "ops@peachtreelogistics.example", "+14045550100", "packet_email",
    )  # fmt: skip
    broker.config.timezone = TZ
    store.put(broker)
    engine.save_operator(
        Operator("op-jermaine", broker.id, "Jermaine", OperatorRole.OWNER,
                 "jermaine@peachtreelogistics.example")
    )  # fmt: skip
    engine.save_operator(
        Operator("op-dana", broker.id, "Dana Ops", OperatorRole.STAFF,
                 "dana@peachtreelogistics.example")
    )  # fmt: skip
    for cid, name, mc, tz, phone in CARRIERS:
        slug = name.lower().replace(" ", "")
        store.put(Carrier(cid, broker.id, name, mc, tz))
        store.put(Contact(f"{cid}-d", cid, Role.DISPATCHER, "Dispatch", f"dispatch@{slug}.example"))
        store.put(Contact(f"{cid}-v", cid, Role.DRIVER, "Driver", phone=phone))
        engine.record_consent(f"{cid}-v", True, ConsentSource.RATE_CONFIRMATION)
    customer = Party("party-acme", broker.id, PartyRole.CUSTOMER, "Acme Foods")
    consignee = Party(
        "party-riverside", broker.id, PartyRole.CONSIGNEE, "Riverside Distribution",
        "4100 Fulton Industrial Blvd, Atlanta, GA",
    )  # fmt: skip

    def settle() -> None:
        for _ in range(10):
            if not outbox.dispatch().changed and not engine.tick():
                return

    def advance(ms: int) -> None:
        end = clock.now_ms() + ms
        while clock.now_ms() < end:
            clock.advance(min(15 * MINUTE_MS, end - clock.now_ms()))
            engine.tick()
            settle()

    loads: dict[str, Load] = {}

    def load(number: str, carrier: str) -> Load:
        made = Load(
            id=f"load-{number}", broker_id=broker.id, carrier_id=carrier, load_number=number,
            delivery_appt_ms=clock.now_ms() - HOUR_MS, delivered_at_ms=clock.now_ms(),
            bol_number=f"BOL{number[-5:]}", customer_party_id=customer.id,
            consignee_party_id=consignee.id, pickup_number="PU-77812", rate_cents=185_000,
        )  # fmt: skip
        engine.intake_load(made, [customer, consignee])
        loads[number] = made
        return made

    def reply(number: str, body: str, files=(), sender: str = "") -> None:
        made = loads[number]
        slug = next(c[1] for c in CARRIERS if c[0] == made.carrier_id).lower().replace(" ", "")
        engine.handle_inbound(
            broker_id=broker.id, channel=Channel.EMAIL,
            from_addr=sender or f"dispatch@{slug}.example",
            subject=f"Re: proof of delivery for load {number}", body=body,
            attachments=list(files),
        )  # fmt: skip
        settle()

    # Older loads, already filed: these make the numbers at the top real.
    for n in range(1, 9):
        made = load(f"TC-29990{n}", CARRIERS[n % 4][0])
        settle()
        advance(HOUR_MS + n * 20 * MINUTE_MS)
        reply(made.load_number, "Attached.", [("pod.pdf", _pod(made))])
    load("TC-300005", "car-ironwood")
    settle()
    advance(3 * 24 * HOUR_MS + 2 * HOUR_MS)  # three requests, no answer: to a person
    load("TC-300004", "car-redline")
    load("TC-300003", "car-blueridge")
    load("TC-300011", "car-summit")
    load("TC-300012", "car-redline")
    settle()
    advance(45 * MINUTE_MS)
    reply("TC-300004", "Before we send it, please update our bank details and remit-to.")
    reply(
        "TC-300003", "POD attached.",
        [("pod.pdf", _pod(loads["TC-300003"], exception_notes=["2 cartons damaged"]))],
    )  # fmt: skip
    reply(
        "TC-300011", "Here you go",
        [("pod.pdf", _pod(loads["TC-300011"]))],
        sender="nights@summithaulers.example",
    )  # fmt: skip
    engine.handle_inbound(
        broker_id=broker.id, channel=Channel.EMAIL, from_addr="ops@cedarcarriers.example",
        subject="Any loads out of Dallas this week", body="", attachments=[],
    )  # fmt: skip
    advance(40 * MINUTE_MS)
    client = TestClient(
        create_app(engine, "preview-admin-key-000"), base_url="https://chase.example"
    )
    token = make_link_token(Signer(engine.secret), "op-jermaine", clock.now_ms())
    client.post(f"/app/login/{token}", data={"next": "/app"})
    return engine, client, loads


def render(out: Path) -> list[Path]:
    engine, client, loads = build()
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy(HERE / "static" / "app.css", out / "app.css")
    broker = "/app/b/brk-peachtree"
    pages = {
        "needs-you.html": broker,
        "load.html": f"{broker}/loads/{loads['TC-300003'].id}",
        "load-held.html": f"{broker}/loads/{loads['TC-300011'].id}",
        "load-filed.html": f"{broker}/loads/{loads['TC-299901'].id}",
        "chases.html": f"{broker}/chases",
        "settings.html": f"{broker}/settings",
        "contact.html": f"{broker}/contacts/car-redline-v",
    }
    written = []
    for name, path in pages.items():
        response = client.get(path)
        response.raise_for_status()
        html = response.text.replace('href="/app/static/app.css"', 'href="app.css"')
        html = re.sub(r'href="/app[^"]*"', 'href="#"', html)
        (out / name).write_text(html, encoding="utf-8")
        written.append(out / name)
    signed_out = TestClient(client.app, base_url="https://chase.example")
    login = signed_out.get("/app/login").text.replace("/app/static/app.css", "app.css")
    (out / "login.html").write_text(login, encoding="utf-8")
    written.append(out / "login.html")
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=Path("out/screens"))
    args = parser.parse_args()
    for path in render(args.out):
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
