"""Run the real handlers against moto's in-memory AWS (no network, no account)."""

import json

import boto3
import pytest

moto = pytest.importorskip("moto")


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ca-central-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("STATE_TABLE", "state")
    monkeypatch.setenv("CONFIG_PARAMETER", "/watchkeeper/config")
    monkeypatch.setenv("TOKEN_PARAMETER", "/watchkeeper/heartbeat_token")
    monkeypatch.setenv("METRIC_NAMESPACE", "watchkeeper")

    with moto.mock_aws():
        from watchkeeper import aws

        # Reset module-level client caches so they bind to the mock.
        monkeypatch.setattr(aws, "_ssm", None)
        monkeypatch.setattr(aws, "_table", None)
        monkeypatch.setattr(aws, "_cloudwatch", None)
        monkeypatch.setattr(aws, "_cache", {})

        boto3.resource("dynamodb").create_table(
            TableName="state",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ssm = boto3.client("ssm")
        config = {"checks": [{"name": "job", "type": "heartbeat", "interval_seconds": 300}]}
        ssm.put_parameter(Name="/watchkeeper/config", Type="String", Value=json.dumps(config))
        ssm.put_parameter(Name="/watchkeeper/heartbeat_token", Type="SecureString", Value="tok")
        yield aws


def test_checker_registers_heartbeat_and_publishes_metrics(aws_env):
    from handlers import checker

    assert checker.handler({}, None) == {"checked": 1, "failing": []}
    assert aws_env.get_heartbeat("job")["status"] == "pending"
    assert aws_env.get_result("job")["ok"] is True

    metrics = boto3.client("cloudwatch").list_metrics(Namespace="watchkeeper")["Metrics"]
    assert {m["MetricName"] for m in metrics} == {"CheckSuccess", "LatencyMs"}


def test_register_does_not_overwrite_a_real_ping(aws_env):
    aws_env.put_heartbeat("job", "ok", "", 100.0)
    aws_env.register_heartbeat("job", 200.0)
    assert aws_env.get_heartbeat("job")["status"] == "ok"


def test_heartbeat_endpoint_end_to_end(aws_env):
    from handlers import heartbeat

    event = {
        "rawPath": "/ping/job/fail",
        "requestContext": {"http": {"method": "POST"}},
        "headers": {"authorization": "Bearer tok"},
        "body": "exit code 1",
    }
    assert heartbeat.handler(event, None)["statusCode"] == 200
    beat = aws_env.get_heartbeat("job")
    assert beat["status"] == "fail" and beat["message"] == "exit code 1"

    from handlers import checker

    assert checker.handler({}, None)["failing"] == ["job"]


def test_aws_backend_reads_logs_metrics_and_changes(aws_env):
    import time as _time

    from watchkeeper.triage.aws_backend import AwsBackend
    from watchkeeper.triage.tools import Toolbox

    logs = boto3.client("logs")
    logs.create_log_group(logGroupName="/aws/lambda/demo")
    logs.create_log_stream(logGroupName="/aws/lambda/demo", logStreamName="s")
    now_ms = int(_time.time() * 1000)
    logs.put_log_events(
        logGroupName="/aws/lambda/demo",
        logStreamName="s",
        logEvents=[
            {"timestamp": now_ms - 60_000, "message": "INFO ok"},
            {"timestamp": now_ms - 30_000, "message": "ERROR boom password=secret123"},
        ],
    )
    boto3.client("cloudwatch").put_metric_data(
        Namespace="watchkeeper",
        MetricData=[{"MetricName": "CheckSuccess", "Dimensions": [{"Name": "Check", "Value": "job"}], "Value": 0.0}],
    )

    backend = AwsBackend(namespace="watchkeeper", log_sources={"demo": "/aws/lambda/demo"}, schedules=["watchkeeper-demo-cron"])
    toolbox = Toolbox(backend)

    lines = json.loads(toolbox.call("search_logs", {"source": "demo", "minutes": 5}))["lines"]
    assert len(lines) == 2 and "secret123" not in lines[1]
    history = backend.check_history("job", 10)
    assert history and history[-1]["success"] is False
    assert backend.action_context()["jobs"] == ["job"]

    # moto has no CloudTrail LookupEvents, so stub just that client.
    from datetime import datetime, timezone

    class FakeTrail:
        def lookup_events(self, **kwargs):
            assert kwargs["LookupAttributes"] == [{"AttributeKey": "ReadOnly", "AttributeValue": "false"}]
            t = datetime.now(timezone.utc)
            return {"Events": [
                {"EventName": "AssumeRole", "EventTime": t, "Username": "x"},
                {"EventName": "DisableRule", "EventTime": t, "Username": "takumi", "EventSource": "events.amazonaws.com",
                 "Resources": [{"ResourceName": "watchkeeper-demo-cron"}]},
            ]}

    backend.cloudtrail = FakeTrail()
    (change,) = backend.recent_changes(60)
    assert change["action"] == "DisableRule" and change["target"] == "watchkeeper-demo-cron" and change["actor"] == "takumi"


def test_triage_handler_investigates_once_per_incident(aws_env, monkeypatch):
    from handlers import triage
    from test_triage import ROLLBACK_REPORT, ScriptedLLM, reply, tool_use

    boto3.client("ssm").put_parameter(Name="/watchkeeper/anthropic_api_key", Type="SecureString", Value="sk-ant-test")
    monkeypatch.setenv("ANTHROPIC_KEY_PARAMETER", "/watchkeeper/anthropic_api_key")
    monkeypatch.setenv("SLACK_WEBHOOK_PARAMETER", "")
    created = []

    def fake_client(api_key, model):
        assert api_key == "sk-ant-test"
        llm = ScriptedLLM(reply(tool_use("list_checks")), reply(tool_use("submit_report", ROLLBACK_REPORT)))
        created.append(llm)
        return llm

    monkeypatch.setattr(triage, "AnthropicClient", fake_client)
    alarm = json.dumps({
        "AlarmName": "watchkeeper-job", "NewStateValue": "ALARM", "OldStateValue": "OK",
        "Trigger": {"Dimensions": [{"name": "Check", "value": "job"}]},
    })
    event = {"Records": [{"Sns": {"Message": alarm}}]}

    assert triage.handler(event, None) == {"investigated": 1}
    assert triage.handler(event, None) == {"investigated": 0}  # cooldown: same incident

    item = boto3.resource("dynamodb").Table("state").get_item(Key={"pk": "triage#job"})["Item"]
    saved = json.loads(item["report_json"])
    # rollback target dpl_104 is not a known deployment in AWS yet, so it is downgraded
    assert saved["report"]["proposal"]["tier"] == "escalate"
    assert "rejected by validator" in saved["proposal_note"]


def test_triage_handler_ignores_recoveries_and_missing_key(aws_env, monkeypatch):
    from handlers import triage

    monkeypatch.setenv("ANTHROPIC_KEY_PARAMETER", "/watchkeeper/anthropic_api_key")  # not created
    assert triage.handler({"Records": []}, None) == {"investigated": 0}
    assert not triage.should_investigate({"state": "OK", "previous_state": "ALARM"})
    assert not triage.should_investigate({"state": "ALARM", "previous_state": "ALARM"})
    assert triage.should_investigate({"state": "ALARM", "previous_state": "INSUFFICIENT_DATA"})
