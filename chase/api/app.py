"""HTTP service. Thin: every decision is made by the engine.

Admin endpoints need the X-API-Key header. The upload endpoints are open to
carriers and are protected by the signed, expiring, single-load token in the URL.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import dataclasses
import hmac
import threading
from enum import Enum
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from chase.domain.models import (
    Attempt,
    Broker,
    Carrier,
    Channel,
    Chase,
    Check,
    ConsentSource,
    Contact,
    Document,
    Escalation,
    EscalationResolution,
    Inbound,
    Load,
    Operator,
    OperatorRole,
    OutboxMessage,
    Party,
    PartyRole,
    Requirement,
    Role,
    WriteBack,
)
from chase.domain.tokens import InvalidToken, read_upload_token
from chase.engine import ChaseEngine
from chase.evidence import canonical_json, sha256_hex
from chase.outbox import OutboxDispatcher

MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_INBOUND_BYTES = 25 * 1024 * 1024


def plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    return value


class BrokerIn(BaseModel):
    id: str
    name: str
    chase_mailbox: str
    rep_email: str
    sms_number: str = ""
    adapter: str = "packet_email"


class CarrierIn(BaseModel):
    id: str
    broker_id: str
    name: str
    mc_number: str
    timezone: str = "America/New_York"


class ContactIn(BaseModel):
    id: str
    carrier_id: str
    role: Role
    name: str = ""
    email: str = ""
    phone: str = ""
    sms_consent: bool = False


class LoadIn(BaseModel):
    id: str
    broker_id: str
    carrier_id: str
    load_number: str
    delivery_appt_ms: int
    bol_number: str = ""
    po_number: str = ""
    expected_pages: int = Field(default=1, ge=1, le=50)
    delivered_at_ms: int | None = None
    contact_ids: list[str] = []
    customer_name: str = ""
    consignee_name: str = ""
    consignee_address: str = ""
    pickup_number: str = ""
    rate_cents: int = Field(default=0, ge=0)


class AttachmentIn(BaseModel):
    filename: str
    content_base64: str


class InboundIn(BaseModel):
    broker_id: str
    channel: Channel
    from_addr: str
    subject: str = ""
    body: str = ""
    attachments: list[AttachmentIn] = []
    external_id: str = ""


class AckIn(BaseModel):
    by: str = Field(min_length=1, description="Operator id")
    resolution: EscalationResolution
    note: str = ""


class OperatorIn(BaseModel):
    id: str
    broker_id: str = ""
    name: str = Field(min_length=1)
    role: OperatorRole
    email: str = ""
    active: bool = True


class ConsentIn(BaseModel):
    granted: bool
    source: ConsentSource
    by: str = Field(min_length=1, description="Operator id")
    note: str = ""


class SwitchIn(BaseModel):
    enabled: bool
    by: str = Field(min_length=1)
    broker_id: str | None = None


class ApproveContactIn(BaseModel):
    carrier_id: str
    role: Role
    by: str = Field(min_length=1)
    email: str = ""
    phone: str = ""


UPLOAD_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Send proof of delivery</title>
<style>
body{font:17px/1.5 system-ui,sans-serif;margin:0;padding:24px;max-width:520px;color:#1a1a1a}
h1{font-size:22px;margin:0 0 8px}
label{display:block;border:2px dashed #888;border-radius:10px;padding:28px;text-align:center;
margin:20px 0;cursor:pointer}
input{display:none}
button{font:inherit;width:100%;padding:14px;border:0;border-radius:10px;background:#1a1a1a;
color:#fff}
button:disabled{opacity:.4}
#msg{margin-top:16px;min-height:1.5em}
</style></head><body>
<h1>Send proof of delivery</h1>
<p>Load <strong>__LOAD__</strong>. Take a photo of the signed bill of lading, or choose a
file. All pages, signature and date visible.</p>
<label id="pick">Tap to take a photo or choose a file
<input id="file" type="file" accept="image/*,application/pdf"></label>
<button id="send" disabled>Send</button>
<p id="msg" role="status"></p>
<script>
const file = document.getElementById('file'), send = document.getElementById('send');
const msg = document.getElementById('msg'), pick = document.getElementById('pick');
file.onchange = () => { if (file.files[0]) { pick.firstChild.textContent = file.files[0].name;
  send.disabled = false; } };
send.onclick = async () => {
  const f = file.files[0]; send.disabled = true; msg.textContent = 'Sending...';
  try {
    const r = await fetch(location.pathname + '?filename=' + encodeURIComponent(f.name),
      {method: 'POST', body: f, headers: {'Content-Type': 'application/octet-stream'}});
    msg.textContent = r.ok ? 'Received. Thank you. You can close this page.'
      : 'That did not go through. Please try again or reply to the email.';
    send.disabled = r.ok;
  } catch (e) { msg.textContent = 'No connection. Please try again.'; send.disabled = false; }
};
</script></body></html>"""


def create_app(
    engine: ChaseEngine,
    api_key: str,
    *,
    login_from: str = "Talon Chase <signin@talonchase.invalid>",
    secure_cookies: bool | None = None,
) -> FastAPI:
    if len(api_key) < 16:
        raise ValueError("the API key must be at least 16 characters")
    app = FastAPI(title="Talon Chase", version="0.1.0")
    lock = threading.Lock()
    store = engine.store

    @contextlib.contextmanager
    def locked():
        with lock, store.transaction():
            yield

    # Claims and completions take the lock; the provider call in between does not.
    dispatcher = OutboxDispatcher(engine, guard=locked)

    def admin(x_api_key: str = Header(default="")) -> None:
        if not hmac.compare_digest(x_api_key.encode(), api_key.encode()):
            raise HTTPException(401, "missing or wrong API key")

    def run(fn, *args, **kwargs):
        """One engine call at a time, inside one database transaction."""
        with lock, store.transaction():
            try:
                return fn(*args, **kwargs)
            except KeyError as exc:
                raise HTTPException(404, str(exc).strip("'\"")) from exc
            except PermissionError as exc:
                raise HTTPException(403, str(exc)) from exc
            except ValueError as exc:  # includes OntologyError
                raise HTTPException(422, str(exc)) from exc

    def party_for(broker_id: str, role: PartyRole, name: str, address: str = "") -> Party:
        """One party per business: the id comes from the broker, role and name."""
        key = canonical_json({"broker": broker_id, "name": name.strip().lower(), "role": role})
        party_id = f"party-{sha256_hex(key)[:20]}"
        existing = store.get(Party, party_id)
        return Party(
            id=party_id,
            broker_id=broker_id,
            role=role,
            name=name.strip(),
            address=address.strip() or (existing.address if existing else ""),
        )

    def need(cls: type, obj_id: str):
        obj = store.get(cls, obj_id)
        if obj is None:
            raise HTTPException(404, f"{cls.__name__} {obj_id} not found")
        return obj

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    # ------------------------------------------------------------------- setup

    @app.put("/v1/brokers/{broker_id}", dependencies=[Depends(admin)])
    def put_broker(broker_id: str, body: BrokerIn) -> dict:
        if body.id != broker_id:
            raise HTTPException(422, "id in the path and body differ")

        def save():
            existing = store.get(Broker, broker_id)
            broker = Broker(**body.model_dump())
            if existing is not None:
                broker.config = existing.config
                broker.outbound_enabled = existing.outbound_enabled
            store.put(broker)
            return broker

        return plain(run(save))

    @app.patch("/v1/brokers/{broker_id}/config", dependencies=[Depends(admin)])
    def patch_config(broker_id: str, changes: dict[str, Any]) -> dict:
        need(Broker, broker_id)
        return plain(run(engine.update_broker_config, broker_id, changes))

    @app.put("/v1/carriers/{carrier_id}", dependencies=[Depends(admin)])
    def put_carrier(carrier_id: str, body: CarrierIn) -> dict:
        if body.id != carrier_id:
            raise HTTPException(422, "id in the path and body differ")
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(body.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise HTTPException(422, f"unknown time zone: {body.timezone}") from exc

        def save():
            need(Broker, body.broker_id)
            carrier = Carrier(**body.model_dump())
            store.put(carrier)
            return carrier

        return plain(run(save))

    @app.put("/v1/contacts/{contact_id}", dependencies=[Depends(admin)])
    def put_contact(contact_id: str, body: ContactIn) -> dict:
        if body.id != contact_id:
            raise HTTPException(422, "id in the path and body differ")

        def save():
            need(Carrier, body.carrier_id)
            contact = Contact(**body.model_dump(exclude={"sms_consent"}), verified=True)
            store.put(contact)
            # The broker's system reports consent; a change becomes a consent record.
            # A STOP reply is never undone by a sync: that needs an operator.
            if body.sms_consent != engine.sms_allowed(contact.id):
                has_history = engine.latest_consent(contact.id) is not None
                if body.sms_consent or has_history:
                    with contextlib.suppress(PermissionError):
                        engine.record_consent(contact.id, body.sms_consent, ConsentSource.TMS_SYNC)
            return plain(contact) | {"sms_allowed": engine.sms_allowed(contact.id)}

        return run(save)

    @app.post("/v1/contacts/{contact_id}/consent", dependencies=[Depends(admin)])
    def post_consent(contact_id: str, body: ConsentIn) -> dict:
        def save():
            consent = engine.record_consent(
                contact_id, body.granted, body.source, by=body.by, note=body.note
            )
            return {"recorded": consent is not None, "sms_allowed": engine.sms_allowed(contact_id)}

        return run(save)

    @app.put("/v1/operators/{operator_id}", dependencies=[Depends(admin)])
    def put_operator(operator_id: str, body: OperatorIn) -> dict:
        if body.id != operator_id:
            raise HTTPException(422, "id in the path and body differ")
        return plain(run(engine.save_operator, Operator(**body.model_dump())))

    @app.post("/v1/contacts/approve", dependencies=[Depends(admin)])
    def approve_contact(body: ApproveContactIn) -> dict:
        if not (body.email or body.phone):
            raise HTTPException(422, "give an email or a phone number")
        return plain(
            run(
                engine.approve_contact,
                body.carrier_id,
                body.role,
                by=body.by,
                email=body.email,
                phone=body.phone,
            )
        )

    # ------------------------------------------------------------------ intake

    @app.post("/v1/loads", dependencies=[Depends(admin)])
    def post_load(body: LoadIn) -> dict:
        def intake():
            need(Broker, body.broker_id)
            carrier = need(Carrier, body.carrier_id)
            if carrier.broker_id != body.broker_id:
                raise ValueError("carrier belongs to a different broker")
            existing = store.get(Load, body.id)
            if existing is not None and existing.broker_id != body.broker_id:
                raise ValueError("load id is already used by another broker")
            # The TMS sends names; the ontology keeps them as parties, one per business.
            parties = []
            fields = body.model_dump(
                exclude={"customer_name", "consignee_name", "consignee_address"}
            )
            if body.customer_name.strip():
                parties.append(party_for(body.broker_id, PartyRole.CUSTOMER, body.customer_name))
                fields["customer_party_id"] = parties[-1].id
            if body.consignee_name.strip():
                parties.append(
                    party_for(
                        body.broker_id,
                        PartyRole.CONSIGNEE,
                        body.consignee_name,
                        body.consignee_address,
                    )
                )
                fields["consignee_party_id"] = parties[-1].id
            chase = engine.intake_load(Load(**fields), parties)
            return {"load_id": body.id, "chase": plain(chase)}

        return run(intake)

    @app.post("/v1/inbound", dependencies=[Depends(admin)])
    async def post_inbound(body: InboundIn, request: Request) -> dict:
        if int(request.headers.get("content-length", "0")) > MAX_INBOUND_BYTES * 4 // 3 + 65536:
            raise HTTPException(413, "message too large")
        if body.channel is Channel.UPLOAD:
            raise HTTPException(422, "uploads come through the upload link")
        attachments = []
        try:
            for item in body.attachments:
                attachments.append(
                    (item.filename, base64.b64decode(item.content_base64, validate=True))
                )
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(422, "attachment is not valid base64") from exc
        inbound = run(
            engine.handle_inbound,
            broker_id=body.broker_id,
            channel=body.channel,
            from_addr=body.from_addr,
            subject=body.subject,
            body=body.body,
            attachments=attachments,
            external_id=body.external_id,
        )
        return plain(inbound)

    @app.post("/v1/tick", dependencies=[Depends(admin)])
    def tick() -> dict:
        """Call every minute or two: do what is due, deliver it, finish what that unblocks."""
        steps = run(engine.tick)
        report = dispatcher.dispatch()
        if report.changed:
            steps += run(engine.tick)
        return {"steps": steps, "dispatch": report.to_dict()}

    @app.post("/v1/dispatch", dependencies=[Depends(admin)])
    def dispatch() -> dict:
        """Deliver due outbox messages only."""
        return dispatcher.dispatch().to_dict()

    # ---------------------------------------------------------------- carriers

    def _token_load(token: str) -> Load:
        try:
            load_id = read_upload_token(engine.secret, token, engine.clock.now_ms())
        except InvalidToken as exc:
            raise HTTPException(404, "this link is not valid or has expired") from exc
        return need(Load, load_id)

    @app.get("/u/{token}", response_class=HTMLResponse)
    def upload_page(token: str) -> str:
        load = _token_load(token)
        safe = "".join(c for c in load.load_number if c.isalnum() or c in "-_ ")
        return UPLOAD_PAGE.replace("__LOAD__", safe)

    @app.post("/u/{token}")
    async def upload(token: str, request: Request, filename: str = "upload") -> dict:
        _token_load(token)
        if int(request.headers.get("content-length", "0") or 0) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "file too large")
        content = b""
        async for chunk in request.stream():
            content += chunk
            if len(content) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "file too large")
        if not content:
            raise HTTPException(422, "empty file")
        name = "".join(c for c in filename if c.isalnum() or c in "-_. ")[:120] or "upload"
        try:
            run(engine.handle_upload, token, name, content)
        except InvalidToken as exc:
            raise HTTPException(404, "this link is not valid or has expired") from exc
        return {"received": True}  # nothing about the load or the check goes back to the sender

    # ------------------------------------------------------------------ people

    @app.get("/v1/brokers/{broker_id}/queue", dependencies=[Depends(admin)])
    def queue(broker_id: str) -> dict:
        need(Broker, broker_id)
        return run(engine.queue, broker_id)

    @app.get("/v1/brokers/{broker_id}/summary", dependencies=[Depends(admin)])
    def summary(broker_id: str) -> dict:
        return run(engine.summary, broker_id)

    @app.get("/v1/brokers/{broker_id}/chases", dependencies=[Depends(admin)])
    def chases(broker_id: str, state: str | None = None) -> list[dict]:
        def build():
            need(Broker, broker_id)
            rows = []
            for chase in store.find(Chase, broker_id=broker_id):
                if state and chase.state.value != state:
                    continue
                load = store.get(Load, chase.load_id)
                carrier = store.get(Carrier, load.carrier_id)
                rows.append(
                    {
                        "chase_id": chase.id,
                        "load_id": load.id,
                        "load_number": load.load_number,
                        "carrier": carrier.name if carrier else "",
                        "state": chase.state.value,
                        "attempts_sent": chase.attempts_sent,
                        "next_step": chase.next_step.value,
                        "next_action_at_ms": chase.next_action_at_ms,
                        "opened_at_ms": chase.opened_at_ms,
                        "closed_at_ms": chase.closed_at_ms,
                        "closed_reason": chase.closed_reason,
                    }
                )
            return sorted(rows, key=lambda r: (-r["opened_at_ms"], r["chase_id"]))

        return run(build)

    @app.get("/v1/loads/{load_id}", dependencies=[Depends(admin)])
    def load_detail(load_id: str) -> dict:
        def build():
            load = need(Load, load_id)
            found = store.find(Chase, load_id=load_id)
            chase = found[0] if found else None
            cid = chase.id if chase else ""
            documents = store.find(Document, load_id=load_id)
            return {
                "load": plain(load),
                "carrier": plain(store.get(Carrier, load.carrier_id)),
                "chase": plain(chase),
                "attempts": plain(store.find(Attempt, chase_id=cid)),
                "inbound": plain(store.find(Inbound, chase_id=cid)),
                "documents": plain(documents),
                "checks": plain(store.find(Check, chase_id=cid)),
                "escalations": plain(store.find(Escalation, load_id=load_id)),
                "parties": plain(engine.parties(load)),
                "requirements": plain(store.find(Requirement, load_id=load_id)),
                "writebacks": plain(store.find(WriteBack, chase_id=cid)),
                "messages": plain(
                    sorted(
                        store.find(OutboxMessage, load_id=load_id),
                        key=lambda m: (m.created_at_ms, m.id),
                    )
                ),
            }

        return run(build)

    @app.get("/v1/loads/{load_id}/evidence", dependencies=[Depends(admin)])
    def evidence(load_id: str) -> dict:
        return run(engine.export_evidence, load_id)

    @app.post("/v1/escalations/{escalation_id}/acknowledge", dependencies=[Depends(admin)])
    def acknowledge(escalation_id: str, body: AckIn) -> dict:
        return plain(
            run(
                engine.acknowledge_escalation,
                escalation_id,
                by=body.by,
                resolution=body.resolution,
                note=body.note,
            )
        )

    @app.post("/v1/outbound/switch", dependencies=[Depends(admin)])
    def switch(body: SwitchIn) -> dict:
        if body.broker_id:
            run(engine.set_broker_outbound, body.broker_id, body.enabled, by=body.by)
        else:
            run(engine.set_global_outbound, body.enabled, by=body.by)
        return {"enabled": body.enabled, "scope": body.broker_id or "global"}

    from chase.app.views import mount_broker_app

    mount_broker_app(
        app,
        engine,
        run,
        login_from=login_from,
        secure_cookies=(
            engine.base_url.startswith("https://") if secure_cookies is None else secure_cookies
        ),
    )

    return app
