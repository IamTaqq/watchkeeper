"""Public endpoint (Lambda Function URL) that jobs call when they finish.

    POST /ping/<check-name>         job succeeded
    POST /ping/<check-name>/fail    job failed (request body = error message)

Requests must carry the shared token:
    Authorization: Bearer <token>     or    ?token=<token>
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import time
from typing import Callable, Iterable

from watchkeeper import aws

MAX_MESSAGE_LENGTH = 500


def _response(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def _token_from(event: dict) -> str:
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (event.get("queryStringParameters") or {}).get("token", "")


def _body(event: dict) -> str:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8", errors="replace")
    return body.strip()[:MAX_MESSAGE_LENGTH]


def handle(
    event: dict,
    *,
    expected_token: str,
    heartbeat_names: Iterable[str],
    record: Callable[[str, str, str, float], None],
    now: Callable[[], float] = time.time,
) -> dict:
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    if method not in ("GET", "POST"):
        return _response(405, {"error": "method not allowed"})

    parts = [p for p in (event.get("rawPath") or "/").split("/") if p]
    if len(parts) not in (2, 3) or parts[0] != "ping" or (len(parts) == 3 and parts[2] != "fail"):
        return _response(404, {"error": "use /ping/<check-name> or /ping/<check-name>/fail"})

    if not expected_token or not hmac.compare_digest(_token_from(event), expected_token):
        return _response(401, {"error": "invalid token"})

    name = parts[1]
    if name not in set(heartbeat_names):
        return _response(404, {"error": f"no heartbeat check named '{name}'"})

    status = "fail" if len(parts) == 3 else "ok"
    message = _body(event) if status == "fail" else ""
    record(name, status, message, now())
    return _response(200, {"check": name, "status": status})


def handler(event, context):
    return handle(
        event,
        expected_token=aws.get_parameter(os.environ["TOKEN_PARAMETER"], ttl_seconds=300),
        heartbeat_names=[c.name for c in aws.load_checks() if c.type == "heartbeat"],
        record=aws.put_heartbeat,
    )
