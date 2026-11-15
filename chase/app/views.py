"""The broker screens: sign-in, Needs you, Load, All chases, Settings.

Server-rendered HTML. Every page works without JavaScript. Every change goes
through the engine, inside the same lock and transaction as the API, and is
recorded against the signed-in operator.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from chase.app import wording
from chase.app.auth import (
    LINK_THROTTLE_MS,
    SESSION_COOKIE,
    SESSION_TTL_MS,
    BadToken,
    Signer,
    make_link_token,
    make_session,
    normalize_email,
    throttle_key,
    used_key,
)
from chase.app.timeline import tell
from chase.domain.models import (
    Attempt,
    Broker,
    Carrier,
    Chase,
    ChaseState,
    Check,
    ConsentSource,
    ConsentStatus,
    Contact,
    Document,
    Escalation,
    EscalationReason,
    EscalationResolution,
    Inbound,
    Load,
    Operator,
    OperatorRole,
    Requirement,
    Role,
)
from chase.engine import ChaseEngine
from chase.evidence import verify_chain
from chase.ports import EmailMessage

HERE = Path(__file__).parent
FLASH_COOKIE = "tc_flash"
_MESSAGE_REF = re.compile(r"\s*\(message (\S+)\)\s*$")
_SAFE_NEXT = re.compile(r"^/app(/[A-Za-z0-9._~\-/]*)?$")
_INLINE_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


class SignInNeeded(Exception):
    def __init__(self, next_path: str) -> None:
        self.next_path = next_path


class Problem(Exception):
    """Show the person a plain explanation instead of a page."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message


def escalation_story(
    engine: ChaseEngine, escalation: Escalation, carrier: str, tz: str
) -> tuple[str, str]:
    """The row's two lines: what is wrong, then the facts a person needs first."""
    store = engine.store
    reason = EscalationReason(escalation.reason)
    detail, inbound_id = message_ref(escalation.detail)
    inbound = store.get(Inbound, inbound_id) if inbound_id else None
    title = wording.REASON_TITLES[reason]
    if reason is EscalationReason.RISK and inbound is not None:
        if detail.startswith("message mentions "):
            terms = detail.removeprefix("message mentions ")
            return (
                "Held: mentions payment details",
                f"{carrier} wrote about {terms}. No reply was sent.",
            )
        return (
            "Held: message from someone not on file",
            f"{inbound.from_addr} is not a contact for {carrier}. No reply was sent.",
        )
    if reason is EscalationReason.UNKNOWN_SENDER and inbound is not None:
        return title, f"{inbound.from_addr} replied. Not on the carrier record, so not used."
    if reason in (EscalationReason.DOCUMENT_EXCEPTION, EscalationReason.LOW_CONFIDENCE):
        documents = sorted(
            store.find(Document, load_id=escalation.load_id), key=lambda d: d.received_at_ms
        )
        seen = documents[-1].extraction if documents else {}
        notes = seen.get("exception_notes") or []
        if reason is EscalationReason.DOCUMENT_EXCEPTION and notes:
            facts = [carrier]
            if seen.get("signature_present") and seen.get("delivery_date_ms"):
                facts.append("Signed and dated")
            return f"POD has a note: {notes[0]}", ". ".join(facts) + ". Not filed."
        if reason is EscalationReason.LOW_CONFIDENCE and seen:
            sure = seen.get("confidence", 0)
            return title, f"{carrier}. The reader was {sure}% sure. Not filed."
    if reason is EscalationReason.NO_RESPONSE:
        chases = store.find(Chase, load_id=escalation.load_id)
        attempts = sorted(
            (a for c in chases for a in store.find(Attempt, chase_id=c.id)),
            key=lambda a: a.queued_at_ms,
        )
        if attempts:
            last = attempts[-1]
            asked = wording.when(last.queued_at_ms, engine.clock.now_ms(), tz)
            copied = ", with you copied" if last.step.value == "THIRD" else ""
            return title, f"{carrier}. Last asked {asked}{copied}."
    sentence = detail[:1].upper() + detail[1:] if detail else ""
    return title, f"{carrier}. {sentence}." if sentence else carrier


def message_ref(detail: str) -> tuple[str, str]:
    """Split "... (message inb-7)" into the readable part and the message id."""
    found = _MESSAGE_REF.search(detail)
    if not found:
        return detail, ""
    return detail[: found.start()], found.group(1)


def mount_broker_app(
    app: FastAPI,
    engine: ChaseEngine,
    run: Callable[..., Any],
    *,
    login_from: str,
    secure_cookies: bool,
) -> None:
    store = engine.store
    signer = Signer(engine.secret)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.globals.update(wording=wording, Role=Role, OperatorRole=OperatorRole)
    templates.env.filters["phone"] = wording.phone
    templates.env.filters["tel"] = wording.tel
    app.mount("/app/static", StaticFiles(directory=str(HERE / "static")), name="app-static")

    # ---------------------------------------------------------------- plumbing

    @app.middleware("http")
    async def screen_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/app"):
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                "script-src 'none'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
            )
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"  # sign-in links stay private
            if not request.url.path.startswith("/app/static"):
                response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(SignInNeeded)
    async def to_sign_in(request: Request, exc: SignInNeeded):
        return RedirectResponse(f"/app/login?next={quote(exc.next_path)}", status_code=303)

    @app.exception_handler(Problem)
    async def problem(request: Request, exc: Problem):
        return templates.TemplateResponse(
            request, "problem.html", {"message": exc.message}, status_code=exc.status
        )

    def now() -> int:
        return engine.clock.now_ms()

    def session_token(request: Request) -> str:
        return request.cookies.get(SESSION_COOKIE, "")

    def signed_in(request: Request) -> Operator:
        token = session_token(request)
        try:
            payload = signer.read("session", token, now())
        except BadToken:
            raise SignInNeeded(request.url.path) from None
        operator = store.get(Operator, payload["op"])
        if operator is None or not operator.active:
            raise SignInNeeded(request.url.path)
        return operator

    def for_broker(request: Request, broker_id: str) -> tuple[Operator, Broker]:
        operator = signed_in(request)
        broker = store.get(Broker, broker_id)
        if broker is None:
            raise Problem(404, "There is no such broker.")
        try:
            engine.operator_for(operator.id, broker_id)
        except PermissionError:
            raise Problem(403, "You do not work for this broker.") from None
        return operator, broker

    async def form(request: Request) -> dict[str, str]:
        data = await request.form()
        fields = {k: str(v) for k, v in data.items()}
        expected = signer.csrf(session_token(request))
        if not session_token(request) or fields.get("csrf") != expected:
            raise Problem(403, "This form has expired. Go back, reload the page and try again.")
        return fields

    def attempt(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return run(fn, *args, **kwargs)
        except HTTPException as exc:
            raise Problem(exc.status_code, str(exc.detail)) from None

    def back(path: str, flash: str = "", error: bool = False) -> RedirectResponse:
        response = RedirectResponse(path, status_code=303)
        if flash:
            response.set_cookie(
                FLASH_COOKIE,
                signer.sign("flash", {"m": flash, "e": error, "exp": now() + 60_000}),
                max_age=60,
                httponly=True,
                samesite="lax",
                secure=secure_cookies,
                path="/app",
            )
        return response

    def render(
        request: Request,
        name: str,
        broker: Broker | None,
        operator: Operator,
        **context: Any,
    ) -> HTMLResponse:
        flash = None
        if FLASH_COOKIE in request.cookies:
            try:
                flash = signer.read("flash", request.cookies[FLASH_COOKIE], now())
            except BadToken:
                flash = None
        needs_you = 0
        if broker is not None:
            needs_you = sum(
                1 for e in store.find(Escalation, broker_id=broker.id) if e.resolution is None
            )
        response = templates.TemplateResponse(
            request,
            name,
            {
                "broker": broker,
                "operator": operator,
                "is_owner": operator.role in (OperatorRole.OWNER, OperatorRole.PLATFORM),
                "is_platform": operator.role is OperatorRole.PLATFORM,
                "needs_you": needs_you,
                "csrf": signer.csrf(session_token(request)),
                "flash": flash,
                "tz": broker.config.timezone if broker else "America/New_York",
                "now": now(),
                **context,
            },
        )
        if flash is not None:
            response.delete_cookie(FLASH_COOKIE, path="/app")
        return response

    def when(at_ms: int | None, broker: Broker) -> str:
        return wording.when(at_ms, now(), broker.config.timezone) if at_ms else ""

    # ----------------------------------------------------------------- sign-in

    @app.get("/app/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/app"):
        return templates.TemplateResponse(request, "login.html", {"next": _safe_next(next)})

    @app.post("/app/login", response_class=HTMLResponse)
    async def send_link(request: Request):
        data = await request.form()
        email = normalize_email(str(data.get("email", "")))
        next_path = _safe_next(str(data.get("next", "/app")))

        def send() -> None:
            matches = [o for o in store.find(Operator) if o.active and o.email and o.email == email]
            if len(matches) != 1:
                return  # same answer either way: no hint whether an address exists
            last = int(store.get_setting(throttle_key(email), "0"))
            if now() - last < LINK_THROTTLE_MS:
                return
            store.set_setting(throttle_key(email), str(now()))
            token = make_link_token(signer, matches[0].id, now())
            link = f"{engine.base_url}/app/login/{token}?next={quote(next_path)}"
            engine.email.send(
                EmailMessage(
                    from_addr=login_from,
                    to=[email],
                    subject="Your Talon Chase sign-in link",
                    body=(
                        f"Hello {matches[0].name},\n\n"
                        f"Open this link to sign in to Talon Chase. It works once, for "
                        f"15 minutes:\n\n{link}\n\n"
                        "If you did not ask for it, ignore this email.\n"
                    ),
                )
            )

        if email:
            attempt(send)
        return templates.TemplateResponse(request, "login_sent.html", {"email": email})

    @app.get("/app/login/{token}", response_class=HTMLResponse)
    def confirm_link(request: Request, token: str, next: str = "/app"):
        try:
            payload = signer.read("link", token, now())
        except BadToken:
            raise Problem(
                400, "This sign-in link has expired or is not valid. Ask for a new one."
            ) from None
        if store.get_setting(used_key(token), ""):
            raise Problem(400, "This sign-in link was already used. Ask for a new one.")
        operator = store.get(Operator, payload["op"])
        return templates.TemplateResponse(
            request,
            "login_confirm.html",
            {"token": token, "next": _safe_next(next), "name": operator.name if operator else ""},
        )

    @app.post("/app/login/{token}")
    async def use_link(request: Request, token: str):
        data = await request.form()
        next_path = _safe_next(str(data.get("next", "/app")))

        def spend() -> Operator:
            try:
                payload = signer.read("link", token, now())
            except BadToken:
                raise Problem(400, "This sign-in link has expired or is not valid.") from None
            if store.get_setting(used_key(token), ""):
                raise Problem(400, "This sign-in link was already used. Ask for a new one.")
            store.set_setting(used_key(token), str(now()))
            operator = store.get(Operator, payload["op"])
            if operator is None or not operator.active:
                raise Problem(403, "This account is not active.")
            engine.record_sign_in(operator.id)
            return operator

        operator = attempt(spend)
        response = RedirectResponse(next_path, status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            make_session(signer, operator.id, now()),
            max_age=SESSION_TTL_MS // 1000,
            httponly=True,
            samesite="lax",
            secure=secure_cookies,
            path="/app",
        )
        return response

    @app.post("/app/logout")
    async def logout(request: Request):
        await form(request)
        response = RedirectResponse("/app/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/app")
        return response

    @app.get("/app")
    def home(request: Request):
        operator = signed_in(request)
        if operator.role is OperatorRole.PLATFORM:
            return RedirectResponse("/app/brokers", status_code=303)
        return RedirectResponse(f"/app/b/{operator.broker_id}", status_code=303)

    @app.get("/app/brokers", response_class=HTMLResponse)
    def brokers(request: Request):
        operator = signed_in(request)
        if operator.role is not OperatorRole.PLATFORM:
            return RedirectResponse(f"/app/b/{operator.broker_id}", status_code=303)
        rows = sorted(store.find(Broker), key=lambda b: b.name.lower())
        return render(request, "brokers.html", None, operator, brokers=rows)

    # --------------------------------------------------------------- needs you

    @app.get("/app/b/{broker_id}", response_class=HTMLResponse)
    def needs_you(request: Request, broker_id: str):
        operator, broker = for_broker(request, broker_id)
        queue = run(engine.queue, broker_id)
        summary = run(engine.summary, broker_id)
        rows = []
        for item in queue["escalations"]:
            reason = EscalationReason(item["reason"])
            title, line = escalation_story(
                engine,
                store.get(Escalation, item["escalation_id"]),
                item["carrier"],
                broker.config.timezone,
            )
            rows.append(
                {
                    **item,
                    "title": title,
                    "summary": line,
                    "urgent": reason in wording.URGENT_REASONS,
                    "waited": wording.waited(now() - item["created_at_ms"]),
                    "phone": item["carrier_phones"][0] if item["carrier_phones"] else "",
                }
            )
        oldest = wording.waited(now() - min(r["created_at_ms"] for r in rows)) if rows else ""
        unmatched = [{**u, "when": when(u["received_at_ms"], broker)} for u in queue["unmatched"]]
        return render(
            request,
            "needs_you.html",
            broker,
            operator,
            page="needs_you",
            rows=rows,
            oldest=oldest,
            summary=summary,
            median=wording.short_duration(summary["median_ms_to_filed"]),
            unmatched=unmatched,
        )

    # -------------------------------------------------------------- all chases

    @app.get("/app/b/{broker_id}/chases", response_class=HTMLResponse)
    def all_chases(request: Request, broker_id: str, state: str = ""):
        operator, broker = for_broker(request, broker_id)
        if state and state not in ChaseState.__members__:
            state = ""
        rows = []
        counts: dict[str, int] = {}
        for chase in store.find(Chase, broker_id=broker_id):
            counts[chase.state.value] = counts.get(chase.state.value, 0) + 1
            if state and chase.state.value != state:
                continue
            load = store.get(Load, chase.load_id)
            carrier = store.get(Carrier, load.carrier_id)
            rows.append(
                {
                    "load": load,
                    "carrier": carrier.name if carrier else "",
                    "state": wording.STATE_WORDS[chase.state],
                    "attempts": chase.attempts_sent,
                    "opened": when(chase.opened_at_ms, broker),
                    "opened_at_ms": chase.opened_at_ms,
                }
            )
        rows.sort(key=lambda r: (-r["opened_at_ms"], r["load"].load_number))
        states = [(s.value, wording.STATE_WORDS[s], counts.get(s.value, 0)) for s in ChaseState]
        return render(
            request,
            "chases.html",
            broker,
            operator,
            page="chases",
            rows=rows,
            states=[s for s in states if s[2]],
            state=state,
            total=sum(counts.values()),
        )

    # -------------------------------------------------------------------- load

    def broker_load(broker_id: str, load_id: str) -> Load:
        load = store.get(Load, load_id)
        if load is None or load.broker_id != broker_id:
            raise Problem(404, "There is no such load.")
        return load

    @app.get("/app/b/{broker_id}/loads/{load_id}", response_class=HTMLResponse)
    def load_page(request: Request, broker_id: str, load_id: str):
        operator, broker = for_broker(request, broker_id)
        load = broker_load(broker_id, load_id)
        carrier = store.get(Carrier, load.carrier_id)
        chases = store.find(Chase, load_id=load.id)
        chase = chases[0] if chases else None
        events = store.evidence(load.id)
        verified = verify_chain(events)
        operators = {o.id: o.name for o in store.find(Operator)}
        load_numbers = {x.id: x.load_number for x in store.find(Load, broker_id=broker_id)}
        filenames = {d.content_sha256: d.filename for d in store.find(Document, load_id=load.id)}
        for message in store.find(Inbound, chase_id=chase.id if chase else ""):
            filenames.update({a.content_sha256: a.filename for a in message.attachments})
        story_context = {
            "operator_names": operators,
            "load_numbers": load_numbers,
            "filenames": filenames,
            "documents": {d.id: d for d in store.find(Document, load_id=load.id)},
        }
        entries = [{"when": when(e.at_ms, broker), "entry": e} for e in tell(events, story_context)]
        escalations = sorted(
            store.find(Escalation, load_id=load.id), key=lambda e: (e.created_at_ms, e.id)
        )
        open_escalation = next((e for e in reversed(escalations) if e.resolution is None), None)
        title_line = ""
        if open_escalation is not None:
            title_line, _ = escalation_story(
                engine, open_escalation, carrier.name, broker.config.timezone
            )
        held = None
        if open_escalation:
            _, inbound_id = message_ref(open_escalation.detail)
            held = store.get(Inbound, inbound_id) if inbound_id else None
        checks = {c.document_id: c for c in store.find(Check, chase_id=chase.id if chase else "")}
        documents = [
            {"doc": d, "check": checks.get(d.id), "when": when(d.received_at_ms, broker)}
            for d in sorted(store.find(Document, load_id=load.id), key=lambda d: d.received_at_ms)
        ]
        contacts = [
            c for c in store.find(Contact, carrier_id=load.carrier_id) if c.verified and c.phone
        ]
        requirement = next(iter(store.find(Requirement, load_id=load.id)), None)
        return render(
            request,
            "load.html",
            broker,
            operator,
            page="needs_you" if open_escalation else "chases",
            load=load,
            carrier=carrier,
            chase=chase,
            state_word=wording.STATE_WORDS[chase.state] if chase else "Not chased yet",
            delivered=when(load.delivered_at_ms, broker),
            entries=entries,
            escalation=open_escalation,
            reason=EscalationReason(open_escalation.reason) if open_escalation else None,
            title_line=title_line,
            past=[e for e in escalations if e.resolution is not None],
            operators=operators,
            held=held,
            documents=documents,
            parties=engine.parties(load),
            phones=sorted({c.phone for c in contacts}),
            requirement=requirement,
            events=len(events),
            verified=verified.ok,
            head=events[-1].hash if events else "",
            resolutions=list(EscalationResolution),
            ready=when(load.ready_to_invoice_at_ms, broker),
        )

    @app.get("/app/b/{broker_id}/loads/{load_id}/evidence.json")
    def load_evidence(request: Request, broker_id: str, load_id: str):
        for_broker(request, broker_id)
        load = broker_load(broker_id, load_id)
        exported = run(engine.export_evidence, load.id)
        name = f"evidence-{_file_safe(load.load_number)}.json"
        return JSONResponse(
            exported, headers={"Content-Disposition": f'attachment; filename="{name}"'}
        )

    def file_response(name: str, sha256: str) -> Response:
        content = store.get_blob(sha256)
        if content is None:
            raise Problem(404, "That file is not stored.")
        suffix = Path(name).suffix.lower()
        media = _INLINE_TYPES.get(suffix, "application/octet-stream")
        disposition = "inline" if suffix in _INLINE_TYPES else "attachment"
        return Response(
            content,
            media_type=media,
            headers={
                "Content-Disposition": f'{disposition}; filename="{_file_safe(name)}"',
                "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self'; "
                "style-src 'unsafe-inline'; object-src 'self'",
            },
        )

    @app.get("/app/b/{broker_id}/documents/{document_id}")
    def document_file(request: Request, broker_id: str, document_id: str):
        for_broker(request, broker_id)
        document = store.get(Document, document_id)
        if document is None:
            raise Problem(404, "There is no such document.")
        broker_load(broker_id, document.load_id)
        return file_response(document.filename, document.content_sha256)

    @app.get("/app/b/{broker_id}/messages/{inbound_id}/files/{index}")
    def message_file(request: Request, broker_id: str, inbound_id: str, index: int):
        for_broker(request, broker_id)
        inbound = store.get(Inbound, inbound_id)
        if inbound is None or inbound.broker_id != broker_id:
            raise Problem(404, "There is no such message.")
        if not 0 <= index < len(inbound.attachments):
            raise Problem(404, "There is no such file.")
        attachment = inbound.attachments[index]
        return file_response(attachment.filename, attachment.content_sha256)

    @app.post("/app/b/{broker_id}/escalations/{escalation_id}/resolve")
    async def resolve(request: Request, broker_id: str, escalation_id: str):
        operator, _ = for_broker(request, broker_id)
        data = await form(request)
        escalation = store.get(Escalation, escalation_id)
        if escalation is None or escalation.broker_id != broker_id:
            raise Problem(404, "There is no such escalation.")
        path = f"/app/b/{broker_id}/loads/{escalation.load_id}"
        resolution = data.get("resolution", "")
        if resolution not in EscalationResolution.__members__:
            return back(path, "Choose what happened.", error=True)
        try:
            attempt(
                engine.acknowledge_escalation,
                escalation_id,
                by=operator.id,
                resolution=resolution,
                note=data.get("note", "").strip(),
            )
        except Problem as exc:
            if exc.status == 422:
                return back(path, exc.message, error=True)
            raise
        load = store.get(Load, escalation.load_id)
        return back(f"/app/b/{broker_id}", f"{load.load_number} is settled.")

    @app.post("/app/b/{broker_id}/messages/{inbound_id}/approve-sender")
    async def approve_sender(request: Request, broker_id: str, inbound_id: str):
        operator, _ = for_broker(request, broker_id)
        data = await form(request)
        inbound = store.get(Inbound, inbound_id)
        if inbound is None or inbound.broker_id != broker_id or not inbound.chase_id:
            raise Problem(404, "There is no such message.")
        chase = store.get(Chase, inbound.chase_id)
        load = store.get(Load, chase.load_id)
        role = data.get("role", "")
        if role not in Role.__members__:
            return back(f"/app/b/{broker_id}/loads/{load.id}", "Choose a role.", error=True)
        is_phone = inbound.channel.value == "SMS"
        attempt(
            engine.approve_contact,
            load.carrier_id,
            Role(role),
            by=operator.id,
            email="" if is_phone else normalize_email(inbound.from_addr),
            phone=inbound.from_addr if is_phone else "",
        )
        return back(
            f"/app/b/{broker_id}/loads/{load.id}",
            f"{inbound.from_addr} is now a contact. Messages from it will be read.",
        )

    # ---------------------------------------------------------------- settings

    @app.get("/app/b/{broker_id}/settings", response_class=HTMLResponse)
    def settings(request: Request, broker_id: str):
        operator, broker = for_broker(request, broker_id)
        carriers = {c.id: c for c in store.find(Carrier, broker_id=broker_id)}
        texting = []
        for contact in store.find(Contact):
            if contact.carrier_id not in carriers or not contact.phone:
                continue
            history = engine.consent_history(contact.id)
            texting.append(
                {
                    "contact": contact,
                    "carrier": carriers[contact.carrier_id].name,
                    "state": _consent_state(engine, history),
                }
            )
        texting.sort(key=lambda r: (r["carrier"].lower(), r["contact"].name.lower()))
        bounced = sorted(
            (
                {
                    "contact": c,
                    "carrier": carriers[c.carrier_id].name,
                    "when": when(c.email_undeliverable_at_ms, broker),
                }
                for c in store.find(Contact)
                if c.carrier_id in carriers and c.email_undeliverable_at_ms is not None
            ),
            key=lambda r: (r["carrier"].lower(), r["contact"].email),
        )
        people = sorted(
            store.find(Operator, broker_id=broker_id),
            key=lambda o: (not o.active, o.role is not OperatorRole.OWNER, o.name.lower()),
        )
        summary = run(engine.summary, broker_id)
        return render(
            request,
            "settings.html",
            broker,
            operator,
            page="settings",
            cfg=broker.config,
            hours=wording.hours,
            global_on=summary["global_outbound_enabled"],
            waiting=summary["messages_waiting"],
            people=people,
            texting=texting,
            bounced=bounced,
            zones=_zones(broker.config.timezone),
        )

    @app.post("/app/b/{broker_id}/outbound")
    async def outbound_switch(request: Request, broker_id: str):
        operator, _ = for_broker(request, broker_id)
        data = await form(request)
        enabled = data.get("enabled") == "1"
        attempt(engine.set_broker_outbound, broker_id, enabled, by=operator.id)
        message = (
            "Messages to carriers are on." if enabled else "Stopped. Nothing goes to carriers."
        )
        return back(f"/app/b/{broker_id}/settings", message)

    @app.post("/app/b/{broker_id}/settings/schedule")
    async def save_schedule(request: Request, broker_id: str):
        operator, broker = for_broker(request, broker_id)
        data = await form(request)
        path = f"/app/b/{broker_id}/settings"
        cfg = broker.config
        # Send only what the person changed. A value set more precisely elsewhere
        # (say 30 minutes through the API) is shown as 0.5 hours and, if left
        # alone, never rewritten.
        changes: dict[str, Any] = {}
        try:
            for name, setting in _HOUR_FIELDS.items():
                if data[name].strip() != wording.hours(getattr(cfg, setting)):
                    changes[setting] = wording.hours_to_ms(data[name])
            for name in _WHOLE_FIELDS:
                if int(data[name]) != getattr(cfg, name):
                    changes[name] = int(data[name])
            if data["timezone"] != cfg.timezone:
                changes["timezone"] = data["timezone"]
        except (KeyError, ValueError):
            return back(path, "Every setting needs a number.", error=True)
        if not changes:
            return back(path, "Nothing changed.")
        try:
            attempt(engine.update_broker_config, broker_id, changes, by=operator.id)
        except Problem as exc:
            if exc.status == 422:
                return back(path, exc.message, error=True)
            raise
        return back(path, "Settings saved.")

    @app.post("/app/b/{broker_id}/people")
    async def save_person(request: Request, broker_id: str):
        operator, _ = for_broker(request, broker_id)
        data = await form(request)
        path = f"/app/b/{broker_id}/settings#people"
        person_id = data.get("id", "")
        existing = store.get(Operator, person_id) if person_id else None
        if existing is not None and existing.broker_id != broker_id:
            raise Problem(404, "There is no such person.")
        if existing is not None and data.get("action") in ("deactivate", "activate"):
            person = dataclasses.replace(existing, active=data["action"] == "activate")
        else:
            role = data.get("role", "STAFF")
            if role not in ("OWNER", "STAFF") or not data.get("name", "").strip():
                return back(path, "A person needs a name and a role.", error=True)
            email = normalize_email(data.get("email", ""))
            if "@" not in email:
                return back(path, "A person needs an email address to sign in.", error=True)
            person = Operator(
                id=existing.id if existing else engine.ids.new("op"),
                broker_id=broker_id,
                name=data["name"].strip(),
                role=OperatorRole(role),
                email=email,
                active=True,
            )
        try:
            attempt(engine.save_operator, person, by=operator.id)
        except Problem as exc:
            if exc.status == 422:
                return back(path, exc.message, error=True)
            raise
        return back(path, f"Saved {person.name}.")

    @app.get("/app/b/{broker_id}/contacts/{contact_id}", response_class=HTMLResponse)
    def contact_page(request: Request, broker_id: str, contact_id: str):
        operator, broker = for_broker(request, broker_id)
        contact = store.get(Contact, contact_id)
        carrier = store.get(Carrier, contact.carrier_id) if contact else None
        if contact is None or carrier is None or carrier.broker_id != broker_id:
            raise Problem(404, "There is no such contact.")
        history = engine.consent_history(contact.id)
        operators = {o.id: o.name for o in store.find(Operator)}
        return render(
            request,
            "contact.html",
            broker,
            operator,
            page="settings",
            contact=contact,
            carrier=carrier,
            state=_consent_state(engine, history),
            history=[
                {
                    "c": c,
                    "when": when(c.recorded_at_ms, broker) if c.recorded_at_ms else "Before",
                    "by": operators.get(c.recorded_by, ""),
                    "source": _SOURCE_WORDS[c.source],
                }
                for c in reversed(history)
            ],
        )

    @app.post("/app/b/{broker_id}/contacts/{contact_id}/consent")
    async def record_consent(request: Request, broker_id: str, contact_id: str):
        operator, _ = for_broker(request, broker_id)
        data = await form(request)
        contact = store.get(Contact, contact_id)
        carrier = store.get(Carrier, contact.carrier_id) if contact else None
        if contact is None or carrier is None or carrier.broker_id != broker_id:
            raise Problem(404, "There is no such contact.")
        path = f"/app/b/{broker_id}/contacts/{contact_id}"
        granted = data.get("granted") == "1"
        note = data.get("note", "").strip()
        if granted and not note:
            return back(path, "Say where the written consent is kept.", error=True)
        attempt(
            engine.record_consent,
            contact_id,
            granted,
            ConsentSource.WRITTEN,
            by=operator.id,
            note=note,
        )
        return back(path, "Texts are on." if granted else "Texts are off.")


_HOUR_FIELDS = {
    "sms_delay_hours": "sms_delay_ms",
    "third_attempt_hours": "third_attempt_ms",
    "escalate_wait_hours": "escalate_wait_ms",
}
_WHOLE_FIELDS = (
    "morning_hour",
    "quiet_start_hour",
    "quiet_end_hour",
    "confidence_floor",
    "max_reasks",
)

_SOURCE_WORDS = {
    ConsentSource.RATE_CONFIRMATION: "Rate confirmation",
    ConsentSource.CARRIER_PACKET: "Carrier packet",
    ConsentSource.WRITTEN: "Written consent",
    ConsentSource.TMS_SYNC: "Your system",
    ConsentSource.STOP_REPLY: "Replied STOP",
}

_US_ZONES = [
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Phoenix",
    "America/Los_Angeles",
    "America/Anchorage",
    "Pacific/Honolulu",
]


def _zones(current: str) -> list[str]:
    return _US_ZONES if current in _US_ZONES else [current, *_US_ZONES]


def _consent_state(engine: ChaseEngine, history: list) -> str:
    if engine.stop_in_force(history):
        return "Replied STOP"
    if history and history[-1].status is ConsentStatus.GRANTED:
        return "Texts on"
    return "Texts off"


def _safe_next(path: str) -> str:
    return path if _SAFE_NEXT.match(path) and "//" not in path else "/app"


def _file_safe(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in "-_. ")[:120] or "file"
