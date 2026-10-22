import json

import pytest

from handlers import checker, demo_app, heartbeat
from watchkeeper import slack
from watchkeeper.checks import CheckResult

TOKEN = "s3cret"


def ping_event(path, token=TOKEN, method="POST", body=None):
    event = {"rawPath": path, "requestContext": {"http": {"method": method}}, "headers": {}}
    if token:
        event["headers"]["Authorization"] = f"Bearer {token}"
    if body is not None:
        event["body"] = body
    return event


class TestHeartbeatEndpoint:
    @pytest.fixture
    def calls(self):
        return []

    def call(self, event, calls):
        return heartbeat.handle(
            event,
            expected_token=TOKEN,
            heartbeat_names=["backup"],
            record=lambda *args: calls.append(args),
            now=lambda: 42.0,
        )

    def test_success_ping(self, calls):
        resp = self.call(ping_event("/ping/backup"), calls)
        assert resp["statusCode"] == 200
        assert calls == [("backup", "ok", "", 42.0)]

    def test_failure_ping_keeps_message(self, calls):
        resp = self.call(ping_event("/ping/backup/fail", body="disk full"), calls)
        assert resp["statusCode"] == 200
        assert calls == [("backup", "fail", "disk full", 42.0)]

    def test_token_in_query_string(self, calls):
        event = ping_event("/ping/backup", token=None)
        event["queryStringParameters"] = {"token": TOKEN}
        assert self.call(event, calls)["statusCode"] == 200

    @pytest.mark.parametrize(
        "event, status",
        [
            (ping_event("/ping/backup", token="wrong"), 401),
            (ping_event("/ping/backup", token=None), 401),
            (ping_event("/ping/unknown"), 404),
            (ping_event("/ping/backup/oops"), 404),
            (ping_event("/"), 404),
            (ping_event("/ping/backup", method="DELETE"), 405),
        ],
    )
    def test_rejections_record_nothing(self, event, status, calls):
        assert self.call(event, calls)["statusCode"] == status
        assert calls == []


def alarm_message(state="ALARM", old="OK", reason="Threshold Crossed: 2 datapoints [0.0, 0.0]"):
    return json.dumps(
        {
            "AlarmName": "watchkeeper-web",
            "AlarmDescription": "Public site",
            "NewStateValue": state,
            "OldStateValue": old,
            "NewStateReason": reason,
            "Trigger": {"Dimensions": [{"name": "Check", "value": "web"}]},
        }
    )


class TestSlackFormatting:
    def test_alarm_includes_last_result(self):
        alarm = slack.parse_alarm(alarm_message())
        msg = slack.build_message(alarm, {"checked_at": 0, "message": "HTTP 500 (expected 200)"}, "https://dash")
        text = msg["blocks"][0]["text"]["text"]
        assert "FAILING: web" in text and "HTTP 500" in text and "Public site" in text and "https://dash" in text

    def test_missing_data_points_at_the_checker(self):
        alarm = slack.parse_alarm(alarm_message(reason="no datapoints were received for 2 periods"))
        text = slack.build_message(alarm, None)["blocks"][0]["text"]["text"]
        assert "checker itself may be down" in text

    def test_recovery(self):
        alarm = slack.parse_alarm(alarm_message(state="OK", old="ALARM"))
        assert slack.build_message(alarm, None)["text"] == "RECOVERED: web"
        assert not slack.is_noise(alarm)

    def test_initial_ok_is_noise(self):
        assert slack.is_noise(slack.parse_alarm(alarm_message(state="OK", old="INSUFFICIENT_DATA")))


def test_checker_metric_payload():
    data = checker.metric_data([CheckResult("web", True, "ok", 12.34), CheckResult("job", False, "late", 0.0)])
    success = {d["Dimensions"][0]["Value"]: d["Value"] for d in data if d["MetricName"] == "CheckSuccess"}
    assert success == {"web": 1.0, "job": 0.0}
    assert {d["MetricName"] for d in data} == {"CheckSuccess", "LatencyMs"}


@pytest.mark.parametrize(
    "path, mode, status",
    [
        ("/health", "none", 200),
        ("/health", "error", 500),
        ("/health", "down", 503),
        ("/status", "none", 200),
        ("/nope", "none", 404),
    ],
)
def test_demo_app_fault_modes(path, mode, status):
    assert demo_app.respond(path, mode, 1_000_000.0)["statusCode"] == status


def test_demo_app_stale_mode_freezes_data():
    body = json.loads(demo_app.respond("/status", "stale", 1_000_000.0)["body"])
    assert body["last_updated"].startswith("1970-01-12T12:46:40")  # one hour before "now"


class TestNotifierHandler:
    @pytest.fixture
    def env(self, monkeypatch):
        from handlers import notifier

        posted = []
        monkeypatch.setenv("SLACK_WEBHOOK_PARAMETER", "/x/slack")
        monkeypatch.setattr(notifier.aws, "get_result", lambda name: {"checked_at": 0, "message": "HTTP 500"})
        monkeypatch.setattr(notifier.slack, "post", lambda url, payload: posted.append((url, payload)))
        return notifier, posted

    def sns(self, *messages):
        return {"Records": [{"Sns": {"Message": m}} for m in messages]}

    def test_posts_alarm_and_skips_initial_ok(self, env, monkeypatch):
        notifier, posted = env
        monkeypatch.setattr(notifier.aws, "get_parameter", lambda name, ttl_seconds=60: "https://hooks.slack/x")
        result = notifier.handler(self.sns(alarm_message(), alarm_message(state="OK", old="INSUFFICIENT_DATA")), None)
        assert result == {"sent": 1}
        assert posted[0][0] == "https://hooks.slack/x"

    def test_missing_webhook_only_logs(self, env, monkeypatch):
        notifier, posted = env

        def missing(name, ttl_seconds=60):
            raise KeyError(name)

        monkeypatch.setattr(notifier.aws, "get_parameter", missing)
        assert notifier.handler(self.sns(alarm_message()), None) == {"sent": 0}
        assert posted == []

    def test_placeholder_webhook_is_not_called(self, env, monkeypatch):
        notifier, posted = env
        monkeypatch.setattr(notifier.aws, "get_parameter", lambda name, ttl_seconds=60: "REPLACE_ME")
        assert notifier.handler(self.sns(alarm_message()), None) == {"sent": 0}
