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


def test_every_escalation_reason_has_screen_wording():
    ui = (ROOT / "docs" / "UI.md").read_text()
    missing = [r.value for r in EscalationReason if f"`{r.value}`" not in ui]
    assert missing == []
