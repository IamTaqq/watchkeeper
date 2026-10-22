"""Practice target: a scheduled job that reports to the heartbeat endpoint.

Pause it (`make pause-cron`) and the heartbeat check goes overdue.
Set fault mode `cron_fail` and it reports an explicit failure instead.
"""

from __future__ import annotations

import os
import urllib.request

from watchkeeper import aws


def handler(event, context):
    fail = aws.get_parameter(os.environ["FAULT_PARAMETER"], ttl_seconds=10) == "cron_fail"
    url = os.environ["HEARTBEAT_URL"].rstrip("/") + f"/ping/{os.environ['CHECK_NAME']}" + ("/fail" if fail else "")
    token = aws.get_parameter(os.environ["TOKEN_PARAMETER"], ttl_seconds=300)

    # The "job" itself would run here. We only simulate success or failure.
    body = b"demo job failed: simulated error" if fail else b""
    req = urllib.request.Request(url, data=body, method="POST", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - our own endpoint
        return {"status": resp.status, "reported": "fail" if fail else "ok"}
