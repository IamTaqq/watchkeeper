"""SNS-triggered Lambda: format alarm state changes and send them to Slack.

This is the seam for the AI triage agent (phase 3): another subscriber on the
same SNS topic can investigate while this one notifies.
"""

from __future__ import annotations

import json
import os

from watchkeeper import aws, slack

PLACEHOLDER_PREFIX = "REPLACE_ME"


def handler(event, context):
    webhook = ""
    param = os.environ.get("SLACK_WEBHOOK_PARAMETER", "")
    if param:
        try:
            webhook = aws.get_parameter(param, ttl_seconds=300)
        except Exception as exc:  # noqa: BLE001 - alerts are still logged without Slack
            print(json.dumps({"warning": f"cannot read {param} ({type(exc).__name__}); logging alerts only"}))

    sent = 0
    for record in event.get("Records", []):
        alarm = slack.parse_alarm(record["Sns"]["Message"])
        if slack.is_noise(alarm):
            print(json.dumps({"skipped": alarm["alarm_name"], "reason": "initial OK after creation"}))
            continue
        payload = slack.build_message(alarm, aws.get_result(alarm["check"]), os.environ.get("DASHBOARD_URL", ""))
        print(json.dumps({"alarm": alarm["alarm_name"], "state": alarm["state"], "text": payload["text"]}))
        if webhook and not webhook.startswith(PLACEHOLDER_PREFIX):
            slack.post(webhook, payload)
            sent += 1
    return {"sent": sent}
