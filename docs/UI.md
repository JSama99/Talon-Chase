# Broker screens

Three screens. The broker should open this for two minutes a day, clear a short list,
and leave. It is not a dashboard to manage.

Designs for the three screens are in Paper: https://app.paper.design/file/01M7E2JX9YN540Z2MN7K42VXAE
The numbers in them are placeholders. "Median to filed" comes from `summary.median_ms_to_filed`.

Every field below exists in the API today. Times arrive as epoch milliseconds; show
them in the broker's time zone.

## 1. Needs you (home)

**Data:** `GET /v1/brokers/{id}/queue`, `GET /v1/brokers/{id}/summary`

- A header line: how many loads need a person, how many are in progress, how many were
  filed. From `summary.needs_a_person`, `in_progress`, `filed`.
- A banner when outbound is off (`outbound_enabled` or `global_outbound_enabled` false),
  with `messages_waiting`: how many are held until it is turned back on.
- One row per open escalation, oldest first: load number, carrier, a plain reason, how
  long it has waited, the carrier's phone numbers as tap-to-call.
- Below it, unmatched messages: sender, subject, received time.
- Empty state: "Nothing needs you."

Plain wording for each reason:

| `reason` | Show |
| --- | --- |
| `NO_RESPONSE` | No reply after 3 requests |
| `RISK` | Held: mentions payment details, or unknown sender |
| `UNKNOWN_SENDER` | New address at this carrier. Approve it? |
| `DOCUMENT_EXCEPTION` | POD has a note: shortage, damage, refusal or date |
| `LOW_CONFIDENCE` | Could not read the POD reliably |
| `NEEDS_HUMAN` | Carrier asked a question |
| `WRONG_PERSON` | Contact says they are the wrong person |
| `WRITEBACK_FAILED` | Could not file the POD |
| `NO_CONTACT` | No dispatcher email for this carrier |
| `REASK_LIMIT` | Carrier keeps sending a document that fails |
| `OUTBOUND_BLOCKED` | A message was stopped before sending |
| `SEND_FAILED` | Could not deliver a message to the carrier |

Action on a row: open the load. Acknowledging happens there, with a required name and
an optional note (`POST /v1/escalations/{id}/acknowledge`).

## 2. Load

**Data:** `GET /v1/loads/{id}`, `GET /v1/loads/{id}/evidence`

- Top: load number, carrier, state, and when it became ready to invoice if it did.
- A timeline, newest last, built from the evidence events: asked, replied, document
  received, check result with reasons, filed, escalated. Group the rapid state changes
  into one line ("Document checked: unsigned, asked again").
- The document itself with its check result beside it.
- If escalated: the reason, the detail, the carrier's phone numbers, and the
  acknowledge control.
- "Export evidence" downloads the JSON. Show the head hash and whether the chain
  verifies (`verification.ok`).

## 3. Settings

**Data:** `PATCH /v1/brokers/{id}/config`, `POST /v1/outbound/switch`, `POST /v1/contacts/approve`

- The off switch, large and first. Requires a name.
- Schedule: hours for the first text, third request and hand-off; quiet hours; morning
  hour. Show them as hours; the API takes milliseconds.
- Strictness: reading confidence floor (0 to 100), maximum re-asks.
- Where PODs are filed (read-only for now): the adapter name.

## Also: all chases

**Data:** `GET /v1/brokers/{id}/chases?state=`

A plain list with a state filter, for looking something up. Not the home screen.

## Carrier upload page

Already built and served at `GET /u/{token}`: one screen, one button, phone-first. It
shows the load number and nothing else about the load, by design.

## Before a broker uses these

The API has one admin key. A real screen needs sign-in and per-broker access first.
