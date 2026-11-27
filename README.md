# Talon Chase

A back-office agent for small freight brokerages. For every delivered load it asks
the carrier for the signed proof of delivery (POD), follows up on a schedule, checks
the document, files it to the broker's system, and hands the load to a person when
it is stuck or anything looks risky.

**Status: version 1, not yet used by a broker.** Email is real through Resend once
an API key is set ([docs/EMAIL.md](docs/EMAIL.md)). Documents are read by a Claude
model once an Anthropic key is set ([docs/READER.md](docs/READER.md)); that reader
has not read a real POD yet. Texts and the TMS sit behind interfaces with in-memory
stand-ins. See [What is not built](#what-is-not-built).

Changing the code, as a person or a coding agent: read [AGENTS.md](AGENTS.md) first.

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

pytest                          # 398 pass, 4 skipped without Postgres; 408 with it
python -m chase.demo            # five loads, end to end, with evidence exports
python -m synthetic.evaluate    # 200 loads, 60 document fixtures, pass/fail report
python tools/verify_evidence.py out/demo/evidence-TC-300002.json
```

To include the Postgres tests, point them at an empty UTF-8 database:

```bash
pip install -e ".[dev,postgres]"
CHASE_TEST_POSTGRES_URL=postgresql+psycopg://user:pass@localhost:5432/chase_test pytest
```

## How a chase runs

```
load delivered ──> ask carrier ──> file arrives? ──yes──> read ──> check document ──pass──> file to TMS ──> closed
                       ^                │ no                            │ fail (say why)
                       └── wait, retry ─┘<──────────────────────────────┘
                                │ 3 tries, no file            any risk flag, any step
                                └──────────────> hand to a person <────────────┘
```

Default schedule, in the carrier's local time (every interval is per-broker config):

| When | Action |
| --- | --- |
| Load marked delivered, or 2 hours after the appointment with no status | Email the dispatcher: load number, upload link |
| 3 hours later | Text the driver, only with consent on file |
| 08:00 next business day | Second email, and a text to the dispatcher with consent |
| 48 hours after the first ask | Third email, copying the broker's rep |
| 24 hours after that | Stop. The load goes to a person with the carrier's phone numbers and full history |

A file is kept and queued in one step, then read outside the engine by the reading
desk (`chase/reading.py`), and checked once every file of that message has been read.
Nothing is sent to the carrier while a file waits to be read. If the reader does not
answer, it is tried again for about 80 minutes, then the document goes to a person;
the carrier is never told a file nobody read is bad. See [docs/READER.md](docs/READER.md).

Nothing is sent between 20:00 and 07:00 carrier-local. A promise ("will send
tomorrow") pushes the next ask back a day. An out-of-office reply changes nothing.

## Rules the code enforces

Each has a test in `tests/test_acceptance.py`.

1. **Deterministic.** The state machine, schedule and checks are plain code. Time and
   ids are injected. The same inputs produce the same evidence hashes.
2. **A chase closes two ways only:** a passed check plus a successful write-back, or
   an escalation resolved by one of the broker's operators, with a recorded resolution.
3. **The agent writes only to contacts on the carrier record.** An address first seen
   in a reply is never used until a person approves it.
4. **Money talk is held.** A message mentioning bank details, remit-to, factoring, wire,
   ACH or a notice of assignment gets no reply and goes to a person, attachment and all.
5. **Outbound carries the load number and the request, nothing else.** A guard refuses
   any message containing a value the ontology marks sensitive: the load's rate, its
   pickup number, or the name or address of its customer or consignee.
6. **Exception notes are never filed clean.** Shortage, damage, refusal or seal notes on
   a POD always go to a person, whatever else is right with the document.
7. **Write-back is idempotent** by (load, document hash, adapter). A lost response is
   retried without attaching twice. An adapter outage falls back to the emailed packet.
8. **Texts need consent and stop on STOP.** Consent is a history of records, each with
   its source. After a STOP only written consent recorded by an operator turns texts back
   on; a contact re-sync cannot.
9. **Kill switches**, global and per broker, stop every outbound message and write-back.
   Messages already queued are held, not dropped.
10. **Sending is separate from deciding, and reading from keeping.** The engine writes
    each message to an outbox in the same transaction as its decision; a dispatcher
    delivers it afterwards, at least once, with a stable idempotency key. Each file is
    kept and queued the same way and read afterwards by the reading desk. See
    [docs/OUTBOX.md](docs/OUTBOX.md) and [docs/READER.md](docs/READER.md).
11. **Every step is evidence.** Each send, reply, document, check, state change and
    write-back appends to a per-load hash chain. See [docs/EVIDENCE.md](docs/EVIDENCE.md).
12. **Records obey the ontology.** Every record and evidence payload is checked against a
    closed schema when it is saved, and 22 cross-record rules (TC-01 to TC-22) hold after
    every test and in the synthetic run. See [docs/ONTOLOGY.md](docs/ONTOLOGY.md).
13. **The model describes; code decides.** The document reader is never told the load,
    never shown the file name, never asked whether a document is good or how sure it
    is. Confidence comes from what it could not make out, and any doubt about the
    signature, the date, a reference number or handwriting sends the document to a
    person, whatever the broker's confidence floor. A file nobody read is never sent
    back to the carrier.

## Layout

```
chase/
  domain/      models, state machine, schedule, risk rules, document checks,
               outbound templates and guard, upload tokens (pure; no I/O)
  evidence/    canonical JSON, SHA-256 hash chain, verifier
  ontology/    schema, evidence payloads, cross-record rules, artifact builder
  engine.py    the orchestration: intake, tick, inbound, checks, write-back, escalation
  outbox.py    the outbox: enqueue, and the dispatcher that delivers, retries, gives up
  reading.py   the reading desk: reads queued files outside the engine, retries, gives up
  ports.py     interfaces for email, text, classifier, reader, TMS adapter, store
  channels/    Resend email (send, inbound, bounces); in-memory email and text stand-ins
  reader/      Claude document reader (claude.py), file preparation (files.py), reader
               evaluation and drawn samples; rule-based reply classifier; stand-in reader
  writeback/   FakeApiAdapter (TMS API stand-in), PacketEmailAdapter (works with any TMS)
  store/       MemoryStore, SqlStore (SQLite and Postgres)
  api/         FastAPI service
  app/         broker screens: sign-in, templates, timeline wording, preview
  demo.py      five-load walkthrough
synthetic/     seeded generator (200 loads, 60 fixtures) and evaluation
migrations/    Alembic: 0001 initial schema, 0002 outbox, 0003 ontology, 0004 undeliverable
               contacts, 0005 reading desk
ontology/      generated JSON Schema and JSON-LD context
tools/         verify_evidence.py, a standalone verifier with no project imports;
               site.py, which records and builds the landing page
site/          the landing page; see docs/SITE.md
docs/          ONTOLOGY.md (generated), DEPLOY.md, EMAIL.md, EVIDENCE.md, LOCAL_SETUP.md,
               OUTBOX.md, READER.md, SITE.md, UI.md, VALIDATION.md
```

## Service

```bash
cp .env.example .env    # fill in the three secrets
docker compose up --build
```

The Docker files were written but **not run**: the build environment had no Docker
daemon. The same service was exercised through its routes in `tests/test_api.py`, and
the migration was run against Postgres 16 in `tests/test_migrations.py`.

Hosted: `render.yaml` deploys the service on Render, with the tick run inside the
service, against a Supabase Postgres database; see [docs/DEPLOY.md](docs/DEPLOY.md).
Locally with Resend and a tunnel: [docs/LOCAL_SETUP.md](docs/LOCAL_SETUP.md).

Without Docker:

```bash
export CHASE_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
export CHASE_API_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
uvicorn chase.api.main:app      # SQLite file chase.db by default
```

Admin routes need the `X-API-Key` header. Interactive reference at `/docs`.

| Route | Purpose |
| --- | --- |
| `PUT /v1/brokers/{id}`, `/v1/carriers/{id}`, `/v1/contacts/{id}` | Sync records from the TMS |
| `PATCH /v1/brokers/{id}/config` | Change schedule and policy settings |
| `POST /v1/loads` | Add or update a load; opens a chase once it is delivered |
| `POST /v1/inbound` | A carrier's email or text, attachments as base64; safe to repeat with the same `external_id` |
| `POST /v1/tick` | Do what is due, deliver queued messages, read queued files, finish what that unblocks; call it every minute or two |
| `POST /v1/dispatch` | Deliver queued messages only |
| `POST /v1/read` | Read queued files only; what a reading leads to happens on the next tick |
| `GET /u/{token}`, `POST /u/{token}` | Carrier upload page and upload; no key, the token is the permission |
| `GET /v1/brokers/{id}/queue` | What needs a person: open escalations and unmatched messages |
| `GET /v1/brokers/{id}/summary`, `/chases` | Counts and the list of chases |
| `GET /v1/loads/{id}` | One load with its attempts, replies, documents, checks, escalations |
| `GET /v1/loads/{id}/evidence` | The evidence export for that load |
| `PUT /v1/operators/{id}` | Add or change one of the broker's people (`OWNER`, `STAFF`), or a TalonSight `PLATFORM` operator |
| `POST /v1/escalations/{id}/acknowledge` | An operator resolves an escalation: `HANDLED_BY_HAND`, `WAIVED`, `DISPUTED`, or `OTHER` with a note |
| `POST /v1/contacts/approve` | An operator approves a new contact |
| `POST /v1/contacts/{id}/consent` | An operator records a grant or withdrawal of text consent, with its source |
| `POST /webhooks/resend` | Resend replies and bounces; Svix-signed, see [docs/EMAIL.md](docs/EMAIL.md) |
| `POST /v1/outbound/switch` | Kill switch: global needs a `PLATFORM` operator; per broker, one of that broker's |

Every `by` is an operator id. An unknown, inactive or other-broker operator gets 403.

## Broker screens

Served at `/app` by the same service. Three screens, designed in Paper and built to
match: **Needs you** (what a person must decide, oldest first, with tap-to-call),
**Load** (what happened, in plain words, with the resolve controls and the evidence
record) and **Settings** (the off switch, the schedule, people, and who may be
texted). Plus **All chases** for looking something up. See [docs/UI.md](docs/UI.md).

- **Sign-in** is an emailed link, tied to an operator record: no passwords. A link
  works once, for 15 minutes, and asks for a button press so mail scanners cannot
  spend it. The session is a signed cookie (12 hours, `HttpOnly`, `SameSite=Lax`,
  `Secure` over https) and every form carries a CSRF token. A turned-off operator is
  signed out on their next click.
- **Roles.** Anyone at the broker can resolve escalations, approve a new address,
  record consent and **stop** messages. Only an owner can start messages again,
  change settings or manage people. A broker always keeps one active owner.
  TalonSight's platform operators can open any broker.
- **No JavaScript.** Every page is server-rendered and works with scripts off; the
  content security policy forbids them.

First sign-in for a new broker: add the owner, then print them a link.

```bash
curl -X PUT $URL/v1/operators/op-owner -H "X-API-Key: $CHASE_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"id":"op-owner","broker_id":"brk-1","name":"Jermaine","role":"OWNER","email":"j@broker.example"}'
python -m chase.app.link op-owner        # or let them use "Email me a link"
```

`python -m chase.app.preview` renders every screen with a believable morning of
work to `out/screens/`, for review without running the service.

## Synthetic evaluation

`python -m synthetic.evaluate` runs the engine over a seeded pack and exits non-zero
if any requirement fails. Current result:

| Class | Correct | Total |
| --- | --- | --- |
| clean | 20 | 20 |
| unreadable | 8 | 8 |
| unsigned | 8 | 8 |
| wrong load (4 unknown, 4 belonging to another open load) | 8 | 8 |
| exception noted | 8 | 8 |
| carrier invoice | 8 | 8 |

Also required and met: zero exception-noted documents filed, zero sends in quiet
hours, all 200 evidence chains verify, zero sensitive details sent, all 5 money
messages held.

**What this does and does not show.** The fixtures are structured stand-ins that state
what a reader would extract from an image. The evaluation proves the routing rules
are right given a correct reading. It says nothing about how well a real model reads
real PODs. That needs labelled documents from a pilot broker, run through
`python -m chase.reader.evaluate` ([docs/READER.md](docs/READER.md#measuring-it)).

The same run with every file read through the reading desk instead of on the spot
comes out identical (`tests/test_synthetic.py`).

## What is not built

- **Real channels, except email.** Email goes through Resend (see
  [docs/EMAIL.md](docs/EMAIL.md)). There is still no text line or TMS integration;
  each needs its own adapter behind the existing interface.
- **Document storage.** Files are stored in the database by content hash. Move them to
  object storage before real volume.
- **Public holidays.** The schedule knows weekends and quiet hours, not holidays.
- **A document reader measured on real PODs.** A reader that uses a language model
  (Claude) is built and tested against a fake of the API ([docs/READER.md](docs/READER.md)).
  It has not been called live and has not read a real POD, so nobody knows yet how
  often it misreads one. Without `ANTHROPIC_API_KEY` the stand-in reads only the
  synthetic fixtures, and a real photo or PDF goes to a person.
- **A real reply classifier.** The stand-in is keyword rules. Anything it does not
  recognise goes to a person, which is safe but will over-escalate on real mail.
- **External anchoring of evidence.** See the limits section of docs/EVIDENCE.md.

## Next steps

1. Validate with brokers: [docs/VALIDATION.md](docs/VALIDATION.md). The landing page
   that asks for pilots is in `site/`: [docs/SITE.md](docs/SITE.md).
2. Set up Resend for the first broker: [docs/EMAIL.md](docs/EMAIL.md), then deploy:
   [docs/DEPLOY.md](docs/DEPLOY.md).
3. Turn the reader on and measure it: set `ANTHROPIC_API_KEY`, run the drawn samples,
   then label the pilot broker's PODs and run the evaluation
   ([docs/READER.md](docs/READER.md#turning-it-on)).
4. One at a time: text messages (Twilio), first TMS adapter.
