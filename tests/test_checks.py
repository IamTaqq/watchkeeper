import json

import pytest

from conftest import ok_response
from watchkeeper.checks import parse_timestamp, run_check
from watchkeeper.config import parse_checks


def one(**item):
    return parse_checks({"checks": [item]})[0]


URL = "https://app.example/health"


class TestHttp:
    def test_passes_on_expected_status(self, world):
        world.responses[URL] = ok_response()
        result = run_check(one(name="web", type="http", url=URL), world.context())
        assert result.ok and result.latency_ms == 120.0

    def test_fails_on_wrong_status(self, world):
        world.responses[URL] = ok_response(status=500)
        result = run_check(one(name="web", type="http", url=URL), world.context())
        assert not result.ok and "HTTP 500" in result.message

    def test_fails_when_too_slow(self, world):
        world.responses[URL] = ok_response(elapsed_ms=4000)
        result = run_check(one(name="web", type="http", url=URL, max_latency_ms=3000), world.context())
        assert not result.ok and "slow response" in result.message

    def test_checks_body_contents(self, world):
        world.responses[URL] = ok_response(body=b"maintenance page")
        result = run_check(one(name="web", type="http", url=URL, contains="Welcome"), world.context())
        assert not result.ok and "does not contain" in result.message

    def test_network_error_becomes_failure_not_crash(self, world):
        world.responses[URL] = TimeoutError("timed out")
        result = run_check(one(name="web", type="http", url=URL), world.context())
        assert not result.ok and result.message == "TimeoutError: timed out"


class TestHeartbeat:
    def check(self):
        return one(name="job", type="heartbeat", interval_seconds=300, grace_seconds=60)

    def test_first_run_registers_and_waits(self, world):
        result = run_check(self.check(), world.context())
        assert result.ok
        assert world.heartbeats["job"]["status"] == "pending"

    def test_pending_job_that_never_reports_fails_after_deadline(self, world):
        run_check(self.check(), world.context())
        world.time += 361
        result = run_check(self.check(), world.context())
        assert not result.ok and "since monitoring started" in result.message

    def test_recent_ping_passes(self, world):
        world.heartbeats["job"] = {"last_ping": world.time - 100, "status": "ok"}
        assert run_check(self.check(), world.context()).ok

    def test_late_ping_fails(self, world):
        world.heartbeats["job"] = {"last_ping": world.time - 361, "status": "ok"}
        result = run_check(self.check(), world.context())
        assert not result.ok and "expected every 300s" in result.message

    def test_reported_failure_fails_immediately(self, world):
        world.heartbeats["job"] = {"last_ping": world.time - 5, "status": "fail", "message": "disk full"}
        result = run_check(self.check(), world.context())
        assert not result.ok and "disk full" in result.message


class TestFreshness:
    def check(self):
        return one(name="data", type="http_json_freshness", url=URL, field="data.updated", max_age_seconds=300)

    def respond(self, world, value):
        world.responses[URL] = ok_response(body=json.dumps({"data": {"updated": value}}).encode())

    def test_fresh_data_passes(self, world):
        self.respond(world, world.time - 60)
        assert run_check(self.check(), world.context()).ok

    def test_stale_data_fails(self, world):
        self.respond(world, world.time - 3600)
        result = run_check(self.check(), world.context())
        assert not result.ok and "stale" in result.message

    def test_missing_field_fails_clearly(self, world):
        world.responses[URL] = ok_response(body=b'{"data": {}}')
        result = run_check(self.check(), world.context())
        assert not result.ok and "data.updated" in result.message


@pytest.mark.parametrize(
    "value, expected",
    [
        (1_700_000_000, 1_700_000_000.0),
        (1_700_000_000_000, 1_700_000_000.0),  # milliseconds
        ("2023-11-14T22:13:20Z", 1_700_000_000.0),
        ("2023-11-14T22:13:20+00:00", 1_700_000_000.0),
        ("2023-11-14T22:13:20", 1_700_000_000.0),  # naive -> UTC
    ],
)
def test_parse_timestamp(value, expected):
    assert parse_timestamp(value) == expected


class TestPostgres:
    def check(self, **extra):
        return one(name="stuck", type="postgres", dsn_parameter="/x/dsn", query="select count(*) from jobs", **extra)

    def test_value_within_limit_passes(self, world):
        world.secrets["/x/dsn"] = "postgresql://ro@db/app"
        world.sql_value = 0
        assert run_check(self.check(), world.context()).ok
        assert world.sql_calls == [("postgresql://ro@db/app", "select count(*) from jobs", 10)]

    def test_value_over_limit_fails(self, world):
        world.secrets["/x/dsn"] = "postgresql://ro@db/app"
        world.sql_value = 3
        result = run_check(self.check(max_value=1), world.context())
        assert not result.ok and "returned 3" in result.message

    def test_missing_secret_fails(self, world):
        assert not run_check(self.check(), world.context()).ok


def test_long_messages_are_truncated(world):
    world.responses[URL] = ValueError("x" * 2000)
    result = run_check(one(name="web", type="http", url=URL), world.context())
    assert len(result.message) == 500 and result.message.endswith("...")
