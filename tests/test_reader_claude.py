"""The Claude reader against a fake of the Messages API. No test touches the network.

What these hold: the request is the documented shape and carries nothing it should
not; the model's description becomes facts by fixed rules; every doubt lands under
the broker's floor with words for a person; and every way the service can fail is
either retried or handed to a person, never passed off as a reading.
"""

from __future__ import annotations

import base64
import io
import json

import httpx
import pytest
from PIL import Image
from pypdf import PdfWriter

from chase.domain.checks import check_document
from chase.domain.clock import FakeClock
from chase.domain.models import (
    BrokerConfig,
    ChaseState,
    CheckResult,
    DocType,
    Document,
    EscalationReason,
)
from chase.ports import ReaderError
from chase.reader import claude
from chase.reader.claude import (
    API_VERSION,
    CONFIDENCE,
    SCHEMA,
    ClaudeReader,
    delivery_date_ms,
    shape_problem,
    to_extraction,
)
from tests.conftest import START, World, at

KEY = "sk-ant-test-" + "k" * 40
NOON_14TH = 1794657600000  # 14 Nov 2026, 12:00 UTC
FLOOR = BrokerConfig().confidence_floor


def report(**overrides) -> dict:
    """What the model says about a clean, signed, dated POD."""
    data = {
        "document_type": "POD",
        "legibility": "clear",
        "reference_numbers": [
            {"label": "Load number", "value": "TC-100001", "certain": True},
            {"label": "B/L number", "value": "BOL55001", "certain": True},
        ],
        "receiver_signature": "present",
        "delivery_date": {
            "as_written": "11/14/2026",
            "month": 11,
            "day": 14,
            "year": 2026,
            "certain": True,
        },
        "handwritten_notes": [],
        "problems": [],
        "addressed_to_reader": False,
    }
    data.update(overrides)
    return data


def jpeg() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (640, 480), "white").save(out, "JPEG")
    return out.getvalue()


def pdf(pages: int = 2) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(612, 792)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


class FakeAnthropic:
    """Answers POST /v1/messages. Queue replies with ``say``; the default is a clean POD."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.replies: list[httpx.Response | Exception] = []

    def say(self, body=None, *, status: int = 200, stop: str = "end_turn", text: str | None = None):
        if status != 200:
            self.replies.append(httpx.Response(status, json=body))
            return
        answer = text if text is not None else json.dumps(body if body is not None else report())
        self.replies.append(
            httpx.Response(
                200,
                json={
                    "type": "message",
                    "stop_reason": stop,
                    "content": [
                        {"type": "thinking", "thinking": "..."},
                        {"type": "text", "text": answer},
                    ],
                    "usage": {"input_tokens": 5200, "output_tokens": 310},
                },
            )
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.replies:
            self.say()
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    @property
    def body(self) -> dict:
        return json.loads(self.requests[-1].content)


@pytest.fixture
def api() -> FakeAnthropic:
    return FakeAnthropic()


@pytest.fixture
def reader(api: FakeAnthropic) -> ClaudeReader:
    return ClaudeReader(KEY, FakeClock(START), transport=httpx.MockTransport(api.handler))


# ---------------------------------------------------------------- the request


def test_the_request_is_the_documented_shape(reader, api):
    reader.read("pod.jpg", jpeg())
    request = api.requests[0]
    assert (request.method, request.url) == ("POST", "https://api.anthropic.com/v1/messages")
    assert request.headers["x-api-key"] == KEY
    assert request.headers["anthropic-version"] == API_VERSION
    body = api.body
    assert set(body) == {"model", "max_tokens", "system", "messages", "output_config"}
    assert body["model"] == "claude-opus-5-5" and body["max_tokens"] >= 8000
    assert body["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}
    (message,) = body["messages"]
    image, ask = message["content"]
    assert message["role"] == "user" and ask == {"type": "text", "text": claude.ASK}
    assert image["type"] == "image" and image["source"]["media_type"] == "image/jpeg"
    assert base64.b64decode(image["source"]["data"]) == jpeg()


def test_a_pdf_goes_as_a_document_and_its_pages_are_counted_here(reader, api):
    extraction = reader.read("scan.pdf", pdf(pages=2))
    (document, _) = api.body["messages"][0]["content"]
    assert document["type"] == "document"
    assert document["source"] == {
        "type": "base64",
        "media_type": "application/pdf",
        "data": base64.standard_b64encode(pdf(pages=2)).decode(),
    }
    assert extraction.page_count == 2  # from the file; the model is never asked


def test_each_page_of_a_fax_goes_as_its_own_picture_before_the_question(reader, api):
    page = Image.new("1", (800, 1000), 1)
    out = io.BytesIO()
    page.save(out, "TIFF", save_all=True, append_images=[page], compression="group4")
    extraction = reader.read("fax.tif", out.getvalue())
    content = api.body["messages"][0]["content"]
    assert [block["type"] for block in content] == ["image", "image", "text"]
    assert extraction.page_count == 2


def test_the_model_is_told_nothing_it_could_use_to_guess(reader, api):
    """Not the file name (the sender chooses it), not the load, not the carrier."""
    reader.read("TC-100001 SIGNED no damage.jpg", jpeg())
    sent = api.requests[0].content.decode()
    assert "TC-100001" not in sent and "SIGNED no damage" not in sent
    assert "temperature" not in api.body and "tools" not in api.body


def test_the_schema_stays_inside_what_the_api_accepts():
    """Structured outputs refuse a schema with open objects, optional fields or
    constraints the grammar cannot express. Checked here so it fails in CI, not live."""
    banned = {"minimum", "maximum", "minLength", "maxLength", "multipleOf", "pattern", "format"}

    def walk(node, path="schema"):
        if isinstance(node, dict):
            assert not banned & set(node), path
            if node.get("type") == "object":
                assert node["additionalProperties"] is False, path
                assert sorted(node["required"]) == sorted(node["properties"]), path
            assert not isinstance(node.get("type"), list) and "anyOf" not in node, path
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for item in node:
                walk(item, path)

    walk(SCHEMA)
    assert SCHEMA["properties"]["document_type"]["enum"] == [t.value for t in DocType]


def test_the_readers_name_says_which_model_and_which_instructions(api):
    reader = ClaudeReader(KEY, FakeClock(START), model="claude-sonnet-5-5",
                          transport=httpx.MockTransport(api.handler))  # fmt: skip
    assert reader.name == f"claude/claude-sonnet-5-5/{claude.PROMPT_VERSION}"
    assert reader.inline is False
    reader.read("pod.jpg", jpeg())
    assert api.body["model"] == "claude-sonnet-5-5"
    with pytest.raises(ValueError, match="API key is required"):
        ClaudeReader("", FakeClock(START))


# ------------------------------------------------------ description -> facts


def test_a_clean_pod_reads_as_facts_the_checks_can_pass(reader, api):
    extraction = reader.read("pod.jpg", jpeg())
    assert extraction.to_dict() == {
        "doc_type": "POD",
        "readable": True,
        "reference_numbers": ["TC-100001", "BOL55001"],
        "signature_present": True,
        "delivery_date_ms": NOON_14TH,
        "page_count": 1,
        "exception_notes": [],
        "confidence": CONFIDENCE["clean"],
        "doubts": [],
    }
    assert CONFIDENCE["clean"] >= FLOOR


def facts(**overrides):
    return to_extraction(report(**overrides), 1, START)


def test_only_a_clean_reading_or_a_surely_unusable_file_clears_the_default_floor():
    above = {name for name, level in CONFIDENCE.items() if level >= FLOOR}
    assert above == {"clean", "sure_it_is_unusable"}


@pytest.mark.parametrize(
    ("change", "doubt"),
    [
        ({"receiver_signature": "unsure"}, "whether the receiver signed"),
        ({"receiver_signature": "Scribble"}, "whether the receiver signed"),
        (
            {"reference_numbers": [{"label": "Load", "value": "TC-10O001", "certain": False}]},
            "a reference number, read as TC-10O001",
        ),
        (
            {"delivery_date": {"as_written": "11/1?/26", "month": 11, "day": 14, "year": 26,
                               "certain": False}},
            "the delivery date, read as 11/1?/26",
        ),
        (
            {"delivery_date": {"as_written": "13/45/2026", "month": 13, "day": 45, "year": 2026,
                               "certain": True}},
            "the delivery date, written 13/45/2026",
        ),
        (
            {"delivery_date": {"as_written": "", "month": 0, "day": 0, "year": 0,
                               "certain": False}},
            "the delivery date",
        ),
        (
            {"handwritten_notes": [{"text": "beside the signature", "kind": "illegible"}]},
            "handwriting that could not be read (beside the signature)",
        ),
        ({"handwritten_notes": [{"text": "", "kind": "smudge"}]},
         "handwriting that could not be read"),
        ({"legibility": "partly_legible"}, "part of it could not be read"),
        ({"legibility": "partly_legible", "problems": ["blurry"]}, "the picture is blurry"),
        ({"problems": ["cut_off"]}, "part of the page is outside the picture"),
        ({"problems": ["glare"]}, "glare hides part of the page"),
        ({"problems": ["several_shipments"]}, "it holds paperwork for more than one shipment"),
        ({"addressed_to_reader": True}, "it has text written to steer an automated reader"),
        ({"document_type": "PASSPORT"}, "what kind of document it is"),
    ],
)  # fmt: skip
def test_any_doubt_puts_the_reading_under_the_floor_and_says_what_to_look_at(change, doubt):
    extraction = facts(**change)
    assert extraction.confidence < FLOOR
    assert doubt in extraction.doubts


def test_a_doubted_reading_goes_to_a_person_and_never_back_to_the_carrier():
    """Whatever else the model reported: an unsure signature on a document that also
    looks unsigned-and-wrong must not become an automatic 'please re-send'."""
    world = World()
    load = world.make_load()
    doubted = facts(receiver_signature="unsure", reference_numbers=[])
    outcome = check_document(doubted, load, [], world.broker.config)
    assert (outcome.result, outcome.reasons) == (CheckResult.EXCEPTION, ["low_confidence"])


def test_the_lowest_doubt_sets_the_confidence_and_every_doubt_is_listed():
    extraction = facts(
        receiver_signature="unsure",
        handwritten_notes=[{"text": "", "kind": "illegible"}],
        legibility="partly_legible",
    )
    assert extraction.confidence == CONFIDENCE["note_in_doubt"]
    assert len(extraction.doubts) == 3


def test_a_note_about_damage_shortage_or_refusal_becomes_an_exception_note():
    extraction = facts(
        handwritten_notes=[
            {"text": "2 PLT  crushed -\n16 rec'd OK", "kind": "exception"},
            {"text": "Rec'd in good order, door 6", "kind": "routine"},
            {"text": "SEAL BROKEN", "kind": "Exception"},
        ]
    )
    assert extraction.exception_notes == ["2 PLT crushed - 16 rec'd OK", "SEAL BROKEN"]
    assert extraction.confidence == CONFIDENCE["clean"]  # read clearly; rule 6 routes it


def test_a_routine_remark_does_not_stop_a_good_pod():
    extraction = facts(handwritten_notes=[{"text": "Rec'd in good order", "kind": "routine"}])
    assert extraction.exception_notes == [] and extraction.doubts == []
    assert extraction.confidence == CONFIDENCE["clean"]


def test_a_note_of_a_kind_the_reader_was_never_offered_is_not_assumed_routine():
    extraction = facts(handwritten_notes=[{"text": "??", "kind": "probably fine"}])
    assert extraction.confidence == CONFIDENCE["note_in_doubt"]


def test_an_unsigned_undated_pod_is_reported_plainly_so_the_checks_can_ask_again():
    extraction = facts(
        receiver_signature="absent",
        delivery_date={"as_written": "", "month": 0, "day": 0, "year": 0, "certain": True},
    )
    assert not extraction.signature_present and extraction.delivery_date_ms is None
    assert extraction.confidence == CONFIDENCE["clean"] and extraction.doubts == []


def test_an_illegible_file_is_unreadable_with_enough_confidence_to_ask_for_a_better_copy():
    extraction = facts(legibility="illegible", problems=["blurry", "too_dark"])
    assert extraction.readable is False
    assert extraction.confidence == CONFIDENCE["sure_it_is_unusable"] >= FLOOR
    assert extraction.doubts == ["the picture is blurry", "the picture is too dark"]
    assert extraction.reference_numbers == [] and not extraction.signature_present


def test_an_illegible_file_that_also_tries_to_steer_the_reader_still_goes_to_a_person():
    extraction = facts(legibility="illegible", addressed_to_reader=True)
    assert extraction.confidence == 0 and extraction.readable is False


def test_something_that_is_plainly_not_a_pod_is_reported_as_what_it_is():
    extraction = facts(document_type="LUMPER_RECEIPT", receiver_signature="absent")
    assert extraction.doc_type is DocType.LUMPER_RECEIPT
    assert extraction.confidence == CONFIDENCE["clean"]


def test_enum_values_are_matched_whatever_their_capitals(reader, api):
    """The API does not promise the capitalisation of enum values."""
    api.say(report(document_type="pod", legibility="Clear", receiver_signature="Present"))
    extraction = reader.read("pod.jpg", jpeg())
    assert extraction.doc_type is DocType.POD and extraction.signature_present
    assert extraction.confidence == CONFIDENCE["clean"]


def test_reference_numbers_are_kept_as_written_once_each_and_bounded():
    many = [{"label": "", "value": f"REF{n}", "certain": True} for n in range(100)]
    many += [{"label": "", "value": "  ", "certain": True}]
    many[1] = {"label": "dup", "value": "REF0", "certain": True}
    extraction = facts(reference_numbers=many)
    assert len(extraction.reference_numbers) == claude.MAX_REFERENCES - 1
    assert extraction.reference_numbers[0] == "REF0"


def test_a_very_long_note_is_cut_and_too_many_notes_send_it_to_a_person():
    extraction = facts(handwritten_notes=[{"text": "damaged " * 200, "kind": "exception"}])
    assert len(extraction.exception_notes[0]) == claude.MAX_NOTE_CHARS
    flood = [{"text": f"note {n}", "kind": "routine"} for n in range(claude.MAX_NOTES + 1)]
    assert facts(handwritten_notes=flood).confidence == CONFIDENCE["note_in_doubt"]


# --------------------------------------------------------------------- dates


def test_a_written_date_becomes_noon_utc_of_that_day():
    assert delivery_date_ms(11, 14, 2026, START) == NOON_14TH
    assert delivery_date_ms(11, 14, 26, START) == NOON_14TH  # a two-digit year


@pytest.mark.parametrize(
    ("today", "month", "day", "expected_year"),
    [
        (at(2026, 11, 16, 9), 11, 14, 2026),
        (at(2027, 1, 5, 9), 12, 29, 2026),  # read after New Year: last year's December
        (at(2026, 12, 30, 9), 1, 2, 2027),  # dated ahead by a receiver: next year's January
        (at(2027, 3, 1, 9), 2, 28, 2027),
    ],
)
def test_a_date_without_a_year_is_the_one_nearest_the_day_it_is_read(
    today, month, day, expected_year
):
    assert delivery_date_ms(month, day, 0, today) == delivery_date_ms(
        month, day, expected_year, today
    )


def test_what_is_not_a_real_date_is_no_date():
    for month, day, year in ((2, 30, 2026), (0, 14, 2026), (11, 0, 2026), (11, 14, 1926),
                             (11, 14, 2099), (2, 30, 0)):  # fmt: skip
        assert delivery_date_ms(month, day, year, START) is None
    assert delivery_date_ms(2, 29, 0, at(2027, 3, 5, 9)) is not None  # 29 Feb 2028 exists


def test_noon_utc_is_within_a_day_of_any_appointment_on_that_date_across_north_america():
    """The date check allows 24 hours. A POD dated the appointment's local day must
    never trip it, from Newfoundland to Hawaii, at any hour of the day."""
    tolerance = BrokerConfig().date_tolerance_ms
    for tz in ("America/St_Johns", "America/New_York", "America/Chicago", "America/Denver",
               "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu"):  # fmt: skip
        for hour in (0, 6, 12, 18, 23):
            appointment = at(2026, 11, 14, hour, 59 if hour == 23 else 0, tz=tz)
            assert abs(NOON_14TH - appointment) <= tolerance, (tz, hour)


# ------------------------------------------------ files that cannot be shown


@pytest.mark.parametrize(
    ("content", "why"),
    [
        (b"PK\x03\x04" + b"\0" * 64, "it is a zip or an Office file, not a photo or a PDF"),
        (b"%PDF-1.7\n", "the PDF is damaged and will not open"),
        (b"", "the file is empty"),
    ],
)
def test_a_file_that_cannot_be_shown_is_answered_without_calling_the_model(
    reader, api, content, why
):
    extraction = reader.read("pod.pdf", content)
    assert api.requests == []
    assert extraction.confidence == 0 and extraction.doubts == [why]
    assert extraction.readable is False  # and so: a person, not a re-ask


# ------------------------------------------------------------------ failures


def test_a_refusal_is_an_answer_and_goes_to_a_person(reader, api):
    api.say(stop="refusal", text="I can't help with that.")
    extraction = reader.read("pod.jpg", jpeg())
    assert extraction.confidence == 0
    assert extraction.doubts == ["the reader declined to read this file"]


@pytest.mark.parametrize("status", [401, 403, 404, 408, 409, 429, 500, 502, 503, 504, 529])
def test_outages_limits_and_set_up_mistakes_are_temporary(reader, api, status):
    api.say({"type": "error", "error": {"type": "some_error", "message": "try later"}},
            status=status)  # fmt: skip
    with pytest.raises(ReaderError) as caught:
        reader.read("pod.jpg", jpeg())
    assert caught.value.transient
    assert str(caught.value) == f"Anthropic {status}/some_error: try later"


@pytest.mark.parametrize("status", [400, 413, 422])
def test_a_request_the_api_will_never_accept_is_given_up_on_at_once(reader, api, status):
    api.say({"type": "error", "error": {"type": "invalid_request_error", "message": "no"}},
            status=status)  # fmt: skip
    with pytest.raises(ReaderError) as caught:
        reader.read("pod.jpg", jpeg())
    assert not caught.value.transient


def test_a_timeout_or_a_dropped_connection_is_temporary(reader, api):
    for failure in (httpx.ReadTimeout("slow"), httpx.ConnectError("down")):
        api.replies.append(failure)
        with pytest.raises(ReaderError) as caught:
            reader.read("pod.jpg", jpeg())
        assert caught.value.transient and type(failure).__name__ in str(caught.value)


def test_an_answer_cut_off_by_the_token_limit_is_not_used(reader, api):
    api.say(stop="max_tokens", text='{"document_type": "POD", "legib')
    with pytest.raises(ReaderError, match="cut off") as caught:
        reader.read("pod.jpg", jpeg())
    assert not caught.value.transient


def test_an_answer_that_is_not_json_or_not_the_right_shape_is_retried_not_used(reader, api):
    api.say(text="Here is what I found: it is signed.")
    with pytest.raises(ReaderError, match="not valid JSON") as caught:
        reader.read("pod.jpg", jpeg())
    assert caught.value.transient
    broken = report()
    del broken["receiver_signature"]
    api.say(broken)
    with pytest.raises(ReaderError, match="wrong shape: receiver_signature is missing"):
        reader.read("pod.jpg", jpeg())


@pytest.mark.parametrize(
    ("damage", "problem"),
    [
        (lambda r: [], "not an object"),
        (lambda r: r | {"addressed_to_reader": "no"}, "addressed_to_reader"),
        (lambda r: r | {"reference_numbers": [{"value": "X"}]}, "reference_numbers"),
        (lambda r: r | {"reference_numbers": "TC-1"}, "reference_numbers must be a list"),
        (lambda r: r | {"handwritten_notes": [{"text": 5, "kind": "routine"}]}, "handwritten"),
        (lambda r: r | {"problems": [1]}, "problems"),
        (lambda r: r | {"legibility": 3}, "legibility"),
        (lambda r: r | {"delivery_date": {**r["delivery_date"], "month": "11"}}, "delivery_date"),
        (lambda r: r | {"delivery_date": {**r["delivery_date"], "day": True}}, "delivery_date"),
        (lambda r: r | {"delivery_date": "11/14"}, "delivery_date"),
    ],
)
def test_every_malformed_answer_is_named(damage, problem):
    assert shape_problem(report()) == ""
    assert problem in shape_problem(damage(report()))


def test_an_error_never_carries_the_key_or_the_document(reader, api):
    api.say({"type": "error", "error": {"type": "overloaded_error", "message": "x" * 5000}},
            status=529)  # fmt: skip
    with pytest.raises(ReaderError) as caught:
        reader.read("pod.jpg", jpeg())
    text = str(caught.value)
    assert KEY not in text and len(text) < 260
    api.say(text="<html>bad gateway</html>", status=200)
    api.replies[-1] = httpx.Response(502, text="<html>bad gateway</html>")
    with pytest.raises(ReaderError) as caught:
        reader.read("pod.jpg", jpeg())
    assert str(caught.value) == "Anthropic 502"


# ------------------------------------------------- end to end, in the engine


def world_with(api: FakeAnthropic) -> World:
    return World(
        reader=lambda clock: ClaudeReader(KEY, clock, transport=httpx.MockTransport(api.handler))
    )


def model_report_for(load, **overrides) -> dict:
    return report(
        reference_numbers=[{"label": "Load number", "value": load.load_number, "certain": True}],
        delivery_date={
            "as_written": "11/10/2026",
            "month": 11,
            "day": 10,
            "year": 2026,
            "certain": True,
        },  # fmt: skip
        **overrides,
    )


def test_a_real_photo_is_read_checked_and_filed(api):
    world = world_with(api)
    load = world.make_load()
    world.tick()
    api.say(model_report_for(load))
    world.reply(load, "POD attached", [("IMG_4417.jpg", jpeg())])
    assert world.chase(load).state is ChaseState.CLOSED and len(world.tms.filed) == 1
    (read,) = [e.payload for e in world.store.evidence(load.id) if e.kind == "document.read"]
    assert read["reader"] == f"claude/claude-opus-5-5/{claude.PROMPT_VERSION}"
    assert read["confidence"] == CONFIDENCE["clean"]


def test_a_damage_note_the_model_reads_reaches_a_person_and_is_never_filed(api):
    world = world_with(api)
    load = world.make_load()
    world.tick()
    api.say(model_report_for(
        load, handwritten_notes=[{"text": "3 cartons wet", "kind": "exception"}]
    ))  # fmt: skip
    world.reply(load, "POD attached", [("pod.jpg", jpeg())])
    assert [e.reason for e in world.escalations(load)] == [EscalationReason.DOCUMENT_EXCEPTION]
    assert world.tms.filed == {}


def test_a_blurry_photo_gets_one_request_for_a_better_copy(api):
    world = world_with(api)
    load = world.make_load()
    world.tick()
    api.say(report(legibility="illegible", problems=["blurry"], reference_numbers=[]))
    world.reply(load, "POD attached", [("pod.jpg", jpeg())])
    chase = world.chase(load)
    assert chase.fail_reasons == ["unreadable"] and chase.reasks_sent == 1
    assert world.escalations(load) == []


def test_a_word_file_goes_to_a_person_with_the_reason_and_the_carrier_hears_nothing(api):
    world = world_with(api)
    load = world.make_load()
    world.tick()
    sent = len(world.email.sent)
    world.reply(load, "POD attached", [("pod.docx", b"PK\x03\x04" + b"\0" * 64)])
    assert api.requests == [] and len(world.email.sent) == sent
    assert [e.reason for e in world.escalations(load)] == [EscalationReason.LOW_CONFIDENCE]
    (document,) = world.store.find(Document, load_id=load.id)
    assert document.extraction["doubts"] == ["it is a zip or an Office file, not a photo or a PDF"]


def test_an_outage_delays_the_reading_and_then_it_is_read(api):
    world = world_with(api)
    load = world.make_load()
    world.tick()
    api.say({"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
            status=529)  # fmt: skip
    api.say(model_report_for(load))
    world.reply(load, "POD attached", [("pod.jpg", jpeg())])
    assert world.chase(load).state is ChaseState.RECEIVED
    world.clock.advance(60_000)
    world.settle()
    assert world.chase(load).state is ChaseState.CLOSED and len(api.requests) == 2
