"""Grade the triage agent on every recorded scenario (phase 4: measure the agent).

    export ANTHROPIC_API_KEY=sk-ant-...
    python scripts/evaluate.py                                   # all scenarios, default model
    python scripts/evaluate.py --models claude-haiku-5-5 claude-sonnet-5-5 --repeat 3

Prints accuracy, tokens, cost and latency per model, and saves the full
results to build/eval/. Exit code 1 if accuracy is below --min-pass.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from watchkeeper.triage.agent import DEFAULT_MAX_STEPS, investigate  # noqa: E402
from watchkeeper.triage.llm import AnthropicClient  # noqa: E402
from watchkeeper.triage.scenario import Scenario, ScenarioBackend, grade  # noqa: E402
from watchkeeper.triage.tools import Toolbox  # noqa: E402


def run(scenarios, models, repeat, max_steps, make_llm):
    rows = []
    for model in models:
        llm = make_llm(model)
        for scenario in scenarios:
            for i in range(repeat):
                result = investigate(scenario.alarm, Toolbox(ScenarioBackend(scenario)), llm, max_steps=max_steps)
                g = grade(result.report, scenario.expected)
                rows.append({
                    "model": model,
                    "scenario": scenario.name,
                    "run": i + 1,
                    "passed": g.passed,
                    "root_cause": g.root_cause,
                    "tier": g.tier,
                    "action": g.action,
                    "details": g.details,
                    **{k: v for k, v in result.to_dict().items() if k not in ("check", "model")},
                })
                mark = "PASS" if g.passed else "FAIL"
                print(f"  {model:<20} {scenario.name:<22} #{i + 1} {mark}  {'; '.join(g.details)}")
    return rows


def summarize(rows):
    print(f"\n{'model':<20} {'pass':>7} {'root':>6} {'tier':>6} {'act':>6} {'avg $':>9} {'avg s':>7} {'avg turns':>10}")
    for model in dict.fromkeys(r["model"] for r in rows):
        rs = [r for r in rows if r["model"] == model]
        n = len(rs)
        pct = lambda key: f"{sum(r[key] for r in rs) / n:.0%}"  # noqa: E731
        costs = [r["cost_usd"] for r in rs if r["cost_usd"] is not None]
        print(
            f"{model:<20} {pct('passed'):>7} {pct('root_cause'):>6} {pct('tier'):>6} {pct('action'):>6} "
            f"{(statistics.mean(costs) if costs else 0):>9.4f} {statistics.mean(r['duration_s'] for r in rs):>7.1f} "
            f"{statistics.mean(r['steps'] for r in rs):>10.1f}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenarios", default=str(ROOT / "scenarios"))
    ap.add_argument("--models", nargs="+", default=[os.environ.get("TRIAGE_MODEL", "claude-sonnet-5-5")])
    ap.add_argument("--repeat", type=int, default=1, help="runs per scenario (models are not deterministic)")
    ap.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    ap.add_argument("--min-pass", type=float, default=0.0, help="fail if pass rate is below this (0-1)")
    args = ap.parse_args()

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("Set ANTHROPIC_API_KEY to run the evaluation.")
        return 2

    scenarios = [Scenario.load(p) for p in sorted(Path(args.scenarios).glob("*.yaml"))]
    print(f"{len(scenarios)} scenarios x {len(args.models)} model(s) x {args.repeat} run(s)")
    rows = run(scenarios, args.models, args.repeat, args.max_steps, lambda m: AnthropicClient(api_key=key, model=m))
    summarize(rows)

    out = ROOT / "build" / "eval" / f"eval-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2, ensure_ascii=False))
    print(f"\nSaved {out.relative_to(ROOT)}")

    rate = sum(r["passed"] for r in rows) / len(rows)
    return 0 if rate >= args.min_pass else 1


if __name__ == "__main__":
    sys.exit(main())
