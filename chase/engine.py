"""The chase engine: ties the state machine, schedule, checks and write-back together.

Nothing here decides anything with a language model. The classifier and the
reader report what a message or a document says; this code decides what happens.
All time comes from the injected clock and every side effect goes through a port.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Sequence
from dataclasses import replace
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from chase.domain import outbound
from chase.domain.checks import CheckOutcome, Extraction, check_document
from chase.domain.clock import Clock, IdGen
from chase.domain.models import (
    MINUTE_MS,
    Attachment,
    Attempt,
    Broker,
    BrokerConfig,
    Carrier,
    Channel,
    Chase,
    ChaseState,
    Check,
    CheckResult,
    Consent,
    ConsentSource,
    ConsentStatus,
    Contact,
    Document,
    Escalation,
    EscalationReason,
    EscalationResolution,
    Inbound,
    InboundStatus,
    Load,
    Operator,
    OperatorRole,
    OutboxMessage,
    OutboxPurpose,
    OutboxStatus,
    Party,
    PartyRole,
    Requirement,
    RequirementStatus,
    Role,
    Step,
    WriteBack,
    WriteBackStatus,
)
from chase.domain.risk import (
    find_risk_terms,
    is_stop_message,
    normalize_email,
    normalize_phone,
    same_company_domain,
)
from chase.domain.schedule import Plan, build_plan, next_allowed
from chase.domain.states import ACCEPTS_DOCUMENTS, ACTIVE_STATES, can_transition, transition
from chase.domain.tokens import make_upload_token, read_upload_token
from chase.evidence import canonical_json, make_event, sha256_hex, verify_chain
from chase.ontology import ONTOLOGY_VERSION, schema_sha256
from chase.outbox import enqueue
from chase.ports import (
    AdapterError,
    AdapterPending,
    DocumentReader,
    EmailPort,
    ReplyClassifier,
    ReplyLabel,
    SmsPort,
    Store,
    TmsAdapter,
    WriteBackRequest,
)
from chase.writeback.adapters import PacketEmailAdapter

SEND_STEPS = frozenset({Step.FIRST_EMAIL, Step.DRIVER_SMS, Step.SECOND, Step.THIRD, Step.REASK})
WRITEBACK_POLL_MS = 5 * MINUTE_MS  # fallback; the dispatcher wakes the chase sooner
GLOBAL_SWITCH = "outbound_enabled"
SYSTEM_CHAIN = "system"


class ChaseEngine:
    def __init__(
        self,
        *,
        store: Store,
        clock: Clock,
        ids: IdGen,
        email: EmailPort,
        sms: SmsPort,
        classifier: ReplyClassifier,
        reader: DocumentReader,
        adapters: list[TmsAdapter] | None = None,
        secret: bytes,
        base_url: str = "https://chase.invalid",
    ) -> None:
        self.store = store
        self.clock = clock
        self.ids = ids
        self.email = email
        self.sms = sms
        self.classifier = classifier
        self.reader = reader
        self.secret = secret
        self.base_url = base_url.rstrip("/")
        packet = PacketEmailAdapter(store, clock, ids)
        self.adapters: dict[str, TmsAdapter] = {packet.name: packet}
        for adapter in adapters or []:
            self.adapters[adapter.name] = adapter

    # ------------------------------------------------------------------ evidence

    def _record(self, chain_id: str, kind: str, payload: dict[str, Any]) -> None:
        event = make_event(
            chain_id, kind, self.clock.now_ms(), payload, self.store.last_evidence(chain_id)
        )
        self.store.append_evidence(event)

    def _move(self, chase: Chase, new: ChaseState) -> None:
        old, _ = transition(chase, new)
        self._record(chase.load_id, "chase.state", {"chase_id": chase.id, "from": old, "to": new})

    def export_evidence(self, load_id: str) -> dict[str, Any]:
        load = self._need(Load, load_id)
        events = self.store.evidence(load_id)
        result = verify_chain(events)
        return {
            "format": "talon-chase-evidence/1",
            "load_id": load.id,
            "load_number": load.load_number,
            "exported_at_ms": self.clock.now_ms(),
            "ontology": {"version": ONTOLOGY_VERSION, "schema_sha256": schema_sha256()},
            "verification": {
                "ok": result.ok,
                "length": result.length,
                "head_hash": result.head_hash,
                "failed_index": result.failed_index,
                "reason": result.reason,
            },
            "events": [event.to_dict() for event in events],
        }

    # ------------------------------------------------------------------- lookups

    def _need(self, cls: type, obj_id: str) -> Any:
        obj = self.store.get(cls, obj_id)
        if obj is None:
            raise KeyError(f"{cls.__name__} {obj_id} not found")
        return obj

    def _known_contacts(self, load: Load) -> list[Contact]:
        """Verified contacts on the carrier record. A message from one of these is expected."""
        contacts = self.store.find(Contact, carrier_id=load.carrier_id)
        return sorted((c for c in contacts if c.verified), key=lambda c: c.id)

    def _send_contacts(self, load: Load) -> list[Contact]:
        """Who the agent may write to: the load's own contacts when it lists any."""
        contacts = self._known_contacts(load)
        if load.contact_ids:
            contacts = [c for c in contacts if c.id in load.contact_ids]
        return contacts

    def _mailable(self, contact: Contact) -> bool:
        return bool(contact.email) and contact.email_undeliverable_at_ms is None

    def _dispatcher_emails(self, load: Load) -> list[str]:
        return [
            c.email
            for c in self._send_contacts(load)
            if c.role is Role.DISPATCHER and self._mailable(c)
        ]

    def consent_history(self, contact_id: str) -> list[Consent]:
        """Every consent record for the contact, in the order they were recorded."""
        return sorted(self.store.find(Consent, contact_id=contact_id), key=lambda c: c.seq)

    def latest_consent(self, contact_id: str) -> Consent | None:
        """The contact's most recent consent record, or None if there has never been one."""
        history = self.consent_history(contact_id)
        return history[-1] if history else None

    @staticmethod
    def stop_in_force(history: Sequence[Consent]) -> bool:
        """True when the contact replied STOP after the last written grant. Only an
        operator recording new written consent lifts it."""
        stopped = False
        for record in history:
            if record.source is ConsentSource.STOP_REPLY:
                stopped = True
            elif record.status is ConsentStatus.GRANTED and record.source is ConsentSource.WRITTEN:
                stopped = False
        return stopped

    def sms_allowed(self, contact_id: str) -> bool:
        """True when the latest consent record for this contact grants texts."""
        latest = self.latest_consent(contact_id)
        return latest is not None and latest.status is ConsentStatus.GRANTED

    def _can_text(self, contact: Contact) -> bool:
        return bool(contact.phone and contact.verified and self.sms_allowed(contact.id))

    def _operator(
        self,
        operator_id: str,
        broker_id: str,
        platform_only: bool = False,
        owner_only: bool = False,
    ) -> Operator:
        """The active operator ``operator_id``, allowed to act for ``broker_id``.

        Platform operators may act for any broker. ``owner_only`` also refuses staff.
        """
        operator = self.store.get(Operator, operator_id) if operator_id else None
        if operator is None:
            raise PermissionError(f"unknown operator {operator_id!r}")
        if not operator.active:
            raise PermissionError(f"operator {operator_id} is not active")
        if operator.role is OperatorRole.PLATFORM:
            return operator
        if platform_only:
            raise PermissionError("only a platform operator may do this")
        if operator.broker_id != broker_id:
            raise PermissionError(f"operator {operator_id} does not work for broker {broker_id}")
        if owner_only and operator.role is not OperatorRole.OWNER:
            raise PermissionError("only the broker's owner may do this")
        return operator

    def operator_for(self, operator_id: str, broker_id: str) -> Operator:
        """The operator, if they may act for this broker; raises PermissionError otherwise."""
        return self._operator(operator_id, broker_id)

    def _outbound_enabled(self, broker: Broker) -> bool:
        return self.store.get_setting(GLOBAL_SWITCH, "1") == "1" and broker.outbound_enabled

    def _upload_link(self, load: Load, cfg: BrokerConfig) -> str:
        expires = self.clock.now_ms() + cfg.upload_link_ttl_ms
        return f"{self.base_url}/u/{make_upload_token(self.secret, load.id, expires)}"

    # -------------------------------------------------------------------- intake

    def intake_load(
        self, load: Load, parties: tuple[Party, ...] | list[Party] = ()
    ) -> Chase | None:
        """Store or update a load, and the parties it names. Opens a chase once the load
        is marked delivered."""
        for party in parties:
            if party.broker_id != load.broker_id:
                raise ValueError(f"party {party.id} belongs to another broker")
            self.store.put(party)
        for field_name, role in (
            ("customer_party_id", PartyRole.CUSTOMER),
            ("consignee_party_id", PartyRole.CONSIGNEE),
        ):
            party_id = getattr(load, field_name)
            if party_id:
                party = self._need(Party, party_id)
                if party.role is not role or party.broker_id != load.broker_id:
                    raise ValueError(f"{field_name} must name a {role.value} of this broker")
        carrier = self._need(Carrier, load.carrier_id)
        if carrier.broker_id != load.broker_id:
            raise ValueError("carrier belongs to a different broker")
        existing = self.store.get(Load, load.id)
        if existing is not None:
            load = replace(load, ready_to_invoice_at_ms=existing.ready_to_invoice_at_ms)
        self.store.put(load)
        if existing is None:
            self._record(
                load.id,
                "load.received",
                {
                    "load_number": load.load_number,
                    "carrier_id": load.carrier_id,
                    "delivery_appt_ms": load.delivery_appt_ms,
                    "delivered_at_ms": load.delivered_at_ms,
                },
            )
        elif existing.delivered_at_ms is None and load.delivered_at_ms is not None:
            self._record(load.id, "load.delivered", {"delivered_at_ms": load.delivered_at_ms})
        if load.delivered_at_ms is not None:
            return self.open_chase(load.id, trigger="delivered")
        return None

    def open_chase(self, load_id: str, trigger: str) -> Chase:
        existing = self.store.find(Chase, load_id=load_id)
        if existing:
            return existing[0]
        load = self._need(Load, load_id)
        broker = self._need(Broker, load.broker_id)
        carrier = self._need(Carrier, load.carrier_id)
        now = self.clock.now_ms()
        requirement = Requirement(self.ids.new("req"), load.id)
        self.store.put(requirement)
        plan = build_plan(now, carrier.timezone, broker.config)
        chase = Chase(
            id=self.ids.new("chase"),
            load_id=load.id,
            broker_id=broker.id,
            requirement_id=requirement.id,
            opened_at_ms=now,
            next_step=Step.FIRST_EMAIL,
            next_action_at_ms=plan.first_ms,
            plan=plan.to_dict(),
        )
        self.store.put(chase)
        self._record(
            load.id,
            "chase.opened",
            {"chase_id": chase.id, "trigger": trigger, "plan": plan.to_dict()},
        )
        if not self._dispatcher_emails(load):
            self._escalate(
                chase, EscalationReason.NO_CONTACT, "no verified dispatcher email for this load"
            )
        return self._need(Chase, chase.id)

    # ---------------------------------------------------------------------- tick

    def tick(self) -> int:
        """Do everything that is due now. Returns the number of steps taken."""
        now = self.clock.now_ms()
        chases = self.store.find(Chase)
        chased_loads = {c.load_id for c in chases}
        brokers: dict[str, Broker] = {}
        for load in sorted(self.store.find(Load, delivered_at_ms=None), key=lambda x: x.id):
            if load.id in chased_loads:
                continue
            broker = brokers.setdefault(load.broker_id, self._need(Broker, load.broker_id))
            if now >= load.delivery_appt_ms + broker.config.no_status_grace_ms:
                chases.append(self.open_chase(load.id, trigger="no_status_after_appointment"))
        steps = 0
        for chase in sorted(chases, key=lambda c: (c.next_action_at_ms, c.id)):
            steps += self._drain(chase.id)
        return steps

    def _drain(self, chase_id: str) -> int:
        steps = 0
        for _ in range(8):
            chase = self._need(Chase, chase_id)
            if (
                chase.state not in ACTIVE_STATES
                or chase.next_step is Step.NONE
                or chase.next_action_at_ms > self.clock.now_ms()
            ):
                break
            if not self._run_step(chase):
                break
            steps += 1
        return steps

    def _run_step(self, chase: Chase) -> bool:
        """Run the chase's due step. Returns True when something was done."""
        load = self._need(Load, chase.load_id)
        broker = self._need(Broker, chase.broker_id)
        carrier = self._need(Carrier, load.carrier_id)
        cfg, tz, now = broker.config, carrier.timezone, self.clock.now_ms()

        if not self._outbound_enabled(broker):
            if not chase.kill_noted:
                chase.kill_noted = True
                self.store.put(chase)
                self._record(load.id, "outbound.blocked", {"step": chase.next_step})
            return False
        chase.kill_noted = False

        step = chase.next_step
        if step in SEND_STEPS:
            allowed = next_allowed(now, tz, cfg)
            if allowed > now:
                chase.next_action_at_ms = allowed
                self.store.put(chase)
                return False

        plan = Plan.from_dict(chase.plan)
        if step is Step.FIRST_EMAIL:
            if not self._send_email(chase, load, broker, step, self._dispatcher_emails(load), []):
                return False
            if chase.state is ChaseState.OPEN:
                self._move(chase, ChaseState.ASKED)
            chase.attempts_sent = 1
            if plan.sms_ms:
                self._schedule(chase, Step.DRIVER_SMS, max(plan.sms_ms, now + cfg.min_gap_ms))
            else:
                self._schedule(chase, Step.SECOND, max(plan.second_ms, now + cfg.min_gap_ms))
        elif step is Step.DRIVER_SMS:
            drivers = [c for c in self._send_contacts(load) if c.role is Role.DRIVER]
            self._send_texts(chase, load, broker, step, drivers)
            self._schedule(chase, Step.SECOND, max(plan.second_ms, now + cfg.min_gap_ms))
        elif step is Step.SECOND:
            if not self._send_email(chase, load, broker, step, self._dispatcher_emails(load), []):
                return False
            dispatchers = [c for c in self._send_contacts(load) if c.role is Role.DISPATCHER]
            self._send_texts(chase, load, broker, step, dispatchers)
            chase.attempts_sent = 2
            self._schedule(chase, Step.THIRD, max(plan.third_ms, now + cfg.third_min_gap_ms))
        elif step is Step.THIRD:
            to = self._dispatcher_emails(load)
            if not self._send_email(chase, load, broker, step, to, [broker.rep_email]):
                return False
            chase.attempts_sent = 3
            self._schedule(chase, Step.ESCALATE, now + cfg.escalate_wait_ms)
        elif step is Step.ESCALATE:
            self._escalate(
                chase,
                EscalationReason.NO_RESPONSE,
                f"no document after {chase.attempts_sent} attempts",
            )
            return True
        elif step is Step.REASK:
            contact = self.store.get(Contact, chase.reply_to_contact_id)
            to = (
                [contact.email]
                if contact is not None and contact.verified and self._mailable(contact)
                else self._dispatcher_emails(load)
            )
            if not self._send_email(chase, load, broker, step, to, [], chase.fail_reasons):
                return False
            self._move(chase, ChaseState.ASKED)
            chase.reasks_sent += 1
            chase.attempts_sent = max(chase.attempts_sent, 1)
            self._resume_schedule(chase, tz, cfg, now + cfg.reask_wait_ms)
        elif step is Step.WRITEBACK:
            return self._write_back(chase, load, broker)
        self.store.put(chase)
        return True

    @staticmethod
    def _schedule(chase: Chase, step: Step, at_ms: int) -> None:
        chase.next_step = step
        chase.next_action_at_ms = at_ms

    def _resume_schedule(self, chase: Chase, tz: str, cfg: BrokerConfig, not_before: int) -> None:
        """Pick up the plan where it left off, no sooner than ``not_before``."""
        plan = Plan.from_dict(chase.plan)
        if chase.attempts_sent <= 1:
            self._schedule(
                chase, Step.SECOND, next_allowed(max(plan.second_ms, not_before), tz, cfg)
            )
        elif chase.attempts_sent == 2:
            self._schedule(chase, Step.THIRD, next_allowed(max(plan.third_ms, not_before), tz, cfg))
        else:
            self._schedule(chase, Step.ESCALATE, max(plan.escalate_ms, not_before))

    # --------------------------------------------------------------------- sends

    def _send_email(
        self,
        chase: Chase,
        load: Load,
        broker: Broker,
        step: Step,
        to: list[str],
        cc: list[str],
        reasons: list[str] | None = None,
    ) -> bool:
        """Queue one request email. It is delivered by the outbox dispatcher."""
        if not to:
            self._escalate(chase, EscalationReason.NO_CONTACT, "no verified dispatcher email")
            return False
        link = self._upload_link(load, broker.config)
        subject = outbound.subject_for(load, step)
        body = outbound.email_body(broker, load, step, link, reasons or [])
        if not self._guard(chase, load, f"{subject}\n{body}", link):
            return False
        self._queue_request(
            chase, load, broker, step, Channel.EMAIL, broker.chase_mailbox, to, cc, subject, body
        )
        return True

    def _send_texts(
        self, chase: Chase, load: Load, broker: Broker, step: Step, contacts: list[Contact]
    ) -> None:
        eligible = [c for c in contacts if self._can_text(c)]
        if not broker.sms_number or not eligible:
            reason = "no text number configured" if not broker.sms_number else "no consent on file"
            self._record(load.id, "sms.skipped", {"step": step, "reason": reason})
            return
        link = self._upload_link(load, broker.config)
        body = outbound.sms_body(broker, load, link)
        if not self._guard(chase, load, body, link, escalate=False):
            return
        for contact in eligible:
            self._queue_request(
                chase, load, broker, step, Channel.SMS, broker.sms_number, [contact.phone], [], "",
                body,
            )  # fmt: skip

    def _guard(self, chase: Chase, load: Load, text: str, link: str, escalate: bool = True) -> bool:
        # The link is a signed token and the load number is what the message is about;
        # neither is checked, so random characters cannot trip the rate pattern.
        checked = text.replace(link, "").replace(load.load_number, "")
        try:
            outbound.assert_safe(checked, load, self.parties(load))
        except outbound.OutboundBlocked as exc:
            self._record(load.id, "outbound.refused", {"reason": str(exc)})
            if escalate:
                self._escalate(chase, EscalationReason.OUTBOUND_BLOCKED, str(exc))
            return False
        return True

    def parties(self, load: Load) -> list[Party]:
        """The load's customer and consignee, as far as they are known."""
        ids = [load.customer_party_id, load.consignee_party_id]
        return [p for p in (self.store.get(Party, i) for i in ids if i) if p is not None]

    def _queue_request(
        self,
        chase: Chase,
        load: Load,
        broker: Broker,
        step: Step,
        channel: Channel,
        from_addr: str,
        to: list[str],
        cc: list[str],
        subject: str,
        body: str,
    ) -> None:
        """Record the attempt and its outbox message together, in the caller's transaction."""
        now = self.clock.now_ms()
        carrier: Carrier = self._need(Carrier, load.carrier_id)
        attempt_id = self.ids.new("att")
        message = enqueue(
            self.store,
            message_id=self.ids.new("msg"),
            now_ms=now,
            broker_id=broker.id,
            load_id=load.id,
            chase_id=chase.id,
            purpose=OutboxPurpose.CARRIER_REQUEST,
            channel=channel,
            idempotency_key=f"attempt:{attempt_id}",
            from_addr=from_addr,
            to=to,
            cc=cc,
            subject=subject,
            body=body,
            quiet_tz=carrier.timezone,
        )
        attempt = Attempt(
            id=attempt_id,
            chase_id=chase.id,
            step=step,
            channel=channel,
            to=to,
            cc=cc,
            queued_at_ms=now,
            outbox_id=message.id,
        )
        self.store.put(attempt)
        self._record(
            load.id,
            "attempt.queued",
            {
                "attempt_id": attempt.id,
                "outbox_id": message.id,
                "step": step,
                "channel": channel,
                "to": to,
                "cc": cc,
                "subject": subject,
                "body_sha256": sha256_hex(body.encode("utf-8")),
            },
        )

    # ------------------------------------------------------------------- inbound

    def handle_inbound(
        self,
        *,
        broker_id: str,
        channel: Channel,
        from_addr: str,
        subject: str = "",
        body: str = "",
        attachments: list[tuple[str, bytes]] | None = None,
        external_id: str = "",
    ) -> Inbound:
        """Take in one carrier message. Safe to call twice with the same external_id."""
        if external_id:
            seen = self.store.find(Inbound, broker_id=broker_id, external_id=external_id)
            if seen:
                return seen[0]
        self._need(Broker, broker_id)
        inbound = self._new_inbound(
            broker_id, channel, from_addr, subject, body, attachments, external_id
        )

        if channel is Channel.SMS and is_stop_message(body):
            self._stop_texts(inbound)
            return self._finish(inbound, InboundStatus.PROCESSED, classification="STOP")

        chase = self._match(inbound)
        if chase is None:
            self._record(
                f"broker:{broker_id}",
                "inbound.unmatched",
                {"inbound_id": inbound.id, "from": from_addr, "channel": channel},
            )
            return self._finish(inbound, InboundStatus.UNMATCHED)

        inbound.chase_id = chase.id
        load = self._need(Load, chase.load_id)
        self._record_inbound(load, inbound)
        known = self._known_contacts(load)
        sender = self._find_sender(inbound, known)

        risk_terms = find_risk_terms(f"{subject}\n{body}")
        if risk_terms:
            detail = "message mentions " + ", ".join(risk_terms)
            return self._hold(inbound, chase, EscalationReason.RISK, detail)
        if sender is None:
            if channel is Channel.EMAIL and same_company_domain(
                from_addr, [c.email for c in known if c.email]
            ):
                return self._hold(
                    inbound,
                    chase,
                    EscalationReason.UNKNOWN_SENDER,
                    "new address at a known carrier domain; not adopted",
                )
            return self._hold(
                inbound, chase, EscalationReason.RISK, "sender is not a contact for this carrier"
            )

        self._process(inbound, chase, load, sender)
        self._drain(chase.id)
        return self._need(Inbound, inbound.id)

    def handle_upload(self, token: str, filename: str, content: bytes) -> Inbound:
        """A file sent through an upload link. The link itself names the load."""
        load_id = read_upload_token(self.secret, token, self.clock.now_ms())
        load = self._need(Load, load_id)
        chases = self.store.find(Chase, load_id=load.id)
        if not chases:
            raise KeyError(f"no chase for load {load_id}")
        inbound = self._new_inbound(
            load.broker_id, Channel.UPLOAD, "upload-link", "", "", [(filename, content)], ""
        )
        inbound.chase_id = chases[0].id
        inbound.trusted = True
        self._record_inbound(load, inbound)
        self._process(inbound, chases[0], load, None)
        self._drain(chases[0].id)
        return self._need(Inbound, inbound.id)

    def _new_inbound(
        self,
        broker_id: str,
        channel: Channel,
        from_addr: str,
        subject: str,
        body: str,
        attachments: list[tuple[str, bytes]] | None,
        external_id: str,
    ) -> Inbound:
        stored: list[Attachment] = []
        for filename, content in attachments or []:
            digest = sha256_hex(content)
            self.store.put_blob(digest, content)
            stored.append(Attachment(filename, digest))
        inbound = Inbound(
            id=self.ids.new("inb"),
            broker_id=broker_id,
            channel=channel,
            from_addr=from_addr,
            received_at_ms=self.clock.now_ms(),
            subject=subject,
            body=body,
            attachments=stored,
            external_id=external_id,
        )
        self.store.put(inbound)
        return inbound

    def _record_inbound(self, load: Load, inbound: Inbound) -> None:
        self._record(
            load.id,
            "inbound.received",
            {
                "inbound_id": inbound.id,
                "channel": inbound.channel,
                "from": inbound.from_addr,
                "subject": inbound.subject,
                "body_sha256": sha256_hex(inbound.body.encode("utf-8")),
                "attachments": [a.content_sha256 for a in inbound.attachments],
            },
        )

    def _finish(self, inbound: Inbound, status: InboundStatus, classification: str = "") -> Inbound:
        inbound.status = status
        if classification:
            inbound.classification = classification
        self.store.put(inbound)
        return inbound

    def _hold(
        self, inbound: Inbound, chase: Chase, reason: EscalationReason, detail: str
    ) -> Inbound:
        """Keep the message, tell a person, and say nothing to the sender."""
        inbound.hold_reason = reason
        self._finish(inbound, InboundStatus.HELD)
        self._record(
            chase.load_id,
            "inbound.held",
            {"inbound_id": inbound.id, "reason": reason, "detail": detail},
        )
        self._escalate(self._need(Chase, chase.id), reason, f"{detail} (message {inbound.id})")
        return inbound

    def _stop_texts(self, inbound: Inbound) -> None:
        phone = normalize_phone(inbound.from_addr)
        carriers = {c.id for c in self.store.find(Carrier, broker_id=inbound.broker_id)}
        for contact in sorted(self.store.find(Contact), key=lambda c: c.id):
            if contact.carrier_id in carriers and normalize_phone(contact.phone) == phone:
                self.record_consent(
                    contact.id, False, ConsentSource.STOP_REPLY, inbound_id=inbound.id
                )

    @staticmethod
    def _find_sender(inbound: Inbound, known: list[Contact]) -> Contact | None:
        if inbound.channel is Channel.SMS:
            phone = normalize_phone(inbound.from_addr)
            return next((c for c in known if c.phone and normalize_phone(c.phone) == phone), None)
        address = normalize_email(inbound.from_addr)
        return next((c for c in known if c.email and normalize_email(c.email) == address), None)

    def _match(self, inbound: Inbound) -> Chase | None:
        """Find the chase a message belongs to, or None when that is not certain."""
        chases = self.store.find(Chase, broker_id=inbound.broker_id)
        loads = {c.id: self._need(Load, c.load_id) for c in chases}
        text = f"{inbound.subject} {inbound.body}".upper()
        named = [
            c
            for c in chases
            if re.search(
                rf"(?<![A-Z0-9]){re.escape(loads[c.id].load_number.upper())}(?![A-Z0-9])", text
            )
        ]
        if named:
            return named[0] if len(named) == 1 else None

        if inbound.channel is Channel.SMS:
            phone = normalize_phone(inbound.from_addr)
            texted = [
                a
                for a in self.store.find(Attempt, channel=Channel.SMS)
                if any(normalize_phone(n) == phone for n in a.to)
            ]
            by_id = {c.id: c for c in chases}
            texted = [a for a in texted if a.chase_id in by_id]
            if texted:
                latest = max(texted, key=lambda a: (a.queued_at_ms, a.id))
                return by_id[latest.chase_id]
            return None

        address = normalize_email(inbound.from_addr)
        carriers = {
            c.carrier_id
            for c in self.store.find(Contact)
            if c.verified and c.email and normalize_email(c.email) == address
        }
        waiting = [
            c for c in chases if loads[c.id].carrier_id in carriers and c.state in ACCEPTS_DOCUMENTS
        ]
        return waiting[0] if len(waiting) == 1 else None

    def _process(self, inbound: Inbound, chase: Chase, load: Load, sender: Contact | None) -> None:
        broker = self._need(Broker, chase.broker_id)
        carrier = self._need(Carrier, load.carrier_id)
        cfg, now = broker.config, self.clock.now_ms()

        if chase.state not in ACCEPTS_DOCUMENTS:
            # The chase is finished or with a person. Keep what arrived; change nothing.
            for attachment in inbound.attachments:
                self._store_document(load, inbound, attachment)
            self._record(
                load.id, "inbound.late", {"inbound_id": inbound.id, "chase_state": chase.state}
            )
            self._finish(inbound, InboundStatus.PROCESSED, classification="LATE")
            return

        verdict = self.classifier.classify(inbound)
        label = verdict.label
        if label != ReplyLabel.HAS_DOCUMENT and verdict.confidence < cfg.confidence_floor:
            label = ReplyLabel.UNKNOWN
        if label == ReplyLabel.HAS_DOCUMENT and not inbound.attachments:
            label = ReplyLabel.UNKNOWN
        self._record(
            load.id,
            "inbound.classified",
            {"inbound_id": inbound.id, "label": label, "confidence": verdict.confidence},
        )
        self._finish(inbound, InboundStatus.PROCESSED, classification=label)

        if label == ReplyLabel.HAS_DOCUMENT:
            self._handle_documents(chase, load, inbound, sender, cfg)
        elif label == ReplyLabel.OUT_OF_OFFICE:
            pass  # not a real reply; the schedule carries on
        elif label == ReplyLabel.PROMISE:
            if chase.state is ChaseState.ASKED:
                self._resume_schedule(chase, carrier.timezone, cfg, now + cfg.promise_grace_ms)
                self.store.put(chase)
                self._record(
                    load.id,
                    "chase.rescheduled",
                    {"next_step": chase.next_step, "at_ms": chase.next_action_at_ms},
                )
        elif label == ReplyLabel.WRONG_PERSON:
            self._escalate(
                chase, EscalationReason.WRONG_PERSON, f"contact says wrong person ({inbound.id})"
            )
        else:
            self._escalate(
                chase, EscalationReason.NEEDS_HUMAN, f"reply needs a person ({inbound.id})"
            )

    # ----------------------------------------------------------------- documents

    def _store_document(
        self, load: Load, inbound: Inbound, attachment: Attachment, extra: dict | None = None
    ) -> tuple[Document, Extraction]:
        content = self.store.get_blob(attachment.content_sha256) or b""
        extraction = self.reader.read(attachment.filename, content)
        document = Document(
            id=self.ids.new("doc"),
            load_id=load.id,
            inbound_id=inbound.id,
            filename=attachment.filename,
            content_sha256=attachment.content_sha256,
            received_at_ms=self.clock.now_ms(),
            doc_type=extraction.doc_type,
            extraction=extraction.to_dict(),
        )
        self.store.put(document)
        payload = {
            "document_id": document.id,
            "sha256": document.content_sha256,
            "filename": document.filename,
            "doc_type": document.doc_type,
            "rerouted_from_load": "",
        }
        self._record(load.id, "document.received", payload | (extra or {}))
        return document, extraction

    def _open_sibling_loads(self, load: Load) -> list[Load]:
        """Other loads of this broker and carrier that are still waiting for a POD."""
        siblings = []
        for chase in self.store.find(Chase, broker_id=load.broker_id):
            if chase.load_id == load.id or chase.state not in ACCEPTS_DOCUMENTS:
                continue
            other = self._need(Load, chase.load_id)
            if other.carrier_id == load.carrier_id:
                siblings.append(other)
        return sorted(siblings, key=lambda x: x.id)

    def _handle_documents(
        self, chase: Chase, load: Load, inbound: Inbound, sender: Contact | None, cfg: BrokerConfig
    ) -> None:
        if sender is not None:
            chase.reply_to_contact_id = sender.id
        self._move(chase, ChaseState.RECEIVED)
        self._move(chase, ChaseState.CHECKING)
        siblings = self._open_sibling_loads(load)
        results: list[tuple[Document, Extraction, CheckOutcome]] = []
        for attachment in inbound.attachments:
            document, extraction = self._store_document(load, inbound, attachment)
            outcome = check_document(extraction, load, siblings, cfg)
            self._log_check(chase, document, outcome)
            results.append((document, extraction, outcome))
        self._apply(chase, [(d, o) for d, _, o in results], cfg)
        # Documents that belong to another load are judged together per target load, so
        # a damage note on one of them is never outvoted by a clean copy in the same reply.
        by_target: dict[str, list[tuple[Document, Extraction]]] = {}
        for document, extraction, outcome in results:
            if outcome.result is CheckResult.REROUTE:
                by_target.setdefault(outcome.reroute_load_id, []).append((document, extraction))
        for target_id, documents in by_target.items():
            self._reroute(load, target_id, documents, inbound, sender)

    def _log_check(self, chase: Chase, document: Document, outcome: CheckOutcome) -> None:
        check = Check(
            id=self.ids.new("chk"),
            chase_id=chase.id,
            document_id=document.id,
            result=outcome.result,
            reasons=outcome.reasons,
            checked_at_ms=self.clock.now_ms(),
            reroute_load_id=outcome.reroute_load_id,
        )
        self.store.put(check)
        self._record(
            chase.load_id,
            "check.completed",
            {
                "check_id": check.id,
                "document_id": document.id,
                "result": outcome.result,
                "reasons": outcome.reasons,
                "reroute_load_id": outcome.reroute_load_id,
            },
        )

    def _apply(
        self, chase: Chase, outcomes: list[tuple[Document, CheckOutcome]], cfg: BrokerConfig
    ) -> None:
        """Turn check outcomes into the chase's next state. The chase is in CHECKING."""
        now = self.clock.now_ms()
        exceptions = [(d, o) for d, o in outcomes if o.result is CheckResult.EXCEPTION]
        passed = [(d, o) for d, o in outcomes if o.result is CheckResult.PASS]
        if exceptions:
            reasons = sorted({r for _, o in exceptions for r in o.reasons})
            only_confidence = reasons == ["low_confidence"]
            self._escalate(
                chase,
                EscalationReason.LOW_CONFIDENCE
                if only_confidence
                else EscalationReason.DOCUMENT_EXCEPTION,
                "document needs a person: " + ", ".join(reasons),
            )
            return
        if passed:
            document = passed[0][0]
            broker = self._need(Broker, chase.broker_id)
            adapter = broker.adapter if broker.adapter in self.adapters else "packet_email"
            writeback = self._new_writeback(chase, document, adapter)
            self._move(chase, ChaseState.PASSED)
            chase.passed_document_id = document.id
            chase.writeback_id = writeback.id
            chase.fail_reasons = []
            self._schedule(chase, Step.WRITEBACK, now)
            self.store.put(chase)
            return
        reasons: list[str] = []
        for _, outcome in outcomes:
            reasons.extend(r for r in outcome.reasons if r not in reasons)
        self._move(chase, ChaseState.FAILED_CHECK)
        chase.fail_reasons = reasons
        if chase.reasks_sent >= cfg.max_reasks:
            self._escalate(
                chase,
                EscalationReason.REASK_LIMIT,
                f"document still failing after {chase.reasks_sent} re-asks: " + ", ".join(reasons),
            )
            return
        self._schedule(chase, Step.REASK, now)
        self.store.put(chase)

    def _reroute(
        self,
        source: Load,
        target_load_id: str,
        documents: list[tuple[Document, Extraction]],
        inbound: Inbound,
        sender: Contact | None,
    ) -> None:
        """Documents sent for one load that belong to another open load of the same carrier.
        All of them are checked first; the target's next state follows from all results."""
        target = self._need(Load, target_load_id)
        chases = self.store.find(Chase, load_id=target.id)
        for document, _ in documents:
            self._record(
                source.id,
                "document.rerouted",
                {
                    "document_id": document.id,
                    "sha256": document.content_sha256,
                    "to_load": target.id,
                },
            )
        copies = [
            (
                self._store_document(
                    target,
                    inbound,
                    Attachment(document.filename, document.content_sha256),
                    {"rerouted_from_load": source.id},
                )[0],
                extraction,
            )
            for document, extraction in documents
        ]
        if not chases or chases[0].state not in ACCEPTS_DOCUMENTS:
            # Kept on the target load for a person to see; its chase is not reopened.
            self._record(
                target.id,
                "inbound.late",
                {
                    "inbound_id": inbound.id,
                    "chase_state": chases[0].state if chases else "NONE",
                },
            )
            return
        chase = chases[0]
        broker = self._need(Broker, chase.broker_id)
        if sender is not None:
            chase.reply_to_contact_id = sender.id
        self._move(chase, ChaseState.RECEIVED)
        self._move(chase, ChaseState.CHECKING)
        outcomes = []
        for copy, extraction in copies:
            outcome = check_document(extraction, target, [], broker.config)
            self._log_check(chase, copy, outcome)
            outcomes.append((copy, outcome))
        self._apply(chase, outcomes, broker.config)
        self._drain(chase.id)

    # ---------------------------------------------------------------- write-back

    def _new_writeback(self, chase: Chase, document: Document, adapter: str) -> WriteBack:
        key = sha256_hex(
            canonical_json(
                {
                    "adapter": adapter,
                    "document_sha256": document.content_sha256,
                    "load_id": chase.load_id,
                }
            )
        )
        writeback = WriteBack(
            id=self.ids.new("wb"),
            chase_id=chase.id,
            document_id=document.id,
            adapter=adapter,
            idempotency_key=key,
        )
        self.store.put(writeback)
        return writeback

    def _write_back(self, chase: Chase, load: Load, broker: Broker) -> bool:
        now = self.clock.now_ms()
        writeback: WriteBack = self._need(WriteBack, chase.writeback_id)
        document: Document = self._need(Document, writeback.document_id)
        delivered = document.extraction.get("delivery_date_ms")
        request = WriteBackRequest(
            idempotency_key=writeback.idempotency_key,
            broker_id=broker.id,
            load_id=load.id,
            chase_id=chase.id,
            load_number=load.load_number,
            document_sha256=document.content_sha256,
            filename=document.filename,
            delivered_at_ms=delivered,
        )
        try:
            receipt = self.adapters[writeback.adapter].file(request)
        except AdapterPending as pending:
            marker = f"pending:{pending.reference}"
            if writeback.detail != marker:
                writeback.detail = marker
                self.store.put(writeback)
                self._record(
                    load.id,
                    "writeback.pending",
                    {
                        "writeback_id": writeback.id,
                        "adapter": writeback.adapter,
                        "reference": pending.reference,
                    },
                )
            chase.next_action_at_ms = now + WRITEBACK_POLL_MS
            self.store.put(chase)
            return False
        except AdapterError as exc:
            writeback.attempts += 1
            backoff = broker.config.writeback_retry_backoff_ms
            self._record(
                load.id,
                "writeback.failed",
                {
                    "writeback_id": writeback.id,
                    "adapter": writeback.adapter,
                    "attempt": writeback.attempts,
                    "transient": exc.transient,
                    "error": str(exc),
                },
            )
            if exc.transient and writeback.attempts <= len(backoff):
                self.store.put(writeback)
                chase.next_action_at_ms = now + backoff[writeback.attempts - 1]
                self.store.put(chase)
                return False
            writeback.status = WriteBackStatus.FAILED
            writeback.detail = str(exc)
            self.store.put(writeback)
            if writeback.adapter != "packet_email":
                fallback = self._new_writeback(chase, document, "packet_email")
                chase.writeback_id = fallback.id
                self._schedule(chase, Step.WRITEBACK, now)
                self.store.put(chase)
                self._record(
                    load.id,
                    "writeback.fallback",
                    {"from": writeback.adapter, "to": "packet_email", "writeback_id": fallback.id},
                )
                return True
            self._escalate(
                chase, EscalationReason.WRITEBACK_FAILED, f"could not file the document: {exc}"
            )
            return False

        writeback.attempts += 1
        writeback.status = WriteBackStatus.SUCCEEDED
        writeback.receipt = receipt
        writeback.detail = ""
        writeback.completed_at_ms = now
        self.store.put(writeback)
        self._record(
            load.id,
            "writeback.succeeded",
            {
                "writeback_id": writeback.id,
                "adapter": writeback.adapter,
                "receipt": receipt,
                "idempotency_key": writeback.idempotency_key,
                "document_sha256": document.content_sha256,
            },
        )
        self._move(chase, ChaseState.FILED)
        load.ready_to_invoice_at_ms = now
        self.store.put(load)
        self._record(load.id, "load.ready_to_invoice", {"at_ms": now})
        self._close_requirement(chase, RequirementStatus.MET, document.id)
        self._move(chase, ChaseState.CLOSED)
        chase.closed_at_ms = now
        chase.closed_reason = "filed"
        chase.next_step = Step.NONE
        self.store.put(chase)
        self._record(load.id, "chase.closed", {"chase_id": chase.id, "reason": "filed"})
        return True

    # --------------------------------------------------------------- escalations

    def _escalate(self, chase: Chase, reason: EscalationReason, detail: str) -> Escalation:
        if can_transition(chase.state, ChaseState.ESCALATED):
            self._move(chase, ChaseState.ESCALATED)
        chase.next_step = Step.NONE
        self.store.put(chase)
        escalation = Escalation(
            id=self.ids.new("esc"),
            chase_id=chase.id,
            load_id=chase.load_id,
            broker_id=chase.broker_id,
            reason=reason,
            detail=detail,
            created_at_ms=self.clock.now_ms(),
        )
        self.store.put(escalation)
        self._record(
            chase.load_id,
            "escalation.opened",
            {"escalation_id": escalation.id, "reason": reason, "detail": detail},
        )
        return escalation

    def _close_requirement(
        self, chase: Chase, status: RequirementStatus, document_id: str = ""
    ) -> None:
        requirement: Requirement = self._need(Requirement, chase.requirement_id)
        if requirement.status is not RequirementStatus.OPEN:
            return
        requirement.status = status
        requirement.met_by_document_id = document_id
        requirement.closed_at_ms = self.clock.now_ms()
        self.store.put(requirement)
        self._record(
            chase.load_id,
            "requirement.closed",
            {"requirement_id": requirement.id, "status": status, "document_id": document_id},
        )

    def acknowledge_escalation(
        self,
        escalation_id: str,
        by: str,
        resolution: EscalationResolution,
        note: str = "",
    ) -> Escalation:
        """An operator finishes the escalation and says how. Closes the chase, and its
        requirement, when no escalation on it remains open."""
        escalation: Escalation = self._need(Escalation, escalation_id)
        self._operator(by, escalation.broker_id)
        resolution = EscalationResolution(resolution)
        if resolution is EscalationResolution.OTHER and not note.strip():
            raise ValueError("say what was done when the resolution is OTHER")
        if escalation.acknowledged_at_ms is not None:
            return escalation
        now = self.clock.now_ms()
        escalation.acknowledged_by = by
        escalation.acknowledged_at_ms = now
        escalation.resolution = resolution
        escalation.note = note
        self.store.put(escalation)
        self._record(
            escalation.load_id,
            "escalation.acknowledged",
            {"escalation_id": escalation.id, "by": by, "resolution": resolution, "note": note},
        )
        chase: Chase = self._need(Chase, escalation.chase_id)
        still_open = [
            e
            for e in self.store.find(Escalation, chase_id=chase.id)
            if e.acknowledged_at_ms is None
        ]
        if not still_open and chase.state is ChaseState.ESCALATED:
            self._move(chase, ChaseState.CLOSED)
            chase.closed_at_ms = now
            chase.closed_reason = "escalation_acknowledged"
            self.store.put(chase)
            self._record(
                chase.load_id,
                "chase.closed",
                {"chase_id": chase.id, "reason": "escalation_acknowledged"},
            )
            self._close_requirement(chase, RequirementStatus.RESOLVED_BY_PERSON)
        return escalation

    # ---------------------------------------------------------- people's actions

    def approve_contact(
        self, carrier_id: str, role: Role, by: str, email: str = "", phone: str = ""
    ) -> Contact:
        """An operator adds a contact. This is the only way a new address is ever used."""
        carrier: Carrier = self._need(Carrier, carrier_id)
        self._operator(by, carrier.broker_id)
        contact = Contact(
            id=self.ids.new("contact"),
            carrier_id=carrier.id,
            role=role,
            email=email,
            phone=phone,
            verified=True,
            source=f"approved:{by}",
        )
        self.store.put(contact)
        self._record(
            f"broker:{carrier.broker_id}",
            "contact.approved",
            {"contact_id": contact.id, "carrier_id": carrier.id, "by": by},
        )
        return contact

    def set_global_outbound(self, enabled: bool, by: str) -> None:
        """The switch for every broker. Platform operators only."""
        self._operator(by, "", platform_only=True)
        self.store.set_setting(GLOBAL_SWITCH, "1" if enabled else "0")
        self._record(SYSTEM_CHAIN, "outbound.switch", {"enabled": enabled, "by": by})

    def set_broker_outbound(self, broker_id: str, enabled: bool, by: str) -> None:
        """Anyone at the broker may stop messages; only the owner may start them again."""
        broker: Broker = self._need(Broker, broker_id)
        self._operator(by, broker_id, owner_only=enabled)
        broker.outbound_enabled = enabled
        self.store.put(broker)
        self._record(f"broker:{broker_id}", "outbound.switch", {"enabled": enabled, "by": by})

    def record_consent(
        self,
        contact_id: str,
        granted: bool,
        source: ConsentSource,
        by: str = "",
        inbound_id: str = "",
        note: str = "",
    ) -> Consent | None:
        """Record a change to whether a contact may be texted.

        Returns None when nothing changes. A STOP reply is always recorded, even
        when texts were already off, because it is what keeps a later sync from
        turning them back on: after a STOP, only an operator recording WRITTEN consent
        can grant texts again, and anything else raises PermissionError.
        """
        contact: Contact = self._need(Contact, contact_id)
        carrier: Carrier = self._need(Carrier, contact.carrier_id)
        source = ConsentSource(source)
        if source is ConsentSource.WRITTEN or by:
            self._operator(by, carrier.broker_id)
        if source is ConsentSource.STOP_REPLY and (granted or not inbound_id):
            raise ValueError("a STOP reply withdraws consent and names the message")
        history = self.consent_history(contact.id)
        latest = history[-1] if history else None
        status = ConsentStatus.GRANTED if granted else ConsentStatus.WITHDRAWN
        is_stop = source is ConsentSource.STOP_REPLY
        if latest is not None and latest.status is status and not is_stop:
            return None
        if granted and source is not ConsentSource.WRITTEN and self.stop_in_force(history):
            raise PermissionError(
                "this contact replied STOP; only an operator can record new written consent"
            )
        consent = Consent(
            id=self.ids.new("consent"),
            contact_id=contact.id,
            channel=Channel.SMS,
            status=status,
            source=source,
            recorded_at_ms=self.clock.now_ms(),
            seq=latest.seq + 1 if latest else 1,
            recorded_by=by,
            inbound_id=inbound_id,
            note=note,
        )
        self.store.put(consent)
        self._record(
            f"broker:{carrier.broker_id}",
            "consent.recorded",
            {
                "consent_id": consent.id,
                "contact_id": contact.id,
                "seq": consent.seq,
                "status": status,
                "source": source,
                "by": by,
                "inbound_id": inbound_id,
            },
        )
        return consent

    def update_broker_config(
        self, broker_id: str, changes: dict[str, Any], by: str = ""
    ) -> BrokerConfig:
        """Change schedule and policy settings. ``by`` is the owner making the change;
        empty means the broker's own system, through the admin API."""
        broker: Broker = self._need(Broker, broker_id)
        if by:
            self._operator(by, broker_id, owner_only=True)
        fields = {f.name: f for f in dataclasses.fields(BrokerConfig)}
        config = replace(broker.config)
        for key, value in changes.items():
            if key not in fields:
                raise ValueError(f"unknown setting: {key}")
            current = getattr(config, key)
            if isinstance(current, str):
                ok = isinstance(value, str)
            elif isinstance(current, list):
                ok = isinstance(value, list) and all(_non_negative_int(v) for v in value)
            else:
                ok = _non_negative_int(value)
            if not ok:
                kind = "text" if isinstance(current, str) else "a non-negative integer"
                raise ValueError(f"{key} must be {kind}")
            setattr(config, key, value)
        if not (0 <= config.quiet_end_hour <= config.morning_hour < config.quiet_start_hour <= 23):
            raise ValueError("hours must satisfy quiet_end <= morning < quiet_start <= 23")
        if not 0 <= config.confidence_floor <= 100:
            raise ValueError("confidence_floor must be between 0 and 100")
        try:
            ZoneInfo(config.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone: {config.timezone}") from exc
        changed = sorted(k for k in changes if getattr(broker.config, k) != getattr(config, k))
        if changed:
            broker.config = config
            self.store.put(broker)
            self._record(f"broker:{broker_id}", "config.changed", {"by": by, "fields": changed})
        return broker.config

    def save_operator(self, operator: Operator, by: str = "") -> Operator:
        """Add or change an operator. ``by`` is the broker's owner (or a platform
        operator); empty means the admin API. A broker always keeps one active owner,
        and active operators' sign-in emails are unique."""
        operator = replace(operator, email=operator.email.strip().lower())
        if (operator.role is OperatorRole.PLATFORM) == bool(operator.broker_id):
            raise ValueError("a platform operator has no broker; everyone else has one")
        if operator.broker_id:
            self._need(Broker, operator.broker_id)
        if by:
            actor = self._operator(by, operator.broker_id, owner_only=True)
            if operator.role is OperatorRole.PLATFORM and actor.role is not OperatorRole.PLATFORM:
                raise PermissionError("only a platform operator may add one")
        if operator.active and operator.email:
            clash = [
                o
                for o in self.store.find(Operator)
                if o.id != operator.id and o.active and o.email.lower() == operator.email
            ]
            if clash:
                raise ValueError(f"{operator.email} already signs in as another operator")
        previous = self.store.get(Operator, operator.id)
        if previous and previous.broker_id != operator.broker_id:
            raise ValueError("an operator cannot move to another broker")
        if previous and previous.role is OperatorRole.OWNER and previous.active:
            still_owner = operator.role is OperatorRole.OWNER and operator.active
            others = [
                o
                for o in self.store.find(Operator, broker_id=previous.broker_id)
                if o.id != operator.id and o.role is OperatorRole.OWNER and o.active
            ]
            if not still_owner and not others:
                raise ValueError("a broker must keep at least one active owner")
        self.store.put(operator)
        chain = f"broker:{operator.broker_id}" if operator.broker_id else SYSTEM_CHAIN
        self._record(
            chain,
            "operator.saved",
            {
                "operator_id": operator.id,
                "role": operator.role,
                "active": operator.active,
                "by": by,
            },
        )
        return operator

    def record_sign_in(self, operator_id: str) -> None:
        """Note that an operator signed in to the broker screens."""
        operator: Operator = self._need(Operator, operator_id)
        chain = f"broker:{operator.broker_id}" if operator.broker_id else SYSTEM_CHAIN
        self._record(chain, "operator.signed_in", {"operator_id": operator.id})

    def broker_for_mail(self, addresses: Sequence[str], mailbox_hash: str = "") -> str:
        """Which broker an incoming email is for: the "+hash" of the provider's inbound
        address names a broker id, otherwise the broker whose chase mailbox it was sent
        to. Empty when neither matches."""
        if mailbox_hash and self.store.get(Broker, mailbox_hash) is not None:
            return mailbox_hash
        wanted = {a.strip().lower() for a in addresses}
        for broker in sorted(self.store.find(Broker), key=lambda b: b.id):
            if broker.chase_mailbox.strip().lower() in wanted:
                return broker.id
        return ""

    def record_unrouted(self, from_addr: str, to: Sequence[str], external_id: str) -> None:
        """An email arrived that is not for any broker's mailbox. Kept as evidence so it
        is never silently lost; nothing else is done with it."""
        self._record(
            SYSTEM_CHAIN,
            "inbound.unrouted",
            {"from": from_addr, "to": list(to), "external_id": external_id},
        )

    def record_bounce(
        self, provider_id: str, email: str, kind: str, permanent: bool, detail: str
    ) -> None:
        """The email provider reports that a message did not arrive.

        A permanent bounce marks the address undeliverable on every contact that has
        it at that carrier (or anywhere, if the message is not ours), so it is never
        emailed again. If it was a request for a POD and no other dispatcher address
        is left, the load goes to a person. Temporary bounces are only recorded.
        """
        email = email.strip().lower()
        sent = self.store.find(OutboxMessage, provider_id=provider_id) if provider_id else []
        message = sent[0] if sent else None
        load = self.store.get(Load, message.load_id) if message else None
        chain = load.id if load else SYSTEM_CHAIN
        self._record(
            chain,
            "message.bounced",
            {
                "outbox_id": message.id if message else "",
                "email": email,
                "kind": kind,
                "permanent": permanent,
                "detail": detail,
            },
        )
        if not permanent:
            return
        for contact in self.store.find(Contact):
            if load is not None and contact.carrier_id != load.carrier_id:
                continue
            if contact.email.strip().lower() == email and contact.email_undeliverable_at_ms is None:
                contact.email_undeliverable_at_ms = self.clock.now_ms()
                self.store.put(contact)
        if message is None or load is None or message.purpose is not OutboxPurpose.CARRIER_REQUEST:
            return
        chase = self._need(Chase, message.chase_id)
        if chase.state in ACTIVE_STATES and not self._dispatcher_emails(load):
            self._escalate(
                chase, EscalationReason.SEND_FAILED, f"email to {email} bounced: {detail}"
            )

    def summary(self, broker_id: str) -> dict[str, Any]:
        """Counts for the broker's home screen."""
        broker: Broker = self._need(Broker, broker_id)
        chases = self.store.find(Chase, broker_id=broker_id)
        by_state: dict[str, int] = {}
        for chase in chases:
            by_state[chase.state.value] = by_state.get(chase.state.value, 0) + 1
        filed = [c for c in chases if c.closed_reason == "filed" and c.closed_at_ms]
        durations = sorted(c.closed_at_ms - c.opened_at_ms for c in filed)
        open_escalations = [
            e for e in self.store.find(Escalation, broker_id=broker_id) if e.resolution is None
        ]
        return {
            "broker_id": broker_id,
            "outbound_enabled": broker.outbound_enabled,
            "global_outbound_enabled": self.store.get_setting(GLOBAL_SWITCH, "1") == "1",
            "chases": len(chases),
            "by_state": dict(sorted(by_state.items())),
            "in_progress": sum(1 for c in chases if c.state in ACTIVE_STATES),
            "needs_a_person": len(open_escalations),
            "messages_waiting": sum(
                1
                for msg in self.store.find(OutboxMessage, broker_id=broker_id)
                if msg.status in (OutboxStatus.PENDING, OutboxStatus.SENDING)
            ),
            "filed": len(filed),
            "median_ms_to_filed": durations[len(durations) // 2] if durations else None,
        }

    def queue(self, broker_id: str) -> dict[str, list[dict[str, Any]]]:
        """What a person needs to look at: open escalations and messages with no home."""
        items = []
        open_escalations = [
            e
            for e in self.store.find(Escalation, broker_id=broker_id)
            if e.acknowledged_at_ms is None
        ]
        for escalation in sorted(open_escalations, key=lambda e: (e.created_at_ms, e.id)):
            load: Load = self._need(Load, escalation.load_id)
            carrier: Carrier = self._need(Carrier, load.carrier_id)
            phones = sorted({c.phone for c in self._known_contacts(load) if c.phone})
            items.append(
                {
                    "escalation_id": escalation.id,
                    "load_id": load.id,
                    "load_number": load.load_number,
                    "carrier": carrier.name,
                    "carrier_phones": phones,
                    "reason": escalation.reason,
                    "detail": escalation.detail,
                    "created_at_ms": escalation.created_at_ms,
                }
            )
        unmatched = [
            {
                "inbound_id": i.id,
                "channel": i.channel,
                "from": i.from_addr,
                "subject": i.subject,
                "received_at_ms": i.received_at_ms,
            }
            for i in sorted(
                self.store.find(Inbound, broker_id=broker_id, status=InboundStatus.UNMATCHED),
                key=lambda i: (i.received_at_ms, i.id),
            )
        ]
        return {"escalations": items, "unmatched": unmatched}


def _non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
