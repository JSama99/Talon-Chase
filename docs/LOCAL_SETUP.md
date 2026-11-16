# Running Talon Chase locally with Resend

A runbook for one machine: the app, a SQLite file, a public tunnel for Resend's
webhook, and one test load. It needs no Docker and no Postgres. For hosting, see the
README; for how email behaves, see [EMAIL.md](EMAIL.md).

Observed on a real run (macOS, Python 3.14, Resend free plan): sign-in email sent
through Resend and received; chase email sent from the broker's own address; carrier
upload link opened through the tunnel; broker screens served through the tunnel. The
parts of the receiving path that depend on Resend's payloads stay listed under "Not
verified against the live service" in EMAIL.md until each has been checked.

## One-time setup

1. **Resend.** Add the sending domain (here `mail.talonsight.com`), create the DNS
   records Resend lists (DKIM TXT, two CNAMEs, and an MX record for receiving), and
   wait for "Domain verified". Turn on **Enable receiving**. Tracking is not needed:
   the emails are plain text.
2. **Code.**
   ```bash
   git clone https://github.com/JSama99/Talon-Chase.git && cd Talon-Chase
   python3 -m venv .venv && source .venv/bin/activate
   pip install -e ".[dev]"
   cp .env.example .env
   ```
3. **`.env`.** Make each secret with
   `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`.
   ```
   CHASE_SECRET=<generated>
   CHASE_API_KEY=<generated, a different value>
   CHASE_BASE_URL=https://<tunnel-host>
   RESEND_API_KEY=<re_... from Resend>
   RESEND_WEBHOOK_SECRET=<whsec_... from the webhook, step 2 of "Every session">
   RESEND_VERIFIED_DOMAINS=mail.talonsight.com
   RESEND_SHARED_FROM=chase@mail.talonsight.com
   RESEND_INBOUND_DOMAIN=mail.talonsight.com
   CHASE_LOGIN_FROM="Talon Chase <login@mail.talonsight.com>"
   ```
   - Leave `POSTGRES_PASSWORD` empty; only Docker reads it.
   - Quote any value with spaces or `<>`, or `source .env` fails with a parse error.
   - `RESEND_SHARED_FROM` is a full address, not a bare domain.
   - Keep `.env` out of screenshots and chat. It is already in `.gitignore`.

## Every session

1. **Tunnel** (terminal A): `cloudflared tunnel --url http://localhost:8000`
   (`brew install cloudflared`; no account needed). It prints a
   `https://<words>.trycloudflare.com` address that changes on every start.
2. **Webhook.** In Resend, Webhooks, add `https://<tunnel-host>/webhooks/resend` with
   the events `email.received`, `email.bounced` and `email.complained`. Copy its
   `whsec_...` secret into `.env`. When the tunnel address changes, update the
   webhook URL here and `CHASE_BASE_URL` in `.env`.
3. **App** (terminal B). The app reads the environment only at start, so restart it
   after every `.env` change:
   ```bash
   source .venv/bin/activate
   set -a; source .env; set +a
   uvicorn chase.api.main:app --port 8000
   ```
   Skip `alembic`: with no `DATABASE_URL` the app uses `chase.db` and creates its
   tables on start. Alembic is for Postgres.

## Test data

Run once; it is kept in `chase.db`. In a third terminal, with `.env` sourced and
`URL=http://127.0.0.1:8000`, send each with `-H "X-API-Key: $CHASE_API_KEY"` and
`-H 'content-type: application/json'`:

| Call | Body |
| --- | --- |
| `PUT /v1/brokers/brk-1` | `{"id":"brk-1","name":"Test Freight","chase_mailbox":"pods@mail.talonsight.com","rep_email":"<you>"}` |
| `PUT /v1/operators/op-owner` | `{"id":"op-owner","broker_id":"brk-1","name":"<you>","role":"OWNER","email":"<inbox you can read>"}` |
| `PUT /v1/carriers/car-1` | `{"id":"car-1","broker_id":"brk-1","name":"Test Carrier LLC","mc_number":"123456"}` |
| `PUT /v1/contacts/ct-1` | `{"id":"ct-1","carrier_id":"car-1","role":"DISPATCHER","name":"Dana","email":"<an address you can send from>"}` |
| `POST /v1/loads` | `id` `ld-1`, `broker_id`, `carrier_id`, `load_number` `TEST-1001`, `delivery_appt_ms` and `delivered_at_ms` in the past (milliseconds), `contact_ids` `["ct-1"]` |

Building the load body in a short Python script avoids shell quoting mistakes.

## Run the loop

1. Open `https://<tunnel-host>/app/login` (there is no page at `/`), enter the
   operator's email, and follow the link that arrives.
2. `POST /v1/tick` sends what is due. Do it before the broker's quiet hours begin
   (20:00 in the broker's time zone by default) or sends wait until morning.
3. Reply to the email that arrives at the contact's address.
4. The app log should show `POST /webhooks/resend ... 200 OK`, and the reply appears on
   the load page, `/app/b/brk-1/loads/ld-1`.

## Rotating secrets

Replace a secret in `.env`, then restart the app.

| Secret | Effect of rotating |
| --- | --- |
| `CHASE_SECRET` | Every sign-in link, session and carrier upload link already issued stops working. |
| `CHASE_API_KEY` | Scripts and `curl` calls need the new value. |
| `RESEND_API_KEY` | Create the new key in Resend, update `.env`, confirm one send, then delete the old key. |
| `RESEND_WEBHOOK_SECRET` | Delete and re-add the webhook (or regenerate its secret, if Resend offers it) and copy the new value. |

## When something fails

| Symptom | Cause |
| --- | --- |
| `command not found: uvicorn` | The virtualenv is not active: `source .venv/bin/activate`. |
| `.env:N: parse error` | A value with spaces or `<>` is not quoted. |
| `404` at `/` | Expected; use `/app/login`. |
| `401` on `/v1/...` | `.env` was not sourced in that terminal, or holds an old key. |
| `401` on `/webhooks/resend` | The secret differs from Resend's, or the app was not restarted. |
| `502` on `/webhooks/resend` | The webhook arrived but fetching the email from Resend failed; read the log line. |
| No webhook call at all | The tunnel address changed, or the event is not ticked in Resend. |
| Sign-in email never arrives | Check Resend, Logs, and that the operator's address can receive mail. |
