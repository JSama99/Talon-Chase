"""Postmark webhooks: carrier replies into the engine, and bounces.

    POST /webhooks/postmark/inbound   the inbound stream's webhook
    POST /webhooks/postmark/events    bounce and spam-complaint webhook

Postmark does not sign webhooks. Both routes require HTTP Basic credentials, which
Postmark sends when they are written into the webhook URL
(``https://user:password@chase.example/webhooks/postmark/inbound``). Without
configured credentials the routes answer 503, so they can never be open by mistake.

Postmark retries a webhook that does not get a 200. Both routes are safe to repeat:
an email is stored once per Postmark MessageID, and an event once per event ID.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from chase.channels.postmark import (
    MAX_INBOUND_BYTES,
    PERMANENT_BOUNCES,
    addresses,
    credentials_match,
    files,
    message_text,
)
from chase.domain.models import Channel
from chase.engine import ChaseEngine


def mount_postmark(
    app: FastAPI,
    engine: ChaseEngine,
    run: Callable[..., Any],
    *,
    user: str,
    password: str,
) -> None:
    store = engine.store

    async def payload(request: Request) -> dict[str, Any]:
        if not user or not password:
            raise HTTPException(503, "Postmark webhooks are not configured")
        if not credentials_match(request.headers.get("authorization", ""), user, password):
            raise HTTPException(
                401, "credentials required", headers={"WWW-Authenticate": 'Basic realm="postmark"'}
            )
        if int(request.headers.get("content-length", "0") or 0) > MAX_INBOUND_BYTES:
            raise HTTPException(413, "message too large")
        raw = b""
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > MAX_INBOUND_BYTES:
                raise HTTPException(413, "message too large")
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise HTTPException(400, "body is not JSON") from exc
        if not isinstance(data, dict):
            raise HTTPException(400, "body is not a JSON object")
        return data

    @app.post("/webhooks/postmark/inbound")
    async def postmark_inbound(request: Request) -> JSONResponse:
        data = await payload(request)
        message_id = str(data.get("MessageID") or "")
        sender = str((data.get("FromFull") or {}).get("Email") or data.get("From") or "")
        if not message_id or "@" not in sender:
            raise HTTPException(400, "not a Postmark inbound message")
        to = addresses(data)
        broker_id = engine.broker_for_mail(to, str(data.get("MailboxHash") or ""))
        external_id = f"postmark:{message_id}"
        if not broker_id:
            run(engine.record_unrouted, sender.lower(), to, external_id)
            return JSONResponse({"routed": False})
        inbound = run(
            engine.handle_inbound,
            broker_id=broker_id,
            channel=Channel.EMAIL,
            from_addr=sender.strip().lower(),
            subject=str(data.get("Subject") or ""),
            body=message_text(data),
            attachments=files(data),
            external_id=external_id,
        )
        return JSONResponse({"routed": True, "inbound_id": inbound.id, "status": inbound.status})

    @app.post("/webhooks/postmark/events")
    async def postmark_events(request: Request) -> JSONResponse:
        data = await payload(request)
        record_type = str(data.get("RecordType") or "")
        if record_type not in ("Bounce", "SpamComplaint"):
            return JSONResponse({"handled": False, "record_type": record_type})
        event_id = str(data.get("ID") or "")
        email = str(data.get("Email") or "")
        if not event_id or "@" not in email:
            raise HTTPException(400, "not a Postmark bounce event")
        kind = str(data.get("Type") or record_type)
        permanent = kind in PERMANENT_BOUNCES or data.get("Inactive") is True
        detail = str(data.get("Description") or data.get("Name") or kind)[:300]
        seen_key = f"postmark-event:{record_type}:{event_id}"

        def handle() -> bool:
            if store.get_setting(seen_key, ""):
                return False
            store.set_setting(seen_key, "1")
            engine.record_bounce(str(data.get("MessageID") or ""), email, kind, permanent, detail)
            return True

        recorded = run(handle)
        return JSONResponse({"handled": True, "recorded": recorded, "permanent": permanent})
