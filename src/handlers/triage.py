"""SNS-triggered Lambda: investigate a newly failing check with the AI agent.

Read-only by construction: the IAM role can only read logs, metrics,
CloudTrail and our own state. The report goes to Slack; nothing is executed.
"""

from __future__ import annotations

import json
import os
import time

from watchkeeper import aws, slack
from watchkeeper.triage import report
from watchkeeper.triage.agent import investigate
from watchkeeper.triage.aws_backend import AwsBackend
from watchkeeper.triage.llm import AnthropicClient
from watchkeeper.triage.tools import Toolbox

PLACEHOLDER_PREFIX = "REPLACE_ME"


def _optional_parameter(name: str) -> str:
    if not name:
        return ""
    try:
        value = aws.get_parameter(name, ttl_seconds=300)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"warning": f"cannot read {name} ({type(exc).__name__})"}))
        return ""
    return "" if value.startswith(PLACEHOLDER_PREFIX) else value


def should_investigate(alarm: dict) -> bool:
    """Only new failures: ALARM entered from OK or INSUFFICIENT_DATA."""
    return alarm["state"] == "ALARM" and alarm["previous_state"] != "ALARM"


def handler(event, context):
    api_key = _optional_parameter(os.environ["ANTHROPIC_KEY_PARAMETER"])
    if not api_key:
        print(json.dumps({"skipped": "no Anthropic API key configured"}))
        return {"investigated": 0}

    webhook = _optional_parameter(os.environ.get("SLACK_WEBHOOK_PARAMETER", ""))
    llm = AnthropicClient(api_key=api_key, model=os.environ.get("TRIAGE_MODEL", "claude-sonnet-5-5"))
    cooldown = int(os.environ.get("COOLDOWN_SECONDS", "1800"))
    max_steps = int(os.environ.get("MAX_STEPS", "8"))

    done = 0
    for record in event.get("Records", []):
        alarm = slack.parse_alarm(record["Sns"]["Message"])
        if not should_investigate(alarm):
            continue
        if not aws.claim_triage(alarm["check"], time.time(), cooldown):
            print(json.dumps({"skipped": alarm["check"], "reason": "already investigated recently"}))
            continue

        result = investigate(alarm, Toolbox(AwsBackend.from_env()), llm, max_steps=max_steps)
        data = result.to_dict()
        print(json.dumps({"triage": {k: v for k, v in data.items() if k != "report"}, "summary": result.report["summary"]}))
        aws.save_triage(alarm["check"], data)
        if webhook:
            slack.post(webhook, report.to_slack(result))
        done += 1
    return {"investigated": done}
