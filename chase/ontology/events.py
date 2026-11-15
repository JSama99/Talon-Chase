"""The payload of every evidence event kind.

Each kind maps field name -> type token:

  str, int, bool       a value of that JSON type (int is never negative)
  int?                 an integer or null
  str[]                a list of strings
  int{}                an object whose values are integers
  <EnumName>           one of that enum's values, e.g. ChaseState

Payloads are closed: no other field is allowed and every listed field is required.
"""

EVENT_PAYLOADS: dict[str, dict[str, str]] = {
    # Freight
    "load.received": {
        "load_number": "str",
        "carrier_id": "str",
        "delivery_appt_ms": "int",
        "delivered_at_ms": "int?",
    },
    "load.delivered": {"delivered_at_ms": "int"},
    "load.ready_to_invoice": {"at_ms": "int"},
    # Need
    "chase.opened": {"chase_id": "str", "trigger": "str", "plan": "int{}"},
    "chase.state": {"chase_id": "str", "from": "ChaseState", "to": "ChaseState"},
    "chase.rescheduled": {"next_step": "Step", "at_ms": "int"},
    "chase.closed": {"chase_id": "str", "reason": "str"},
    "requirement.closed": {
        "requirement_id": "str",
        "status": "RequirementStatus",
        "document_id": "str",
    },
    # Exchange: requests
    "attempt.queued": {
        "attempt_id": "str",
        "outbox_id": "str",
        "step": "Step",
        "channel": "Channel",
        "to": "str[]",
        "cc": "str[]",
        "subject": "str",
        "body_sha256": "str",
    },
    "outbound.blocked": {"step": "Step"},
    "outbound.refused": {"reason": "str"},
    "sms.skipped": {"step": "Step", "reason": "str"},
    # Exchange: delivery
    "message.sent": {
        "outbox_id": "str",
        "channel": "Channel",
        "to": "str[]",
        "provider_id": "str",
        "try": "int",
    },
    "message.retry": {
        "outbox_id": "str",
        "try": "int",
        "error": "str",
        "next_attempt_at_ms": "int",
    },
    "message.failed": {"outbox_id": "str", "tries": "int", "permanent": "bool", "error": "str"},
    "message.cancelled": {"outbox_id": "str", "reason": "str"},
    "message.deferred": {"outbox_id": "str", "reason": "str", "until_ms": "int"},
    "message.reclaimed": {"outbox_id": "str", "after_try": "int"},
    "message.duplicate": {"outbox_id": "str", "provider_id": "str"},
    # Exchange: inbound
    "inbound.received": {
        "inbound_id": "str",
        "channel": "Channel",
        "from": "str",
        "subject": "str",
        "body_sha256": "str",
        "attachments": "str[]",
    },
    "inbound.unmatched": {"inbound_id": "str", "from": "str", "channel": "Channel"},
    "inbound.classified": {"inbound_id": "str", "label": "str", "confidence": "int"},
    "inbound.held": {"inbound_id": "str", "reason": "EscalationReason", "detail": "str"},
    "inbound.late": {"inbound_id": "str", "chase_state": "str"},
    # Proof
    "document.received": {
        "document_id": "str",
        "sha256": "str",
        "filename": "str",
        "doc_type": "DocType",
        "rerouted_from_load": "str",
    },
    "document.rerouted": {"document_id": "str", "sha256": "str", "to_load": "str"},
    "check.completed": {
        "check_id": "str",
        "document_id": "str",
        "result": "CheckResult",
        "reasons": "str[]",
        "reroute_load_id": "str",
    },
    # Outcome
    "writeback.pending": {"writeback_id": "str", "adapter": "str", "reference": "str"},
    "writeback.failed": {
        "writeback_id": "str",
        "adapter": "str",
        "attempt": "int",
        "transient": "bool",
        "error": "str",
    },
    "writeback.fallback": {"from": "str", "to": "str", "writeback_id": "str"},
    "writeback.succeeded": {
        "writeback_id": "str",
        "adapter": "str",
        "receipt": "str",
        "idempotency_key": "str",
        "document_sha256": "str",
    },
    "escalation.opened": {
        "escalation_id": "str",
        "reason": "EscalationReason",
        "detail": "str",
    },
    "escalation.acknowledged": {
        "escalation_id": "str",
        "by": "str",
        "resolution": "EscalationResolution",
        "note": "str",
    },
    # Policy and people
    "consent.recorded": {
        "consent_id": "str",
        "contact_id": "str",
        "seq": "int",
        "status": "ConsentStatus",
        "source": "ConsentSource",
        "by": "str",
        "inbound_id": "str",
    },
    "contact.approved": {"contact_id": "str", "carrier_id": "str", "by": "str"},
    "config.changed": {"by": "str", "fields": "str[]"},
    "operator.saved": {"operator_id": "str", "role": "OperatorRole", "active": "bool", "by": "str"},
    "operator.signed_in": {"operator_id": "str"},
    "outbound.switch": {"enabled": "bool", "by": "str"},
}

# Kinds older chains may contain but that nothing records any more. A verifier
# accepts them in exported history; stores refuse to append them.
RETIRED_PAYLOADS: dict[str, dict[str, str]] = {
    # Replaced in 1.0.0 by consent.recorded with source STOP_REPLY (migration 0003).
    "sms.stopped": {"contact_id": "str", "inbound_id": "str"},
}
