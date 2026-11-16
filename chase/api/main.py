"""Service entry point: uvicorn chase.api.main:app

Version 1 runs with the in-memory text-message stand-in and the rule-based reader.
Email is real once RESEND_API_KEY is set; without it, email goes to an in-memory
stand-in that records what it would send and contacts no one.

Environment:
  DATABASE_URL    database address; a plain postgres:// or postgresql:// address is
                  opened with psycopg 3. Default: sqlite+pysqlite:///chase.db
  CHASE_API_KEY   admin key, at least 16 characters
  CHASE_SECRET    signs upload links, at least 32 characters
  CHASE_BASE_URL  public address used in upload and sign-in links; on Render it
                  defaults to RENDER_EXTERNAL_URL
  CHASE_TICK_SECONDS  run the tick inside the service every N seconds (0, the
                  default, leaves it to an outside scheduler calling POST /v1/tick)
  CHASE_LOGIN_FROM  sender of sign-in emails; its domain must be verified in Resend

Email through Resend (see docs/EMAIL.md):
  RESEND_API_KEY            API key (sending access is enough to send; receiving
                            replies needs full access)
  RESEND_WEBHOOK_SECRET     the webhook's signing secret, whsec_...
  RESEND_VERIFIED_DOMAINS   comma-separated domains verified in Resend; mail from them
                            is sent as written
  RESEND_SHARED_FROM        address on a verified domain that brokers without their own
                            verified domain send through, e.g. chase@mail.example.com
  RESEND_INBOUND_DOMAIN     where Resend receives mail (required with RESEND_SHARED_FROM)
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from chase.api.app import create_app
from chase.channels.fakes import FakeEmail, FakeSms
from chase.channels.resend import (
    Identity,
    ResendClient,
    ResendEmail,
    ResendInbox,
    identity_from_env,
    secret_key,
)
from chase.domain.clock import SystemClock, UuidIds
from chase.domain.models import Broker
from chase.engine import ChaseEngine
from chase.ports import EmailPort
from chase.reader.rules import RuleBasedClassifier, StandInReader
from chase.store.sql import SqlStore, normalize_database_url
from chase.writeback.adapters import FakeApiAdapter


def _broker_of(store):
    """The (id, name) of the broker whose chase mailbox is this address."""

    def lookup(address: str) -> tuple[str, str] | None:
        wanted = address.strip().lower()
        for broker in sorted(store.find(Broker), key=lambda b: b.id):
            if broker.chase_mailbox.strip().lower() == wanted:
                return broker.id, broker.name
        return None

    return lookup


def _resend(
    store, env: Mapping[str, str] | None = None
) -> tuple[EmailPort, ResendInbox | None, Identity | None]:
    """The email port, the mail fetcher and the identity: real when RESEND_API_KEY is
    set, else the in-memory stand-in. Raises RuntimeError for a half-set-up Resend."""
    env = os.environ if env is None else env
    api_key = env.get("RESEND_API_KEY", "")
    if not api_key:
        return FakeEmail(), None, None
    try:
        identity = identity_from_env(env)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    if secret_key(env.get("RESEND_WEBHOOK_SECRET", "")) is None:
        raise RuntimeError(
            "RESEND_WEBHOOK_SECRET must be the webhook's signing secret (whsec_...); "
            "without it replies and bounces cannot be received"
        )
    client = ResendClient(api_key)
    email = ResendEmail(client, store.get_blob, identity, _broker_of(store))
    return email, ResendInbox(client), identity


def base_url(env: Mapping[str, str] | None = None) -> str:
    """The public address used in links: CHASE_BASE_URL, else the address Render
    gives the service, else the local one."""
    env = os.environ if env is None else env
    for key in ("CHASE_BASE_URL", "RENDER_EXTERNAL_URL"):
        value = env.get(key, "").strip().rstrip("/")
        if value:
            return value
    return "http://localhost:8000"


def tick_seconds(env: Mapping[str, str] | None = None) -> float:
    """CHASE_TICK_SECONDS as a number: 0 (off) or at least 5 seconds."""
    env = os.environ if env is None else env
    raw = env.get("CHASE_TICK_SECONDS", "").strip() or "0"
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"CHASE_TICK_SECONDS must be a number, not {raw!r}") from exc
    if value != 0 and not 5 <= value <= 3600:
        raise RuntimeError("CHASE_TICK_SECONDS must be 0 (off) or between 5 and 3600")
    return value


def build_app():
    secret = os.environ.get("CHASE_SECRET", "")
    if len(secret) < 32:
        raise RuntimeError("CHASE_SECRET must be set to at least 32 characters")
    store = SqlStore(
        normalize_database_url(os.environ.get("DATABASE_URL", "sqlite+pysqlite:///chase.db"))
    )
    if store.engine.dialect.name == "sqlite":
        store.create_schema()  # Postgres is set up by `alembic upgrade head`
    email, inbox, identity = _resend(store)
    engine = ChaseEngine(
        store=store,
        clock=SystemClock(),
        ids=UuidIds(),
        email=email,
        sms=FakeSms(),
        classifier=RuleBasedClassifier(),
        reader=StandInReader(),
        adapters=[FakeApiAdapter()],
        secret=secret.encode(),
        base_url=base_url(),
    )
    return create_app(
        engine,
        os.environ.get("CHASE_API_KEY", ""),
        resend_webhook_secret=os.environ.get("RESEND_WEBHOOK_SECRET", "") if inbox else "",
        resend_inbox=inbox,
        resend_inbound_domain=identity.inbound_domain if identity else "",
        login_from=os.environ.get("CHASE_LOGIN_FROM", "Talon Chase <signin@talonchase.invalid>"),
        tick_every_s=tick_seconds(),
    )


app = build_app() if os.environ.get("CHASE_SECRET") else None
