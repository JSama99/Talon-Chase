# Broker screens

Three screens. The broker should open this for two minutes a day, clear a short list,
and leave. It is not a dashboard to manage.

Designs are in Paper: https://app.paper.design/file/01M7E2JX9YN540Z2MN7K42VXAE
**Built** in `chase/app/`, served at `/app`, and checked against those designs with
`python -m chase.app.preview` (renders every screen to `out/screens/`).

| Screen | Route |
| --- | --- |
| Sign in | `GET /app/login`, link `GET`/`POST /app/login/{token}`, `POST /app/logout` |
| Needs you | `GET /app/b/{broker}` |
| Load | `GET /app/b/{broker}/loads/{id}`, files under `/documents/{id}` and `/messages/{id}/files/{n}`, `evidence.json` |
| All chases | `GET /app/b/{broker}/chases?state=` |
| Settings | `GET /app/b/{broker}/settings`; contact consent at `/contacts/{id}` |
| Platform | `GET /app/brokers` (TalonSight operators pick a broker) |

Times are shown in the broker's time zone (`BrokerConfig.timezone`, set on Settings).
What each person may do:

| Action | Staff | Owner | Platform |
| --- | --- | --- | --- |
| Resolve an escalation, approve a new address, record consent | yes | yes | yes |
| Stop messages | yes | yes | yes |
| Start messages again, change settings, manage people | no | yes | yes |

The sections below are the original brief the screens were built from.

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

Action on a row: open the load. Resolving happens there
(`POST /v1/escalations/{id}/acknowledge`): the signed-in operator, one of four
resolutions, and a note.

| Resolution | Button | Note |
| --- | --- | --- |
| `HANDLED_BY_HAND` | Got it myself | optional |
| `WAIVED` | Not needed | optional |
| `DISPUTED` | In dispute | optional |
| `OTHER` | Something else | required |

## 2. Load

**Data:** `GET /v1/loads/{id}`, `GET /v1/loads/{id}/evidence`

- Top: load number, carrier, state, and when it became ready to invoice if it did.
- A timeline, newest last, built from the evidence events: asked, replied, document
  received, check result with reasons, filed, escalated. Group the rapid state changes
  into one line ("Document checked: unsigned, asked again").
- The document itself with its check result beside it.
- If escalated: the reason, the detail, the carrier's phone numbers, and the
  resolve control (above).
- The requirement's status: open, met (with the document), or resolved by a person.
- Parties (customer, consignee) are shown to the broker only; they never appear in
  anything sent to the carrier.
- "Export evidence" downloads the JSON. Show the head hash and whether the chain
  verifies (`verification.ok`).

## 3. Settings

**Data:** `PATCH /v1/brokers/{id}/config`, `POST /v1/outbound/switch`, `POST /v1/contacts/approve`,
`PUT /v1/operators/{id}`, `POST /v1/contacts/{id}/consent`

- The off switch, large and first. Records the signed-in operator; this broker only.
- People: the broker's operators, owner or staff, and whether each is active.
- Text consent per contact: current state and its history (source, when, who). A
  contact who replied STOP shows it, and can be turned back on only by recording
  written consent.
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

Sign-in is built (emailed one-time links, signed sessions, CSRF tokens, per-broker
access), and sign-in emails go through Resend once `RESEND_API_KEY` is set
(see [EMAIL.md](EMAIL.md)). `python -m chase.app.link <operator-id>` still prints a
link for the very first owner, or when email is down.
