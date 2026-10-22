"""Turn CloudWatch alarm notifications into readable Slack messages."""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone
from typing import Optional

STATE_STYLE = {
    "ALARM": (":red_circle:", "FAILING"),
    "OK": (":large_green_circle:", "RECOVERED"),
    "INSUFFICIENT_DATA": (":white_circle:", "NO DATA"),
}


def parse_alarm(sns_message: str) -> dict:
    """Extract the fields we need from a CloudWatch alarm SNS payload."""
    alarm = json.loads(sns_message)
    check = None
    for dim in alarm.get("Trigger", {}).get("Dimensions", []):
        if dim.get("name") == "Check":
            check = dim.get("value")
    return {
        "alarm_name": alarm.get("AlarmName", "unknown"),
        "check": check or alarm.get("AlarmName", "unknown"),
        "state": alarm.get("NewStateValue", "UNKNOWN"),
        "previous_state": alarm.get("OldStateValue", "UNKNOWN"),
        "reason": alarm.get("NewStateReason", ""),
        "description": alarm.get("AlarmDescription") or "",
        "time": alarm.get("StateChangeTime", ""),
        "region": alarm.get("Region", ""),
    }


def is_noise(alarm: dict) -> bool:
    """A new alarm going INSUFFICIENT_DATA -> OK is not a recovery; don't announce it."""
    return alarm["state"] == "OK" and alarm["previous_state"] == "INSUFFICIENT_DATA"


def build_message(alarm: dict, last_result: Optional[dict], dashboard_url: str = "") -> dict:
    emoji, label = STATE_STYLE.get(alarm["state"], (":grey_question:", alarm["state"]))
    lines = [f"{emoji} *{label}: {alarm['check']}*"]
    if alarm["description"]:
        lines.append(alarm["description"])

    if alarm["state"] == "ALARM" and "no datapoints" in alarm["reason"].lower():
        lines.append(
            "No results were received. The checker itself may be down - check the checker Lambda's logs."
        )
    elif last_result:
        checked = datetime.fromtimestamp(int(last_result["checked_at"]), tz=timezone.utc)
        lines.append(f"Last result ({checked:%Y-%m-%d %H:%M UTC}): {last_result['message']}")

    if dashboard_url:
        lines.append(f"<{dashboard_url}|Open dashboard>")

    text = "\n".join(lines)
    return {
        "text": f"{label}: {alarm['check']}",  # plain-text fallback for notifications
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    }


def post(webhook_url: str, payload: dict, timeout: int = 10) -> int:
    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - webhook URL is ours
        return resp.status
