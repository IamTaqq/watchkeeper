from pathlib import Path

import pytest
import yaml

from watchkeeper.config import ConfigError, parse_checks

ROOT = Path(__file__).resolve().parents[1]


def test_example_file_is_valid():
    assert parse_checks(yaml.safe_load((ROOT / "checks.yaml").read_text())) == []


def test_commented_examples_are_valid():
    """The examples in checks.yaml must stay correct when a user uncomments them."""
    text = (ROOT / "checks.yaml").read_text()
    examples = text.split("checks: []", 1)[1]
    # Example lines are indented as "#  - name: ..."; section headers ("# ---") stay comments.
    uncommented = "\n".join(line[1:] if line.startswith("#  ") else line for line in examples.splitlines())
    checks = parse_checks(yaml.safe_load("checks:\n" + uncommented))
    assert [c.type for c in checks] == ["http", "heartbeat", "http_json_freshness", "postgres"]


def test_defaults_are_filled_in():
    (check,) = parse_checks({"checks": [{"name": "site", "type": "http", "url": "https://example.com"}]})
    assert check.failure_threshold == 2
    assert check.params == {"url": "https://example.com", "expected_status": 200, "timeout_seconds": 10}


@pytest.mark.parametrize(
    "item, message",
    [
        ({"name": "Bad_Name", "type": "http", "url": "https://x.io"}, "invalid check name"),
        ({"name": "a", "type": "ping"}, "unknown type"),
        ({"name": "a", "type": "http"}, "'url' is required"),
        ({"name": "a", "type": "http", "url": "ftp://x.io"}, "http(s) URL"),
        ({"name": "a", "type": "heartbeat", "interval_seconds": 0}, "positive integer"),
        ({"name": "a", "type": "heartbeat", "interval_seconds": True}, "positive integer"),
        ({"name": "a", "type": "postgres", "dsn_parameter": "dsn", "query": "select 1"}, "starting with '/'"),
        ({"name": "a", "type": "http", "url": "https://x.io", "failure_threshold": -1}, "positive integer"),
    ],
)
def test_invalid_checks_are_rejected(item, message):
    with pytest.raises(ConfigError, match=message.replace("(", r"\(").replace(")", r"\)")):
        parse_checks({"checks": [item]})


def test_duplicate_names_are_rejected():
    item = {"name": "a", "type": "heartbeat", "interval_seconds": 60}
    with pytest.raises(ConfigError, match="duplicate"):
        parse_checks({"checks": [item, item]})


def test_empty_checks_key_is_allowed():
    assert parse_checks({"checks": None}) == []
