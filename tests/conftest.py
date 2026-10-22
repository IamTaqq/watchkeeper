import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from watchkeeper.checks import Context, HttpResponse  # noqa: E402


class FakeWorld:
    """In-memory stand-in for HTTP, the clock, DynamoDB, SSM and Postgres."""

    def __init__(self):
        self.time = 1_000_000.0
        self.responses = {}
        self.heartbeats = {}
        self.secrets = {}
        self.sql_value = 0
        self.sql_calls = []

    def http_get(self, url, timeout):
        resp = self.responses.get(url)
        if isinstance(resp, Exception):
            raise resp
        if resp is None:
            raise ConnectionError(f"no route to {url}")
        return resp

    def register(self, name, now):
        self.heartbeats.setdefault(name, {"last_ping": now, "status": "pending", "message": ""})

    def run_sql(self, dsn, query, timeout):
        self.sql_calls.append((dsn, query, timeout))
        return self.sql_value

    def context(self):
        return Context(
            http_get=self.http_get,
            now=lambda: self.time,
            get_heartbeat=self.heartbeats.get,
            register_heartbeat=self.register,
            get_secret=self.secrets.__getitem__,
            run_sql=self.run_sql,
        )


@pytest.fixture
def world():
    return FakeWorld()


def ok_response(body=b"{}", status=200, elapsed_ms=120.0):
    return HttpResponse(status=status, body=body, elapsed_ms=elapsed_ms)
