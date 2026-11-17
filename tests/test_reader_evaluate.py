"""The evaluation scores what would have happened to each document, not how many
fields matched: it routes every file through the real checks twice, once from a
person's label and once from the reader's answer."""

from __future__ import annotations

import json

import pytest

from chase.domain.checks import Extraction
from chase.ports import ReaderError
from chase.reader import evaluate as ev
from chase.reader import samples
from chase.reader.files import prepare

NOON = 1794657600000  # 14 Nov 2026, 12:00 UTC
READ_ON = 1794830400000  # 16 Nov 2026, 12:00 UTC

LABEL = {
    "doc_type": "POD",
    "readable": True,
    "reference_numbers": ["TC-104417", "BOL5591207"],
    "signature_present": True,
    "delivery_date": "2026-11-14",
    "exception_notes": [],
    "page_count": 1,
}


def read(**overrides) -> Extraction:
    base = dict(
        doc_type="POD",
        readable=True,
        reference_numbers=["TC-104417", "BOL5591207"],
        signature_present=True,
        delivery_date_ms=NOON,
        page_count=1,
        exception_notes=[],
        confidence=95,
    )
    return Extraction.from_dict(base | overrides)


def row(label_changes: dict, **read_changes) -> ev.Row:
    return ev.score("x.jpg", LABEL | label_changes, read(**read_changes), 80, READ_ON)


def test_a_good_pod_read_right_is_filed_both_ways():
    r = row({})
    assert (r.wanted, r.got, r.verdict, r.fields_wrong) == ("file", "file", "right", [])


def test_filing_a_pod_whose_damage_note_was_missed_is_unsafe():
    r = row({"exception_notes": ["2 PLT crushed"]})
    assert (r.wanted, r.got, r.verdict) == ("person", "file", "unsafe")
    assert r.notes_wanted == 1 and not r.notes_reached_a_person
    assert r.fields_wrong == ["exception_notes"]


def test_seeing_a_signature_that_is_not_there_is_unsafe():
    r = row({"signature_present": False})
    assert (r.wanted, r.got, r.verdict) == ("ask again", "file", "unsafe")


def test_a_note_that_reached_a_person_counts_even_when_read_as_doubt():
    r = row({"exception_notes": ["short 1"]}, confidence=50, doubts=["handwriting"])
    assert r.verdict == "right" and r.notes_reached_a_person


def test_a_missed_note_on_a_pod_sent_back_to_the_carrier_is_still_a_missed_note():
    r = row({"exception_notes": ["refused"]}, signature_present=False)
    assert (r.got, r.verdict) == ("ask again", "wrong ask")
    assert not r.notes_reached_a_person


def test_sending_a_good_pod_to_a_person_is_extra_work_not_a_danger():
    r = row({}, confidence=60, doubts=["whether the receiver signed"])
    assert (r.wanted, r.got, r.verdict) == ("file", "person", "extra work")


def test_asking_again_for_a_good_pod_is_a_wrong_ask():
    r = row({}, signature_present=False)
    assert (r.wanted, r.got, r.verdict) == ("file", "ask again", "wrong ask")
    assert r.fields_wrong == ["signature_present"]


def test_a_document_marked_for_a_person_must_reach_one():
    assert row({"needs_person": True}).verdict == "unsafe"
    assert row({"needs_person": True}, confidence=0).verdict == "right"


def test_reference_numbers_match_whatever_their_case_and_spacing():
    assert row({}, reference_numbers=["tc-104417", "BOL 5591207"]).fields_wrong == []
    assert row({}, reference_numbers=["TC-104417"]).fields_wrong == ["reference_numbers"]


class Scripted:
    name = "scripted/v1"
    inline = False

    def __init__(self, answers: dict) -> None:
        self.answers = answers
        self.read_at: dict[str, int] = {}
        self.clock = None

    def read(self, filename: str, content: bytes) -> Extraction:
        self.read_at[filename] = self.clock.now_ms()
        answer = self.answers[filename]
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def folder(tmp_path):
    labels = {
        "a-good.jpg": LABEL,
        "b-damage.jpg": LABEL | {"exception_notes": ["2 PLT crushed"], "read_on": "2027-01-05"},
        "c-down.jpg": LABEL,
    }
    for name in labels:
        (tmp_path / name).write_bytes(b"not looked at by the scripted reader")
    (tmp_path / "labels.json").write_text(json.dumps(labels))
    return tmp_path


def test_a_run_adds_up_and_a_missed_note_makes_it_unsafe(folder):
    reader = Scripted(
        {"a-good.jpg": read(), "b-damage.jpg": read(), "c-down.jpg": ReaderError("529")}
    )
    clock = reader.clock = ev.DayClock(READ_ON)
    report = ev.evaluate(folder, reader, clock)
    assert [r.verdict for r in report.rows] == ["right", "unsafe", "error"]
    assert (report.notes_missed, report.with_notes, report.safe) == (1, 1, False)
    summary = report.to_dict()
    assert summary["unsafe"] == 1 and summary["errors"] == 1 and summary["documents"] == 3
    text = ev.render(report)
    assert "NOT SAFE" in text and "filed when it should not have been           1" in text
    assert "misread: exception_notes" in text and "529" in text
    # Each document is read as of its own day; without one, today.
    assert (
        reader.read_at["b-damage.jpg"] == 1799150400000 and reader.read_at["a-good.jpg"] == READ_ON
    )


def test_a_run_with_every_note_caught_is_safe(folder):
    reader = Scripted({
        "a-good.jpg": read(),
        "b-damage.jpg": read(exception_notes=["2 PLT crushed"]),
        "c-down.jpg": read(confidence=60),
    })  # fmt: skip
    clock = reader.clock = ev.DayClock(READ_ON)
    report = ev.evaluate(folder, reader, clock)
    assert [r.verdict for r in report.rows] == ["right", "right", "extra work"]
    assert report.safe and ev.render(report).endswith("SAFE")


def test_the_command_says_what_is_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CHASE_READER_MODEL", raising=False)
    assert ev.main([str(tmp_path)]) == 2
    assert "labels.json not found" in capsys.readouterr().out
    (tmp_path / "labels.json").write_text("{}")
    assert ev.main([str(tmp_path)]) == 2
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().out


# -------------------------------------------------------------- the samples


def test_the_drawn_samples_are_the_same_every_time_and_every_one_can_be_shown(tmp_path):
    names = samples.write(tmp_path)
    labels = json.loads((tmp_path / "labels.json").read_text())
    assert sorted(labels) == names and len(names) == len(samples.SAMPLES) >= 12
    for sample in samples.SAMPLES:
        name, content = samples.build(sample)
        assert samples.build(sample) == (name, content)  # seeded: no run differs
        prepared = prepare(content)
        assert prepared.page_count == labels[name]["page_count"]
    assert {name.rsplit(".", 1)[1] for name in names} == {"jpg", "heic", "pdf", "tif"}


def test_the_samples_cover_every_route_and_the_cases_that_must_reach_a_person(tmp_path):
    samples.write(tmp_path)
    labels = json.loads((tmp_path / "labels.json").read_text())
    truth = Scripted({name: ev._truth(label) for name, label in labels.items()})
    clock = truth.clock = ev.DayClock(READ_ON)
    report = ev.evaluate(tmp_path, truth, clock)
    wanted = {r.name.rsplit(".", 1)[0]: r.wanted for r in report.rows}
    assert set(wanted.values()) == {"file", "ask again", "person"}
    assert wanted["damage-noted"] == wanted["short-count"] == "person"
    assert wanted["note-aimed-at-the-reader"] == wanted["receiver-section-cut-off"] == "person"
    assert wanted["good-order-remark"] == wanted["date-without-year"] == "file"
    assert wanted["unsigned"] == wanted["blurry"] == wanted["lumper-receipt"] == "ask again"
    assert report.with_notes == 2
