# Email through Postmark

Talon Chase sends carrier requests, filing packets and sign-in links through
Postmark, and receives carrier replies and bounce reports from it. Without
`POSTMARK_SERVER_TOKEN` the service keeps the in-memory stand-in and sends nothing.

Code: `chase/channels/postmark.py` (sending, parsing) and
`chase/api/postmark_hooks.py` (webhooks). Tests: `tests/test_postmark.py`.

## How it behaves

| Situation | What happens |
| --- | --- |
| Send accepted | Postmark's `MessageID` is stored as the message's provider id |
| 422 from Postmark (bad or inactive address, invalid request) | Permanent: the message fails and the outbox records why |
| 401, 429, 5xx, timeout, connection error | Temporary: the outbox retries on its backoff schedule |
| A retry after a send whose response was lost | Postmark has no idempotency key, so every message carries ours as metadata (`tc_key`). Before a retry, Postmark's outbound search is asked for it. If the first try got through, its id is used and nothing is sent again. |
| Reply arrives | Routed to the broker whose `chase_mailbox` it was sent to, or whose id is the `+hash` of the Postmark inbound address. The reply text (without the quoted request) and attachments go to the engine. Small inline images under 20 KB (signature logos) are dropped; inline photos are kept. |
| Same reply delivered twice | Stored once: the external id is `postmark:<MessageID>` |
| Reply for no broker | Kept as `inbound.unrouted` evidence on the `system` chain |
| Hard bounce, spam complaint, or a bounce that deactivated the address | The address is marked undeliverable at that carrier and never emailed again. If a POD request bounced and no other dispatcher address is left, the load goes to a person (`SEND_FAILED`). Settings lists the dead addresses. |
| Soft or transient bounce | Recorded as evidence only |
| Same event delivered twice | Recorded once, by Postmark's event ID |

Postmark does not sign webhooks. Both webhook routes require HTTP Basic credentials,
which Postmark sends when they are written into the webhook URL. Without
`POSTMARK_WEBHOOK_USER` and `POSTMARK_WEBHOOK_PASSWORD` the routes answer 503.

## Setup, once

1. **Server.** In Postmark, create a server for Talon Chase. Copy its Server API token
   into `POSTMARK_SERVER_TOKEN`. Leave `POSTMARK_MESSAGE_STREAM=outbound` unless you
   made a separate transactional stream.
2. **Webhook credentials.** Generate them:
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
   Set `POSTMARK_WEBHOOK_USER=postmark` and `POSTMARK_WEBHOOK_PASSWORD=<that>`. The
   password must be at least 24 characters.
3. **Sign-in sender.** Verify the domain of `CHASE_LOGIN_FROM` in Postmark (Sender
   Signatures → add domain → add the DKIM and Return-Path DNS records it shows).
4. **Inbound webhook.** Open the server's Inbound stream → Settings → Webhook URL:
   `https://postmark:<password>@<your-host>/webhooks/postmark/inbound`.
   Check that your plan includes inbound processing.
5. **Bounce webhook.** Server → Webhooks → Add webhook for the outbound stream:
   `https://postmark:<password>@<your-host>/webhooks/postmark/events`. Tick **Bounce**
   and **Spam Complaint**.
6. **Optional:** allow only Postmark's published webhook IP ranges at your firewall.
   Postmark says they can change, so keep Basic credentials either way.

## Each broker

Carrier requests go out **from the broker's chase mailbox** (for example
`pods@peachtreelogistics.com`), so carriers reply to it.

1. **Let Postmark send as the broker.** Add the broker's domain in Postmark and have
   the broker add the DKIM and Return-Path records. Until they do, Postmark refuses
   the sends (422) and the outbox records it, so nothing goes out unsigned.
2. **Get replies to Postmark.** Pick one:
   - **Forwarding (no DNS change):** the broker forwards `pods@` to the server's
     inbound address with the broker id as hash, for example
     `a1b2c3+brk-peachtree@inbound.postmarkapp.com`. Routing uses the hash.
   - **Inbound domain:** point an MX record for a subdomain (for example
     `chase.peachtreelogistics.com`) at `inbound.postmarkapp.com`, add the domain to
     the inbound stream, and set the broker's `chase_mailbox` to
     `pods@chase.peachtreelogistics.com`. Routing uses the address.
3. **Check it end to end.** Send an email to the chase mailbox with a load number in
   the subject. It should appear on that load's timeline. Postmark's blackhole
   address (`test@blackhole.postmarkapp.com`) is useful as a carrier contact for a dry
   run: Postmark accepts and drops it, and shows it in Activity.

## Limits to know

- Attachments on a sent message: 10 MB in total. Inbound messages: up to 35 MB.
- At most 50 recipients per message (To and Cc together); Talon Chase sends to a few.
- Opens and links are not tracked: these are operational messages.
- Postmark's outbound search can lag a send by a short time. The outbox's first
  retry comes after a minute or more, which is longer than that lag in practice.
  A message that does go out twice is the same request twice; it never contains
  anything that should not be sent.

## References

- Email API: https://postmarkapp.com/developer/api/email-api
- Messages API (outbound search by metadata): https://postmarkapp.com/developer/api/messages-api
- Inbound webhook: https://postmarkapp.com/developer/webhooks/inbound-webhook
- Bounce webhook: https://postmarkapp.com/developer/webhooks/bounce-webhook
- Webhook security (Basic auth, IP ranges): https://postmarkapp.com/developer/webhooks/webhooks-overview
