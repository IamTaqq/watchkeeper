"""Investigate a recorded incident with the AI triage agent, on your laptop.

    export ANTHROPIC_API_KEY=sk-ant-...
    python scripts/simulate.py scenarios/bad-deploy.yaml
    python scripts/simulate.py scenarios/bad-deploy.yaml --model claude-haiku-5-5 --verbose

Without an API key, use --tools-only to see what the agent's tools return.
Add --slack to also post the report to $SLACK_WEBHOOK_URL.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from watchkeeper import slack  # noqa: E402
from watchkeeper.triage import report  # noqa: E402
from watchkeeper.triage.agent import DEFAULT_MAX_STEPS, investigate  # noqa: E402
from watchkeeper.triage.llm import AnthropicClient  # noqa: E402
from watchkeeper.triage.scenario import Scenario, ScenarioBackend, grade  # noqa: E402
from watchkeeper.triage.tools import Toolbox  # noqa: E402


def tools_only(toolbox: Toolbox, scenario: Scenario) -> None:
    check = scenario.alarm["check"]
    calls = [("list_checks", {}), ("get_check_history", {"check": check, "minutes": 60}), ("recent_changes", {})]
    calls += [("search_logs", {"source": s, "minutes": 30, "limit": 5}) for s in toolbox.backend.log_sources()]
    for name, args in calls:
        print(f"\n>>> {name}({json.dumps(args)})")
        print(json.dumps(json.loads(toolbox.call(name, args)), indent=2, ensure_ascii=False))


class VerboseLLM:
    """Wraps the client and prints each tool call as it happens."""

    def __init__(self, inner):
        self.inner, self.model = inner, inner.model

    def create(self, **request):
        resp = self.inner.create(**request)
        for block in resp.get("content", []):
            if block.get("type") == "text" and block.get("text", "").strip():
                print(f"  [thinking aloud] {block['text'].strip()[:300]}")
            if block.get("type") == "tool_use":
                print(f"  -> {block['name']}({json.dumps(block.get('input'), ensure_ascii=False)[:200]})")
        return resp


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenario")
    ap.add_argument("--model", default=os.environ.get("TRIAGE_MODEL", "claude-sonnet-5-5"))
    ap.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    ap.add_argument("--tools-only", action="store_true", help="no API calls; print tool outputs")
    ap.add_argument("--verbose", action="store_true", help="print each tool call")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    ap.add_argument("--slack", action="store_true", help="post the report to $SLACK_WEBHOOK_URL")
    args = ap.parse_args()

    scenario = Scenario.load(args.scenario)
    toolbox = Toolbox(ScenarioBackend(scenario))
    print(f"Scenario: {scenario.name} - {scenario.description}")

    if args.tools_only:
        tools_only(toolbox, scenario)
        return 0

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("Set ANTHROPIC_API_KEY (https://platform.claude.com/), or use --tools-only.")
        return 2

    llm = AnthropicClient(api_key=key, model=args.model)
    result = investigate(scenario.alarm, toolbox, VerboseLLM(llm) if args.verbose else llm, max_steps=args.max_steps)

    print()
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False) if args.json else report.to_text(result))

    if scenario.expected:
        g = grade(result.report, scenario.expected)
        print(f"\nGrade: {'PASS' if g.passed else 'FAIL'}" + (f" ({'; '.join(g.details)})" if g.details else ""))

    if args.slack:
        hook = os.environ.get("SLACK_WEBHOOK_URL")
        if not hook:
            print("Set SLACK_WEBHOOK_URL to use --slack.")
            return 2
        slack.post(hook, report.to_slack(result))
        print("Posted to Slack.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
