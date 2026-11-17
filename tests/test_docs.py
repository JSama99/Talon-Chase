"""Documentation cannot drift from the code: every evidence kind and every
escalation reason the code can produce must be described."""

import pathlib
import re

from chase.domain.models import EscalationReason

ROOT = pathlib.Path(__file__).resolve().parent.parent
# self._record(<chain>, "kind.name", ...) where <chain> may contain one call, f"broker:{x}"
PATTERN = r'_record\(\s*[^,()]+(?:\([^()]*\))?,\s*"([a-z_]+\.[a-z_]+)"'


def emitted_kinds() -> set[str]:
    kinds = set()
    for path in (ROOT / "chase").rglob("*.py"):
        text = path.read_text()
        kinds |= set(
            re.findall(r'_record\(\s*[^,()]+(?:\([^()]*\))?,\s*"([a-z_]+\.[a-z_]+)"', text)
        )
    return kinds


def test_every_evidence_kind_is_documented():
    kinds = emitted_kinds()
    assert len(kinds) >= 35, sorted(kinds)  # the pattern still finds them
    spec = (ROOT / "docs" / "EVIDENCE.md").read_text()
    missing = sorted(k for k in kinds if f"`{k}`" not in spec)
    assert missing == []


def test_the_ontology_declares_exactly_the_kinds_the_code_records():
    from chase.ontology.events import EVENT_PAYLOADS

    assert emitted_kinds() == set(EVENT_PAYLOADS)


def test_every_escalation_reason_has_screen_wording():
    ui = (ROOT / "docs" / "UI.md").read_text()
    missing = [r.value for r in EscalationReason if f"`{r.value}`" not in ui]
    assert missing == []


def test_agents_md_lists_every_setting_the_service_reads():
    """AGENTS.md is how coding agents learn the app; its settings table must not drift."""
    code = "".join(path.read_text() for path in (ROOT / "chase").rglob("*.py"))
    read = set(re.findall(r'"((?:CHASE|RESEND|RENDER|ANTHROPIC)_[A-Z_]+|DATABASE_URL)"', code))
    assert len(read) >= 10, sorted(read)  # the pattern still finds them
    agents = (ROOT / "AGENTS.md").read_text()
    missing = sorted(key for key in read if f"| `{key}` |" not in agents)
    assert missing == []


def test_agents_md_points_only_at_files_that_exist():
    agents = (ROOT / "AGENTS.md").read_text()
    paths = set(re.findall(r"\]\(([^)#]+)\)", agents))
    paths |= {
        p.rstrip("/")
        for p in re.findall(
            r"`((?:chase|tests|migrations|docs|tools|synthetic|ontology)/[\w./]*)`", agents
        )
        if "<" not in p and "*" not in p
    }
    assert len(paths) >= 20, sorted(paths)
    missing = sorted(p for p in paths if not (ROOT / p).exists())
    assert missing == []
