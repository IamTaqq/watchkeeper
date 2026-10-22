"""Real implementations of the side-effecting dependencies used in Lambda."""

from __future__ import annotations

import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

import boto3

from .checks import Context, HttpResponse
from .config import Check, parse_checks

USER_AGENT = "watchkeeper-monitor/0.1"

_ssm = None
_table = None
_cloudwatch = None
_cache: dict[str, tuple[float, Any]] = {}


def ssm():
    global _ssm
    if _ssm is None:
        _ssm = boto3.client("ssm")
    return _ssm


def table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["STATE_TABLE"])
    return _table


def cloudwatch():
    global _cloudwatch
    if _cloudwatch is None:
        _cloudwatch = boto3.client("cloudwatch")
    return _cloudwatch


def get_parameter(name: str, ttl_seconds: int = 60) -> str:
    """Read an SSM parameter (decrypting SecureStrings), cached per container."""
    hit = _cache.get(name)
    if hit and time.time() - hit[0] < ttl_seconds:
        return hit[1]
    value = ssm().get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
    _cache[name] = (time.time(), value)
    return value


def load_checks() -> list[Check]:
    return parse_checks(json.loads(get_parameter(os.environ["CONFIG_PARAMETER"])))


def http_get(url: str, timeout: int, headers: Optional[dict] = None) -> HttpResponse:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - URLs come from trusted config
            body = resp.read(1_000_000)
            status = resp.status
    except urllib.error.HTTPError as exc:
        body, status = exc.read(1_000_000), exc.code
    except (TimeoutError, socket.timeout) as exc:
        raise TimeoutError(f"no response within {timeout}s") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise TimeoutError(f"no response within {timeout}s") from exc
        raise ConnectionError(f"cannot connect: {exc.reason}") from exc
    return HttpResponse(status=status, body=body, elapsed_ms=(time.monotonic() - started) * 1000)


# --- state stored in DynamoDB -------------------------------------------------

def get_heartbeat(name: str) -> Optional[dict]:
    return table().get_item(Key={"pk": f"hb#{name}"}).get("Item")


def put_heartbeat(name: str, status: str, message: str, now: float) -> None:
    table().put_item(
        Item={"pk": f"hb#{name}", "last_ping": int(now), "status": status, "message": message}
    )


def register_heartbeat(name: str, now: float) -> None:
    """Create a 'pending' record only if none exists (a real ping may race us)."""
    try:
        table().put_item(
            Item={"pk": f"hb#{name}", "last_ping": int(now), "status": "pending", "message": ""},
            ConditionExpression="attribute_not_exists(pk)",
        )
    except table().meta.client.exceptions.ConditionalCheckFailedException:
        pass


def put_result(name: str, ok: bool, message: str, checked_at: float) -> None:
    table().put_item(
        Item={"pk": f"result#{name}", "ok": ok, "message": message, "checked_at": int(checked_at)}
    )


def get_result(name: str) -> Optional[dict]:
    return table().get_item(Key={"pk": f"result#{name}"}).get("Item")


def claim_triage(name: str, now: float, cooldown_seconds: int) -> bool:
    """Return True if we may investigate this check now (one triage per incident window).

    Alarm storms would otherwise start many paid investigations of the same problem.
    """
    try:
        table().put_item(
            Item={"pk": f"triage#{name}", "started_at": int(now), "expires_at": int(now + cooldown_seconds)},
            ConditionExpression="attribute_not_exists(pk) OR expires_at < :now",
            ExpressionAttributeValues={":now": int(now)},
        )
        return True
    except table().meta.client.exceptions.ConditionalCheckFailedException:
        return False


def save_triage(name: str, result: dict) -> None:
    """Keep the latest report (audit trail, and input for phase 6 approvals)."""
    table().update_item(
        Key={"pk": f"triage#{name}"},
        UpdateExpression="SET report_json = :r",
        ExpressionAttributeValues={":r": json.dumps(result, ensure_ascii=False, default=str)[:350_000]},
    )


# --- Postgres (read-only) -----------------------------------------------------

def run_sql(dsn: str, query: str, timeout: int) -> Any:
    """Run a single-value query in a READ ONLY transaction and return the first column."""
    import pg8000.native  # imported lazily: only needed for postgres checks

    url = urlparse(dsn)
    params = parse_qs(url.query)
    sslmode = params.get("sslmode", ["require"])[0]
    ssl_context: Any = None
    if sslmode != "disable":
        ssl_context = ssl.create_default_context()
        if sslmode in ("require", "prefer", "allow"):
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

    conn = pg8000.native.Connection(
        user=unquote(url.username or ""),
        password=unquote(url.password or ""),
        host=url.hostname,
        port=url.port or 5432,
        database=(url.path or "/postgres").lstrip("/"),
        ssl_context=ssl_context,
        timeout=timeout,
        application_name="watchkeeper-monitor",
    )
    try:
        # Guardrail: even if the DB user had write access, this transaction cannot
        # write. Transaction-scoped settings (not session-wide SET) are also safe
        # behind connection poolers such as Supabase's, where sessions are shared.
        conn.run("BEGIN TRANSACTION READ ONLY")
        conn.run(f"SET LOCAL statement_timeout = {int(timeout) * 1000}")
        rows = conn.run(query)
        return rows[0][0] if rows else None
    except pg8000.native.DatabaseError as exc:
        detail = exc.args[0] if exc.args else {}
        message = detail.get("M", str(exc)) if isinstance(detail, dict) else str(exc)
        raise RuntimeError(f"SQL error: {message}") from None
    finally:
        try:
            conn.run("ROLLBACK")
        except Exception:  # noqa: BLE001 - never hide the original error
            pass
        conn.close()


def context() -> Context:
    return Context(
        http_get=http_get,
        now=time.time,
        get_heartbeat=get_heartbeat,
        register_heartbeat=register_heartbeat,
        get_secret=lambda name: get_parameter(name, ttl_seconds=300),
        run_sql=run_sql,
    )
