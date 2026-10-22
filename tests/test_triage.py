"""Tests for the triage agent, using a scripted stand-in for the Claude API."""

import io
import json
import urllib.error
from pathlib import Path

import pytest

from watchkeeper.triage import actions, report
from watchkeeper.triage.agent import investigate, normalize
from watchkeeper.triage.llm import AnthropicClient, LLMError, Usage
from watchkeeper.triage.redact import redact
from watchkeeper.triage.scenario import Scenario, ScenarioBackend, grade
from watchkeeper.triage.tools import Toolbox

SCENARIOS = Path(__file__).resolve().parents[1] / "scenarios"


def load(name):
    return Scenario.load(SCENARIOS / f"{name}.yaml")


def tool_use(name, tool_input=None, id_=None):
    return {"type": "tool_use", "id": id_ or f"toolu_{name}", "name": name, "input": tool_input or {}}


def reply(*blocks):
    return {"content": list(blocks), "usage": {"input_tokens": 1000, "output_tokens": 100}}


ROLLBACK_REPORT = {
    "summary": "Deploy dpl_105 (config refactor) broke /health with KeyError PRICE_TABLE.",
    "impact": "/health returns 500 since 14:22.",
    "evidence": [{"source": "search_logs demo-app", "detail": "KeyError: 'PRICE_TABLE' from 14:22"}],
    "hypotheses": [{"cause": "Deploy dpl_105 removed the PRICE_TABLE config", "confidence": "high", "reasoning": "timing"}],
    "proposal": {"tier": "action", "action": "rollback_deploy", "params": {"deployment_id": "dpl_104"}, "rationale": "Restore last good deploy"},
}


class ScriptedLLM:
    """Plays back responses and records every request the agent makes."""

    model = "claude-sonnet-5-5"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **request):
        self.requests.append(json.loads(json.dumps(request)))  # snapshot
        item = self.responses.pop(0)
        return item(request) if callable(item) else item


def agent_for(name):
    scenario = load(name)
    return scenario, Toolbox(ScenarioBackend(scenario))


class TestInvestigationLoop:
    def test_happy_path_bad_deploy(self):
        scenario, toolbox = agent_for("bad-deploy")
        llm = ScriptedLLM(
            reply(tool_use("get_check_history", {"check": "demo-app-health"}), tool_use("recent_changes", {"minutes": 60})),
            reply(tool_use("search_logs", {"source": "demo-app", "contains": "ERROR"})),
            reply(tool_use("submit_report", ROLLBACK_REPORT)),
        )
        result = investigate(scenario.alarm, toolbox, llm)

        assert result.finish == "report" and result.steps == 3
        assert [c.name for c in result.tool_calls] == ["get_check_history", "recent_changes", "search_logs"]
        assert result.report["proposal"]["params"] == {"deployment_id": "dpl_104"}
        assert grade(result.report, scenario.expected).passed

        # Tool results are returned to the model against the right tool_use ids.
        second = llm.requests[1]["messages"][-1]["content"]
        assert {b["tool_use_id"] for b in second} == {"toolu_get_check_history", "toolu_recent_changes"}
        # The allowlist with currently-allowed values is in the system prompt.
        assert "rollback_deploy(deployment_id in ['dpl_104', 'dpl_103'])" in llm.requests[0]["system"]
        assert result.usage.input_tokens == 3000 and result.cost_usd == pytest.approx(0.009)

    def test_report_is_forced_on_the_last_turn(self):
        scenario, toolbox = agent_for("bad-deploy")

        def last(request):
            assert request["tool_choice"] == {"type": "tool", "name": "submit_report"}
            return reply(tool_use("submit_report", ROLLBACK_REPORT))

        llm = ScriptedLLM(reply(tool_use("list_checks")), reply(tool_use("list_checks")), last)
        result = investigate(scenario.alarm, toolbox, llm, max_steps=3)
        assert result.finish == "forced" and result.steps == 3
        assert llm.requests[2]["messages"][-1]["content"][-1]["text"].startswith("Last turn")

    def test_prose_answer_gets_one_forced_retry(self):
        scenario, toolbox = agent_for("bad-deploy")
        llm = ScriptedLLM(
            reply({"type": "text", "text": "I think it is the deploy."}),
            reply(tool_use("submit_report", ROLLBACK_REPORT)),
        )
        result = investigate(scenario.alarm, toolbox, llm)
        assert result.finish == "forced"
        assert llm.requests[1]["tool_choice"]["name"] == "submit_report"

    def test_bad_tool_input_is_reported_back_not_raised(self):
        scenario, toolbox = agent_for("bad-deploy")
        llm = ScriptedLLM(
            reply(tool_use("search_logs", {"source": "nope"}), tool_use("get_check_history", {})),
            reply(tool_use("submit_report", ROLLBACK_REPORT)),
        )
        result = investigate(scenario.alarm, toolbox, llm)
        results = llm.requests[1]["messages"][-1]["content"]
        assert all(r["is_error"] for r in results)
        assert "unknown log source" in results[0]["content"] and "'check' is required" in results[1]["content"]
        assert [c.ok for c in result.tool_calls] == [False, False]

    def test_failing_backend_does_not_end_triage(self):
        scenario, toolbox = agent_for("bad-deploy")
        toolbox.backend.recent_changes = lambda minutes: (_ for _ in ()).throw(TimeoutError("CloudTrail slow"))
        llm = ScriptedLLM(reply(tool_use("recent_changes")), reply(tool_use("submit_report", ROLLBACK_REPORT)))
        investigate(scenario.alarm, toolbox, llm)
        assert "Tool failed: TimeoutError" in llm.requests[1]["messages"][-1]["content"][0]["content"]

    def test_api_failure_falls_back_to_escalation(self):
        scenario, toolbox = agent_for("bad-deploy")

        def boom(request):
            raise LLMError("HTTP 401: invalid x-api-key")

        result = investigate(scenario.alarm, toolbox, ScriptedLLM(boom))
        assert result.finish == "error"
        assert result.report["proposal"]["tier"] == "escalate"
        assert "invalid x-api-key" in result.report["summary"]


class TestProposalValidation:
    CONTEXT = {"deployments": ["dpl_104"], "jobs": ["demo-cron"], "schedules": [], "flags": []}

    def test_unknown_or_out_of_range_actions_are_downgraded(self):
        for action, params, reason in [
            ("rollback_deploy", {"deployment_id": "dpl_105"}, "not one of the allowed values"),
            ("delete_database", {}, "not an allowlisted action"),
            ("rerun_job", {"job": "demo-cron", "force": True}, "unexpected parameter"),
            ("rerun_job", {}, "requires 'job'"),
        ]:
            report_in = {**ROLLBACK_REPORT, "proposal": {"tier": "action", "action": action, "params": params, "rationale": "x"}}
            clean, note = normalize(report_in, self.CONTEXT)
            assert clean["proposal"]["tier"] == "escalate", action
            assert reason in note
            assert clean["proposal"]["rejected_action"]["action"] == action
            assert "action" not in clean["proposal"]

    def test_valid_action_is_kept(self):
        clean, note = normalize(ROLLBACK_REPORT, self.CONTEXT)
        assert note == "" and clean["proposal"]["action"] == "rollback_deploy"

    def test_non_action_tiers_drop_stray_action_fields(self):
        r = {**ROLLBACK_REPORT, "proposal": {"tier": "escalate", "action": "rerun_job", "params": {"job": "x"}, "rationale": "x"}}
        clean, _ = normalize(r, self.CONTEXT)
        assert "action" not in clean["proposal"] and "params" not in clean["proposal"]

    def test_malformed_report(self):
        clean, note = normalize("not a dict", self.CONTEXT)
        assert clean["proposal"]["tier"] == "escalate" and note

    def test_registry_has_no_destructive_actions(self):
        assert not {"delete", "drop", "truncate", "terminate"} & {w for a in actions.REGISTRY for w in a.split("_")}


class TestTools:
    def test_history_is_compressed(self):
        _, toolbox = agent_for("red-herring-deploy")
        out = json.loads(toolbox.call("get_check_history", {"check": "demo-app-health", "minutes": 120}))
        assert len(out["periods"]) == 3  # fast PASS, slow PASS, FAIL
        assert "PASS x24, latency avg 2588ms" in out["periods"][1]
        assert out["current_failure_streak_started"].startswith("2026-10-22T02:09:00Z")

    def test_repeated_log_lines_are_collapsed(self):
        _, toolbox = agent_for("upstream-rate-limit")
        out = json.loads(toolbox.call("search_logs", {"source": "ingest", "contains": "429", "minutes": 60, "limit": 200}))
        assert out["matched"] == 42 and len(out["lines"]) == 1 and "repeated x42" in out["lines"][0]

    def test_secrets_and_personal_data_never_reach_the_model(self):
        for name in ["bad-deploy", "upstream-rate-limit"]:
            _, toolbox = agent_for(name)
            text = toolbox.call("search_logs", {"source": toolbox.backend.log_sources()[-1], "minutes": 200, "limit": 200})
            text += toolbox.call("search_logs", {"source": toolbox.backend.log_sources()[0], "minutes": 200, "limit": 200})
            text += toolbox.call("recent_changes", {"minutes": 1440})
            for secret in ["hunter2", "takumi@example.com", "sk-test-1234567890abcdef"]:
                assert secret not in text

    def test_minutes_are_clamped(self):
        _, toolbox = agent_for("bad-deploy")
        out = json.loads(toolbox.call("recent_changes", {"minutes": 99999}))
        assert out["window_minutes"] == 1440

    def test_unknown_check_history(self):
        _, toolbox = agent_for("bad-deploy")
        with pytest.raises(KeyError):
            toolbox.call("get_check_history", {"check": "nope"})


@pytest.mark.parametrize(
    "text, leaked",
    [
        ("dsn=postgresql://app:hunter2@db:5432/app", "hunter2"),
        ("Authorization: Bearer abcdefghijklmnop", "abcdefghijklmnop"),
        ("key sk-ant-api03-AAAABBBBCCCC", "AAAABBBBCCCC"),
        ("AKIAABCDEFGHIJKLMNOP used", "AKIAABCDEFGHIJKLMNOP"),
        ("https://hooks.slack.com/services/T0/B0/xyz", "T0/B0/xyz"),
        ("password=letmein&x=1", "letmein"),
        ("contact jane.doe@example.org", "jane.doe@example.org"),
        ("call 604-555-1234", "604-555-1234"),
    ],
)
def test_redact(text, leaked):
    assert leaked not in redact(text)


def test_redact_keeps_useful_context():
    assert redact("postgresql://app:pw@db.internal/app") == "postgresql://app:[REDACTED]@db.internal/app"


class TestGrading:
    def test_forbidden_action_counts_even_if_rejected(self):
        scenario = load("red-herring-deploy")
        proposal = {"tier": "escalate", "rationale": "", "manual_steps": [],
                    "rejected_action": {"action": "rollback_deploy", "params": {}}}
        r = {"summary": "pay.example.com timeouts", "hypotheses": [], "proposal": proposal}
        g = grade(r, scenario.expected)
        assert g.root_cause and g.tier and not g.action

    def test_all_scenarios_load_and_are_well_formed(self):
        for path in SCENARIOS.glob("*.yaml"):
            s = Scenario.load(path)
            assert s.alarm["check"] in {c["name"] for c in s.checks}, path.name
            assert s.expected.get("tier") in ("action", "pull_request", "escalate"), path.name
            if s.expected["tier"] == "action":
                actions.validate(s.expected["action"], s.expected["params"], s.context)


class TestClient:
    def test_request_shape_and_retry(self, monkeypatch):
        sent = []
        responses = [
            urllib.error.HTTPError("u", 529, "overloaded", {}, io.BytesIO(b'{"error":"overloaded"}')),
            io.BytesIO(json.dumps({"content": [], "usage": {}}).encode()),
        ]

        class Resp:
            def __init__(self, buf):
                self.buf = buf

            def __enter__(self):
                return self.buf

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout):
            sent.append(req)
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return Resp(item)

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        client = AnthropicClient(api_key="k", model="claude-haiku-5-5", sleep=lambda s: None)
        client.create(messages=[], max_tokens=10)
        assert len(sent) == 2
        assert sent[0].get_header("X-api-key") == "k" and sent[0].get_header("Anthropic-version") == "2023-06-01"
        assert json.loads(sent[0].data)["model"] == "claude-haiku-5-5"

    def test_client_error_is_not_retried(self, monkeypatch):
        calls = []

        def fake_urlopen(req, timeout):
            calls.append(1)
            raise urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b"bad request"))

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        with pytest.raises(LLMError, match="HTTP 400"):
            AnthropicClient(api_key="k", sleep=lambda s: None).create(messages=[])
        assert calls == [1]

    def test_cost(self):
        u = Usage(input_tokens=1_000_000, output_tokens=100_000)
        assert u.cost_usd("claude-haiku-5-5") == pytest.approx(0.15)
        assert u.cost_usd("some-future-model") is None


def test_report_rendering_says_nothing_was_executed():
    scenario, toolbox = agent_for("bad-deploy")
    result = investigate(scenario.alarm, toolbox, ScriptedLLM(reply(tool_use("submit_report", ROLLBACK_REPORT))))
    text = report.to_text(result)
    assert "nothing was executed" in text and "rollback_deploy(deployment_id=dpl_104)" in text
    slack_msg = report.to_slack(result)
    assert slack_msg["text"] == "AI triage: demo-app-health"
    assert "claude-sonnet-5-5" in slack_msg["blocks"][1]["elements"][0]["text"]


def test_evaluation_harness_runs_with_any_llm(monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("evaluate", SCENARIOS.parent / "scripts" / "evaluate.py")
    evaluate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evaluate)

    rows = evaluate.run(
        [load("bad-deploy")], ["claude-sonnet-5-5"], 2, 8,
        lambda model: ScriptedLLM(*[reply(tool_use("submit_report", ROLLBACK_REPORT))] * 2),
    )
    assert [r["passed"] for r in rows] == [True, True]
    evaluate.summarize(rows)


def test_event_target_reads_names_not_values():
    import json as _json
    from watchkeeper.triage.aws_backend import event_target

    event = {
        "Resources": [],
        "EventSource": "ssm.amazonaws.com",
        "CloudTrailEvent": _json.dumps({"requestParameters": {
            "name": "/watchkeeper/demo/fault_mode", "value": "error", "overwrite": True}}),
    }
    assert event_target(event) == "/watchkeeper/demo/fault_mode"
    assert "error" not in event_target(event)

    lam = {"Resources": [{"ResourceName": "watchkeeper-demo-app"}],
           "CloudTrailEvent": _json.dumps({"requestParameters": {"functionName": "watchkeeper-demo-app"}})}
    assert event_target(lam) == "watchkeeper-demo-app"
    assert event_target({"EventSource": "iam.amazonaws.com", "CloudTrailEvent": "not json"}) == "iam.amazonaws.com"
