"""The SQL store must behave exactly like the in-memory one.

Runs on SQLite always, and on Postgres when CHASE_TEST_POSTGRES_URL is set.
"""

from __future__ import annotations

import os

import pytest
import sqlalchemy as sa

from chase.domain.models import HOUR_MS, Broker, BrokerConfig, Chase, ChaseState, Inbound, Load
from chase.evidence import make_event, verify_chain
from chase.store.sql import METADATA, SqlStore
from tests.conftest import World, check_worlds, pod
from tests.test_acceptance import _scenario

URLS = ["sqlite+pysqlite:///:memory:"]
if os.environ.get("CHASE_TEST_POSTGRES_URL"):
    URLS.append(os.environ["CHASE_TEST_POSTGRES_URL"])


@pytest.fixture(params=URLS, ids=lambda u: u.split(":")[0].split("+")[0])
def store(request):
    store = SqlStore(request.param)
    METADATA.drop_all(store.engine)
    store.create_schema()
    yield store
    check_worlds()
    METADATA.drop_all(store.engine)
    store.engine.dispose()


def test_round_trip_keeps_types(store):
    broker = Broker("b1", "Peachtree", "pods@p.example", "ops@p.example")
    broker.config = BrokerConfig(max_reasks=5, writeback_retry_backoff_ms=[1, 2])
    store.put(broker)
    loaded = store.get(Broker, "b1")
    assert loaded == broker and isinstance(loaded.config, BrokerConfig)

    load = Load("l1", "b1", "c1", "TC-1", delivery_appt_ms=1_794_337_200_000)
    store.put(load)
    assert store.get(Load, "l1").delivery_appt_ms == 1_794_337_200_000
    assert store.find(Load, delivered_at_ms=None) == [load]
    load.delivered_at_ms = 5
    store.put(load)
    assert store.find(Load, delivered_at_ms=None) == [] and store.get(Load, "missing") is None

    chase = Chase(
        "ch1",
        "l1",
        "b1",
        "r1",
        opened_at_ms=1,
        plan={"first_ms": 2, "sms_ms": 3, "second_ms": 4, "third_ms": 5, "escalate_ms": 6},
    )
    store.put(chase)
    assert store.find(Chase, state=ChaseState.OPEN)[0].plan["first_ms"] == 2
    assert store.find(Chase, state=ChaseState.CLOSED) == []


def test_blobs_settings_and_evidence(store):
    store.put_blob("abc", b"\x00\xff binary")
    store.put_blob("abc", b"ignored: content is addressed by hash")
    assert store.get_blob("abc") == b"\x00\xff binary" and store.get_blob("nope") is None
    assert store.get_setting("k", "d") == "d"
    store.set_setting("k", "1")
    store.set_setting("k", "2")
    assert store.get_setting("k", "d") == "2"

    first = make_event(
        "L", "inbound.late", 1, {"inbound_id": "Montréal", "chase_state": "ASKED"}, None
    )
    second = make_event("L", "load.delivered", 2, {"delivered_at_ms": 2}, first)
    store.append_evidence(first)
    store.append_evidence(second)
    assert store.last_evidence("L") == second and verify_chain(store.evidence("L")).ok
    fork = make_event("L", "load.delivered", 3, {"delivered_at_ms": 3}, first)
    with pytest.raises(sa.exc.IntegrityError):
        store.append_evidence(fork)


def test_transaction_rolls_back_as_one(store):
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.put(Load("l1", "b", "c", "TC-1", delivery_appt_ms=1))
            store.set_setting("k", "v")
            raise RuntimeError("boom")
    assert store.get(Load, "l1") is None and store.get_setting("k", "none") == "none"


def test_engine_runs_the_same_on_sql(store):
    world = World(store=store)
    load = world.make_load()
    other = world.make_load()
    world.tick()
    world.send_pod(load)
    world.reply(other, "bank details changed", [("pod.pdf", pod(other))])
    world.run(3 * HOUR_MS)
    assert world.chase(load).state is ChaseState.CLOSED
    assert world.chase(other).state is ChaseState.ESCALATED
    assert len(store.find(Inbound)) == 2
    for chain in (load.id, other.id):
        assert verify_chain(store.evidence(chain)).ok


def test_sql_and_memory_produce_identical_evidence(store):
    def scenario(world: World) -> dict[str, str]:
        loads = [world.make_load() for _ in range(3)]
        world.tick()
        world.send_pod(loads[0])
        world.send_pod(loads[1], signature_present=False)
        world.run(4 * 24 * HOUR_MS, step_ms=HOUR_MS)
        return {x.id: verify_chain(world.store.evidence(x.id)).head_hash for x in loads}

    assert scenario(World(store=store)) == scenario(World())
    assert _scenario  # the memory-only determinism test lives in test_acceptance


def test_two_writers_cannot_claim_the_same_place_in_a_consent_history(store):
    from chase.domain.models import (
        Carrier,
        Channel,
        Consent,
        ConsentSource,
        ConsentStatus,
        Contact,
        Role,
    )

    store.put(Broker("b1", "B", "pods@b.example", "ops@b.example"))
    store.put(Carrier("c1", "b1", "C", "MC1", "America/Chicago"))
    store.put(Contact("k1", "c1", Role.DRIVER, "Sam", phone="+13125550142"))

    def consent(consent_id: str, status: ConsentStatus) -> Consent:
        return Consent(consent_id, "k1", Channel.SMS, status, ConsentSource.TMS_SYNC, 5, seq=1)

    store.put(consent("cns-a", ConsentStatus.GRANTED))
    with pytest.raises(sa.exc.IntegrityError):
        store.put(consent("cns-b", ConsentStatus.WITHDRAWN))
