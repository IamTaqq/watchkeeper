"""Run the checks in checks.yaml from your own machine, without AWS.

    python scripts/run_local.py               # uses ./checks.yaml
    python scripts/run_local.py other.yaml

Heartbeat checks have no real state locally, so they only show as
"waiting for the first heartbeat". Postgres checks read the DSN from an
environment variable named after the parameter, e.g. /watchkeeper/dsn/app ->
WATCHKEEPER_DSN_APP.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from watchkeeper import aws  # noqa: E402
from watchkeeper.checks import Context, run_check  # noqa: E402
from watchkeeper.config import parse_checks  # noqa: E402


def env_secret(parameter: str) -> str:
    key = re.sub(r"[^A-Z0-9]+", "_", parameter.upper()).strip("_")
    if key not in os.environ:
        raise KeyError(f"set {key} to test {parameter} locally")
    return os.environ[key]


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "checks.yaml")
    checks = parse_checks(yaml.safe_load(path.read_text()))
    if not checks:
        print(f"{path} has no checks yet. Uncomment an example and try again.")
        return 0

    heartbeats: dict = {}
    ctx = Context(
        http_get=aws.http_get,
        now=time.time,
        get_heartbeat=heartbeats.get,
        register_heartbeat=lambda name, now: heartbeats.setdefault(name, {"last_ping": now, "status": "pending"}),
        get_secret=env_secret,
        run_sql=aws.run_sql,
    )

    failed = 0
    for check in checks:
        result = run_check(check, ctx)
        failed += not result.ok
        print(f"{'PASS' if result.ok else 'FAIL'}  {check.name:<28} {result.latency_ms:>7.0f} ms  {result.message}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
