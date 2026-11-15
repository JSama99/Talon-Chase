"""The ontology: its published files, its schema, and every cross-entity rule.

Each rule gets a test that breaks it on purpose in an otherwise healthy world and
expects that rule, by id, to fire. The healthy world itself must have no violations.
"""

from __future__ import annotations

import dataclasses

import pytest
from jsonschema import Draft202012Validator

from chase.domain import models as m
from chase.domain.models import (
    Attempt,
    Broker,
    Carrier,
    Channel,
    Chase,
    Check,
    CheckResult,
    Consent,
    ConsentSource,
    ConsentStatus,
    Escalation,
    EscalationReason,
    Inbound,
    InboundStatus,
    Load,
    OutboxMessage,
    OutboxPurpose,
    OutboxStatus,
    Requirement,
    RequirementStatus,
    Step,
    WriteBack,
)
from chase.evidence import make_event
from chase.ontology import (
    GROUPS,
    ONTOLOGY_VERSION,
    OntologyError,
    build_context,
    build_schema,
    schema_sha256,
)
from chase.ontology import build as builder
from chase.ontology.constraints import RULES, check_store
from chase.ontology.events import EVENT_PAYLOADS, RETIRED_PAYLOADS
from chase.ontology.schema import ENUMS, SHAPES, _plain
from chase.store.memory import MemoryStore
from tests.conftest import World

# ------------------------------------------------------------------ published files


def test_published_files_match_the_code():
    assert builder.stale() == [], "run: python -m chase.ontology.build"


def test_check_mode_reports_a_stale_file(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "DOC_PATH", tmp_path / "ONTOLOGY.md")
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    monkeypatch.setattr(builder, "SCHEMA_PATH", tmp_path / "s.json")
    monkeypatch.setattr(builder, "CONTEXT_PATH", tmp_path / "c.jsonld")
    assert builder.main(["--check"]) == 1
    assert builder.main([]) == 0
    assert builder.main(["--check"]) == 0


# --------------------------------------------------------------------------- schema


def test_schema_is_valid_draft_2020_12_and_versioned():
    schema = build_schema()
    Draft202012Validator.check_schema(schema)
    assert schema["x-version"] == ONTOLOGY_VERSION
    assert schema["$id"].endswith(ONTOLOGY_VERSION)
    assert len(schema_sha256()) == 64 and schema_sha256() == schema_sha256()


def test_every_entity_is_in_exactly_one_group():
    grouped = [cls for members in GROUPS.values() for cls in members]
    assert len(grouped) == len(set(grouped))
    entities = set(m.ENTITY_TYPES)
    assert entities <= set(grouped)
    for cls in set(grouped) - entities:
        assert cls.__name__ in SHAPES or cls.__name__ == "EvidenceEvent"


def test_every_object_is_closed_and_every_field_documented():
    defs = build_schema()["$defs"]
    for name, definition in defs.items():
        if definition.get("type") != "object":
            continue
        assert definition["additionalProperties"] is False, name
        assert set(definition["required"]) == set(definition["properties"]), name
        if not name.startswith("payload."):
            for field_name, prop in definition["properties"].items():
                assert prop.get("description"), f"{name}.{field_name} has no description"


def test_every_reference_names_an_entity_and_every_enum_is_declared():
    defs = build_schema()["$defs"]
    entity_names = {cls.__name__ for cls in m.ENTITY_TYPES}
    refs = []
    for definition in defs.values():
        for prop in definition.get("properties", {}).values():
            if "x-ref" in prop:
                refs.append(prop["x-ref"])
                assert prop["x-ref"] in entity_names
    assert len(refs) >= 25
    for enum in ENUMS:
        assert defs[enum.__name__]["enum"] == [e.value for e in enum]


def test_sensitive_fields_are_exactly_what_a_carrier_must_not_see():
    defs = build_schema()["$defs"]
    marked = {
        f"{name}.{field_name}"
        for name, definition in defs.items()
        for field_name, prop in definition.get("properties", {}).items()
        if prop.get("x-sensitive")
    }
    assert marked == {"Load.pickup_number", "Load.rate_cents", "Party.name", "Party.address"}


def test_closed_schema_rejects_unknown_fields_and_wrong_types(world):
    load = world.make_load()
    validator = Draft202012Validator({"$ref": "#/$defs/Load", "$defs": build_schema()["$defs"]})
    record = _plain(load)
    assert validator.is_valid(record)
    assert not validator.is_valid(record | {"customer_name": "Acme Foods"})
    assert not validator.is_valid(record | {"rate_cents": "1850.00"})
    assert not validator.is_valid({k: v for k, v in record.items() if k != "broker_id"})


def test_stores_refuse_records_that_do_not_conform():
    store = MemoryStore()
    bad = Broker("b", "B", "a@b.example", "c@d.example")
    bad.config = dataclasses.replace(bad.config, max_reasks=-1)
    with pytest.raises(OntologyError, match="BrokerConfig|max_reasks|minimum"):
        store.put(bad)
    with pytest.raises(OntologyError):
        store.put(Load("l", "b", "c", "TC-1", 1, expected_pages="two"))  # type: ignore[arg-type]
    assert store.find(Broker) == [] and store.find(Load) == []


def test_retired_kinds_are_described_but_cannot_be_appended():
    defs = build_schema()["$defs"]
    assert defs["payload.sms.stopped"]["deprecated"] is True
    assert "sms.stopped" not in EVENT_PAYLOADS and "sms.stopped" in RETIRED_PAYLOADS
    with pytest.raises(OntologyError):
        MemoryStore().append_evidence(
            make_event("broker:b", "sms.stopped", 1, {"contact_id": "k", "inbound_id": "i"}, None)
        )


def test_json_ld_context_names_every_entity_and_field():
    context = build_context()["@context"]
    for cls in m.ENTITY_TYPES:
        assert cls.__name__ in context
    assert context["@vocab"].startswith("urn:talonsight:talon-chase:")


def test_exports_say_which_ontology_they_were_written_against(world):
    load = world.make_load()
    exported = world.engine.export_evidence(load.id)
    assert exported["ontology"] == {"version": ONTOLOGY_VERSION, "schema_sha256": schema_sha256()}


# ---------------------------------------------------------------------------- rules


def busy_world() -> World:
    """A world with one of everything the rules look at, and no violations:

    load-1  filed after a clean POD
    load-2  still being chased; the driver has been texted
    load-3  a money message, escalated, then resolved by a person
    """
    world = World()
    world.check_rules = False  # the tests below break it on purpose
    filed, chased, settled = world.make_load(), world.make_load(), world.make_load()
    world.tick()
    world.send_pod(filed)
    world.run(4 * m.HOUR_MS)
    world.reply(settled, "Please update our bank details before paying.")
    escalation = world.escalations(settled)[0]
    world.engine.acknowledge_escalation(
        escalation.id, by="op-jermaine", resolution="HANDLED_BY_HAND", note="called them"
    )
    assert [world.chase(x).state for x in (filed, chased, settled)] == [
        m.ChaseState.CLOSED, m.ChaseState.ASKED, m.ChaseState.CLOSED,
    ]  # fmt: skip
    assert any(msg.channel is Channel.SMS for msg in world.messages(chased))
    return world


def _one(store, cls, **where):
    found = sorted(store.find(cls, **where), key=lambda o: o.id)
    assert found, (cls.__name__, where)
    return found[0]


def _change(store, obj, **changes):
    store.put(dataclasses.replace(obj, **changes))


def _sms(store):
    return _one(store, OutboxMessage, load_id="load-2", channel=Channel.SMS)


def _request(store):
    return _one(store, OutboxMessage, load_id="load-2", channel=Channel.EMAIL)


def _break_tc02(s):
    s.put(Broker("brk-2", "Other", "pods@o.example", "ops@o.example"))
    s.put(Carrier("car-2", "brk-2", "Elsewhere", "MC2", "America/Chicago"))
    _change(s, s.get(Load, "load-2"), carrier_id="car-2")


def _break_tc08(s):
    chase = _one(s, Chase, load_id="load-2")
    s.put(Escalation("esc-x", chase.id, "load-2", "brk-1", EscalationReason.NO_RESPONSE, "test", 1))


def _break_tc13(s):
    _change(s, _request(s), purpose=OutboxPurpose.PACKET)


def _break_tc14(s):
    consent = _one(s, Consent, contact_id="con-drv")
    _change(s, consent, status=ConsentStatus.WITHDRAWN)


def _break_tc18(s):
    s.put(
        Consent(
            "cns-x", "con-drv", Channel.SMS, ConsentStatus.WITHDRAWN,
            ConsentSource.STOP_REPLY, 10**13, 99,
        )
    )  # fmt: skip


def _break_tc21(s):
    owner = s.get(m.Operator, "op-jermaine")
    s.put(dataclasses.replace(owner, email="j@peachtree.example"))
    s.put(dataclasses.replace(owner, id="op-twin", email="J@peachtree.example"))


def _break_tc19(s):
    s.put(Load("load-ghost", "brk-1", "car-1", "TC-999", 1))


CORRUPTIONS = {
    "TC-01": lambda s: _change(s, s.get(Load, "load-2"), carrier_id="car-nope"),
    "TC-02": _break_tc02,
    "TC-03": lambda s: s.put(dataclasses.replace(_one(s, Chase, load_id="load-1"), id="ch-x")),
    "TC-04": lambda s: _change(s, _one(s, Chase, load_id="load-2"), next_step=Step.WRITEBACK),
    "TC-05": lambda s: _change(s, _one(s, Chase, load_id="load-2"), attempts_sent=4),
    "TC-06": lambda s: _change(s, s.get(Load, "load-1"), ready_to_invoice_at_ms=None),
    "TC-07": lambda s: _change(s, _one(s, Chase, load_id="load-3"), closed_reason="gave up"),
    "TC-08": _break_tc08,
    "TC-09": lambda s: _change(s, _one(s, Escalation, load_id="load-3"), resolution=None),
    "TC-10": lambda s: _change(s, _one(s, Requirement, load_id="load-2"), closed_at_ms=5),
    "TC-11": lambda s: _change(s, _one(s, Attempt), to=["someone@else.example"]),
    "TC-12": lambda s: _change(s, _sms(s), sent_at_ms=None),
    "TC-13": _break_tc13,
    "TC-14": _break_tc14,
    "TC-15": lambda s: _change(s, _one(s, Inbound), status=InboundStatus.NEW),
    "TC-16": lambda s: _change(s, _one(s, Check, result=CheckResult.PASS), reasons=["odd"]),
    "TC-17": lambda s: _change(s, _one(s, WriteBack), idempotency_key="0" * 64),
    "TC-18": _break_tc18,
    "TC-19": _break_tc19,
    "TC-20": lambda s: _change(s, _request(s), body=_request(s).body + " for Acme Foods"),
    "TC-21": _break_tc21,
}


def test_every_rule_has_a_test_that_breaks_it():
    assert set(CORRUPTIONS) == {r.id for r in RULES}
    assert [r.id for r in RULES] == [f"TC-{n:02d}" for n in range(1, len(RULES) + 1)]


def test_the_busy_world_keeps_every_rule():
    assert check_store(busy_world().store) == []


@pytest.mark.parametrize("rule_id", sorted(CORRUPTIONS))
def test_each_rule_fires_when_broken(rule_id):
    world = busy_world()
    CORRUPTIONS[rule_id](world.store)
    fired = {v.rule for v in check_store(world.store)}
    assert rule_id in fired, fired


def test_violations_name_the_record_and_say_what_is_wrong():
    world = busy_world()
    CORRUPTIONS["TC-05"](world.store)
    (violation,) = [v for v in check_store(world.store) if v.rule == "TC-05"]
    assert violation.entity == "Chase" and violation.entity_id.startswith("ch")
    assert str(violation).startswith("TC-05 Chase ") and "4 scheduled requests" in str(violation)


def test_requirement_status_follows_the_chase():
    world = busy_world()
    statuses = {r.load_id: r.status for r in world.store.find(Requirement)}
    assert statuses == {
        "load-1": RequirementStatus.MET,
        "load-2": RequirementStatus.OPEN,
        "load-3": RequirementStatus.RESOLVED_BY_PERSON,
    }
    assert {OutboxStatus.SENT} >= {msg.status for msg in world.messages() if msg.tries}
