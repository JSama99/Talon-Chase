import dataclasses

import pytest

from chase.evidence import (
    GENESIS_HASH,
    CanonicalError,
    canonical_json,
    make_event,
    sha256_hex,
    verify_chain,
)


def build(n=5):
    events, prev = [], None
    for i in range(n):
        prev = make_event("load-1", "note", 1_000 + i, {"i": i, "who": "agent"}, prev)
        events.append(prev)
    return events


def test_canonical_sorts_keys_and_strips_whitespace():
    assert canonical_json({"b": 1, "a": [True, None, "x"]}) == b'{"a":[true,null,"x"],"b":1}'


def test_canonical_is_independent_of_insertion_order():
    assert canonical_json({"a": 1, "b": {"d": 2, "c": 3}}) == canonical_json(
        {"b": {"c": 3, "d": 2}, "a": 1}
    )


def test_canonical_keeps_non_ascii_as_utf8():
    assert canonical_json({"city": "Montréal"}) == '{"city":"Montréal"}'.encode()


@pytest.mark.parametrize("bad", [1.5, {"a": 0.1}, [float("nan")], {1: "x"}, {"a": object()}, b"x"])
def test_canonical_rejects_floats_and_unknown_types(bad):
    with pytest.raises(CanonicalError):
        canonical_json(bad)


def test_known_vector():
    # Fixed vector so another implementation can check itself against this one.
    event = make_event("L-1", "chase.opened", 1_700_000_000_000, {"load": "L-1"}, None)
    assert event.prev_hash == GENESIS_HASH
    assert event.payload_digest == sha256_hex(b'{"load":"L-1"}')
    header = (
        b'{"at_ms":1700000000000,"chain_id":"L-1","kind":"chase.opened",'
        b'"payload_digest":"' + event.payload_digest.encode() + b'","seq":0}'
    )
    assert event.hash == sha256_hex(header + GENESIS_HASH.encode())


def test_chain_verifies():
    result = verify_chain(build())
    assert result.ok and result.length == 5 and result.failed_index is None
    assert result.head_hash == build()[-1].hash


def test_empty_chain_verifies():
    assert verify_chain([]).ok


@pytest.mark.parametrize("index", [0, 2, 4])
def test_payload_tamper_detected_at_index(index):
    events = build()
    events[index] = dataclasses.replace(events[index], payload={"i": 99, "who": "agent"})
    result = verify_chain(events)
    assert not result.ok and result.failed_index == index


def test_header_tamper_detected():
    events = build()
    events[1] = dataclasses.replace(events[1], at_ms=5)
    assert verify_chain(events).failed_index == 1


def test_removed_event_detected():
    events = build()
    del events[2]
    assert verify_chain(events).failed_index == 2


def test_reordered_events_detected():
    events = build()
    events[1], events[2] = events[2], events[1]
    assert verify_chain(events).failed_index == 1


def test_recomputed_tamper_still_breaks_the_next_link():
    events = build()
    forged = make_event("load-1", "note", 1_001, {"i": 99, "who": "agent"}, events[0])
    events[1] = forged
    assert verify_chain(events).failed_index == 2


def test_round_trip_dict():
    from chase.evidence import EvidenceEvent

    events = [EvidenceEvent.from_dict(e.to_dict()) for e in build()]
    assert verify_chain(events).ok


def test_memory_store_evidence_cannot_be_changed_through_a_returned_copy():
    from chase.store.memory import MemoryStore

    store = MemoryStore()
    payload = {"chase_id": "c", "trigger": "t", "plan": {"first_ms": 1}}
    store.append_evidence(make_event("L", "chase.opened", 1, payload, None))
    payload["plan"]["first_ms"] = 2
    store.evidence("L")[0].payload["plan"]["first_ms"] = 3
    store.last_evidence("L").payload["plan"]["first_ms"] = 4
    assert store.evidence("L")[0].payload["plan"] == {"first_ms": 1}
    assert verify_chain(store.evidence("L")).ok


def test_stores_refuse_events_and_records_the_ontology_does_not_allow():
    from chase.domain.models import Broker
    from chase.ontology import OntologyError
    from chase.store.memory import MemoryStore

    store = MemoryStore()
    with pytest.raises(OntologyError, match="kind"):
        store.append_evidence(make_event("L", "made.up", 1, {}, None))
    with pytest.raises(OntologyError, match="extra"):
        store.append_evidence(
            make_event("L", "load.delivered", 1, {"delivered_at_ms": 1, "extra": 1}, None)
        )
    with pytest.raises(OntologyError):
        store.append_evidence(
            make_event("L", "load.delivered", 1, {"delivered_at_ms": "soon"}, None)
        )
    with pytest.raises(OntologyError):
        store.put(
            Broker(
                id="b",
                name="x",
                chase_mailbox="a@b.example",
                rep_email="c@d.example",
                sms_number="",
                adapter="fake_api",
                outbound_enabled="yes",
            )
        )
    assert store.evidence("L") == []
