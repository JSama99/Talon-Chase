# Evidence chain specification

Format name: `talon-chase-evidence/1`

Every load has one chain of events. Each event's hash covers the hash of the event
before it, so changing, removing or reordering any event breaks every hash after it.
This document is enough to verify an export without any Talon Chase code.
`tools/verify_evidence.py` is a reference check in about 90 lines of standard-library Python.

## Canonical JSON

Every hash is taken over canonical JSON bytes:

- UTF-8, no byte-order mark.
- Object keys sorted by Unicode code point. Keys are strings.
- No whitespace between tokens: separators are `,` and `:`.
- Allowed values: object, array, string, integer, `true`, `false`, `null`.
- **No floats.** Every time is an integer count of milliseconds since the Unix epoch.
- Non-ASCII characters are written as themselves, not as `\u` escapes. Characters JSON
  requires to be escaped (`"`, `\`, control characters) use the shortest standard escape.

In Python: `json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")`,
after rejecting floats and non-string keys.

## Event

| Field | Type | Meaning |
| --- | --- | --- |
| `chain_id` | string | The load id. Two other chains exist: `broker:<id>` and `system` |
| `seq` | integer | Position in the chain, starting at 0, no gaps |
| `kind` | string | What happened, for example `attempt.queued` |
| `at_ms` | integer | When, in epoch milliseconds |
| `payload` | object | Details of the event |
| `payload_digest` | string | Lowercase hex SHA-256 of canonical JSON of `payload` |
| `prev_hash` | string | `hash` of the previous event; 64 zeros for the first |
| `hash` | string | See below |

```
payload_digest = hex(SHA-256(canonical_json(payload)))
header         = {"at_ms", "chain_id", "kind", "payload_digest", "seq"}   # exactly these five
hash           = hex(SHA-256(canonical_json(header) || ascii(prev_hash)))
```

`||` is byte concatenation. `prev_hash` is appended as its 64 ASCII hex characters.

## Verifying

For each event in order, starting with `prev = "0" * 64`:

1. `chain_id` equals the first event's `chain_id`.
2. `seq` equals the event's index.
3. `prev_hash` equals `prev`.
4. SHA-256 of the canonical payload equals `payload_digest`.
5. The recomputed `hash` equals `hash`.
6. Set `prev = hash`.

The last `hash` is the **head hash**. A chain cut short at the end still verifies, so
compare the head hash with one you recorded earlier:

```bash
python tools/verify_evidence.py export.json --head <head hash you were given>
```

## Test vectors

Event 0: `chain_id` `L-1`, `kind` `chase.opened`, `at_ms` `1700000000000`, payload `{"load":"L-1"}`

```
payload_digest fa58f84698a3fb2378f895bd16f841fbc6110c68cc6e8737e6c339e0d37e4993
hash           9e3999afeee8c1fe54c07fa76346810b4596d458e87787da81096f3d56542474
```

Event 1: `kind` `attempt.queued`, `at_ms` `1700000060000`,
payload `{"step":"FIRST_EMAIL","to":["dispatch@carrier.example"]}`

```
payload_digest 8939bba25b95e1a741f9a0de4768bcd5150cb0d5d5ffe813db88634adf99379d
hash           e416250130e9984385f335a43fc2ef0965c46aa6d2c0db8ef4582714b2b51a8b
```

## Event kinds

| Kind | Recorded when |
| --- | --- |
| `load.received`, `load.delivered`, `load.ready_to_invoice` | Load arrives, is marked delivered, has a filed POD |
| `chase.opened`, `chase.state`, `chase.rescheduled`, `chase.closed` | Chase lifecycle; `chase.state` carries `from` and `to` |
| `attempt.queued` | The agent decided to ask: step, channel, recipients, subject, SHA-256 of the body, outbox id |
| `message.sent`, `message.retry`, `message.deferred`, `message.cancelled`, `message.failed`, `message.reclaimed`, `message.duplicate` | What happened to that message in the outbox; see [OUTBOX.md](OUTBOX.md) |
| `sms.skipped`, `outbound.blocked`, `outbound.refused` | A request was not made, and why |
| `inbound.received`, `inbound.classified`, `inbound.held`, `inbound.late` | A reply arrived and what was done with it |
| `document.received`, `document.rerouted` | A file arrived: its SHA-256, name and type |
| `check.completed` | Result and reasons for one document |
| `writeback.pending`, `writeback.succeeded`, `writeback.failed`, `writeback.fallback` | Filing attempts, with adapter and receipt; `pending` while the packet email is in the outbox |
| `escalation.opened`, `escalation.acknowledged` | Handed to a person; resolved by a named operator, with a resolution (`HANDLED_BY_HAND`, `WAIVED`, `DISPUTED`, or `OTHER` with a note) |
| `requirement.closed` | The document a load needed is in hand (`MET`, naming the document) or a person settled it (`RESOLVED_BY_PERSON`) |
| `inbound.unmatched`, `contact.approved`, `consent.recorded`, `outbound.switch` | On the broker or system chain; `inbound.unmatched` is a message that could not be tied to a load; `consent.recorded` is a change to a contact's permission to receive texts, with its source (a STOP reply names the message) |

Message bodies and file contents are not in the chain. Their SHA-256 hashes are, so a
stored message or document can be shown to be the one the chain refers to.

Every payload has a fixed shape, given per kind in the ontology schema
([ONTOLOGY.md](ONTOLOGY.md), `ontology/talon-chase.schema.json`, `$defs/payload.<kind>`).
Stores refuse an event whose kind is unknown or whose payload does not match. An
export carries `ontology.version` and `ontology.schema_sha256`, so a verifier knows
which shapes the events were written against.

## What this proves, and its limits

It proves that an export is internally consistent and complete up to a known head
hash: nothing in it was edited, dropped from the middle, or reordered.

It does not prove on its own that:

- **The whole chain was not rebuilt.** Someone with write access to the database could
  replace every event and recompute every hash. The defence is to give the head hash
  to someone else as it grows, for example a daily email of head hashes to the broker,
  or a public timestamp. That is not built.
- **The events are true.** The chain records what the system did and saw. A forged
  document that passes the checks is recorded faithfully as a document that passed.
- **Timestamps are honest.** `at_ms` is the service's own clock.
