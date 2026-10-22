"""Check evaluators.

Every evaluator is a pure function of (check, context). The context bundles
everything that touches the outside world (HTTP, clock, DynamoDB, SSM,
Postgres), so the logic can be unit-tested with fakes.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .config import Check

MAX_MESSAGE_LENGTH = 500


@dataclass
class HttpResponse:
    status: int
    body: bytes
    elapsed_ms: float


@dataclass
class CheckResult:
    name: str
    ok: bool
    message: str
    latency_ms: float = 0.0
    cached: bool = False  # reused from an earlier run (see Check.check_every_seconds)


@dataclass
class Context:
    """Side-effecting dependencies, injected so tests can replace them."""

    http_get: Callable[..., HttpResponse]  # (url, timeout, headers=None)
    now: Callable[[], float]
    get_heartbeat: Callable[[str], Optional[dict]]
    register_heartbeat: Callable[[str, float], None]
    get_secret: Callable[[str], str]
    run_sql: Callable[[str, str, int], Any]


def _truncate(text: str) -> str:
    return text if len(text) <= MAX_MESSAGE_LENGTH else text[: MAX_MESSAGE_LENGTH - 3] + "..."


def check_http(check: Check, ctx: Context) -> CheckResult:
    p = check.params
    resp = ctx.http_get(p["url"], p["timeout_seconds"])
    if resp.status != p["expected_status"]:
        return CheckResult(check.name, False, f"HTTP {resp.status} (expected {p['expected_status']})", resp.elapsed_ms)
    if "max_latency_ms" in p and resp.elapsed_ms > p["max_latency_ms"]:
        return CheckResult(
            check.name, False, f"slow response: {resp.elapsed_ms:.0f} ms (limit {p['max_latency_ms']} ms)", resp.elapsed_ms
        )
    if "contains" in p and p["contains"] not in resp.body.decode("utf-8", errors="replace"):
        return CheckResult(check.name, False, f"response body does not contain '{p['contains']}'", resp.elapsed_ms)
    return CheckResult(check.name, True, f"HTTP {resp.status} in {resp.elapsed_ms:.0f} ms", resp.elapsed_ms)


def check_heartbeat(check: Check, ctx: Context) -> CheckResult:
    p = check.params
    beat = ctx.get_heartbeat(check.name)
    if not beat:
        # First time we see this check: start the clock now, so a newly added
        # job gets one full interval to report before it counts as missing.
        ctx.register_heartbeat(check.name, ctx.now())
        return CheckResult(check.name, True, "monitoring started; waiting for the first heartbeat")

    age = ctx.now() - float(beat["last_ping"])
    deadline = p["interval_seconds"] + p["grace_seconds"]
    status = beat.get("status")
    if status == "fail":
        reason = beat.get("message") or "no details"
        return CheckResult(check.name, False, f"job reported failure {age:.0f}s ago: {reason}")
    if age > deadline:
        if status == "pending":
            return CheckResult(check.name, False, f"no heartbeat received since monitoring started {age:.0f}s ago")
        return CheckResult(check.name, False, f"last heartbeat {age:.0f}s ago (expected every {p['interval_seconds']}s)")
    if status == "pending":
        return CheckResult(check.name, True, "waiting for the first heartbeat")
    return CheckResult(check.name, True, f"last heartbeat {age:.0f}s ago")


def _dig(data: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(data, list):
            data = data[int(part)]
        elif isinstance(data, dict) and part in data:
            data = data[part]
        else:
            raise KeyError(f"field '{path}' not found")
    return data


def parse_timestamp(value: Any) -> float:
    """Accept epoch seconds/milliseconds or an ISO-8601 string; return epoch seconds."""
    if isinstance(value, bool):
        raise ValueError("boolean is not a timestamp")
    if isinstance(value, (int, float)):
        return value / 1000.0 if value > 1e12 else float(value)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def check_freshness(check: Check, ctx: Context) -> CheckResult:
    p = check.params
    resp = ctx.http_get(p["url"], p["timeout_seconds"])
    if resp.status != 200:
        return CheckResult(check.name, False, f"HTTP {resp.status} while reading {p['field']}", resp.elapsed_ms)
    payload = json.loads(resp.body)
    age = ctx.now() - parse_timestamp(_dig(payload, p["field"]))
    if age > p["max_age_seconds"]:
        return CheckResult(
            check.name, False, f"data is stale: '{p['field']}' is {age:.0f}s old (limit {p['max_age_seconds']}s)", resp.elapsed_ms
        )
    return CheckResult(check.name, True, f"'{p['field']}' is {age:.0f}s old", resp.elapsed_ms)


def check_postgres(check: Check, ctx: Context) -> CheckResult:
    p = check.params
    started = ctx.now()
    dsn = ctx.get_secret(p["dsn_parameter"])
    value = ctx.run_sql(dsn, p["query"], p["timeout_seconds"])
    latency = (ctx.now() - started) * 1000
    if value is None:
        value = 0
    if float(value) > p["max_value"]:
        return CheckResult(check.name, False, f"query returned {value} (limit {p['max_value']})", latency)
    return CheckResult(check.name, True, f"query returned {value}", latency)


ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models?limit=1"


def check_anthropic_key(check: Check, ctx: Context) -> CheckResult:
    """Is the AI triage agent's API key still accepted? Uses a free metadata call."""
    p = check.params
    key = ctx.get_secret(p["key_parameter"])
    resp = ctx.http_get(
        ANTHROPIC_MODELS_URL, p["timeout_seconds"], headers={"x-api-key": key, "anthropic-version": "2023-06-01"}
    )
    if resp.status in (401, 403):
        return CheckResult(
            check.name,
            False,
            f"API key rejected (HTTP {resp.status}): it may have expired or been revoked. "
            f"AI triage cannot run until a new key is stored in {p['key_parameter']}.",
            resp.elapsed_ms,
        )
    if resp.status != 200:
        # Rate limits or an API outage say nothing about the key itself.
        return CheckResult(check.name, True, f"key not verified: Claude API returned HTTP {resp.status}", resp.elapsed_ms)
    return CheckResult(check.name, True, "API key accepted", resp.elapsed_ms)


EVALUATORS = {
    "http": check_http,
    "heartbeat": check_heartbeat,
    "http_json_freshness": check_freshness,
    "postgres": check_postgres,
    "anthropic_api_key": check_anthropic_key,
}


def run_check(check: Check, ctx: Context) -> CheckResult:
    """Run one check. Any exception becomes a failed result, never a crash."""
    started = time.monotonic()
    try:
        result = EVALUATORS[check.type](check, ctx)
    except Exception as exc:  # noqa: BLE001 - a broken check must not stop the others
        result = CheckResult(check.name, False, f"{type(exc).__name__}: {exc}")
    if not result.latency_ms:
        result.latency_ms = (time.monotonic() - started) * 1000
    result.message = _truncate(result.message)
    return result
