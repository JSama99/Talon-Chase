# Email through Resend

Talon Chase sends carrier requests, filing packets and sign-in links through
[Resend](https://resend.com), and receives carrier replies and bounce reports from it.
Without `RESEND_API_KEY` the service keeps the in-memory stand-in and sends nothing.
If Resend is only half set up, the service refuses to start and says what is missing.

Code: `chase/channels/resend.py` (sending, fetching received mail, signatures) and
`chase/api/resend_hooks.py` (the webhook). Tests: `tests/test_resend.py`.

## How it behaves

| Situation | What happens |
| --- | --- |
| Send accepted | Resend's email id is stored as the message's provider id |
| 400 or 422 from Resend (bad address, invalid request) | Permanent: the message fails and the outbox records why |
| The same idempotency key reused with different content (409) | Permanent. Retrying cannot help |
| 401, 403 (key or domain not set up), 429 (rate or quota), 5xx, a key still in flight (409), timeout, connection error | Temporary: the outbox retries on its backoff schedule, and when the schedule runs out a person sees the message failed |
| A retry after a send whose response was lost | Every message carries our idempotency key as Resend's `Idempotency-Key` header. Resend answers a retry with the first answer, so nothing is sent twice. Resend remembers a key for 24 hours; the default retry schedule ends after about five and a half, and a test holds it under 24. |
| Reply arrives | The webhook holds only metadata, so the text and files are fetched. The reply is routed to a broker (below). The text without the quoted request, and the attachments, go to the engine. Small inline images under 20 KB (signature logos) are dropped; inline photos are kept. |
| Fetching the reply fails | The webhook answers 502 and Resend sends it again. Nothing is half-stored. |
| Same reply delivered twice | Stored once: the external id is `resend:<email id>` |
| Reply for no broker | Kept as `inbound.unrouted` evidence on the `system` chain |
| Hard bounce or spam complaint | The address is marked undeliverable at that carrier and never emailed again, including by requests already queued or retrying for other loads (see docs/OUTBOX.md). A contact sync of the same address keeps it suppressed; a new address starts clean. If the chase is still waiting on the carrier and no other dispatcher address is left, the load goes to a person (`SEND_FAILED`). Once a reply is in, a bounce does not interrupt filing. Settings lists the dead addresses. |
| Temporary bounce | Recorded as evidence only |
| Same event delivered twice | Recorded once, by the webhook message id |

**Webhook security.** Resend signs every webhook (Svix: `svix-id`, `svix-timestamp`,
`svix-signature`). The route checks the HMAC-SHA256 over `id.timestamp.body` with the
`whsec_` secret, accepts any one of several signatures (secret rotation), and refuses a
timestamp more than five minutes from now, so a captured webhook cannot be replayed.
Without `RESEND_WEBHOOK_SECRET` the route answers 503. The API key is never sent to the
attachment download host.

## Which address a message goes out as

Resend can only send from domains verified in it, and its free plan has one domain.
So there are two modes, chosen per message by its sender domain:

| Mode | When | From | Replies come back by |
| --- | --- | --- | --- |
| **Own domain** | The broker's chase mailbox is on a domain in `RESEND_VERIFIED_DOMAINS` | As written: `pods@brokerage.com` | The broker forwarding `pods@` to the Resend inbound address, or an MX record (below) |
| **Shared** | Anything else | `<Broker name> via Talon Chase <RESEND_SHARED_FROM>` | A Reply-To of `chase+<broker id>@<RESEND_INBOUND_DOMAIN>`; the `+<broker id>` routes it |

Start every broker in shared mode. Move one to its own domain when they pay and add the
domain (Resend Pro allows 10 domains). A message for neither mode (a domain that is not
verified and no shared sender set up) is refused before anything is sent.

A reply is routed by, in order: the `+hash` of an address on `RESEND_INBOUND_DOMAIN`
(it must be a broker id); then a broker whose chase mailbox is among the recipients,
including the original recipient left in `X-Forwarded-To`, `Delivered-To`,
`X-Original-To` or the Received `for` clause when mail was forwarded.

## Setup, once

1. **API key.** Resend → API Keys → create one with **full access** (reading received
   mail needs more than sending access). Set `RESEND_API_KEY`.
2. **Sending domain.** Resend → Domains → add the domain you send Talon Chase mail from
   (for example `mail.yourdomain.com`), add the DNS records it shows, wait for
   Verified. Set `RESEND_SHARED_FROM=chase@mail.yourdomain.com` and make
   `CHASE_LOGIN_FROM` an address on the same domain. List every verified domain in
   `RESEND_VERIFIED_DOMAINS`.
3. **Receiving domain.** Resend → Emails → Receiving. Either use the generated
   `<something>.resend.app` address (no DNS needed, good for the pilot) or add an MX
   record for a domain you own. Set `RESEND_INBOUND_DOMAIN` to that domain.
4. **Webhook.** Resend → Webhooks → Add: `https://<your-host>/webhooks/resend`. Tick
   `email.received`, `email.bounced` and `email.complained`. Copy the signing secret
   into `RESEND_WEBHOOK_SECRET`.
5. Start the service. It will not start with a missing or malformed secret, no
   verified domain, or a shared sender without an inbound domain.

## Each broker

1. **Shared mode (default).** Nothing to do. Set the broker's `chase_mailbox` to the
   address that should appear to carriers as the broker's, and carriers' replies go to
   `chase+<broker id>@<inbound domain>`.
2. **Own domain (later).** Add the broker's domain in Resend, have them add the DNS
   records, add it to `RESEND_VERIFIED_DOMAINS`, and get replies to Resend: either they
   forward `pods@` to `anything+<broker id>@<inbound domain>`, or they point an MX
   record for a subdomain at Resend and use `pods@<that subdomain>` as the chase mailbox.
3. **Check it end to end.** Add a carrier contact with Resend's test address
   `delivered@resend.dev`; the send should be accepted and shown in Resend's Emails
   list. To test receiving, email `chase+<broker id>@<inbound domain>` from your own
   account with a load number in the subject; it should appear on that load's timeline.

## Limits to know

- **Free plan:** 3,000 emails a month and 100 a day, one domain. Received mail counts
  as email against both. 100 a day is enough for a pilot with a few brokers; watch it.
  When the quota is hit, sends fail with 429 and are retried; past the retry schedule
  a person sees them failed.
- Resend's default API rate limit is two requests a second. Talon Chase sends one at a
  time and retries a 429 after a minute, so a burst is spread out by the outbox.
- Attachments on a sent message: 40 MB in total after base64. Received mail and its
  files are limited to 40 MB in total here too.
- Resend remembers an idempotency key for 24 hours. A broker who sets retry waits that
  add up to more than that could, in a rare case, get a duplicate send; the default
  does not.
- Opens and links are not tracked: these are operational messages.
- Received-mail signed download links expire; the files are fetched immediately.

## Not verified against the live service

The tests run against a stand-in built from Resend's documentation. The first live
check is step 3 of "Each broker". Things to watch for the first time: that the
`email.received` event has the fields the code reads (`email_id`, `from`, `to`,
`attachments`), that the `+hash` survives in the `to` address, and that the attachments
list answers with a `data` array of `download_url`s.

## References

- Send email: https://resend.com/docs/api-reference/emails/send-email
- Idempotency keys: https://resend.com/docs/dashboard/emails/idempotency-keys
- Errors: https://resend.com/docs/api-reference/errors
- Receiving email: https://resend.com/docs/dashboard/receiving/introduction
- Received email and attachments: https://resend.com/docs/api-reference/emails/retrieve-received-email
- Verify webhooks: https://resend.com/docs/webhooks/verify-webhooks-requests
- Event types: https://resend.com/docs/webhooks/event-types
- Usage limits: https://resend.com/docs/knowledge-base/resend-sending-limits
