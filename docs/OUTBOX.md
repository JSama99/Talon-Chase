# Outbox

The engine never calls an email or text provider. When it decides to send, it writes
an `OutboxMessage` in the same database transaction as the decision. A separate
dispatcher delivers those messages afterwards.

The problem this solves: before, a message was sent inside the transaction. If the
commit then failed, the carrier had the email but the database had no record of it,
and the next run sent it again. Now the decision and the message commit together or
not at all. Sending happens only after the commit succeeds.

## The guarantee, stated exactly

| Situation | Result |
| --- | --- |
| The engine's transaction rolls back | No message exists, nothing is sent. The next tick decides again. |
| The provider is down | Retried after 1, 5, 15, 60 and 240 minutes (per-broker setting), then failed and handed to a person. |
| The provider rejects the message for good | Failed at once, handed to a person. |
| The dispatcher dies before calling the provider | The lease expires after 5 minutes; another run sends it. Sent once. |
| The dispatcher dies after the provider accepted, before recording it | The lease expires; the message is sent again **with the same idempotency key and Message-ID**. |
| Two dispatchers run at once | Each message is claimed by one of them. Tested with four on Postgres. |

Delivery is **at least once**. The only duplicate risk is the fourth row, and its
window is the time between the provider's reply and one database commit. A provider
that honours the idempotency key drops the repeat. One that does not delivers a
duplicate, with the same Message-ID, which many mail clients collapse. When a real
provider is chosen, pass `EmailMessage.idempotency_key` to its idempotency feature if
it has one, and confirm what it does with a repeat.

## Lifecycle

```
PENDING ──claim──> SENDING ──provider ok──> SENT
   ^                  │
   │                  ├──temporary error, retries left──> PENDING (later)
   │                  ├──temporary error, none left, or permanent error──> FAILED
   │                  └──lease expired (dispatcher died)──> claimed again
   │
   ├──kill switch off──> stays PENDING, untouched
   ├──carrier quiet hours──> stays PENDING until 07:00 carrier time
   └──chase no longer asking (document arrived, escalated)──> CANCELLED
```

The checks in the bottom half run at claim time, so they use the state at the moment
of sending, not at the moment of deciding:

- **Kill switch.** Queued messages are held, not dropped. They go out when it is
  turned back on, unless one of the other checks stops them.
- **Quiet hours.** If the dispatcher was down and a message queued at 19:55 would now
  go at 20:10, it waits for the morning.
- **Still needed.** A request for paperwork is cancelled if the chase is no longer
  asking: the document came through the upload link, or the load went to a person.
  Packets to the broker are never cancelled this way.

## Filing through the emailed packet

The packet email to the broker also goes through the outbox. The load is marked
ready to invoice only once that email is actually delivered: until then the write-back
is pending and the chase stays `PASSED`. If delivery fails for good, the write-back
fails and the load goes to a person. The dispatcher wakes the chase when the packet
is sent or fails, so the engine finishes on its next tick.

## Running it

`POST /v1/tick` does both jobs: the engine's due steps, then delivery, then whatever
delivery unblocked. Call it every minute or two from a scheduler (Cloud Scheduler,
cron). `POST /v1/dispatch` delivers only.

The provider call holds no lock and no transaction, so a slow provider does not block
the engine. Claims and completions are short transactions that take the same lock as
the engine, so on Postgres several processes can dispatch safely.

## Evidence

Every step is on the load's chain: `attempt.queued` (the decision, with the outbox id),
then `message.sent`, `message.retry`, `message.deferred`, `message.cancelled`,
`message.failed`, `message.reclaimed` or `message.duplicate`. For packets,
`writeback.pending` comes before `message.sent`, which comes before `writeback.succeeded`.

## Limits

- **No maximum age.** A request delayed by a long outage still goes out (outside quiet
  hours) unless its chase stopped asking. The engine's plan carries on meanwhile, so
  after a two-day outage a carrier could get two requests close together.
- **Due messages are found by status, then filtered in Python.** Fine at pilot volume;
  add an index on `(status, next_attempt_at_ms)` and push the time filter into SQL
  before thousands of messages are pending at once.
- **One lease length.** Five minutes. A provider call that takes longer than that
  would let a second dispatcher send the same message; real provider calls should
  time out well under it.
