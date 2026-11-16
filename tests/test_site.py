"""The landing page (site/) may not say more than the product does.

Three things are held here: the recorded chases on the page are what the engine does
today; every statement a reader could rely on is listed in site/claims.json with a
test or a document behind it; and the page cannot be published with a dead form.
"""

import json
import pathlib
import re
import subprocess
import sys
from html.parser import HTMLParser

import pytest

from chase.domain.models import BrokerConfig

ROOT = pathlib.Path(__file__).resolve().parent.parent
SITE = ROOT / "site"


def _load_tool():
    import importlib.util

    spec = importlib.util.spec_from_file_location("site_tool", ROOT / "tools" / "site.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = _load_tool()
PAGE = (SITE / "index.html").read_text()
CSS = (SITE / "site.css").read_text()
CLAIMS = {k: v for k, v in json.loads((SITE / "claims.json").read_text()).items() if k[0] != "_"}


class Reader(HTMLParser):
    """Collects what the tests ask about: ids, claims, text, headings, fields, links."""

    VOID = {"meta", "link", "input", "img", "br", "source", "option"}

    def __init__(self):
        super().__init__()
        self.ids, self.claims, self.h1, self.text = [], [], 0, []
        self.inputs, self.labels_for, self.images, self.refs, self.inline = [], set(), [], [], []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag not in self.VOID:
            self.stack.append(tag)
        if "id" in a:
            self.ids.append(a["id"])
        if "data-claim" in a:
            self.claims.append(a["data-claim"])
        if tag == "h1":
            self.h1 += 1
        if tag in ("input", "select", "textarea"):
            self.inputs.append(a)
        if tag == "label" and "for" in a:
            self.labels_for.add(a["for"])
        if tag == "img":
            self.images.append(a)
        for key in ("href", "src", "action"):
            if key in a:
                self.refs.append((tag, key, a[key]))
        if "style" in a or any(k.startswith("on") for k in a):
            self.inline.append((tag, a))
        if tag == "style" or (tag == "script" and "src" not in a):
            self.inline.append((tag, a))

    def handle_endtag(self, tag):
        if tag in self.stack:
            while self.stack.pop() != tag:
                pass

    def handle_data(self, data):
        if "script" not in self.stack and "style" not in self.stack:
            self.text.append(data)


def read(page: str = PAGE) -> Reader:
    reader = Reader()
    reader.feed(page)
    return reader


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text)


# ------------------------------------------------------------ the recordings


def test_the_recorded_chases_are_what_the_engine_does_today():
    """Change the engine, the schedule or the timeline wording and this fails until
    `python tools/site.py runs` records the page again."""
    assert tool.render_page(PAGE) == PAGE


def test_every_choice_on_the_page_has_a_recording_and_a_rule_to_show_it():
    page = read()
    for key in tool.DELIVERIES:
        assert f"d-{key}" in page.ids
        assert f".sim:has(#d-{key}:checked) .run:not(.d-{key})" in CSS
    for key in tool.CARRIERS:
        assert f"c-{key}" in page.ids
        assert f".sim:has(#c-{key}:checked) .run:not(.c-{key})" in CSS
    assert set(tool.ORDER) == set(tool.CARRIERS)
    runs = re.findall(r'<article class="run d-([a-z-]+) c-([a-z]+)( is-default)?"', PAGE)
    assert len(runs) == len(tool.DELIVERIES) * len(tool.CARRIERS) == len(set(runs))
    assert [r[:2] for r in runs if r[2]] == [tool.DEFAULT]


def test_recordings_end_the_only_two_ways_a_chase_can():
    expected = {
        "silent": "person", "promise": "filed", "clean": "filed",
        "unsigned": "filed", "damage": "person", "bank": "person",
    }  # fmt: skip
    for delivery in tool.DELIVERIES:
        for carrier, outcome in expected.items():
            run = tool.record(delivery, carrier)
            assert run["outcome"] == outcome, (delivery, carrier)
            assert run["chain_verifies"] is True
    silent = tool.record("fri-afternoon", "silent")
    asked = [e["when"] for e in silent["entries"] if e["title"].startswith("Asked")]
    assert asked == ["Fri 4:30 pm", "Mon 8:00 am", "Mon 12:00 pm"]  # the weekend is skipped
    night = tool.record("tue-night", "silent")
    assert next(e["when"] for e in night["entries"] if e["title"].startswith("Asked")) == (
        "Wed 7:00 am"
    )


def test_the_recorded_email_is_the_real_template_and_carries_no_load_details():
    run = tool.record(*tool.DEFAULT)
    shown = tool.render_email(run)
    assert "TC-300001" in shown and "(one-time link)" in shown
    assert "/u/ZGV" not in PAGE  # no upload token on a public page
    for secret in ("Acme Foods", "PU-77812", "1,850", "185000"):
        assert secret not in PAGE


def test_the_hours_on_the_page_are_the_default_schedule():
    cfg = BrokerConfig()
    assert (cfg.quiet_start_hour, cfg.quiet_end_hour, cfg.morning_hour) == (20, 7, 8)
    text = squash(" ".join(read().text))
    assert "between 8 pm and 7 am" in text and "asked about at 7 the next morning" in text


# ------------------------------------------------------------------ the claims


def test_every_claim_on_the_page_is_listed_and_every_listed_claim_is_on_the_page():
    on_page = read().claims
    assert len(on_page) == len(set(on_page))
    assert set(on_page) == set(CLAIMS)


def _test_exists(ref: str) -> bool:
    path, name = ref.split("::")
    return bool(re.search(rf"^def {re.escape(name)}\(", (ROOT / path).read_text(), re.MULTILINE))


@pytest.mark.parametrize("claim", sorted(CLAIMS))
def test_each_claim_is_still_backed(claim):
    backing = CLAIMS[claim]
    assert backing, claim
    for item in backing:
        if "test" in item:
            assert _test_exists(item["test"]), item["test"]
        elif "text" in item:
            assert item["has"] in squash((ROOT / item["text"]).read_text()), item
        else:
            assert item.get("owner"), item


def test_owner_promises_are_named_in_the_guide():
    guide = squash((ROOT / "docs" / "SITE.md").read_text())
    for claim, backing in CLAIMS.items():
        if any("owner" in item for item in backing):
            assert f"`{claim}`" in guide, claim


def test_the_page_says_what_stage_it_is_at_before_anything_else():
    text = squash(" ".join(read().text))
    assert "not yet used by a broker" in squash((ROOT / "README.md").read_text())
    assert text.index("Not yet used by a brokerage") < text.index("Stop chasing carriers")


def test_what_the_readme_says_is_not_built_is_not_sold_as_built():
    readme = squash((ROOT / "README.md").read_text())
    not_built = readme[readme.index("## What is not built") : readme.index("## Next steps")]
    page = squash(" ".join(read().text))
    built = page[page.index("Working now") : page.index("Not built")]
    for words in ("text line", "language model", "TMS integration", "Public holidays"):
        assert words in not_built
    for word in ("TMS", "text message", "holiday", "reads the POD", "reading"):
        assert word.lower() not in built.lower(), word


def test_no_invented_proof():
    text = squash(" ".join(read().text)).lower()
    for phrase in (
        "trusted by", "testimonial", "our customers", "customers say", "case study",
        "% of", "accuracy", "guarantee", "award", "as seen", "save up to", "roi",
        "ai-powered", "revolution",
    ):  # fmt: skip
        assert not re.search(rf"\b{re.escape(phrase)}\b" if phrase[0] != "%" else re.escape(phrase),
                             text), phrase  # fmt: skip
    assert "%" not in text and "$" not in text


# ------------------------------------------------------------------- the page


def test_the_page_is_sound():
    page = read()
    assert page.h1 == 1
    assert len(page.ids) == len(set(page.ids))
    for a in page.inputs:
        if a.get("name") == "_gotcha" or a.get("type") == "radio":
            continue
        assert a["id"] in page.labels_for, a
    for a in page.inputs:
        if a.get("type") == "radio":
            assert a["id"] in page.labels_for
    for image in page.images:
        assert image.get("alt") and image.get("width") and image.get("height")
    for _tag, _, target in page.refs:
        if target.startswith("#"):
            assert target[1:] in page.ids, target


def test_the_page_loads_nothing_from_anyone_else_and_runs_no_inline_code():
    page = read()
    assert page.inline == []
    for tag, key, target in page.refs:
        if tag == "a" or key == "action":
            continue
        assert not re.match(r"^(?:[a-z]+:)?//", target), target
        assert (SITE / target).is_file(), target
    for name in re.findall(r"img/[\w.-]+", PAGE) + re.findall(r'url\("([^"]+)"\)', CSS):
        assert (SITE / name).is_file() and name in tool.SHIPPED, name
    policy = re.search(r'Content-Security-Policy" content="([^"]+)"', PAGE).group(1)
    assert "default-src 'none'" in policy and "unsafe" not in policy
    assert "@import" not in CSS and "http" not in CSS


def test_the_page_is_whole_without_scripts_or_motion():
    """Nothing is parked invisible waiting for a script or a timeline."""
    assert "opacity: 0;" not in re.sub(r"@keyframes[^}]+\{[^}]+\}[^}]*\}", "", CSS)
    motion = CSS[CSS.index("@media (prefers-reduced-motion: no-preference)") :]
    assert CSS.count("animation:") == motion.count("animation:")  # all motion is opt-out
    assert "@supports (animation-timeline: view())" in motion
    assert "@supports not selector(:has(a))" in CSS
    assert 'method="post"' in PAGE and "<noscript" not in PAGE


def _luminance(colour: str) -> float:
    parts = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = (c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in parts)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_every_text_colour_clears_aa_on_every_surface_it_sits_on():
    token = dict(re.findall(r"--(\w+): (#[0-9a-f]{6});", CSS))
    pairs = [
        ("ink", "paper"), ("ink", "wash"), ("ink", "yellow"), ("paper", "ink"),
        ("muted", "paper"), ("muted", "wash"), ("green", "paper"), ("red", "paper"),
    ]  # fmt: skip
    for text, surface in pairs:
        assert _contrast(token[text], token[surface]) >= 4.5, (text, surface)
    assert _contrast("#8a8a8a", token["paper"]) >= 3  # control borders, the neutral dot


def test_the_screenshots_are_listed_with_how_to_retake_them():
    guide = (ROOT / "docs" / "SITE.md").read_text()
    for name in tool.SHIPPED:
        if name.startswith("img/"):
            assert (SITE / name).stat().st_size < 120_000, name
            assert name.split("/")[1] in guide, name
    assert "python -m chase.app.preview" in guide


# ------------------------------------------------------------------ the build


def test_the_build_refuses_to_publish_a_dead_form(tmp_path):
    for bad in ("", "http://formspree.io/f/x", "https://example.invalid/x", "not a url",
                'https://a.co/"><script>'):  # fmt: skip
        with pytest.raises(SystemExit):
            tool.build(bad, tmp_path / "out")
    assert tool.FORM_PLACEHOLDER in PAGE  # the repo never holds a real address


def test_the_build_writes_the_form_address_and_only_the_listed_files(tmp_path):
    out = tool.build("https://forms.example.com/f/abc123", tmp_path / "out")
    built = (out / "index.html").read_text()
    assert 'action="https://forms.example.com/f/abc123"' in built
    assert "form-action https://forms.example.com;" in built
    assert "connect-src https://forms.example.com;" in built
    assert "invalid" not in built
    shipped = sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file())
    assert shipped == sorted(tool.SHIPPED) and "claims.json" not in shipped


def test_the_one_file_preview_needs_nothing_else(tmp_path):
    out = tool.build("https://forms.example.com/f/abc123", tmp_path / "out", inline=True)
    assert [p.name for p in out.iterdir()] == ["index.html"]
    page = (out / "index.html").read_text()
    assert "<style>" in page and "font/woff2;base64," in page and "image/webp;base64," in page


def test_the_command_line_checks_and_builds(tmp_path):
    run = lambda *args: subprocess.run(  # noqa: E731
        [sys.executable, str(ROOT / "tools" / "site.py"), *args], capture_output=True, text=True
    )
    assert run("runs", "--check").returncode == 0
    assert run("build", "--out", str(tmp_path / "x")).returncode != 0
    done = run("build", "--form-url", "https://forms.example.com/f/a", "--out", str(tmp_path / "y"))
    assert done.returncode == 0 and (tmp_path / "y" / "index.html").is_file()


def test_the_form_script_is_the_only_script_and_keeps_what_was_typed():
    script = (SITE / "site.js").read_text()
    assert re.findall(r'<script[^>]*src="([^"]+)"', PAGE) == ["site.js"]
    assert "form.action" in script and "http" not in script  # one address, set by the build
    failure = script[script.index(".catch(") :]
    assert "form.reset()" not in failure and "err.hidden = false" in failure
    for forbidden in ("innerHTML", "eval(", "document.write", "localStorage", "cookie"):
        assert forbidden not in script
