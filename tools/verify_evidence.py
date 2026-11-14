#!/usr/bin/env python3
"""Verify a Talon Chase evidence export. Standard library only; no project code.

    python tools/verify_evidence.py export.json [--head EXPECTED_HEAD_HASH]

Exit 0 when every event verifies (and the head hash matches, if given), 1 otherwise.
The rules are written out in docs/EVIDENCE.md. This file is the reference check.
"""

import hashlib
import json
import sys

GENESIS = "0" * 64


def canonical(value) -> bytes:
    def check(v, path):
        if isinstance(v, float):
            raise ValueError(f"float at {path}")
        if isinstance(v, dict):
            for k, item in v.items():
                if not isinstance(k, str):
                    raise ValueError(f"non-string key at {path}")
                check(item, f"{path}.{k}")
        elif isinstance(v, list):
            for i, item in enumerate(v):
                check(item, f"{path}[{i}]")
        elif not (v is None or isinstance(v, (bool, int, str))):
            raise ValueError(f"unsupported type at {path}")

    check(value, "$")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify(events: list) -> tuple[bool, str, str]:
    """Returns (ok, head_hash, message)."""
    prev = GENESIS
    chain_id = events[0]["chain_id"] if events else ""
    for index, e in enumerate(events):
        where = f"event {index}"
        if e["chain_id"] != chain_id:
            return False, prev, f"{where}: chain_id differs"
        if e["seq"] != index:
            return False, prev, f"{where}: seq is {e['seq']}, expected {index}"
        if e["prev_hash"] != prev:
            return False, prev, f"{where}: prev_hash does not match the previous event"
        try:
            digest = sha(canonical(e["payload"]))
        except ValueError as exc:
            return False, prev, f"{where}: payload is not canonical ({exc})"
        if digest != e["payload_digest"]:
            return False, prev, f"{where}: payload does not match payload_digest"
        header = {
            "at_ms": e["at_ms"],
            "chain_id": e["chain_id"],
            "kind": e["kind"],
            "payload_digest": e["payload_digest"],
            "seq": e["seq"],
        }
        if sha(canonical(header) + prev.encode("ascii")) != e["hash"]:
            return False, prev, f"{where}: hash does not match the header"
        prev = e["hash"]
    return True, prev, f"{len(events)} events verified"


def main(argv: list) -> int:
    if len(argv) not in (2, 4) or (len(argv) == 4 and argv[2] != "--head"):
        print(__doc__)
        return 2
    with open(argv[1], encoding="utf-8") as handle:
        # Reject floats at parse time: an integer written as 1.0 is not canonical.
        export = json.load(
            handle, parse_float=lambda s: (_ for _ in ()).throw(ValueError(f"float {s}"))
        )
    events = export["events"] if isinstance(export, dict) else export
    ok, head, message = verify(events)
    print(("OK: " if ok else "FAILED: ") + message)
    print(f"head hash: {head}")
    if ok and len(argv) == 4 and argv[3] != head:
        print("FAILED: head hash differs from the one you expected (events removed from the end?)")
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
