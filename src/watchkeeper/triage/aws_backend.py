"""tools.Backend implemented with read-only AWS APIs (used by the triage Lambda)."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import boto3

from watchkeeper import aws

# CloudTrail events that are noise for "what changed?"
IGNORED_EVENTS = {"AssumeRole", "ConsoleLogin", "GetSessionToken", "StartQuery", "CreateLogStream"}


class AwsBackend:
    def __init__(self, namespace: str, log_sources: dict[str, str], schedules: list[str]):
        self.namespace = namespace
        self.sources = log_sources
        self.schedules = schedules
        self.cloudwatch = boto3.client("cloudwatch")
        self.logs = boto3.client("logs")
        self.cloudtrail = boto3.client("cloudtrail")

    @classmethod
    def from_env(cls) -> "AwsBackend":
        return cls(
            namespace=os.environ.get("METRIC_NAMESPACE", "watchkeeper"),
            log_sources=json.loads(os.environ.get("LOG_SOURCES", "{}")),
            schedules=json.loads(os.environ.get("SCHEDULES", "[]")),
        )

    def now(self) -> float:
        return time.time()

    def list_checks(self) -> list[dict]:
        out = []
        for check in aws.load_checks():
            result = aws.get_result(check.name) or {}
            out.append({
                "name": check.name,
                "type": check.type,
                "ok": bool(result.get("ok", False)),
                "message": result.get("message", "no result yet"),
                "checked_at": result.get("checked_at"),
            })
        return out

    def _series(self, metric: str, check: str, minutes: int, stat: str) -> dict[float, float]:
        end = datetime.now(timezone.utc) + timedelta(minutes=1)  # include the current, still-open minute
        resp = self.cloudwatch.get_metric_statistics(
            Namespace=self.namespace,
            MetricName=metric,
            Dimensions=[{"Name": "Check", "Value": check}],
            StartTime=end - timedelta(minutes=minutes),
            EndTime=end,
            Period=60,
            Statistics=[stat],
        )
        return {p["Timestamp"].timestamp(): p[stat] for p in resp.get("Datapoints", [])}

    def check_history(self, check: str, minutes: int) -> list[dict]:
        success = self._series("CheckSuccess", check, minutes, "Minimum")
        latency = self._series("LatencyMs", check, minutes, "Average")
        return [{"t": t, "success": v >= 1, "latency_ms": latency.get(t, 0.0)} for t, v in sorted(success.items())]

    def log_sources(self) -> list[str]:
        return sorted(self.sources)

    def search_logs(self, source: str, contains: str, minutes: int, limit: int) -> list[dict]:
        kwargs = {
            "logGroupName": self.sources[source],
            "startTime": int((time.time() - minutes * 60) * 1000),
            "limit": 1000,
        }
        if contains:
            kwargs["filterPattern"] = json.dumps(contains)  # quoted = exact phrase
        lines: list[dict] = []
        for _ in range(5):  # bounded pagination
            resp = self.logs.filter_log_events(**kwargs)
            lines += [{"t": e["timestamp"] / 1000, "message": e["message"].rstrip()} for e in resp.get("events", [])]
            if not resp.get("nextToken"):
                break
            kwargs["nextToken"] = resp["nextToken"]
        return sorted(lines, key=lambda l: l["t"])[-limit:]

    def heartbeat(self, check: str) -> Optional[dict]:
        return aws.get_heartbeat(check)

    def recent_changes(self, minutes: int) -> list[dict]:
        # Management events only; CloudTrail can lag by several minutes.
        resp = self.cloudtrail.lookup_events(
            LookupAttributes=[{"AttributeKey": "ReadOnly", "AttributeValue": "false"}],
            StartTime=datetime.now(timezone.utc) - timedelta(minutes=minutes),
            MaxResults=50,
        )
        changes = []
        for e in resp.get("Events", []):
            if e.get("EventName") in IGNORED_EVENTS:
                continue
            changes.append({
                "t": e["EventTime"].timestamp(),
                "actor": e.get("Username", "unknown"),
                "action": e.get("EventName"),
                "target": ", ".join(r.get("ResourceName", "") for r in e.get("Resources", [])) or e.get("EventSource", ""),
                "details": e.get("EventSource", ""),
            })
        return sorted(changes, key=lambda c: c["t"])

    def action_context(self) -> dict[str, list[str]]:
        return {
            "jobs": [c.name for c in aws.load_checks() if c.type == "heartbeat"],
            "schedules": list(self.schedules),
            "deployments": [],  # filled by a deploy-history integration (e.g. Vercel) in phase 5
            "flags": [],
        }
