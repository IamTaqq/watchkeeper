"""Practice target: a tiny web app you can break on purpose.

Set the fault mode with SSM (see `make fault MODE=...`):
    none        healthy
    error       /health returns 500
    slow        /health sleeps longer than the check timeout
    stale       /status reports data that stopped updating an hour ago
    down        every route returns 503
    cron_fail   the demo cron job reports a failure (app itself stays healthy)
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

from watchkeeper import aws

SLOW_SECONDS = 12

log = logging.getLogger()
log.setLevel(logging.INFO)


def _json(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def _fault_mode() -> str:
    try:
        return aws.get_parameter(os.environ["FAULT_PARAMETER"], ttl_seconds=10)
    except Exception:  # noqa: BLE001 - a missing parameter just means "healthy"
        return "none"


def respond(path: str, mode: str, now: float) -> dict:
    if mode == "down":
        return _json(503, {"error": "service unavailable (demo fault: down)"})
    if path == "/health":
        if mode == "error":
            return _json(500, {"ok": False, "error": "demo fault: error"})
        if mode == "slow":
            time.sleep(SLOW_SECONDS)
        return _json(200, {"ok": True})
    if path == "/status":
        updated = now - 3600 if mode == "stale" else now
        return _json(200, {"last_updated": datetime.fromtimestamp(updated, tz=timezone.utc).isoformat()})
    return _json(404, {"error": "try /health or /status"})


def handler(event, context):
    path = event.get("rawPath") or "/"
    mode = _fault_mode()
    response = respond(path, mode, time.time())
    if response["statusCode"] >= 500:
        # A real app would log its errors; this gives the triage agent something to find.
        log.error("request failed path=%s status=%s fault_mode=%s", path, response["statusCode"], mode)
    elif mode != "none":
        log.warning("serving with demo fault active path=%s fault_mode=%s", path, mode)
    return response
