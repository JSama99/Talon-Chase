"""Print a sign-in link for an operator, for the first owner or when email is down.

    CHASE_SECRET=... DATABASE_URL=... CHASE_BASE_URL=https://chase.example \\
        python -m chase.app.link op-jermaine

The link works once, for 15 minutes, exactly like an emailed one.
"""

from __future__ import annotations

import argparse
import os
import sys

from chase.app.auth import Signer, make_link_token
from chase.domain.clock import SystemClock
from chase.domain.models import Operator
from chase.store.sql import SqlStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("operator_id")
    args = parser.parse_args(argv)
    secret = os.environ.get("CHASE_SECRET", "")
    if len(secret) < 32:
        print("CHASE_SECRET must be set to at least 32 characters", file=sys.stderr)
        return 2
    store = SqlStore(os.environ.get("DATABASE_URL", "sqlite+pysqlite:///chase.db"))
    operator = store.get(Operator, args.operator_id)
    if operator is None or not operator.active:
        print(f"no active operator {args.operator_id}", file=sys.stderr)
        return 1
    base = os.environ.get("CHASE_BASE_URL", "http://localhost:8000").rstrip("/")
    token = make_link_token(Signer(secret.encode()), operator.id, SystemClock().now_ms())
    print(f"{base}/app/login/{token}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
