"""Load and validate check definitions.

The same checks.yaml is read by Terraform (to create one alarm per check)
and, after Terraform merges in the demo checks, stored as JSON in an SSM
parameter that the checker Lambda reads at runtime.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

DEFAULT_FAILURE_THRESHOLD = 2
DEFAULT_TIMEOUT_SECONDS = 10


class ConfigError(ValueError):
    """Raised when a check definition is invalid."""


@dataclass(frozen=True)
class Check:
    name: str
    type: str
    params: dict[str, Any] = field(default_factory=dict)
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    description: str = ""


def _require(raw: dict, key: str, name: str) -> Any:
    if key not in raw or raw[key] in (None, ""):
        raise ConfigError(f"check '{name}': '{key}' is required")
    return raw[key]


def _positive_int(value: Any, key: str, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"check '{name}': '{key}' must be a positive integer")
    return value


def _url(value: Any, name: str) -> str:
    parsed = urlparse(str(value))
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(f"check '{name}': 'url' must be an http(s) URL")
    return str(value)


def _timeout(raw: dict, name: str) -> int:
    return _positive_int(raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS), "timeout_seconds", name)


def _http(raw: dict, name: str) -> dict:
    params = {
        "url": _url(_require(raw, "url", name), name),
        "expected_status": _positive_int(raw.get("expected_status", 200), "expected_status", name),
        "timeout_seconds": _timeout(raw, name),
    }
    if "max_latency_ms" in raw:
        params["max_latency_ms"] = _positive_int(raw["max_latency_ms"], "max_latency_ms", name)
    if "contains" in raw:
        params["contains"] = str(raw["contains"])
    return params


def _heartbeat(raw: dict, name: str) -> dict:
    return {
        "interval_seconds": _positive_int(_require(raw, "interval_seconds", name), "interval_seconds", name),
        "grace_seconds": _positive_int(raw.get("grace_seconds", 60), "grace_seconds", name),
    }


def _freshness(raw: dict, name: str) -> dict:
    return {
        "url": _url(_require(raw, "url", name), name),
        "field": str(_require(raw, "field", name)),
        "max_age_seconds": _positive_int(_require(raw, "max_age_seconds", name), "max_age_seconds", name),
        "timeout_seconds": _timeout(raw, name),
    }


def _postgres(raw: dict, name: str) -> dict:
    max_value = raw.get("max_value", 0)
    if isinstance(max_value, bool) or not isinstance(max_value, (int, float)):
        raise ConfigError(f"check '{name}': 'max_value' must be a number")
    dsn_parameter = str(_require(raw, "dsn_parameter", name))
    if not dsn_parameter.startswith("/"):
        raise ConfigError(f"check '{name}': 'dsn_parameter' must be an SSM name starting with '/'")
    return {
        "dsn_parameter": dsn_parameter,
        "query": str(_require(raw, "query", name)),
        "max_value": max_value,
        "timeout_seconds": _timeout(raw, name),
    }


PARSERS = {
    "http": _http,
    "heartbeat": _heartbeat,
    "http_json_freshness": _freshness,
    "postgres": _postgres,
}


def parse_checks(raw: Any) -> list[Check]:
    """Validate a {"checks": [...]} document and return normalized checks."""
    if not isinstance(raw, dict):
        raise ConfigError("config must be a mapping with a 'checks' list")
    items = raw.get("checks") or []
    if not isinstance(items, list):
        raise ConfigError("'checks' must be a list")

    checks: list[Check] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ConfigError("each check must be a mapping")
        name = str(item.get("name", ""))
        if not NAME_RE.match(name):
            raise ConfigError(
                f"invalid check name '{name}': use lowercase letters, digits and hyphens"
            )
        if name in seen:
            raise ConfigError(f"duplicate check name '{name}'")
        seen.add(name)

        check_type = item.get("type")
        if check_type not in PARSERS:
            raise ConfigError(
                f"check '{name}': unknown type '{check_type}' (expected one of {sorted(PARSERS)})"
            )

        checks.append(
            Check(
                name=name,
                type=check_type,
                params=PARSERS[check_type](item, name),
                failure_threshold=_positive_int(
                    item.get("failure_threshold", DEFAULT_FAILURE_THRESHOLD), "failure_threshold", name
                ),
                description=str(item.get("description", "")),
            )
        )
    return checks
