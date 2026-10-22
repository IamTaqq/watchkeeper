"""Recorded incident scenarios: a fake world the agent can investigate offline.

A scenario YAML describes checks, metric history, logs, heartbeats and recent
changes, with times given in minutes relative to "now" (negative = past).
It also states the expected outcome, which scripts/evaluate.py grades.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml


def _ts(text: str) -> float:
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()


@dataclass
class Scenario:
    name: str
    description: str
    now: float
    alarm: dict
    checks: list[dict]
    history: dict[str, list[dict]]
    logs: dict[str, list[dict]]
    heartbeats: dict[str, dict]
    changes: list[dict]
    context: dict[str, list[str]]
    expected: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "Scenario":
        raw = yaml.safe_load(Path(path).read_text())
        now = _ts(raw["now"])

        def at(minutes: Any) -> float:
            return now + float(minutes) * 60

        history = {}
        for check, segments in (raw.get("history") or {}).items():
            points = []
            for seg in segments:
                minute = int(seg["from"])
                while minute <= int(seg["to"]):
                    points.append({"t": at(minute), "success": bool(seg["success"]), "latency_ms": float(seg.get("latency_ms", 0))})
                    minute += 1
            history[check] = sorted(points, key=lambda p: p["t"])

        logs = {}
        for source, entries in (raw.get("logs") or {}).items():
            lines = []
            for e in entries:
                if "every" in e:
                    minute = float(e["from"])
                    while minute <= float(e["to"]):
                        lines.append({"t": at(minute), "message": e["message"]})
                        minute += float(e["every"])
                else:
                    lines.append({"t": at(e["at"]), "message": e["message"]})
            logs[source] = sorted(lines, key=lambda l: l["t"])

        return cls(
            name=raw["name"],
            description=raw.get("description", ""),
            now=now,
            alarm=raw["alarm"],
            checks=[{**c, "checked_at": at(c.get("checked_at", -1))} for c in raw.get("checks") or []],
            history=history,
            logs=logs,
            heartbeats={k: {**v, "last_ping": at(v["last_ping"])} for k, v in (raw.get("heartbeats") or {}).items()},
            changes=sorted(({**c, "t": at(c["at"])} for c in raw.get("changes") or []), key=lambda c: c["t"]),
            context={k: [str(x) for x in v] for k, v in (raw.get("context") or {}).items()},
            expected=raw.get("expected") or {},
        )


class ScenarioBackend:
    """Implements tools.Backend from a Scenario."""

    def __init__(self, scenario: Scenario):
        self.s = scenario

    def now(self) -> float:
        return self.s.now

    def list_checks(self) -> list[dict]:
        return [{k: v for k, v in c.items()} for c in self.s.checks]

    def check_history(self, check: str, minutes: int) -> list[dict]:
        if check not in self.s.history and check not in {c["name"] for c in self.s.checks}:
            raise KeyError(f"no check named '{check}'")
        since = self.s.now - minutes * 60
        return [p for p in self.s.history.get(check, []) if p["t"] >= since]

    def log_sources(self) -> list[str]:
        return sorted(self.s.logs)

    def search_logs(self, source: str, contains: str, minutes: int, limit: int) -> list[dict]:
        since = self.s.now - minutes * 60
        lines = [l for l in self.s.logs.get(source, []) if since <= l["t"] <= self.s.now]
        if contains:
            lines = [l for l in lines if contains in l["message"]]
        return lines[-limit:]

    def heartbeat(self, check: str) -> Optional[dict]:
        return self.s.heartbeats.get(check)

    def recent_changes(self, minutes: int) -> list[dict]:
        since = self.s.now - minutes * 60
        return [c for c in self.s.changes if since <= c["t"] <= self.s.now]

    def action_context(self) -> dict[str, list[str]]:
        return dict(self.s.context)


# --- grading -------------------------------------------------------------------------

@dataclass
class Grade:
    root_cause: bool
    tier: bool
    action: bool
    details: list[str]

    @property
    def passed(self) -> bool:
        return self.root_cause and self.tier and self.action


def grade(report: dict, expected: dict) -> Grade:
    """Each entry in expected.root_cause is a regex; all must match the summary or top hypothesis."""
    top = report["hypotheses"][0]["cause"] if report.get("hypotheses") else ""
    text = f"{report.get('summary', '')}\n{top}"
    details = []

    patterns = expected.get("root_cause", [])
    missing = [p for p in patterns if not re.search(p, text, re.I)]
    if missing:
        details.append(f"root cause missing {missing}")

    proposal = report["proposal"]
    tier_ok = proposal["tier"] == expected.get("tier", proposal["tier"])
    if not tier_ok:
        details.append(f"tier {proposal['tier']} != expected {expected.get('tier')}")

    action_ok = True
    if expected.get("tier") == "action":
        action_ok = proposal.get("action") == expected.get("action") and proposal.get("params") == expected.get("params", proposal.get("params"))
        if not action_ok:
            details.append(f"action {proposal.get('action')}({proposal.get('params')}) != expected {expected.get('action')}({expected.get('params')})")
    for forbidden in expected.get("forbid_actions", []):
        rejected = (proposal.get("rejected_action") or {}).get("action")
        if forbidden in (proposal.get("action"), rejected):
            action_ok = False
            details.append(f"proposed forbidden action {forbidden}")

    return Grade(root_cause=not missing, tier=tier_ok, action=action_ok, details=details)
