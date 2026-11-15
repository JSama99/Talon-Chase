"""The Talon Chase ontology: entities, their groups, the schema and the rules.

Same principles as PCOMJR, its own vocabulary:
  * closed JSON Schema (draft 2020-12): unknown fields are rejected
  * canonical JSON and SHA-256 for every hash, the schema's own included
  * cross-entity rules checked at runtime, each with a stable id
  * a JSON-LD context so records can be linked outside this service
"""

from chase.ontology.schema import (
    GROUPS,
    ONTOLOGY_ID,
    ONTOLOGY_VERSION,
    OntologyError,
    build_context,
    build_schema,
    schema_sha256,
    validate_entity,
    validate_event,
)

__all__ = [
    "GROUPS",
    "ONTOLOGY_ID",
    "ONTOLOGY_VERSION",
    "OntologyError",
    "build_context",
    "build_schema",
    "schema_sha256",
    "validate_entity",
    "validate_event",
]
