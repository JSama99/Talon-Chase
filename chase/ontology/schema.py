"""Generate the JSON Schema and JSON-LD context from the model definitions, and
validate records against them."""

from __future__ import annotations

import dataclasses
import functools
import types
import typing
from enum import Enum
from typing import Any, get_args, get_origin, get_type_hints

from jsonschema import Draft202012Validator

from chase.domain import models as m
from chase.domain.checks import Extraction
from chase.domain.schedule import Plan
from chase.evidence.canonical import canonical_json, sha256_hex
from chase.evidence.chain import EvidenceEvent
from chase.ontology.events import EVENT_PAYLOADS, RETIRED_PAYLOADS

ONTOLOGY_VERSION = "1.0.0"
ONTOLOGY_ID = f"urn:talonsight:talon-chase:ontology:{ONTOLOGY_VERSION}"
VOCAB = "urn:talonsight:talon-chase:"

GROUPS: dict[str, tuple[type, ...]] = {
    "Who": (m.Broker, m.Operator, m.Carrier, m.Contact),
    "Freight": (m.Load, m.Party),
    "Need": (m.Requirement, m.Chase),
    "Exchange": (m.Attempt, m.OutboxMessage, m.Inbound),
    "Proof": (m.Document, m.Check, EvidenceEvent),
    "Outcome": (m.WriteBack, m.Escalation),
    "Policy": (m.BrokerConfig, m.Consent),
}
GROUP_PURPOSE = {
    "Who": "The broker, its people, and the carriers and carrier contacts it works with.",
    "Freight": "The loads and the businesses on them that carriers must never see named.",
    "Need": "What a load still needs, and the chase that obtains it.",
    "Exchange": "Requests the agent decides on, their delivery, and what comes back.",
    "Proof": "Documents, the verdict on each, and the hash-chained record of everything.",
    "Outcome": "Filing in the broker's system, and loads handed to a person.",
    "Policy": "How a broker wants chasing done, and who agreed to be texted.",
}
# Value types: stored inside an entity, never on their own.
SHAPES: dict[str, type] = {
    "BrokerConfig": m.BrokerConfig,
    "Attachment": m.Attachment,
    "Plan": Plan,
    "Extraction": Extraction,
}
ENUMS: tuple[type[Enum], ...] = (
    m.ChaseState, m.Step, m.Channel, m.Role, m.OperatorRole, m.PartyRole, m.DocType,
    m.RequirementStatus, m.CheckResult, m.EscalationReason, m.EscalationResolution,
    m.InboundStatus, m.OutboxStatus, m.OutboxPurpose, m.WriteBackStatus,
    m.ConsentStatus, m.ConsentSource,
)  # fmt: skip
_ENUM_NAMES = {e.__name__ for e in ENUMS}


class OntologyError(ValueError):
    """A record does not conform to the ontology."""


def group_of(cls: type) -> str:
    for group, members in GROUPS.items():
        if cls in members:
            return group
    return ""


def _payload_def_name(kind: str) -> str:
    return "payload." + kind


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/$defs/{name}"}


def _unwrap_optional(tp: Any) -> tuple[Any, bool]:
    if get_origin(tp) in (typing.Union, types.UnionType):
        args = [a for a in get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
    return tp, False


def _type_schema(tp: Any, shape: str = "") -> dict[str, Any]:
    tp, optional = _unwrap_optional(tp)
    if shape:
        schema: dict[str, Any] = _ref(shape)
    elif isinstance(tp, type) and issubclass(tp, Enum):
        schema = _ref(tp.__name__)
    elif dataclasses.is_dataclass(tp):
        schema = _ref(tp.__name__)
    elif tp is bool:
        schema = {"type": "boolean"}
    elif tp is int:
        schema = {"type": "integer", "minimum": 0}
    elif tp is str:
        schema = {"type": "string"}
    elif get_origin(tp) is list:
        (inner,) = get_args(tp)
        schema = {"type": "array", "items": _type_schema(inner)}
    elif get_origin(tp) is dict:
        _, value = get_args(tp)
        schema = {"type": "object"}
        if value is not Any:
            schema["additionalProperties"] = _type_schema(value)
    else:  # pragma: no cover - a new type needs a rule here
        raise TypeError(f"no schema rule for {tp!r}")
    if optional:
        return {"anyOf": [schema, {"type": "null"}]}
    return schema


def _object_schema(cls: type) -> dict[str, Any]:
    hints = get_type_hints(cls)
    properties: dict[str, Any] = {}
    for fld in dataclasses.fields(cls):
        meta = fld.metadata
        prop = _type_schema(hints[fld.name], meta.get("shape", ""))
        if meta.get("doc"):
            prop = {"description": meta["doc"], **prop}
        if meta.get("ref"):
            prop["x-ref"] = meta["ref"]
            if meta.get("optional_ref"):
                prop["x-ref-optional"] = True
        if meta.get("sensitive"):
            prop["x-sensitive"] = True
        properties[fld.name] = prop
    schema: dict[str, Any] = {
        "type": "object",
        "title": cls.__name__,
        "additionalProperties": False,
        "required": [fld.name for fld in dataclasses.fields(cls)],
        "properties": properties,
    }
    doc = (cls.__doc__ or "").strip()
    if doc and not doc.startswith(cls.__name__ + "("):
        schema["description"] = " ".join(doc.split())
    group = group_of(cls)
    if group:
        schema["x-group"] = group
    return schema


def _token_schema(token: str) -> dict[str, Any]:
    if token == "str":
        return {"type": "string"}
    if token == "int":
        return {"type": "integer", "minimum": 0}
    if token == "int?":
        return {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]}
    if token == "bool":
        return {"type": "boolean"}
    if token == "str[]":
        return {"type": "array", "items": {"type": "string"}}
    if token == "int{}":
        return {"type": "object", "additionalProperties": {"type": "integer", "minimum": 0}}
    if token in _ENUM_NAMES:
        return _ref(token)
    raise ValueError(f"unknown payload type token {token!r}")


def _payload_schema(kind: str, fields: dict[str, str]) -> dict[str, Any]:
    return {
        "type": "object",
        "title": f"{kind} payload",
        "additionalProperties": False,
        "required": list(fields),
        "properties": {name: _token_schema(token) for name, token in fields.items()},
    }


def _event_schema() -> dict[str, Any]:
    schema = _object_schema(EvidenceEvent)
    schema["description"] = (
        "One entry in a load's hash chain. The payload's shape depends on the kind."
    )
    props = schema["properties"]
    props["kind"] = {"description": "What happened.", "enum": sorted(EVENT_PAYLOADS)}
    props["seq"]["description"] = "Position in the chain, from 0, no gaps."
    props["chain_id"]["description"] = "The load id, or broker:<id>, or system."
    props["at_ms"]["description"] = "When it happened."
    props["payload"]["description"] = "Details; see the payload definition for the kind."
    hex64 = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    for name, doc in (
        ("payload_digest", "SHA-256 of the canonical payload."),
        ("prev_hash", "The previous event's hash; 64 zeros for the first."),
        ("hash", "SHA-256 over the canonical header and prev_hash."),
    ):
        props[name] = {"description": doc, **hex64}
    schema["allOf"] = [
        {
            "if": {"properties": {"kind": {"const": kind}}},
            "then": {"properties": {"payload": _ref(_payload_def_name(kind))}},
        }
        for kind in sorted(EVENT_PAYLOADS)
    ]
    return schema


@functools.cache
def build_schema() -> dict[str, Any]:
    defs: dict[str, Any] = {}
    for enum in ENUMS:
        defs[enum.__name__] = {
            "title": enum.__name__,
            "enum": [member.value for member in enum],
        }
        doc = (enum.__doc__ or "").strip()
        if doc and not doc.startswith("An enumeration"):
            defs[enum.__name__]["description"] = " ".join(doc.split())
    for name, cls in SHAPES.items():
        defs[name] = _object_schema(cls)
    for cls in m.ENTITY_TYPES:
        defs[cls.__name__] = _object_schema(cls)
    defs["EvidenceEvent"] = _event_schema()
    for kind, fields in sorted(EVENT_PAYLOADS.items()):
        defs[_payload_def_name(kind)] = _payload_schema(kind, fields)
    for kind, fields in sorted(RETIRED_PAYLOADS.items()):
        defs[_payload_def_name(kind)] = _payload_schema(kind, fields) | {"deprecated": True}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": ONTOLOGY_ID,
        "title": "Talon Chase ontology",
        "description": (
            "Entities, value types, enumerations and evidence payloads of Talon Chase. "
            "Every object is closed. x-ref names the entity an id points at; "
            "x-sensitive marks values that must never be sent to a carrier."
        ),
        "x-version": ONTOLOGY_VERSION,
        "x-groups": {group: [c.__name__ for c in members] for group, members in GROUPS.items()},
        "x-retired-kinds": sorted(RETIRED_PAYLOADS),
        "$defs": dict(sorted(defs.items())),
    }


@functools.cache
def schema_sha256() -> str:
    """Canonical hash of the schema; exports carry it so a reader knows the rules used."""
    return sha256_hex(canonical_json(build_schema()))


def build_context() -> dict[str, Any]:
    """JSON-LD context: entity and field names as IRIs, references as links."""
    context: dict[str, Any] = {
        "@version": 1.1,
        "@vocab": VOCAB,
        "xsd": "http://www.w3.org/2001/XMLSchema#",
        "id": "@id",
    }
    # 1.1 is the JSON-LD version number. It is the one float anywhere in the ontology,
    # so the context is written with json, not hashed.
    for cls in (*m.ENTITY_TYPES, *SHAPES.values(), EvidenceEvent):
        context[cls.__name__] = {"@id": VOCAB + cls.__name__}
    seen: set[str] = set()
    for cls in (*m.ENTITY_TYPES, *SHAPES.values()):
        hints = get_type_hints(cls)
        for fld in dataclasses.fields(cls):
            if fld.name in seen or fld.name == "id":
                continue
            seen.add(fld.name)
            term: dict[str, Any] = {"@id": VOCAB + fld.name}
            base, _ = _unwrap_optional(hints[fld.name])
            if fld.metadata.get("ref"):
                term["@type"] = "@id"
                if get_origin(base) is list:
                    term["@container"] = "@set"
            elif base is int:
                term["@type"] = "xsd:integer"
            elif base is bool:
                term["@type"] = "xsd:boolean"
            context[fld.name] = term
    return {"@context": context}


# ------------------------------------------------------------------ validation


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


@functools.cache
def _validator(def_name: str) -> Draft202012Validator:
    schema = build_schema()
    root = {"$schema": schema["$schema"], "$defs": schema["$defs"], **_ref(def_name)}
    return Draft202012Validator(root)


def _raise_first(def_name: str, record: Any, label: str) -> None:
    errors = sorted(_validator(def_name).iter_errors(record), key=lambda e: list(e.path))
    if errors:
        error = errors[0]
        where = ".".join(str(p) for p in error.path) or "(record)"
        raise OntologyError(f"{label}: {where}: {error.message}")


def validate_entity(obj: Any) -> None:
    """Raise OntologyError unless ``obj`` matches its entity definition."""
    name = type(obj).__name__
    if name not in build_schema()["$defs"]:
        raise OntologyError(f"{name} is not part of the ontology")
    _raise_first(name, _plain(obj), f"{name} {getattr(obj, 'id', '')}".strip())


def validate_event(event: EvidenceEvent) -> None:
    """Raise OntologyError unless the event, and its payload for its kind, conform."""
    _raise_first("EvidenceEvent", _plain(event), f"evidence {event.chain_id}#{event.seq}")
