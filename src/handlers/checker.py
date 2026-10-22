"""Scheduled Lambda: run every check, publish metrics, remember the last result.

The checker does not decide when to alert. It reports CheckSuccess (1 or 0)
per check; CloudWatch Alarms decide, which keeps alerting logic declarative
and lets "the checker stopped running" itself trigger an alarm (missing data
is treated as breaching).
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

from typing import Callable, Optional

from watchkeeper import aws
from watchkeeper.checks import CheckResult, run_check
from watchkeeper.config import Check

MAX_WORKERS = 8


def metric_data(results: list[CheckResult]) -> list[dict]:
    data = []
    for r in results:
        dims = [{"Name": "Check", "Value": r.name}]
        # CheckSuccess every run, even for cached results, so alarms never see missing data.
        data.append({"MetricName": "CheckSuccess", "Dimensions": dims, "Value": 1.0 if r.ok else 0.0, "Unit": "None"})
        if not r.cached:
            data.append({"MetricName": "LatencyMs", "Dimensions": dims, "Value": round(r.latency_ms, 1), "Unit": "Milliseconds"})
    return data


def split_due(checks: list[Check], get_result: Callable[[str], Optional[dict]], now: float):
    """Separate checks that must run now from those whose last result is still fresh enough.

    Only passing results are reused. A failing check runs again every time, so a
    one-off blip clears on the next run instead of being repeated for an hour
    (which would defeat failure_threshold).
    """
    due, reused = [], []
    for c in checks:
        last = get_result(c.name) if c.check_every_seconds else None
        if last and last.get("ok") and now - float(last.get("checked_at", 0)) < c.check_every_seconds:
            reused.append(CheckResult(c.name, bool(last.get("ok")), str(last.get("message", "")), cached=True))
        else:
            due.append(c)
    return due, reused


def publish(results: list[CheckResult], namespace: str) -> None:
    data = metric_data(results)
    for i in range(0, len(data), 20):
        aws.cloudwatch().put_metric_data(Namespace=namespace, MetricData=data[i : i + 20])


def handler(event, context):
    checks = aws.load_checks()
    ctx = aws.context()
    due, reused = split_due(checks, aws.get_result, time.time())

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        fresh = list(pool.map(lambda c: run_check(c, ctx), due))

    now = time.time()
    for r in fresh:
        aws.put_result(r.name, r.ok, r.message, now)
    results = fresh + reused
    for r in results:
        # One structured log line per check: easy to query with Logs Insights.
        print(json.dumps({
            "check": r.name,
            "status": "PASS" if r.ok else "FAIL",
            "cached": r.cached,
            "latency_ms": round(r.latency_ms, 1),
            "message": r.message,
        }))

    publish(results, os.environ.get("METRIC_NAMESPACE", "Watchkeeper"))

    failing = [r.name for r in results if not r.ok]
    return {"checked": len(results), "failing": failing}
