"""Service entry point: uvicorn chase.api.main:app

Version 1 runs with the in-memory channel fakes and the rule-based reader, so it
records what it would send and never contacts anyone. Real adapters replace the
fakes here once they exist.

Environment:
  DATABASE_URL    SQLAlchemy URL (postgresql+psycopg://... or sqlite+pysqlite:///chase.db)
  CHASE_API_KEY   admin key, at least 16 characters
  CHASE_SECRET    signs upload links, at least 32 characters
  CHASE_BASE_URL  public address used in upload and sign-in links
  CHASE_LOGIN_FROM  sender of sign-in emails (a Postmark-verified sender)

Email through Postmark (without POSTMARK_SERVER_TOKEN, email goes to the in-memory
stand-in and nothing is sent):
  POSTMARK_SERVER_TOKEN       server API token
  POSTMARK_MESSAGE_STREAM     outbound stream id, default "outbound"
  POSTMARK_WEBHOOK_USER       Basic credentials Postmark sends to the webhooks
  POSTMARK_WEBHOOK_PASSWORD   (both required, at least 24 characters for the password)
"""

from __future__ import annotations

import os

from chase.api.app import create_app
from chase.channels.fakes import FakeEmail, FakeSms
from chase.channels.postmark import PostmarkEmail
from chase.domain.clock import SystemClock, UuidIds
from chase.engine import ChaseEngine
from chase.reader.rules import RuleBasedClassifier, StandInReader
from chase.store.sql import SqlStore
from chase.writeback.adapters import FakeApiAdapter


def _email(store: SqlStore):
    token = os.environ.get("POSTMARK_SERVER_TOKEN", "")
    if not token:
        return FakeEmail()
    return PostmarkEmail(
        token,
        store.get_blob,
        message_stream=os.environ.get("POSTMARK_MESSAGE_STREAM", "outbound"),
    )


def build_app():
    secret = os.environ.get("CHASE_SECRET", "")
    if len(secret) < 32:
        raise RuntimeError("CHASE_SECRET must be set to at least 32 characters")
    store = SqlStore(os.environ.get("DATABASE_URL", "sqlite+pysqlite:///chase.db"))
    if store.engine.dialect.name == "sqlite":
        store.create_schema()  # Postgres is set up by `alembic upgrade head`
    engine = ChaseEngine(
        store=store,
        clock=SystemClock(),
        ids=UuidIds(),
        email=_email(store),
        sms=FakeSms(),
        classifier=RuleBasedClassifier(),
        reader=StandInReader(),
        adapters=[FakeApiAdapter()],
        secret=secret.encode(),
        base_url=os.environ.get("CHASE_BASE_URL", "http://localhost:8000"),
    )
    hook_user = os.environ.get("POSTMARK_WEBHOOK_USER", "")
    hook_password = os.environ.get("POSTMARK_WEBHOOK_PASSWORD", "")
    if hook_password and len(hook_password) < 24:
        raise RuntimeError("POSTMARK_WEBHOOK_PASSWORD must be at least 24 characters")
    return create_app(
        engine,
        os.environ.get("CHASE_API_KEY", ""),
        postmark_webhook_user=hook_user,
        postmark_webhook_password=hook_password,
        login_from=os.environ.get("CHASE_LOGIN_FROM", "Talon Chase <signin@talonchase.invalid>"),
    )


app = build_app() if os.environ.get("CHASE_SECRET") else None
