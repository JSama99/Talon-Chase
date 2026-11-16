# AGENTS.md: working on Talon Chase

Read this before changing anything. It says what the app is, how it is put together,
which rules the code must keep, and how to make the common kinds of change without
breaking them. `tests/test_docs.py` checks the parts of this file that can drift from
the code (the settings table and the links).

## What it is

A back-office agent for small freight brokerages. When a load is delivered, it asks
the carrier for the signed proof of delivery (POD), follows up on a schedule, checks
the document, files it into the broker's system (TMS), and hands the load to a person
when it is stuck or anything looks risky. Every step is recorded as tamper-evident
evidence.

Status: version 1, not yet used by a broker. Email is real (Resend). Texts, the
document reader, the reply classifier and the TMS connection are interfaces with
in-memory or rule-based stand-ins.

## Commands

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,postgres]"

ruff check . && ruff format --check .      # style; line length 100, Python 3.12+
python -m chase.ontology.build --check     # generated ontology files are current
pytest                                     # all tests
python -m synthetic.evaluate               # 200 loads; exits non-zero on any failure
python -m chase.demo                       # five loads end to end, writes out/demo/
python -m chase.app.preview                # renders every broker screen to out/screens/
uvicorn chase.api.main:app                 # the service; SQLite chase.db by default
```

CI (`.github/workflows/ci.yml`) runs exactly the first five commands against
Postgres 16. A change is done when all five pass.

The Postgres tests run only when `CHASE_TEST_POSTGRES_URL` points at an empty UTF-8
database, for example
`postgresql+psycopg://chase:chase@localhost:5432/chase_test`. Without it they are
skipped, so run them before changing anything in `chase/store/` or `migrations/`.

## Layout

| Path | What it holds |
| --- | --- |
| `chase/domain/` | Pure code, no I/O: models (dataclasses), the state machine, the schedule, risk rules, document checks, outbound templates and the outbound guard, upload tokens |
| `chase/engine.py` | The orchestration: intake, tick, inbound, checks, write-back, escalation, bounces. The only place that changes chase state |
| `chase/outbox.py` | The outbox: the engine enqueues messages; the dispatcher claims, sends, retries and gives up |
| `chase/ports.py` | Interfaces to the outside: email, text, classifier, reader, TMS adapter, store |
| `chase/channels/` | `resend.py` (real email: send, fetch replies, signatures); `fakes.py` (in-memory email and text) |
| `chase/reader/` | Rule-based reply classifier and a stand-in document reader |
| `chase/writeback/` | TMS adapters: an API stand-in, and the emailed packet that works with any TMS |
| `chase/store/` | `MemoryStore` and `SqlStore` (SQLite and Postgres); tables are built from the models |
| `chase/evidence/` | Canonical JSON, the per-load SHA-256 hash chain, the verifier |
| `chase/ontology/` | The schema, evidence payload shapes, 21 cross-record rules (TC-01 to TC-21), and the builder of the published files |
| `chase/api/` | FastAPI: `app.py` (routes), `main.py` (reads settings and wires everything), `resend_hooks.py` (Resend webhook) |
| `chase/app/` | Broker screens: sign-in, server-rendered pages, timeline wording |
| `migrations/` | Alembic; one numbered file per schema change |
| `ontology/` | Generated JSON Schema and JSON-LD context. Never edit by hand |
| `synthetic/` | Seeded generator and evaluation |
| `tools/verify_evidence.py` | A standalone evidence verifier with no project imports |
| `tests/` | `conftest.py` holds `World`, the test harness |

## How a chase runs

```
load delivered -> ask carrier -> file arrives? -yes-> check document -pass-> file to TMS -> closed
                     ^                | no                  | fail (say why)
                     +-- wait, retry -+<--------------------+
                              | 3 tries, no file          any risk flag, any step
                              +--------------> hand to a person <--------+
```

The states and every legal move between them are in `chase/domain/states.py`
(`ALLOWED`). Nothing else may change `chase.state`; the engine calls `_move`.
A chase closes two ways only: a passed check and a successful write-back, or a person
resolving the escalation.

Time moves forward only through `engine.tick()`. In the service, `POST /v1/tick`
runs it, or the service runs it itself every `CHASE_TICK_SECONDS`. Each tick does what
is due, then the dispatcher sends what was queued, then the engine runs once more to
finish what the sends unblocked.

## Rules the code must keep

Each is enforced by tests; breaking one fails CI.

1. **Deterministic.** The clock and ids are injected. The real ones (`SystemClock`,
   `UuidIds`) live in `chase/domain/clock.py` and are wired only in `chase/api/main.py`;
   tests use fixed ones. No `time.time()`, `datetime.now()`, `uuid4()` or randomness
   anywhere else in `chase/domain/` or in `chase/engine.py`. The same inputs produce the same evidence hashes.
2. **State changes only through the state machine**, inside the engine.
3. **Contacts.** The agent writes only to contacts on the carrier record. An address
   first seen in a reply is used only after a person approves it.
4. **Money talk is held.** Bank details, remit-to, factoring, wire, ACH or a notice of
   assignment: no reply, straight to a person.
5. **Outbound carries the load number and the request, nothing else.** The guard in
   `chase/domain/outbound.py` refuses any message containing a value the models mark
   `sensitive=True` (rate, pickup number, customer or consignee name and address).
6. **Exception notes on a POD** (shortage, damage, refusal, seal) always go to a person.
7. **Write-back is idempotent** by (load, document hash, adapter).
8. **Texts need consent and stop on STOP.** After a STOP, only written consent recorded
   by an operator turns texts back on.
9. **Kill switches** (global, per broker) stop all outbound messages and write-backs.
   Queued messages are held, not dropped.
10. **Deciding and sending are separate.** The engine writes each message to the outbox
    in the same transaction as its decision; the dispatcher sends it later with a stable
    idempotency key. Never call a provider from the engine.
11. **Every step is evidence.** Sends, replies, documents, checks, state changes and
    write-backs each append an event to the load's hash chain (`engine._record`).
12. **Records obey the ontology.** Every record and evidence payload is checked against
    a closed schema when saved, and the 21 cross-record rules are checked after every
    test (an autouse fixture in `tests/conftest.py`) and in the synthetic run.

## Making common changes

**A new field on a model.**
1. Add it to the dataclass in `chase/domain/models.py` using the `f(...)` helper, with a
   one-line description. Use `sensitive=True` if it must never reach a carrier.
2. Run `python -m chase.ontology.build` to regenerate `ontology/` and `docs/ONTOLOGY.md`,
   and commit the results.
3. Add a migration, `migrations/versions/000N_<name>.py`, with `down_revision` set to
   the previous number. Use `op.batch_alter_table` so it works on SQLite too.
4. Run the Postgres tests: `test_migrations.py` runs `alembic check`, which fails if
   the migration and the models differ.

**A new evidence kind.** Call `self._record(chain, "noun.verb", payload)`, declare the
payload shape in `chase/ontology/events.py` (`EVENT_PAYLOADS`), describe the kind in
`docs/EVIDENCE.md`, and regenerate the ontology. `test_docs.py` fails if any of these is
missing.

**A new escalation reason.** Add it to `EscalationReason` in `chase/domain/models.py`,
give it a title in `REASON_TITLES` in `chase/app/wording.py`, and describe it in `docs/UI.md`.

**A new cross-record rule.** Add a function decorated with `@rule("TC-22", "<one
sentence>")` in `chase/ontology/constraints.py`, using the next free number. It
receives a `Snapshot` of the store and yields a `Violation` for each breach. Regenerate the ontology.

**A new outside service** (texts, a document reader, a TMS):
1. Implement the matching interface in `chase/ports.py` in a new module under
   `chase/channels/`, `chase/reader/` or `chase/writeback/`.
2. Wire it in `chase/api/main.py`. Read its settings from the environment, refuse to
   start on a half-set-up configuration, and keep the stand-in as the default.
3. Test it with `httpx.MockTransport`; see `FakeResend` in `tests/test_resend.py`. The
   tests never touch the network.
4. Map the provider's errors: refused for good raises `PermanentSendError`; anything
   else is temporary and retried.
5. Write `docs/<SERVICE>.md`, including what was **not** verified against the live
   service.

**A new route.** Admin routes depend on `admin` (the `X-API-Key` header). Engine calls
go through `run(...)` in `chase/api/app.py`, which takes the lock and opens one store
transaction. Any `by` field is an operator id, checked with `engine.operator_for`.

## Tests

- `World` in `tests/conftest.py` builds a whole engine with a fixed clock, a store,
  fake channels and one broker, carrier and contact. Use it instead of wiring things
  by hand: `world.make_load()`, `world.tick()`, `world.reply(...)`,
  `world.send_pod(load)`, `world.run(duration_ms)`.
- Name tests after the behaviour, as a sentence:
  `test_a_bounce_after_the_pod_arrived_does_not_abandon_the_filing`.
- Every bug fix gets a test that fails without the fix.
- `test_acceptance.py` is the rules above. `test_review_findings.py` holds regression
  tests for findings from earlier reviews.

## Settings

The service reads these from the environment (`chase/api/main.py`). It refuses to
start on a missing or malformed required value, and says which one.

| Setting | Required | What it is |
| --- | --- | --- |
| `CHASE_SECRET` | yes | Signs sign-in links, sessions and upload links; 32+ characters |
| `CHASE_API_KEY` | yes | The admin key (`X-API-Key`); 16+ characters |
| `DATABASE_URL` | no | Default `sqlite+pysqlite:///chase.db`. A plain `postgres://` address is opened with psycopg 3 |
| `CHASE_BASE_URL` | no | Public address used in links; falls back to `RENDER_EXTERNAL_URL`, then localhost |
| `RENDER_EXTERNAL_URL` | no | Set by Render itself |
| `CHASE_TICK_SECONDS` | no | Run the tick inside the service every N seconds (0 = off, or 5 to 3600) |
| `CHASE_LOGIN_FROM` | no | Sender of sign-in emails |
| `RESEND_API_KEY` | no | Turns on real email. With it, the next four are required |
| `RESEND_WEBHOOK_SECRET` | with Resend | The webhook's `whsec_...` signing secret |
| `RESEND_VERIFIED_DOMAINS` | with Resend | Domains verified in Resend, comma-separated |
| `RESEND_SHARED_FROM` | with Resend | Sender for brokers without a verified domain |
| `RESEND_INBOUND_DOMAIN` | with Resend | Where replies arrive (`chase+<broker id>@...`) |

## Outside services

- **Resend** (email): sending domain and receiving at `mail.talonsight.com`, both
  verified and tested live. Replies arrive by webhook at `/webhooks/resend`. See
  [docs/EMAIL.md](docs/EMAIL.md).
- **Database:** Postgres 16 is what the migrations are tested against. The chosen
  hosted database is Supabase (Session pooler address).
- **Hosting:** not deployed yet, and the host is open. `render.yaml` and
  [docs/DEPLOY.md](docs/DEPLOY.md) describe Render; whatever runs the service must keep
  it running (or call `POST /v1/tick` every minute) and give it a fixed HTTPS address.
- Locally, with a tunnel for the webhook: [docs/LOCAL_SETUP.md](docs/LOCAL_SETUP.md).

## Writing

Docs and user-facing text are plain and short: what it does, what to do, what was not
verified. A doc never claims something was tested unless a test or a recorded run
shows it. Broker screens use the wording in `chase/app/wording.py`, written for a
dispatcher, not an engineer. More in [docs/UI.md](docs/UI.md).

## Do not

- Put secrets in the repo, in tests, or in docs, including "placeholder" secrets. Docs
  tell people how to generate a value instead.
- Edit `ontology/` or `docs/ONTOLOGY.md` by hand; regenerate them.
- Change an existing migration that is already on `main`; add a new one.
- Add JavaScript to the broker screens; the content security policy forbids it.
- Call a provider (email, text, TMS) from the engine, or anywhere except the dispatcher
  and the adapters.
- Weaken a rule above to make a test pass.

## Not built yet

Texts (Twilio), a real reply classifier and document reader (tested against labelled
PODs), a TMS adapter, object storage for files (they live in the database today),
public holidays in the schedule, and external anchoring of evidence. See the README's
"What is not built" and [docs/VALIDATION.md](docs/VALIDATION.md).
