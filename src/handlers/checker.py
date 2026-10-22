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

from watchkeeper import aws
from watchkeeper.checks import CheckResult, run_check

MAX_WORKERS = 8


def metric_data(results: list[CheckResult]) -> list[dict]:
    data = []
    for r in results:
        dims = [{"Name": "Check", "Value": r.name}]
        data.append({"MetricName": "CheckSuccess", "Dimensions": dims, "Value": 1.0 if r.ok else 0.0, "Unit": "None"})
        data.append({"MetricName": "LatencyMs", "Dimensions": dims, "Value": round(r.latency_ms, 1), "Unit": "Milliseconds"})
    return data


def publish(results: list[CheckResult], namespace: str) -> None:
    data = metric_data(results)
    for i in range(0, len(data), 20):
        aws.cloudwatch().put_metric_data(Namespace=namespace, MetricData=data[i : i + 20])


def handler(event, context):
    checks = aws.load_checks()
    ctx = aws.context()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(lambda c: run_check(c, ctx), checks))

    now = time.time()
    for r in results:
        aws.put_result(r.name, r.ok, r.message, now)
        # One structured log line per check: easy to query with Logs Insights.
        print(json.dumps({
            "check": r.name,
            "status": "PASS" if r.ok else "FAIL",
            "latency_ms": round(r.latency_ms, 1),
            "message": r.message,
        }))

    publish(results, os.environ.get("METRIC_NAMESPACE", "Watchkeeper"))

    failing = [r.name for r in results if not r.ok]
    return {"checked": len(results), "failing": failing}
