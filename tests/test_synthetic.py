"""The synthetic evaluation must pass, and must be reproducible."""

from synthetic.evaluate import run
from synthetic.generate import CLASS_COUNTS, build_pack


def test_synthetic_evaluation_passes_and_is_deterministic():
    first = run()
    assert first["passed"], first["requirements"]
    assert first["loads"] == 200 and first["fixtures"] == 60
    assert {k: v["total"] for k, v in first["by_class"].items()} == CLASS_COUNTS
    assert first["exception_noted_filed"] == 0 and first["misses"] == []
    assert run() == first


def test_pack_is_seeded():
    a, b, c = build_pack(0), build_pack(0), build_pack(0, seed=7)
    assert [f.extraction for f in a.fixtures] == [f.extraction for f in b.fixtures]
    assert [x.rate_cents for x in a.loads] != [x.rate_cents for x in c.loads]
    assert len({f.load_id for f in a.fixtures}) == 60
