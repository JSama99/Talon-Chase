"""Resend webhook: carrier replies into the engine, and bounces.

    POST /webhooks/resend

One URL takes every event Resend is set to send:

* ``email.received``: a carrier replied. The webhook holds only metadata, so the
  text and files are fetched (``ResendInbox``) and then handed to the engine.
* ``email.bounced`` and ``email.complained``: an address stopped working.
* anything else is acknowledged and ignored.

Every request must carry a valid Svix signature (``svix-id``, ``svix-timestamp``,
``svix-signature``) made with the webhook's signing secret. Without a configured
secret the route answers 503, so it can never be open by mistake.

Resend retries a webhook that does not answer 2xx. The route is safe to repeat: a
reply is stored once per Resend email id, and a bounce once per webhook message id.
If the reply cannot be fetched, the route answers 502 so Resend tries again.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from chase.channels.resend import ResendError, ResendInbox, mailbox_hashes, verify_signature
from chase.domain.models import Channel
from chase.engine import ChaseEngine

MAX_WEBHOOK_BYTES = 1024 * 1024  # events are metadata; the body and files are fetched


def mount_resend(
    app: FastAPI,
    engine: ChaseEngine,
    run: Callable[..., Any],
    *,
    secret: str,
    inbox: ResendInbox | None,
    inbound_domain: str = "",
    after_inbound: Callable[[], None] | None = None,
) -> None:
    store = engine.store

    async def verified(request: Request) -> tuple[str, dict[str, Any]]:
        if not secret:
            raise HTTPException(503, "the Resend webhook is not configured")
        if int(request.headers.get("content-length", "0") or 0) > MAX_WEBHOOK_BYTES:
            raise HTTPException(413, "event too large")
        raw = b""
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > MAX_WEBHOOK_BYTES:
                raise HTTPException(413, "event too large")
        message_id = request.headers.get("svix-id", "")
        if not verify_signature(
            secret,
            message_id,
            request.headers.get("svix-timestamp", ""),
            request.headers.get("svix-signature", ""),
            raw,
            engine.clock.now_ms() / 1000,
        ):
            raise HTTPException(401, "signature missing or wrong")
        try:
            event = json.loads(raw)
        except ValueError as exc:
            raise HTTPException(400, "body is not JSON") from exc
        if not isinstance(event, dict):
            raise HTTPException(400, "body is not a JSON object")
        return message_id, event

    @app.post("/webhooks/resend")
    async def resend_webhook(request: Request) -> JSONResponse:
        message_id, event = await verified(request)
        kind = str(event.get("type") or "")
        data = event.get("data")
        data = data if isinstance(data, dict) else {}
        if kind == "email.received":
            return await received(data)
        if kind in ("email.bounced", "email.complained"):
            return bounced(kind, data, message_id)
        return JSONResponse({"handled": False, "type": kind})

    async def received(data: dict[str, Any]) -> JSONResponse:
        if not data.get("email_id"):
            raise HTTPException(400, "not a Resend received-mail event")
        if inbox is None:
            raise HTTPException(503, "receiving is not configured")
        try:
            mail = await run_in_threadpool(inbox.fetch, data)
        except ResendError as exc:
            raise HTTPException(502, f"could not fetch the email from Resend: {exc}") from exc
        if "@" not in mail.sender:
            raise HTTPException(400, "the email has no sender")
        external_id = f"resend:{mail.email_id}"

        def route() -> str:
            for tag in [*mailbox_hashes(mail.to, inbound_domain), ""]:
                broker_id = engine.broker_for_mail(mail.to, tag)
                if broker_id:
                    return broker_id
            return ""

        broker_id = run(route)
        if not broker_id:
            run(engine.record_unrouted, mail.sender, mail.to, external_id)
            return JSONResponse({"routed": False})
        inbound = run(
            engine.handle_inbound,
            broker_id=broker_id,
            channel=Channel.EMAIL,
            from_addr=mail.sender,
            subject=mail.subject,
            body=mail.body,
            attachments=mail.attachments,
            external_id=external_id,
        )
        return JSONResponse(
            {"routed": True, "inbound_id": inbound.id, "status": inbound.status},
            # Files are read after Resend has its answer: a webhook must return quickly.
            background=BackgroundTask(after_inbound) if after_inbound else None,
        )

    def bounced(kind: str, data: dict[str, Any], message_id: str) -> JSONResponse:
        recipients = [str(a).strip().lower() for a in data.get("to") or [] if "@" in str(a)]
        if not message_id or not recipients:
            raise HTTPException(400, "not a Resend bounce event")
        if kind == "email.complained":
            label, permanent, detail = "Complaint", True, "the recipient marked the email as spam"
        else:
            bounce = data.get("bounce") if isinstance(data.get("bounce"), dict) else {}
            label = (
                "/".join(str(bounce[k]) for k in ("type", "subType") if bounce.get(k)) or "Bounce"
            )
            permanent = str(bounce.get("type")) == "Permanent"
            detail = str(bounce.get("message") or label)[:300]
        email_id = str(data.get("email_id") or "")

        def handle() -> bool:
            fresh = False
            for address in recipients:
                seen_key = f"resend-event:{message_id}:{address}"
                if store.get_setting(seen_key, ""):
                    continue
                store.set_setting(seen_key, "1")
                engine.record_bounce(email_id, address, label, permanent, detail)
                fresh = True
            return fresh

        recorded = run(handle)
        return JSONResponse({"handled": True, "recorded": recorded, "permanent": permanent})
