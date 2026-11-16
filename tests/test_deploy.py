"""Deployment: the host's addresses and port, the in-service tick, and the Render
Blueprint, checked against what the code actually reads."""

from __future__ import annotations

import contextlib
import io
import pathlib
import re
import time

import pytest
import yaml
from fastapi.testclient import TestClient

from chase.api.app import create_app
from chase.api.main import base_url, tick_seconds
from chase.channels.resend import secret_key
from chase.domain.models import HOUR_MS
from chase.store.sql import normalize_database_url
from tests.conftest import World

ROOT = pathlib.Path(__file__).resolve().parent.parent
KEY = "test-api-key-0123456789"
H = {"X-API-Key": KEY}


# ------------------------------------------------------------------ addresses


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("postgres://u:p@h:5432/db", "postgresql+psycopg://u:p@h:5432/db"),
        ("postgresql://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgresql+asyncpg://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
        ("sqlite+pysqlite:///chase.db", "sqlite+pysqlite:///chase.db"),
        ("sqlite:///:memory:", "sqlite:///:memory:"),
    ],
)
def test_a_plain_postgres_address_is_opened_with_psycopg_3(given, expected):
    assert normalize_database_url(given) == expected


def test_the_migration_opens_the_same_address_the_service_does():
    env = (ROOT / "migrations" / "env.py").read_text()
    assert "normalize_database_url(url)" in env


def test_links_use_the_set_address_then_renders_then_the_local_one():
    assert base_url({"CHASE_BASE_URL": "https://chase.example/"}) == "https://chase.example"
    assert (
        base_url(
            {
                "CHASE_BASE_URL": "https://chase.example",
                "RENDER_EXTERNAL_URL": "https://x.onrender.com",
            }
        )
        == "https://chase.example"
    )
    assert base_url({"RENDER_EXTERNAL_URL": "https://x.onrender.com"}) == "https://x.onrender.com"
    assert base_url({"CHASE_BASE_URL": "  "}) == "http://localhost:8000"
    assert base_url({}) == "http://localhost:8000"


@pytest.mark.parametrize(("raw", "expected"), [("", 0), ("0", 0), ("60", 60), ("5", 5)])
def test_tick_interval_accepts_off_or_a_sane_number_of_seconds(raw, expected):
    assert tick_seconds({"CHASE_TICK_SECONDS": raw}) == expected


@pytest.mark.parametrize("raw", ["soon", "1", "-60", "86400"])
def test_tick_interval_refuses_anything_else(raw):
    with pytest.raises(RuntimeError, match="CHASE_TICK_SECONDS"):
        tick_seconds({"CHASE_TICK_SECONDS": raw})


def test_the_container_listens_on_the_port_the_host_gives_it():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "--port ${PORT:-8000}" in dockerfile
    assert "alembic upgrade head &&" in dockerfile


# ----------------------------------------------------------- in-service tick


def _app_with_a_due_chase(world: World, every_s: float):
    return create_app(world.engine, KEY, tick_every_s=every_s)


def _add_delivered_load(world: World, client: TestClient) -> None:
    body = {
        "id": "tick-load-1",
        "broker_id": "brk-1",
        "carrier_id": "car-1",
        "load_number": "TC-777001",
        "delivery_appt_ms": world.clock.now_ms() - HOUR_MS,
        "delivered_at_ms": world.clock.now_ms(),
    }
    assert client.post("/v1/loads", json=body, headers=H).status_code == 200


def _wait_for(condition, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_the_service_sends_what_is_due_without_anyone_calling_tick():
    world = World()
    with TestClient(_app_with_a_due_chase(world, 0.05)) as client:
        _add_delivered_load(world, client)
        assert _wait_for(lambda: len(world.email.sent) >= 1), "the first request never went out"
    assert "TC-777001" in world.email.sent[0].subject + world.email.sent[0].body


def test_a_failed_tick_is_logged_and_the_next_one_still_runs(caplog):
    world = World()
    real_tick = world.engine.tick
    calls = {"n": 0}

    def flaky_tick():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database went away for a moment")
        return real_tick()

    world.engine.tick = flaky_tick
    with TestClient(_app_with_a_due_chase(world, 0.05)) as client:
        _add_delivered_load(world, client)
        assert _wait_for(lambda: len(world.email.sent) >= 1), "the schedule stopped after a failure"
    assert calls["n"] >= 2
    assert "scheduled tick failed" in caplog.text


def test_no_tick_runs_unless_it_is_switched_on():
    world = World()
    with TestClient(create_app(world.engine, KEY)) as client:
        _add_delivered_load(world, client)
        time.sleep(0.3)
        assert world.email.sent == []
        assert client.post("/v1/tick", headers=H).json()["dispatch"]["sent"] == 1
    assert len(world.email.sent) == 1


def test_the_schedule_stops_when_the_service_stops():
    world = World()
    with TestClient(_app_with_a_due_chase(world, 0.05)):
        pass
    before = world.email.calls
    time.sleep(0.3)
    assert world.email.calls == before


# -------------------------------------------------------------- the Blueprint


@pytest.fixture(scope="module")
def blueprint() -> dict:
    return yaml.safe_load((ROOT / "render.yaml").read_text())


@pytest.fixture(scope="module")
def web(blueprint) -> dict:
    (service,) = [s for s in blueprint["services"] if s["type"] == "web"]
    return service


def _env(service: dict) -> dict[str, dict]:
    return {v["key"]: v for v in service["envVars"]}


def test_the_service_is_built_from_the_dockerfile_and_checked_on_a_real_route(web):
    assert web["runtime"] == "docker"
    assert web["healthCheckPath"] == "/healthz"
    world = World()
    assert TestClient(create_app(world.engine, KEY)).get("/healthz").status_code == 200


def test_the_service_and_its_database_share_a_region_and_neither_sleeps(blueprint, web):
    (db,) = blueprint["databases"]
    assert web["region"] == db["region"]
    assert web["plan"] != "free"  # a sleeping instance would stop the tick
    assert db["plan"] != "free"  # a free database is deleted after a trial period
    assert db["ipAllowList"] == []


def test_the_database_address_comes_from_the_blueprints_own_database(blueprint, web):
    (db,) = blueprint["databases"]
    ref = _env(web)["DATABASE_URL"]["fromDatabase"]
    assert ref == {"name": db["name"], "property": "connectionString"}


def test_every_setting_the_service_reads_is_provided_and_no_secret_is_committed(web):
    env = _env(web)
    main = (ROOT / "chase" / "api" / "main.py").read_text()
    read = set(re.findall(r'"((?:CHASE|RESEND)_[A-Z_]+)"', main))
    resend = (ROOT / "chase" / "channels" / "resend.py").read_text()
    read |= set(re.findall(r'"(RESEND_[A-Z_]+)"', resend))
    # CHASE_BASE_URL is left to Render's own address on purpose.
    assert read - {"CHASE_BASE_URL"} <= set(env), sorted(read - set(env))
    for key in ("CHASE_SECRET", "CHASE_API_KEY"):
        assert env[key] == {"key": key, "generateValue": True}
    for key in (
        "RESEND_API_KEY",
        "RESEND_WEBHOOK_SECRET",
        "RESEND_VERIFIED_DOMAINS",
        "RESEND_SHARED_FROM",
        "RESEND_INBOUND_DOMAIN",
        "CHASE_LOGIN_FROM",
    ):
        assert env[key].get("sync") is False and "value" not in env[key], key
    assert tick_seconds({"CHASE_TICK_SECONDS": env["CHASE_TICK_SECONDS"]["value"]}) == 60


def test_the_deploy_guide_publishes_no_webhook_secret():
    """A secret written in the repo would let anyone sign webhook events."""
    guide = (ROOT / "docs" / "DEPLOY.md").read_text()
    assert re.findall(r"whsec_[A-Za-z0-9+/=]{8,}", guide) == []


def test_the_guides_command_makes_a_fresh_secret_the_service_accepts():
    guide = (ROOT / "docs" / "DEPLOY.md").read_text()
    (command,) = re.findall(r'python3 -c "(import base64, secrets; print\(.*\))"', guide)
    first, second = (_run_print(command) for _ in range(2))
    assert first != second
    for value in (first, second):
        key = secret_key(value)
        assert value.startswith("whsec_") and key is not None and len(key) == 32


def _run_print(code: str) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(code, {})  # the guide's own one-liner, read from the repo
    return out.getvalue().strip()
