"""The reader in the running service: which reader the settings choose, and that a
file is read after the response has gone out, outside the engine's lock."""

from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient

from chase.api.app import create_app
from chase.api.main import document_reader
from chase.domain.checks import Extraction
from chase.domain.clock import FakeClock
from chase.domain.models import HOUR_MS, MINUTE_MS, ChaseState, Load, Reading, ReadingStatus
from chase.ports import ReaderError
from chase.reader.claude import DEFAULT_MODEL, PROMPT_VERSION, ClaudeReader
from chase.reader.rules import StandInReader
from tests.conftest import START, World, pod

KEY = "test-api-key-0123456789"
H = {"X-API-Key": KEY}
ANTHROPIC = "sk-ant-api03-" + "a" * 60


# ------------------------------------------------------------------ settings


def test_without_a_key_the_stand_in_reads_and_real_files_go_to_a_person():
    reader = document_reader(FakeClock(START), {})
    assert isinstance(reader, StandInReader) and reader.inline
    assert reader.read("pod.jpg", b"\xff\xd8\xff").confidence == 0


def test_a_key_turns_the_real_reader_on_with_the_default_model():
    reader = document_reader(FakeClock(START), {"ANTHROPIC_API_KEY": f'  "{ANTHROPIC}"\n'})
    assert isinstance(reader, ClaudeReader) and not reader.inline
    assert reader.name == f"claude/{DEFAULT_MODEL}/{PROMPT_VERSION}"
    assert reader.http.headers["x-api-key"] == ANTHROPIC  # quotes and spaces cleaned off


def test_the_model_can_be_chosen():
    env = {"ANTHROPIC_API_KEY": ANTHROPIC, "CHASE_READER_MODEL": "claude-sonnet-5-5"}
    assert document_reader(FakeClock(START), env).model == "claude-sonnet-5-5"


@pytest.mark.parametrize(
    ("env", "problem"),
    [
        ({"CHASE_READER_MODEL": "claude-opus-5-5"}, "ANTHROPIC_API_KEY is not"),
        ({"ANTHROPIC_API_KEY": "re_123456789012345678901234"}, "looks like a Resend key"),
        ({"ANTHROPIC_API_KEY": "whsec_12345678901234567890"}, "looks like a Resend key"),
        ({"ANTHROPIC_API_KEY": "sk-ant-short"}, "too short"),
        ({"ANTHROPIC_API_KEY": "sk-ant-api03-aaaa aaaaaaaaaaaaaaaaaaaa"}, "a space"),
        ({"ANTHROPIC_API_KEY": ANTHROPIC, "CHASE_READER_MODEL": "gpt-9"}, "Claude model id"),
        ({"ANTHROPIC_API_KEY": ANTHROPIC, "CHASE_READER_MODEL": "Claude Opus"}, "Claude model id"),
    ],
)
def test_a_half_set_up_reader_stops_the_service_and_says_why(env, problem):
    with pytest.raises(RuntimeError) as caught:
        document_reader(FakeClock(START), env)
    assert problem in str(caught.value)
    assert env.get("ANTHROPIC_API_KEY", "x" * 30) not in str(caught.value)  # never echoed


# --------------------------------------------------------------- the service


class SlowReader:
    """Not inline, like any real reader. Notes whether the service's lock was free
    while it was 'thinking', and can be made to fail."""

    name = "slow/v1"
    inline = False

    def __init__(self) -> None:
        self.calls = 0
        self.failures: list[Exception] = []
        self.probe = None
        self.lock_was_free: list[bool] = []

    def read(self, filename: str, content: bytes) -> Extraction:
        self.calls += 1
        if self.probe:
            self.lock_was_free.append(self.probe())
        if self.failures:
            raise self.failures.pop(0)
        return Extraction.from_dict(json.loads(content))


@pytest.fixture
def service():
    reader = SlowReader()
    world = World(reader=reader)
    client = TestClient(create_app(world.engine, KEY))
    return world, client, reader


def new_load(world: World, client: TestClient) -> Load:
    body = {
        "id": "api-load-1",
        "broker_id": "brk-1",
        "carrier_id": "car-1",
        "load_number": "TC-200001",
        "delivery_appt_ms": world.clock.now_ms() - HOUR_MS,
        "delivered_at_ms": world.clock.now_ms(),
    }
    assert client.post("/v1/loads", json=body, headers=H).status_code == 200
    client.post("/v1/tick", headers=H)
    return world.store.get(Load, "api-load-1")


def upload_path(world: World) -> str:
    link = world.email.sent[0].body.split("upload it here:\n")[1].split("\n")[0]
    return "/u/" + link.rsplit("/u/", 1)[1]


def test_an_upload_is_answered_at_once_and_read_straight_after(service):
    world, client, reader = service
    load = new_load(world, client)
    response = client.post(upload_path(world), content=pod(load))
    assert response.status_code == 200 and response.json() == {"received": True}
    # The test client returns once the work queued behind the response has run.
    assert reader.calls == 1
    assert world.chase(load).state is ChaseState.CLOSED


def test_the_model_is_called_while_the_service_stays_free(service):
    """A reading can take half a minute. Broker screens, webhooks and other uploads
    must not wait behind it: the service's lock is free while the reader works."""
    world, client, reader = service
    load = new_load(world, client)

    def other_request_gets_through() -> bool:
        done = threading.Event()

        def ask():
            if client.get("/v1/brokers/brk-1/summary", headers=H).status_code == 200:
                done.set()

        thread = threading.Thread(target=ask)
        thread.start()
        thread.join(timeout=5)
        return done.is_set()

    reader.probe = other_request_gets_through
    client.post(upload_path(world), content=pod(load))
    assert reader.lock_was_free == [True]


def test_an_emailed_pod_posted_to_the_api_is_read_after_the_response(service):
    import base64

    world, client, reader = service
    load = new_load(world, client)
    body = {
        "broker_id": "brk-1",
        "channel": "EMAIL",
        "from_addr": world.dispatcher.email,
        "subject": f"POD {load.load_number}",
        "body": "attached",
        "attachments": [
            {"filename": "pod.pdf", "content_base64": base64.b64encode(pod(load)).decode()}
        ],
    }
    answer = client.post("/v1/inbound", json=body, headers=H)
    assert answer.status_code == 200 and answer.json()["status"] == "PROCESSED"
    assert reader.calls == 1 and world.chase(load).state is ChaseState.CLOSED


def test_a_reading_that_fails_after_the_response_is_picked_up_by_the_tick(service):
    world, client, reader = service
    load = new_load(world, client)
    reader.failures = [ReaderError("Anthropic 529/overloaded_error")]
    assert client.post(upload_path(world), content=pod(load)).status_code == 200
    assert world.chase(load).state is ChaseState.RECEIVED
    (reading,) = world.store.find(Reading)
    assert reading.status is ReadingStatus.PENDING

    assert client.post("/v1/tick", headers=H).json()["read"]["read"] == 0  # not due yet
    world.clock.advance(MINUTE_MS)
    tick = client.post("/v1/tick", headers=H).json()
    assert tick["read"] == {"read": 1, "retried": 0, "failed": 0, "duplicates": 0}
    assert world.chase(load).state is ChaseState.CLOSED  # read, checked and filed in one tick


def test_a_reading_that_leads_to_a_re_ask_sends_it_in_the_same_tick(service):
    world, client, reader = service
    load = new_load(world, client)
    reader.failures = [ReaderError("down")]
    client.post(upload_path(world), content=pod(load, signature_present=False))
    sent = len(world.email.sent)
    world.clock.advance(MINUTE_MS)
    tick = client.post("/v1/tick", headers=H).json()
    assert tick["read"]["read"] == 1 and tick["dispatch"]["sent"] == 1
    assert len(world.email.sent) == sent + 1 and world.chase(load).state is ChaseState.ASKED


def test_the_read_route_reads_due_files_and_needs_the_key(service):
    world, client, reader = service
    load = new_load(world, client)
    reader.failures = [ReaderError("down")]
    client.post(upload_path(world), content=pod(load))
    world.clock.advance(MINUTE_MS)
    assert client.post("/v1/read").status_code == 401
    assert client.post("/v1/read", headers=H).json()["read"] == 1


def test_a_crash_while_reading_after_the_response_only_delays_it(service, caplog):
    world, client, reader = service
    load = new_load(world, client)
    reader.failures = [MemoryError("out of memory decoding a huge scan")]
    assert client.post(upload_path(world), content=pod(load)).status_code == 200
    (reading,) = world.store.find(Reading)
    assert reading.status is ReadingStatus.PENDING and "MemoryError" in reading.last_error


def test_with_the_stand_in_nothing_is_queued_and_nothing_waits():
    world = World()
    client = TestClient(create_app(world.engine, KEY))
    load = new_load(world, client)
    client.post(upload_path(world), content=pod(load))
    assert world.chase(load).state is ChaseState.CLOSED
    assert [r.status for r in world.store.find(Reading)] == [ReadingStatus.DONE]
    assert client.post("/v1/tick", headers=H).json()["read"]["read"] == 0
