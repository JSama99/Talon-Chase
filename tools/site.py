"""The landing page's recorded chases, and its deploy build.

    python tools/site.py runs            # rewrite the recorded chases in site/index.html
    python tools/site.py runs --check    # fail if they are stale (CI runs this)
    python tools/site.py build --form-url https://... --out dist [--inline]

The "Run a chase" panel on the page replays chases recorded from the real engine,
with the same stand-in channels the tests use. Nothing on it is hand-written: the
times come from the schedule, the wording from the broker screens' timeline, and the
hashes from the evidence chain. ``runs --check`` keeps the page from drifting away
from the engine. See docs/SITE.md.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from chase.app.timeline import tell  # noqa: E402
from chase.app.wording import REASON_TITLES  # noqa: E402
from chase.channels.fakes import FakeEmail, FakeSms  # noqa: E402
from chase.domain.clock import FakeClock, SequentialIds  # noqa: E402
from chase.domain.models import (  # noqa: E402
    HOUR_MS,
    MINUTE_MS,
    Broker,
    Carrier,
    Channel,
    Chase,
    ChaseState,
    ConsentSource,
    Contact,
    Document,
    Escalation,
    Load,
    Party,
    PartyRole,
    Role,
)
from chase.domain.schedule import to_local  # noqa: E402
from chase.engine import ChaseEngine  # noqa: E402
from chase.outbox import OutboxDispatcher  # noqa: E402
from chase.reader.rules import RuleBasedClassifier, StandInReader  # noqa: E402
from chase.store.memory import MemoryStore  # noqa: E402
from chase.writeback.adapters import FakeApiAdapter  # noqa: E402

SITE = ROOT / "site"
PAGE = SITE / "index.html"
TZ = "America/Chicago"
DISPATCH = "dispatch@redlinetrucking.example"
FORM_PLACEHOLDER = "https://example.invalid/set-SIGNUP_FORM_URL"


def _at(day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(2026, 11, day, hour, minute, tzinfo=ZoneInfo(TZ)).timestamp()) * 1000


# When the load is marked delivered, in the carrier's local time.
DELIVERIES = {
    "tue-afternoon": ("Tuesday 2:00 pm", _at(10, 14)),
    "fri-afternoon": ("Friday 4:30 pm", _at(13, 16, 30)),
    "tue-night": ("Tuesday 9:15 pm", _at(10, 21, 15)),
}

# What the carrier does. Each step is (delay after the first request, body, POD overrides
# or None for no attachment).
CARRIERS: dict[str, tuple[str, list[tuple[int, str, dict | None]]]] = {
    "clean": ("Sends a signed POD", [(40 * MINUTE_MS, "Attached.", {})]),
    "unsigned": (
        "Sends it unsigned first",
        [
            (40 * MINUTE_MS, "Here.", {"signature_present": False}),
            (100 * MINUTE_MS, "Signed copy.", {}),
        ],
    ),
    "promise": (
        "Says “will send tomorrow”",
        [
            (40 * MINUTE_MS, "Driver is still out. Will send tomorrow.", None),
            (27 * HOUR_MS, "Attached.", {}),
        ],
    ),
    "damage": (
        "POD has a damage note",
        [(40 * MINUTE_MS, "POD.", {"exception_notes": ["2 cartons damaged"]})],
    ),
    "bank": (
        "Asks to change bank details",
        [
            (
                40 * MINUTE_MS,
                "Before we send it, please update our bank details and remit-to address.",
                None,
            )
        ],
    ),
    "silent": ("Never answers", []),
}


def _stand_in_pod(load: Load, **overrides) -> bytes:
    data = {
        "doc_type": "POD",
        "readable": True,
        "reference_numbers": [load.load_number],
        "signature_present": True,
        "delivery_date_ms": load.delivery_appt_ms,
        "page_count": 1,
        "exception_notes": [],
        "confidence": 96,
    }
    data.update(overrides)
    return json.dumps(data).encode()


def _when(ms: int) -> str:
    local = to_local(ms, TZ)
    hour = local.hour % 12 or 12
    return f"{local:%a} {hour}:{local:%M} {'am' if local.hour < 12 else 'pm'}"


def record(delivery: str, carrier: str) -> dict:
    """Run one chase through the real engine and return what the page shows."""
    start = DELIVERIES[delivery][1]
    store, clock = MemoryStore(), FakeClock(start)
    email, sms, tms = FakeEmail(), FakeSms(), FakeApiAdapter()
    engine = ChaseEngine(
        store=store, clock=clock, ids=SequentialIds(), email=email, sms=sms,
        classifier=RuleBasedClassifier(), reader=StandInReader(), adapters=[tms],
        secret=b"landing-page-recording",
    )  # fmt: skip
    outbox = OutboxDispatcher(engine)
    broker = Broker(
        "brk-demo", "Peachtree Logistics", "pods@peachtreelogistics.example",
        "ops@peachtreelogistics.example", "+14045550100", "fake_api",
    )  # fmt: skip
    store.put(broker)
    store.put(Carrier("car-demo", broker.id, "Redline Trucking", "MC482910", TZ))
    store.put(Contact("con-d", "car-demo", Role.DISPATCHER, "Dana", DISPATCH))
    store.put(Contact("con-v", "car-demo", Role.DRIVER, "Sam", phone="+13125550142"))
    engine.record_consent("con-v", True, ConsentSource.RATE_CONFIRMATION)
    customer = Party("party-acme", broker.id, PartyRole.CUSTOMER, "Acme Foods")
    load = Load(
        id="demo-1", broker_id=broker.id, carrier_id="car-demo", load_number="TC-300001",
        delivery_appt_ms=start - HOUR_MS, delivered_at_ms=start,
        customer_party_id=customer.id, pickup_number="PU-77812", rate_cents=185_000,
    )  # fmt: skip

    def settle() -> None:
        for _ in range(10):
            if not outbox.dispatch().changed and not engine.tick():
                return

    def advance_to(until_ms: int) -> None:
        while clock.now_ms() < until_ms:
            clock.advance(min(15 * MINUTE_MS, until_ms - clock.now_ms()))
            engine.tick()
            settle()

    engine.intake_load(load, [customer])
    engine.tick()
    settle()
    chase = store.find(Chase, load_id=load.id)[0]
    first_ms = chase.plan["first_ms"] if isinstance(chase.plan, dict) else chase.plan.first_ms
    for delay, body, overrides in CARRIERS[carrier][1]:
        advance_to(first_ms + delay)
        engine.handle_inbound(
            broker_id=broker.id, channel=Channel.EMAIL, from_addr=DISPATCH,
            subject=f"Re: proof of delivery for load {load.load_number}", body=body,
            attachments=[("pod.pdf", _stand_in_pod(load, **overrides))]
            if overrides is not None
            else [],
        )  # fmt: skip
        settle()
    advance_to(start + 6 * 24 * HOUR_MS)

    chase = store.find(Chase, load_id=load.id)[0]
    events = store.evidence(load.id)
    documents = {d.id: d for d in store.find(Document, load_id=load.id)}
    entries = tell(
        events,
        {
            "documents": documents,
            "filenames": {d.content_sha256: d.filename for d in documents.values()},
            "load_numbers": {load.id: load.load_number},
        },
    )
    export = engine.export_evidence(load.id)
    escalations = store.find(Escalation, load_id=load.id)
    filed = chase.state in (ChaseState.FILED, ChaseState.CLOSED) and not escalations
    to_carrier = [m for m in email.sent if m.to != [broker.rep_email]]
    first_email = to_carrier[0]
    return {
        "delivery": delivery,
        "carrier": carrier,
        "outcome": "filed" if filed else "person",
        "outcome_title": "Filed. Ready to invoice."
        if filed
        else REASON_TITLES[escalations[0].reason],
        "entries": [
            {"when": _when(e.at_ms), "title": e.title, "detail": e.detail, "tone": e.tone}
            for e in entries
        ],
        "messages_to_carrier": len(to_carrier) + len(sms.sent),
        "evidence_events": len(events),
        "chain_verifies": export["verification"]["ok"],
        "head_hash": export["verification"]["head_hash"],
        "first_email": {"subject": first_email.subject, "body": first_email.body},
        "chain": [{"seq": e.seq, "kind": e.kind, "hash": e.hash} for e in events[:5]],
    }


# ------------------------------------------------------------------ the page

DEFAULT = ("tue-afternoon", "silent")
ORDER = ["silent", "promise", "clean", "unsigned", "damage", "bank"]
GLYPH = {"good": "✓", "bad": "!", "": "·"}
esc = html.escape


def all_runs() -> list[dict]:
    return [record(d, c) for d in DELIVERIES for c in ORDER]


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _pick(name: str, legend: str, options: list[tuple[str, str]], chosen: str) -> str:
    lines = [f'    <fieldset class="pick"><legend class="label">{esc(legend)}</legend>']
    lines.append('      <div class="pick-list">')
    for key, label in options:
        checked = " checked" if key == chosen else ""
        lines.append(
            f'        <input type="radio" name="{name}" id="{name}-{key}"{checked}>'
            f'<label for="{name}-{key}">{esc(label)}</label>'
        )
    lines.append("      </div>")
    lines.append("    </fieldset>")
    return "\n".join(lines)


def render_runs(runs: list[dict]) -> str:
    out = [
        _pick("d", "The load is marked delivered", [(k, v[0]) for k, v in DELIVERIES.items()],
              DEFAULT[0]),
        _pick("c", "The carrier", [(k, CARRIERS[k][0]) for k in ORDER], DEFAULT[1]),
        '    <div class="runs">',
    ]  # fmt: skip
    for run in runs:
        default = " is-default" if (run["delivery"], run["carrier"]) == DEFAULT else ""
        when, does = DELIVERIES[run["delivery"]][0], CARRIERS[run["carrier"]][0]
        what = f"Delivered {when}. Carrier: {does}"
        out.append(
            f'      <article class="run d-{run["delivery"]} c-{run["carrier"]}{default}" '
            f'aria-label="{esc(what)}">'
        )
        out.append('        <ol class="tl">')
        for e in run["entries"]:
            tone = e["tone"]
            detail = f'<span class="d">{esc(e["detail"])}</span>' if e["detail"] else ""
            out.append(
                f'          <li class="{tone}"><time>{esc(e["when"])}</time>'
                f'<span class="g" aria-hidden="true">{GLYPH[tone]}</span>'
                f'<div><span class="t">{esc(e["title"])}</span>{detail}</div></li>'
            )
        out.append("        </ol>")
        if run["outcome"] == "filed":
            out.append(
                '        <p class="stamp filed"><strong>Filed</strong>'
                "<span>Ready to invoice</span></p>"
            )
        else:
            out.append(
                '        <p class="stamp person"><strong>Needs you</strong>'
                f"<span>{esc(run['outcome_title'])}</span></p>"
            )
        verifies = "chain verifies" if run["chain_verifies"] else "CHAIN DOES NOT VERIFY"
        out.append(
            f'        <p class="facts">'
            f"{_count(run['messages_to_carrier'], 'message', 'messages')} to the carrier · "
            f"{_count(run['evidence_events'], 'entry', 'entries')} on the record · "
            f"{verifies} · seal {run['head_hash'][:16]}</p>"
        )
        out.append("      </article>")
    out.append("    </div>")
    return "\n".join(out)


def render_email(run: dict) -> str:
    """The first request exactly as sent, with the one-time upload link shortened."""
    body = re.sub(r"https://\S+/u/\S+", "https://(your address)/u/(one-time link)",
                  run["first_email"]["body"]).strip()  # fmt: skip
    return (
        "      <details>\n"
        "        <summary>Read the email the carrier gets</summary>\n"
        f"        <pre>Subject: {esc(run['first_email']['subject'])}\n\n{esc(body)}</pre>\n"
        "      </details>"
    )


def render_chain(run: dict) -> str:
    lines = ['    <ol class="chain rise" aria-label="The first entries on one load\'s record">']
    for e in run["chain"]:
        lines.append(
            f'      <li><span class="n">{e["seq"]:02d}</span>'
            f'<span class="k">{esc(e["kind"])}</span>'
            f'<span class="h">{e["hash"][:40]}…</span></li>'
        )
    lines.append("    </ol>")
    return "\n".join(lines)


def _fill(page: str, name: str, block: str) -> str:
    pattern = re.compile(rf"(<!-- {name}:start -->\n).*?(<!-- {name}:end -->)", re.DOTALL)
    assert pattern.search(page), f"marker {name} missing from the page"
    return pattern.sub(lambda m: m.group(1) + block + "\n" + m.group(2), page)


def render_page(page: str) -> str:
    runs = all_runs()
    default = next(r for r in runs if (r["delivery"], r["carrier"]) == DEFAULT)
    page = _fill(page, "runs", render_runs(runs))
    page = _fill(page, "email", render_email(default))
    return _fill(page, "chain", render_chain(default))


# ----------------------------------------------------------------- the build

SHIPPED = [
    "index.html", "site.css", "site.js", "favicon.svg",
    "fonts/inter-latin-var.woff2", "fonts/LICENSE-Inter.txt",
    "img/needs-you.webp", "img/needs-you-1200.webp", "img/needs-you-phone.webp",
]  # fmt: skip


def check_form_url(url: str) -> str:
    """The address the signup form posts to. Refuses anything that would ship a dead form."""
    url = (url or "").strip()
    parts = urlsplit(url)
    if not url:
        raise SystemExit("set the form address: --form-url or SIGNUP_FORM_URL (see docs/SITE.md)")
    if parts.scheme != "https" or not parts.hostname or "." not in parts.hostname:
        raise SystemExit("the form address must be a full https:// address")
    if parts.hostname.endswith(".invalid") or parts.hostname.endswith(".example"):
        raise SystemExit("the form address is still a stand-in; use the real one")
    if any(c in url for c in "\"'<> "):
        raise SystemExit("the form address contains a character that does not belong in one")
    return url


def build(form_url: str, out: Path, inline: bool = False) -> Path:
    form_url = check_form_url(form_url)
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(form_url))
    page = PAGE.read_text()
    if page != render_page(page):
        raise SystemExit("the recorded chases are stale: run python tools/site.py runs")
    page = page.replace(FORM_PLACEHOLDER, form_url).replace("https://example.invalid", origin)
    assert "example.invalid" not in page
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    if inline:
        (out / "index.html").write_text(_inline(page))
        return out
    for name in SHIPPED:
        target = out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if name == "index.html":
            target.write_text(page)
        else:
            shutil.copyfile(SITE / name, target)
    return out


def _inline(page: str) -> str:
    """One self-contained file, for previews. The deployed site is the separate files."""
    import base64

    def data(path: str, mime: str) -> str:
        return f"data:{mime};base64," + base64.b64encode((SITE / path).read_bytes()).decode()

    css = (
        (SITE / "site.css")
        .read_text()
        .replace(
            'url("fonts/inter-latin-var.woff2")',
            f'url("{data("fonts/inter-latin-var.woff2", "font/woff2")}")',
        )
    )
    page = re.sub(r"<meta http-equiv=\"Content-Security-Policy\"[^>]*>\n", "", page)
    page = page.replace(
        '      <button class="btn" type="submit">',
        '      <p class="note">Preview copy: this form is not connected.</p>\n'
        '      <button class="btn" type="submit">',
    )
    page = re.sub(r"<link rel=\"(?:preload|icon)\"[^>]*>\n", "", page)
    page = page.replace('<link rel="stylesheet" href="site.css">', f"<style>\n{css}</style>")
    page = page.replace(
        '<script src="site.js" defer></script>',
        f"<script>\n{(SITE / 'site.js').read_text()}</script>",
    )
    page = re.sub(r', img/needs-you\.webp 2400w" sizes="[^"]*"', '"', page).replace(" 1200w", "")
    for name in ("img/needs-you-1200.webp", "img/needs-you-phone.webp"):
        page = page.replace(name, data(name, "image/webp"))
    assert not re.search(r'(?:src|srcset|href)="(?!data:|#|https://)', page), (
        "a file is not inlined"
    )
    return page


def main(argv: list[str] | None = None) -> int:
    import os

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    runs = sub.add_parser("runs", help="rewrite the recorded chases in site/index.html")
    runs.add_argument("--check", action="store_true", help="fail if they are stale")
    b = sub.add_parser("build", help="write the site, ready to publish")
    b.add_argument("--form-url", default=os.environ.get("SIGNUP_FORM_URL", ""))
    b.add_argument("--out", type=Path, default=ROOT / "out" / "site")
    b.add_argument("--inline", action="store_true", help="one self-contained file, for previews")
    args = parser.parse_args(argv)

    if args.command == "runs":
        current = PAGE.read_text()
        fresh = render_page(current)
        if args.check:
            if fresh != current:
                print("site/index.html is stale: run python tools/site.py runs")
                return 1
            print("site/index.html matches the engine")
            return 0
        PAGE.write_text(fresh)
        print("recorded 18 chases into site/index.html")
        return 0
    print(f"site written to {build(args.form_url, args.out, args.inline)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
