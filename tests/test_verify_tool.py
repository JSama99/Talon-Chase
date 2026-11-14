"""The standalone verifier shares no code with the package and must agree with it."""

import json
import subprocess
import sys

from tests.conftest import World


def run_tool(*args):
    return subprocess.run(
        [sys.executable, "tools/verify_evidence.py", *args], capture_output=True, text=True
    )


def test_standalone_verifier_agrees_and_detects_tamper(tmp_path):
    world = World()
    load = world.make_load(consignee_name="Entrepôt Montréal")
    world.engine.tick()
    world.send_pod(load, signature_present=False)
    world.send_pod(load)
    export = world.engine.export_evidence(load.id)
    head = export["verification"]["head_hash"]

    good = tmp_path / "good.json"
    good.write_text(json.dumps(export, ensure_ascii=False), encoding="utf-8")
    result = run_tool(str(good), "--head", head)
    assert result.returncode == 0 and head in result.stdout

    export["events"][6]["payload"]["forged"] = True
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(export))
    result = run_tool(str(bad))
    assert result.returncode == 1 and "event 6" in result.stdout

    export = world.engine.export_evidence(load.id)
    export["events"] = export["events"][:-2]
    short = tmp_path / "short.json"
    short.write_text(json.dumps(export))
    assert run_tool(str(short)).returncode == 0  # a shortened chain is still a valid chain
    assert run_tool(str(short), "--head", head).returncode == 1  # which is why the head matters
