"""Canonical JSON used for every hash in the evidence chain.

Rules (see docs/EVIDENCE.md):
  * UTF-8 bytes, object keys sorted by Unicode code point
  * no insignificant whitespace
  * allowed values: object (string keys), array, string, integer, true, false, null
  * floats are rejected, so a time is always integer epoch milliseconds
  * non-ASCII characters are written as themselves, not as \\u escapes
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


class CanonicalError(ValueError):
    """Raised when a value cannot be written as canonical JSON."""


def _check(value: Any, path: str) -> None:
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        raise CanonicalError(f"float is not allowed at {path}; use an integer")
    if isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            _check(item, f"{path}[{i}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                kind = type(key).__name__
                raise CanonicalError(f"object key at {path} must be a string, got {kind}")
            _check(item, f"{path}.{key}")
        return
    raise CanonicalError(f"type {type(value).__name__} is not allowed at {path}")


def canonical_json(value: Any) -> bytes:
    """Return the canonical UTF-8 encoding of ``value``."""
    _check(value, "$")
    text = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return text.encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
