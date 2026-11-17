# Reading documents

How a file a carrier sends becomes facts the checks can work on. Code:
`chase/reader/` (the reader) and `chase/reading.py` (the queue it is read from).

**Status.** Built and tested against a fake of the API. **Not run against the live
API, and not measured on real PODs.** It is off until `ANTHROPIC_API_KEY` is set;
until then the stand-in hands every real photo or PDF to a person. Read
[What was not verified](#what-was-not-verified) and run the
[evaluation](#measuring-it) before turning it on for a broker.

## What reads, and what decides

A Claude model looks at the file and describes it. It does not decide anything.

| Step | Who | Where |
| --- | --- | --- |
| What kind of file is this, how many pages, make it showable | Code | `chase/reader/files.py` |
| What is on the page | The model | `chase/reader/claude.py` (`SYSTEM`, `SCHEMA`) |
| How sure is that reading | Code, from what the model could not make out | `to_extraction`, `CONFIDENCE` |
| Is the document acceptable | Code | `chase/domain/checks.py` |
| What happens next | Code | `chase/engine.py` |

The model is never told which load the file is for, never shown the file name (the
sender chooses it), never asked whether the document is good, and never asked for a
confidence number. It answers in a fixed shape (structured output, `SCHEMA`), and
`shape_problem` checks that it did.

The page tells the model that the file is evidence, not instructions. If a document
carries text aimed at an automated reader, the model is asked to say so, and that
alone sends the document to a person.

## From a file to facts

**Files** (`files.prepare`). The type comes from the bytes, never the name.

| Sent | Becomes |
| --- | --- |
| JPEG, PNG | Passed through unchanged when upright and no larger than 2576 px |
| Larger photos, HEIC (iPhone), WebP, GIF, BMP | An upright JPEG, long edge at most 2576 px |
| A photo the phone stored sideways | Turned upright first |
| Multi-page TIFF (fax) | One JPEG per page |
| PDF | Sent as it is; pages counted from the file |
| PDF locked with an empty password (scanner software does this) | Unlocked and rewritten |

A file that cannot be shown is answered at once, without a model call, with zero
confidence and the reason in plain words: a Word or zip file, a video, a web page, a
password-protected or damaged PDF, a damaged picture, more than 20 pages, a PDF
over 24 MB. Zero confidence means a person looks. The carrier is not asked for a
better copy of a file nothing looked at.

**The model's report.** Document type; legibility (clear, partly legible,
illegible); every reference number with whether each character is certain; whether
the *receiver* signed (present, absent, unsure; the shipper's and driver's
signatures do not count); the delivery date as written and as numbers; every
handwritten or stamped remark, each marked exception, routine or illegible;
problems with the picture; and whether any text is aimed at an automated reader.

**Confidence and doubts** are set by code, from that report:

| The model said | Confidence | Doubt written | What the checks then do |
| --- | --- | --- | --- |
| Clear, nothing in doubt | 95 | none | Decide: pass, or fail with the reason (unsigned, wrong load, not a POD) |
| Illegible | 90, unreadable | none | Ask the carrier for a better copy |
| Partly legible | 75 | what could not be read | A person looks |
| Unsure of the signature, the date or any reference number (including one it could not transcribe at all); part of the page cut off, dark or under glare | 60 | which one | A person looks |
| Handwriting it could not read | 50 | where it is | A person looks: it may be a damage note |
| Paperwork for several shipments in one file; text aimed at the reader | 0 | that | A person looks |

The lowest confidence applies. **Any doubt sends the document to a person,
whatever the broker's confidence floor**: the floor is a setting a broker can lower,
and a doubt about a signature or a handwritten note is a fact about the page, not a
number to trade. The floor still applies on top: at 100, every document goes to a
person. Each doubt is shown on the "Needs you" screen in words: "Not sure about:
whether the receiver signed."

A remark the model marks as an exception (shortage, overage, damage, refusal, a seal
problem, a count that differs, "subject to count") becomes an exception note, and
rule 6 sends the load to a person whatever else is on the page. When the model is
unsure whether a remark is an exception, it is told to say exception.

**Dates.** The delivery date becomes noon UTC of the written day, which is within a
day of any appointment on that local date anywhere in North America. A date written
without a year ("11/14") is taken as the one nearest the day it is read. A date that
is not a real date is a doubt.

**Pages** are counted from the file, never by the model.

## The reading desk

Reading is slow (seconds, sometimes half a minute) and can fail, so it happens
outside the engine, the same way sending does ([OUTBOX.md](OUTBOX.md)).

1. A file arrives. In one transaction the engine keeps it, writes a `Reading`, and
   moves the chase to `RECEIVED`. Nothing more is sent to the carrier from here.
2. The reading desk claims the reading, lets go of the database, and calls the
   reader. Broker screens, webhooks and other uploads do not wait behind it.
3. The desk records the answer. When every file of that message has been read, the
   engine checks them together and the chase moves on.

The desk runs right after a file arrives (behind the response to an upload, to
`POST /v1/inbound` and to the Resend webhook) and again on every tick, so a reading
that failed is retried without anyone asking.

| When | What happens |
| --- | --- |
| The API is overloaded, rate-limited, timing out, or the key or model name is wrong | Tried again after 1, 5, 15 and 60 minutes |
| Still failing after that (about 80 minutes) | The document goes to a person, unread |
| The API refuses this one request for good (400, 413, 422), or the answer is cut off | The document goes to a person at once |
| The model declines to read the file | The document goes to a person |
| The service dies mid-reading | The file is read again after 10 minutes |
| Two readings of one file finish | The first one recorded stands; the other is dropped |

In every one of these the carrier hears nothing: a file nobody read is never called
blurry. Reading is not stopped by the kill switch, which stops what goes out; what
a reading leads to (a re-ask, a filing) is what the switch holds.

A reader that is a pure function of the bytes (the stand-in) sets `inline = True`
and the engine calls it on the spot, through the same records and the same evidence.

Evidence: `document.arrived` when the file is kept, `document.read` with the reader's
name, type and confidence, `reading.retry` and `reading.failed`. The reader's name
(`claude/<model>/<prompt version>`) is on the document and in the chain, so every
reading says which model and which instructions produced it. Change `PROMPT_VERSION`
in `chase/reader/claude.py` whenever `SYSTEM`, `ASK` or `SCHEMA` changes.

## Turning it on

| Setting | What it is |
| --- | --- |
| `ANTHROPIC_API_KEY` | An Anthropic API key. Turns the real reader on. Create one in the Claude Console |
| `CHASE_READER_MODEL` | Optional. Which model reads; default `claude-opus-5-5` |

The service refuses to start on a model without a key, a key that looks like a
Resend secret, or a model id that is not a Claude one. It does not call the API at
start-up: a wrong key shows as readings being retried, with the API's error in the
evidence (`reading.retry`).

**Before the first real broker:**

1. `python -m chase.reader.samples out/samples` then
   `python -m chase.reader.evaluate out/samples`. This is the first live call: it
   shows the key, the API version and the request shape are accepted.
2. Collect real PODs from the pilot broker, label them, and run the evaluation on
   those ([Measuring it](#measuring-it)). The drawn samples say nothing about real ones.
3. For the first weeks, raise the broker's `confidence_floor` to 100 in Settings. No
   reading clears it, so every document comes to a person with the reader's findings
   beside it, and nothing is filed or sent back on the reader's word. Lower it once
   the numbers support it.

**What it costs.** One model call per file: the pages as images or a PDF, a prompt
of about 900 words, and a short answer. Token counts for every reading are logged
(`chase.reader`), with no document content. Work the price out from those and the
model's current rates; it was not measured here.

**Where the document goes.** The file is sent to Anthropic's API and to no one
else. A POD carries names, addresses and signatures. Check Anthropic's data
retention terms for your account before sending a broker's documents, and tell the
broker. That promise is the owner's to make; the code cannot keep it for you.

## Measuring it

```bash
python -m chase.reader.evaluate <folder> [--floor 80] [--json report.json]
```

`<folder>` holds documents and a `labels.json` saying what a careful person sees on
each (the format is at the top of `chase/reader/evaluate.py`). Every document is
routed twice through the real checks, once from the label and once from the
reader's answer, and the routes are compared:

| Line | Meaning |
| --- | --- |
| Filed when it should not have been | **Must be 0.** A missed damage note, a signature that was not there |
| Notes that did not reach a person | **Must be 0.** Counted even when the document was sent back rather than filed |
| Carrier asked again for no reason | An annoyance, and a slower chase |
| Handed to a person, could have been done | Extra work; safe |
| Handled as a careful person would | The rest |

It exits 1 when either of the first two is above zero. Run it before changing the
model, the prompt, the schema or the `CONFIDENCE` table, and keep the JSON report.

A few hundred labelled PODs from the pilot broker, with every damage, shortage and
refusal note they can find, is the set that matters. Label a date written without a
year with the right year, and add `"read_on"` (the day it would have arrived).

`python -m chase.reader.samples <folder>` writes 15 drawn documents with labels:
clean, HEIC, a two-page PDF, a fax TIFF, damage and shortage notes, a routine
remark, unsigned, undated, no year, sideways, blurry, cut off, a lumper receipt,
and a note aimed at the reader. They test the wiring. Drawn text is far easier than
a driver's photo of a crumpled page, so a clean score on them proves nothing about
real paperwork.

## Limits

- **Each file is judged on its own.** A POD sent as two photos is two one-page
  documents: with a load that expects two pages, each fails as "pages missing". One
  PDF, or one photo per one-page POD, works.
- **One message at a time.** A second email that arrives while the first is being
  read is kept and read, and is not checked: it shows on the load for a person.
- **The model can be wrong and say it is sure.** Nothing in the code can catch a
  confident misreading. The evaluation measures how often it happens; the
  `confidence_floor` at 100 removes the risk while that is being measured.
- **No second opinion.** One model reads each file once.
- **English-language paperwork, month/day/year dates.** A day-first date is misread.
- **Files live in the database** and are sent whole on each try.

## What was not verified

- **No call was made to the live Anthropic API.** The request shape, the
  `anthropic-version` value (`2026-10-21`), structured output (`output_config.format`)
  and the model id were taken from the API documentation on 16 November 2026 and
  tested against a fake (`FakeAnthropic` in `tests/test_reader_claude.py`).
- **No real POD was read.** Whether the prompt gets a real driver's photo right,
  especially handwriting and whose signature is whose, is unknown.
- **The confidence table is a judgement,** not a calibration. It has never been
  compared with real error rates.
- **Cost and speed** were not measured.
- **HEIC, TIFF and PDF handling** were tested on files made by Pillow and pypdf, not
  on files from real phones, fax machines and scanners.
