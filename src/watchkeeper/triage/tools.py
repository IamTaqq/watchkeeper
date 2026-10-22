"""Read-only tools the agent can call, backed by a pluggable data source.

The same tools run against AWS in Lambda (aws_backend.py) and against
recorded incident scenarios on your laptop (scenario.py).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Protocol

from .redact import redact

MAX_TOOL_OUTPUT = 6000


class Backend(Protocol):
    def now(self) -> float: ...
    def list_checks(self) -> list[dict]: ...
    def check_history(self, check: str, minutes: int) -> list[dict]: ...
    def log_sources(self) -> list[str]: ...
    def search_logs(self, source: str, contains: str, minutes: int, limit: int) -> list[dict]: ...
    def heartbeat(self, check: str) -> Optional[dict]: ...
    def recent_changes(self, minutes: int) -> list[dict]: ...
    def action_context(self) -> dict[str, list[str]]: ...


class ToolInputError(ValueError):
    pass


REPORT_TOOL = {
    "name": "submit_report",
    "description": (
        "Submit your final triage report. Call this exactly once, when you have enough evidence "
        "or when you are told to stop. This ends the investigation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "2-3 sentences: what is broken and the most likely cause."},
            "impact": {"type": "string", "description": "Who or what is affected, and since when."},
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "description": "Tool and source, e.g. 'search_logs demo-app'"},
                        "detail": {"type": "string", "description": "The concrete observation, with timestamps."},
                    },
                    "required": ["source", "detail"],
                },
            },
            "hypotheses": {
                "type": "array",
                "description": "Ranked, most likely first.",
                "items": {
                    "type": "object",
                    "properties": {
                        "cause": {"type": "string"},
                        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                        "reasoning": {"type": "string"},
                    },
                    "required": ["cause", "confidence", "reasoning"],
                },
            },
            "proposal": {
                "type": "object",
                "properties": {
                    "tier": {
                        "type": "string",
                        "enum": ["action", "pull_request", "escalate"],
                        "description": "action = one allowlisted action fits; pull_request = needs a code/config change; escalate = a human must decide.",
                    },
                    "action": {"type": "string", "description": "Allowlisted action id (tier 'action' only)."},
                    "params": {"type": "object", "description": "Parameters for the action (tier 'action' only)."},
                    "rationale": {"type": "string"},
                    "manual_steps": {
                        "type": "array",
                        "description": "Steps for a human. Text only; nothing is executed.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "step": {"type": "string"},
                                "risk": {"type": "string", "enum": ["read_only", "changes_state", "irreversible"]},
                            },
                            "required": ["step", "risk"],
                        },
                    },
                },
                "required": ["tier", "rationale"],
            },
        },
        "required": ["summary", "impact", "evidence", "hypotheses", "proposal"],
    },
}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _int(value: Any, name: str, default: int, low: int, high: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolInputError(f"'{name}' must be a number")
    return max(low, min(high, int(value)))


def _str(value: Any, name: str, required: bool = True) -> str:
    if value is None or value == "":
        if required:
            raise ToolInputError(f"'{name}' is required")
        return ""
    return str(value)


class Toolbox:
    def __init__(self, backend: Backend):
        self.backend = backend
        self._handlers: dict[str, Callable[[dict], Any]] = {
            "list_checks": self._list_checks,
            "get_check_history": self._check_history,
            "search_logs": self._search_logs,
            "get_heartbeat": self._heartbeat,
            "recent_changes": self._recent_changes,
        }

    def definitions(self) -> list[dict]:
        sources = self.backend.log_sources()
        return [
            {
                "name": "list_checks",
                "description": "Current status and last result message of every monitored check.",
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "get_check_history",
                "description": "Pass/fail and latency of one check over time. Use it to find when a problem started.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "check": {"type": "string"},
                        "minutes": {"type": "integer", "description": "Look-back window, 5-1440 (default 60)."},
                    },
                    "required": ["check"],
                },
            },
            {
                "name": "search_logs",
                "description": "Recent log lines from one source, newest last. Optionally only lines containing a phrase.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "enum": sources},
                        "contains": {"type": "string", "description": "Case-sensitive phrase, e.g. 'ERROR' or 'Traceback'."},
                        "minutes": {"type": "integer", "description": "Look-back window, 1-1440 (default 30)."},
                        "limit": {"type": "integer", "description": "Max lines, 1-200 (default 50)."},
                    },
                    "required": ["source"],
                },
            },
            {
                "name": "get_heartbeat",
                "description": "Last ping (time, ok/fail, message) reported by a scheduled job's heartbeat check.",
                "input_schema": {"type": "object", "properties": {"check": {"type": "string"}}, "required": ["check"]},
            },
            {
                "name": "recent_changes",
                "description": "Deploys and configuration changes in the window (who changed what). Changes are suspects, not proof.",
                "input_schema": {
                    "type": "object",
                    "properties": {"minutes": {"type": "integer", "description": "1-1440 (default 120)."}},
                },
            },
            REPORT_TOOL,
        ]

    def call(self, name: str, tool_input: dict) -> str:
        """Run a tool and return redacted, size-capped JSON text for the model."""
        if name not in self._handlers:
            raise ToolInputError(f"unknown tool '{name}'")
        if not isinstance(tool_input, dict):
            raise ToolInputError("tool input must be an object")
        result = self._handlers[name](tool_input)
        text = redact(json.dumps(result, ensure_ascii=False, default=str))
        if len(text) > MAX_TOOL_OUTPUT:
            text = text[:MAX_TOOL_OUTPUT] + f"... [truncated {len(text) - MAX_TOOL_OUTPUT} chars; narrow the query]"
        return text

    # --- handlers -----------------------------------------------------------------

    def _ago(self, ts: float) -> str:
        return f"{_iso(ts)} ({(self.backend.now() - ts) / 60:.0f} min ago)"

    def _list_checks(self, _: dict) -> list[dict]:
        out = []
        for c in self.backend.list_checks():
            item = {"name": c["name"], "type": c.get("type"), "status": "PASS" if c.get("ok") else "FAIL",
                    "last_message": c.get("message", "")}
            if c.get("checked_at"):
                item["checked_at"] = self._ago(float(c["checked_at"]))
            out.append(item)
        return out

    def _check_history(self, args: dict) -> dict:
        check = _str(args.get("check"), "check")
        minutes = _int(args.get("minutes"), "minutes", 60, 5, 1440)
        points = self.backend.check_history(check, minutes)
        first_fail = next((p for p in points if not p["success"]), None)
        streak_start = None
        for p in reversed(points):
            if p["success"]:
                break
            streak_start = p
        return {
            "check": check,
            "window_minutes": minutes,
            "periods": self._compress(points)[-30:],
            "first_failure_in_window": self._ago(first_fail["t"]) if first_fail else None,
            "current_failure_streak_started": self._ago(streak_start["t"]) if streak_start else None,
        }

    @staticmethod
    def _compress(points: list[dict]) -> list[str]:
        """Collapse consecutive points with the same status and similar latency into one line.

        Saves tokens: two hours of per-minute data becomes a handful of lines.
        """
        out: list[str] = []
        run: list[dict] = []

        def flush() -> None:
            if not run:
                return
            lat = [p.get("latency_ms", 0.0) for p in run]
            status = "PASS" if run[0]["success"] else "FAIL"
            span = _iso(run[0]["t"]) if len(run) == 1 else f"{_iso(run[0]['t'])} to {_iso(run[-1]['t'])}"
            out.append(f"{span} {status} x{len(run)}, latency avg {sum(lat) / len(lat):.0f}ms max {max(lat):.0f}ms")

        for p in points:
            if run:
                base = sum(q.get("latency_ms", 0.0) for q in run) / len(run)
                similar = abs(p.get("latency_ms", 0.0) - base) <= max(200.0, base * 0.5)
                if p["success"] != run[0]["success"] or not similar:
                    flush()
                    run = []
            run.append(p)
        flush()
        return out

    def _search_logs(self, args: dict) -> dict:
        source = _str(args.get("source"), "source")
        if source not in self.backend.log_sources():
            raise ToolInputError(f"unknown log source '{source}'; use one of {self.backend.log_sources()}")
        contains = _str(args.get("contains"), "contains", required=False)
        minutes = _int(args.get("minutes"), "minutes", 30, 1, 1440)
        limit = _int(args.get("limit"), "limit", 50, 1, 200)
        lines = self.backend.search_logs(source, contains, minutes, limit)
        # Collapse consecutive identical messages ("... repeated x12") to save tokens.
        groups: list[list[dict]] = []
        for line in lines:
            if groups and groups[-1][0]["message"] == line["message"]:
                groups[-1].append(line)
            else:
                groups.append([line])
        rendered = []
        for g in groups:
            message = str(g[0]["message"])[:400]
            if len(g) == 1:
                rendered.append(f"{_iso(g[0]['t'])} {message}")
            else:
                rendered.append(f"{_iso(g[0]['t'])} to {_iso(g[-1]['t'])} (repeated x{len(g)}) {message}")
        return {"source": source, "matched": len(lines), "lines": rendered}

    def _heartbeat(self, args: dict) -> dict:
        check = _str(args.get("check"), "check")
        beat = self.backend.heartbeat(check)
        if not beat:
            return {"check": check, "found": False}
        return {
            "check": check,
            "status": beat.get("status"),
            "last_ping": self._ago(float(beat["last_ping"])),
            "message": beat.get("message", ""),
        }

    def _recent_changes(self, args: dict) -> dict:
        minutes = _int(args.get("minutes"), "minutes", 120, 1, 1440)
        changes = self.backend.recent_changes(minutes)
        return {
            "window_minutes": minutes,
            "changes": [{**{k: v for k, v in c.items() if k != "t"}, "time": self._ago(c["t"])} for c in changes],
        }

