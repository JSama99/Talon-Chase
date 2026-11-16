"""SQL store (Postgres in production, SQLite for local runs and tests).

One table per entity, built from the dataclass definitions: plain fields become
columns, lists and nested objects become JSON columns. Evidence events get their
own table whose primary key is (chain_id, seq), so two writers can never fork a
chain: the second insert fails.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import threading
import types
import typing
from enum import Enum
from typing import Any, TypeVar, get_args, get_origin, get_type_hints

import sqlalchemy as sa

from chase.domain.models import ENTITY_TYPES
from chase.evidence.chain import EvidenceEvent
from chase.ontology.schema import validate_entity, validate_event

T = TypeVar("T")
ADVISORY_KEY = 7_423_001
# A second insert with the same key fails, so one decision can never become two messages.
UNIQUE = {"OutboxMessage": ("idempotency_key",)}
# Unique together: two writers racing on one contact's history fail instead of
# both claiming the same position.
UNIQUE_TOGETHER = {"Consent": [("contact_id", "seq")]}


def _unwrap_optional(tp: Any) -> tuple[Any, bool]:
    if get_origin(tp) in (typing.Union, types.UnionType):
        args = [a for a in get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
    return tp, False


def _column_type(tp: Any) -> sa.types.TypeEngine:
    tp, _ = _unwrap_optional(tp)
    if tp is bool:
        return sa.Boolean()
    if tp is int:
        return sa.BigInteger()  # epoch milliseconds do not fit in 32 bits
    if tp is str or (isinstance(tp, type) and issubclass(tp, Enum)):
        return sa.Text()
    return sa.JSON()


def table_name(cls: type) -> str:
    out = []
    for i, ch in enumerate(cls.__name__):
        if ch.isupper() and i:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def build_metadata() -> tuple[sa.MetaData, dict[type, sa.Table]]:
    metadata = sa.MetaData()
    tables: dict[type, sa.Table] = {}
    indexed = {"load_id", "chase_id", "broker_id", "carrier_id", "state", "status", "external_id"}
    for cls in ENTITY_TYPES:
        hints = get_type_hints(cls)
        columns = []
        for f in dataclasses.fields(cls):
            optional = _unwrap_optional(hints[f.name])[1]
            unique = f.name in UNIQUE.get(cls.__name__, ())
            columns.append(
                sa.Column(
                    f.name,
                    _column_type(hints[f.name]),
                    primary_key=f.name == "id",
                    nullable=optional,
                    index=(f.name in indexed) and not unique,
                    unique=unique,
                )
            )
        constraints = [
            sa.UniqueConstraint(*names, name=f"uq_{table_name(cls)}_{'_'.join(names)}")
            for names in UNIQUE_TOGETHER.get(cls.__name__, ())
        ]
        tables[cls] = sa.Table(table_name(cls), metadata, *columns, *constraints)
    sa.Table(
        "blob",
        metadata,
        sa.Column("sha256", sa.Text, primary_key=True),
        sa.Column("content", sa.LargeBinary, nullable=False),
    )
    sa.Table(
        "evidence_event",
        metadata,
        sa.Column("chain_id", sa.Text, primary_key=True),
        sa.Column("seq", sa.BigInteger, primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("at_ms", sa.BigInteger, nullable=False),
        sa.Column("payload", sa.Text, nullable=False),  # JSON text, kept byte for byte
        sa.Column("payload_digest", sa.Text, nullable=False),
        sa.Column("prev_hash", sa.Text, nullable=False),
        sa.Column("hash", sa.Text, nullable=False),
    )
    sa.Table(
        "setting",
        metadata,
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
    )
    return metadata, tables


METADATA, TABLES = build_metadata()


def _to_plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _to_plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, (list, tuple)):
        return [_to_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_plain(v) for k, v in value.items()}
    return value


def _from_plain(tp: Any, value: Any) -> Any:
    tp, _ = _unwrap_optional(tp)
    if value is None:
        return None
    if isinstance(tp, type) and issubclass(tp, Enum):
        return tp(value)
    if dataclasses.is_dataclass(tp):
        hints = get_type_hints(tp)
        return tp(**{k: _from_plain(hints[k], v) for k, v in value.items() if k in hints})
    if get_origin(tp) is list:
        (inner,) = get_args(tp)
        return [_from_plain(inner, v) for v in value]
    return value


def normalize_database_url(url: str) -> str:
    """Point a plain Postgres address at the psycopg 3 driver.

    Hosts such as Render hand out ``postgres://`` or ``postgresql://`` addresses, which
    SQLAlchemy would open with psycopg2, a driver this project does not install. An
    address that already names a driver, and any other database, is left as it is.
    """
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


class SqlStore:
    def __init__(self, url: str) -> None:
        kwargs: dict[str, Any] = {}
        if url.startswith("sqlite") and ":memory:" in url:
            from sqlalchemy.pool import StaticPool

            kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
        self.engine = sa.create_engine(url, **kwargs)
        self._local = threading.local()
        self._hints = {cls: get_type_hints(cls) for cls in ENTITY_TYPES}

    def create_schema(self) -> None:
        """Create tables directly. Deployments use the Alembic migration instead."""
        METADATA.create_all(self.engine)

    # -- transactions ----------------------------------------------------------

    @contextlib.contextmanager
    def transaction(self):
        """Everything inside is committed together, or not at all."""
        if getattr(self._local, "conn", None) is not None:
            yield
            return
        with self.engine.begin() as conn:
            if self.engine.dialect.name == "postgresql":
                # One engine transaction at a time across every worker, so two
                # requests can never act on the same chase or fork a chain.
                conn.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": ADVISORY_KEY})
            self._local.conn = conn
            try:
                yield
            finally:
                self._local.conn = None

    @contextlib.contextmanager
    def _conn(self):
        current = getattr(self._local, "conn", None)
        if current is not None:
            yield current
        else:
            with self.engine.begin() as conn:
                yield conn

    # -- entities --------------------------------------------------------------

    def put(self, obj: Any) -> None:
        validate_entity(obj)
        table = TABLES[type(obj)]
        row = {f.name: _to_plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
        with self._conn() as conn:
            updated = conn.execute(
                table.update().where(table.c.id == obj.id).values(**row)
            ).rowcount
            if not updated:
                conn.execute(table.insert().values(**row))

    def _build(self, cls: type[T], row: Any) -> T:
        hints = self._hints[cls]
        return cls(**{name: _from_plain(hints[name], value) for name, value in row.items()})

    def get(self, cls: type[T], obj_id: str) -> T | None:
        table = TABLES[cls]
        with self._conn() as conn:
            row = conn.execute(table.select().where(table.c.id == obj_id)).mappings().first()
        return self._build(cls, row) if row is not None else None

    def find(self, cls: type[T], **equals: Any) -> list[T]:
        table = TABLES[cls]
        query = table.select().order_by(table.c.id)
        for key, value in equals.items():
            column = table.c[key]
            query = query.where(column.is_(None) if value is None else column == _to_plain(value))
        with self._conn() as conn:
            rows = conn.execute(query).mappings().all()
        return [self._build(cls, row) for row in rows]

    # -- blobs -----------------------------------------------------------------

    def put_blob(self, sha256: str, content: bytes) -> None:
        table = METADATA.tables["blob"]
        with self._conn() as conn:
            exists = conn.execute(sa.select(table.c.sha256).where(table.c.sha256 == sha256)).first()
            if not exists:
                conn.execute(table.insert().values(sha256=sha256, content=content))

    def get_blob(self, sha256: str) -> bytes | None:
        table = METADATA.tables["blob"]
        with self._conn() as conn:
            row = conn.execute(sa.select(table.c.content).where(table.c.sha256 == sha256)).first()
        return bytes(row[0]) if row else None

    # -- evidence --------------------------------------------------------------

    @staticmethod
    def _event(row: Any) -> EvidenceEvent:
        return EvidenceEvent(
            chain_id=row["chain_id"],
            seq=row["seq"],
            kind=row["kind"],
            at_ms=row["at_ms"],
            payload=json.loads(row["payload"]),
            payload_digest=row["payload_digest"],
            prev_hash=row["prev_hash"],
            hash=row["hash"],
        )

    def append_evidence(self, event: EvidenceEvent) -> None:
        validate_event(event)
        table = METADATA.tables["evidence_event"]
        row = event.to_dict()
        row["payload"] = json.dumps(
            _to_plain(event.payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        with self._conn() as conn:
            conn.execute(table.insert().values(**row))

    def last_evidence(self, chain_id: str) -> EvidenceEvent | None:
        table = METADATA.tables["evidence_event"]
        query = (
            table.select().where(table.c.chain_id == chain_id).order_by(table.c.seq.desc()).limit(1)
        )
        with self._conn() as conn:
            row = conn.execute(query).mappings().first()
        return self._event(row) if row else None

    def evidence(self, chain_id: str) -> list[EvidenceEvent]:
        table = METADATA.tables["evidence_event"]
        query = table.select().where(table.c.chain_id == chain_id).order_by(table.c.seq)
        with self._conn() as conn:
            return [self._event(row) for row in conn.execute(query).mappings().all()]

    # -- settings --------------------------------------------------------------

    def get_setting(self, key: str, default: str) -> str:
        table = METADATA.tables["setting"]
        with self._conn() as conn:
            row = conn.execute(sa.select(table.c.value).where(table.c.key == key)).first()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        table = METADATA.tables["setting"]
        with self._conn() as conn:
            updated = conn.execute(
                table.update().where(table.c.key == key).values(value=value)
            ).rowcount
            if not updated:
                conn.execute(table.insert().values(key=key, value=value))
