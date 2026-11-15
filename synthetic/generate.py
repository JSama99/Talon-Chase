"""Deterministic synthetic pack: 200 loads and 60 proof-of-delivery fixtures.

A fixture is a JSON stand-in for a document image: it states what a reader would
extract from the page. Nothing here is real data. The same seed always produces
the same pack.

    python -m synthetic.generate --out out/pack
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path

from chase.domain.models import HOUR_MS, Broker, Carrier, Contact, Load, Party, PartyRole, Role

SEED = 20261114
LOAD_COUNT = 200
CARRIER_COUNT = 25
CLASS_COUNTS = {
    "clean": 20,
    "unreadable": 8,
    "unsigned": 8,
    "wrong_load": 8,
    "exception_noted": 8,
    "carrier_invoice": 8,
}
# What the engine should do with each class.
EXPECTED = {
    "clean": "filed",
    "unreadable": "reask:unreadable",
    "unsigned": "reask:unsigned",
    "wrong_load_unknown": "reask:wrong_load",
    "wrong_load_sibling": "reask:rerouted+sibling_filed",
    "exception_noted": "escalated:DOCUMENT_EXCEPTION",
    "carrier_invoice": "reask:not_a_pod",
}
TIMEZONES = ["America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"]
NAME_A = ["Redline", "Blue Ridge", "Ironwood", "Summit", "Delta", "Prairie", "Harbor", "Cedar"]
NAME_B = ["Trucking", "Freight", "Transport", "Haulers", "Carriers", "Logistics"]
CUSTOMERS = ["Acme Foods", "Northfield Paper", "Orchard Beverage", "Granite Building Supply"]
CONSIGNEES = ["Riverside Distribution", "Eastgate Cold Storage", "Midtown Crossdock"]
EXCEPTION_NOTES = [
    "2 cartons damaged",
    "short 1 pallet",
    "seal broken on arrival",
    "refused 3 cases, crushed",
    "1 pallet wet",
    "over 2 cases, subject to count",
    "driver count only, shrink wrap torn",
    "received damaged, noted on BOL",
]


@dataclass
class Fixture:
    id: str
    klass: str
    load_id: str
    filename: str
    extraction: dict
    expected: str
    sibling_load_id: str = ""

    def content(self) -> bytes:
        return json.dumps(self.extraction, sort_keys=True).encode()


@dataclass
class Pack:
    broker: Broker
    carriers: list[Carrier]
    contacts: list[Contact]
    loads: list[Load]
    fixtures: list[Fixture]
    parties: list[Party] = field(default_factory=list)
    # Drivers who agreed to texts in their rate confirmation.
    texting_contact_ids: list[str] = field(default_factory=list)
    silent_load_ids: list[str] = field(default_factory=list)
    promise_load_ids: list[str] = field(default_factory=list)
    out_of_office_load_ids: list[str] = field(default_factory=list)
    risk_load_ids: list[str] = field(default_factory=list)


def _extraction(load: Load, **overrides) -> dict:
    data = {
        "doc_type": "POD",
        "readable": True,
        "reference_numbers": [load.load_number, load.bol_number],
        "signature_present": True,
        "delivery_date_ms": load.delivery_appt_ms,
        "page_count": load.expected_pages,
        "exception_notes": [],
        "confidence": 96,
    }
    data.update(overrides)
    return data


def build_pack(start_ms: int, seed: int = SEED) -> Pack:
    rng = random.Random(seed)
    broker = Broker(
        id="brk-syn",
        name="Peachtree Logistics",
        chase_mailbox="pods@peachtreelogistics.example",
        rep_email="ops@peachtreelogistics.example",
        sms_number="+14045550100",
        adapter="fake_api",
    )
    carriers, contacts, texting = [], [], []
    for i in range(1, CARRIER_COUNT + 1):
        name = f"{NAME_A[i % len(NAME_A)]} {NAME_B[i % len(NAME_B)]} {i}"
        slug = name.lower().replace(" ", "")
        carrier = Carrier(
            f"car-{i:03d}", broker.id, name, f"MC{400000 + i * 137}", rng.choice(TIMEZONES)
        )
        carriers.append(carrier)
        contacts.append(
            Contact(
                f"con-{i:03d}-d",
                carrier.id,
                Role.DISPATCHER,
                "Dispatch",
                f"dispatch@{slug}.example",
            )
        )
        driver = Contact(
            f"con-{i:03d}-v", carrier.id, Role.DRIVER, "Driver", phone=f"+1312555{1000 + i:04d}"
        )
        contacts.append(driver)
        if rng.random() < 0.6:
            texting.append(driver.id)

    loads, parties = [], []
    for n in range(1, LOAD_COUNT + 1):
        carrier = carriers[(n - 1) % CARRIER_COUNT]
        appt = start_ms - rng.randrange(1, 6) * HOUR_MS
        load_id = f"load-{n:04d}"
        po_number = f"PO{rng.randrange(10000, 99999)}"
        expected_pages = rng.choice([1, 1, 1, 2])
        customer = Party(
            f"{load_id}-customer", broker.id, PartyRole.CUSTOMER, rng.choice(CUSTOMERS)
        )
        consignee = Party(
            f"{load_id}-consignee",
            broker.id,
            PartyRole.CONSIGNEE,
            rng.choice(CONSIGNEES),
            f"{rng.randrange(100, 9999)} Industrial Blvd",
        )
        parties += [customer, consignee]
        loads.append(
            Load(
                id=load_id,
                broker_id=broker.id,
                carrier_id=carrier.id,
                load_number=f"TC-{100000 + n}",
                delivery_appt_ms=appt,
                bol_number=f"BOL{550000 + n}",
                po_number=po_number,
                expected_pages=expected_pages,
                delivered_at_ms=start_ms,
                customer_party_id=customer.id,
                consignee_party_id=consignee.id,
                pickup_number=f"PU-{rng.randrange(10000, 99999)}",
                rate_cents=rng.randrange(600, 4200) * 100,
            )
        )

    order = [load.id for load in loads]
    rng.shuffle(order)
    by_id = {load.id: load for load in loads}
    pool = iter(order)
    used: set[str] = set()

    def take() -> Load:
        for load_id in pool:
            if load_id not in used:
                used.add(load_id)
                return by_id[load_id]
        raise RuntimeError("ran out of loads")

    fixtures: list[Fixture] = []

    def add(klass: str, load: Load, extraction: dict, expected: str, sibling: str = "") -> None:
        fid = f"fx-{len(fixtures) + 1:03d}"
        ext = "jpg" if klass in ("unreadable", "unsigned") else "pdf"
        fixtures.append(
            Fixture(fid, klass, load.id, f"{fid}-{klass}.{ext}", extraction, expected, sibling)
        )

    for _ in range(CLASS_COUNTS["clean"]):
        load = take()
        add("clean", load, _extraction(load), EXPECTED["clean"])
    for _ in range(CLASS_COUNTS["unreadable"]):
        load = take()
        add(
            "unreadable",
            load,
            _extraction(load, readable=False, reference_numbers=[]),
            EXPECTED["unreadable"],
        )
    for _ in range(CLASS_COUNTS["unsigned"]):
        load = take()
        add("unsigned", load, _extraction(load, signature_present=False), EXPECTED["unsigned"])
    for k in range(CLASS_COUNTS["wrong_load"]):
        load = take()
        if k % 2 == 0:
            refs = [f"TC-{900000 + k}", f"BOL{990000 + k}"]
            add(
                "wrong_load",
                load,
                _extraction(load, reference_numbers=refs),
                EXPECTED["wrong_load_unknown"],
            )
        else:
            sibling = next(x for x in loads if x.carrier_id == load.carrier_id and x.id not in used)
            used.add(sibling.id)
            add(
                "wrong_load",
                load,
                _extraction(sibling),
                EXPECTED["wrong_load_sibling"],
                sibling.id,
            )
    for k in range(CLASS_COUNTS["exception_noted"]):
        load = take()
        signed = k % 4 != 3  # some are unsigned as well; they must still go to a person
        add(
            "exception_noted",
            load,
            _extraction(load, exception_notes=[EXCEPTION_NOTES[k]], signature_present=signed),
            EXPECTED["exception_noted"],
        )
    for _ in range(CLASS_COUNTS["carrier_invoice"]):
        load = take()
        add(
            "carrier_invoice",
            load,
            _extraction(load, doc_type="CARRIER_INVOICE", signature_present=False),
            EXPECTED["carrier_invoice"],
        )

    rest = [load_id for load_id in order if load_id not in used]
    pack = Pack(broker, carriers, contacts, loads, fixtures, parties, texting)
    pack.promise_load_ids = rest[:20]
    pack.out_of_office_load_ids = rest[20:30]
    pack.risk_load_ids = rest[30:35]
    pack.silent_load_ids = rest[35:]
    return pack


def write_pack(pack: Pack, out: Path) -> None:
    (out / "pods").mkdir(parents=True, exist_ok=True)
    (out / "loads.json").write_text(json.dumps([asdict(x) for x in pack.loads], indent=1))
    (out / "carriers.json").write_text(json.dumps([asdict(x) for x in pack.carriers], indent=1))
    (out / "contacts.json").write_text(json.dumps([asdict(x) for x in pack.contacts], indent=1))
    (out / "parties.json").write_text(json.dumps([asdict(x) for x in pack.parties], indent=1))
    (out / "texting_contacts.json").write_text(json.dumps(pack.texting_contact_ids, indent=1))
    index = []
    for fixture in pack.fixtures:
        (out / "pods" / f"{fixture.filename}.json").write_bytes(fixture.content())
        index.append({k: v for k, v in asdict(fixture).items() if k != "extraction"})
    (out / "fixtures.json").write_text(json.dumps(index, indent=1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("out/pack"))
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()
    from synthetic.evaluate import START_MS

    pack = build_pack(START_MS, args.seed)
    write_pack(pack, args.out)
    print(f"wrote {len(pack.loads)} loads and {len(pack.fixtures)} fixtures to {args.out}")


if __name__ == "__main__":
    main()
