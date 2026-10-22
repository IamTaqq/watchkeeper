"""Render a triage result for Slack and for the terminal."""

from __future__ import annotations

from .agent import TriageResult

RISK_LABEL = {"read_only": "read-only", "changes_state": "CHANGES STATE", "irreversible": "IRREVERSIBLE"}
TIER_LABEL = {
    "action": "Proposed action (needs human approval; nothing was executed)",
    "pull_request": "Proposed fix via pull request",
    "escalate": "Needs a human",
}


def _lines(result: TriageResult) -> list[str]:
    r = result.report
    p = r["proposal"]
    out = [f"Summary: {r['summary']}"]
    if r.get("impact"):
        out.append(f"Impact: {r['impact']}")
    if r["hypotheses"]:
        out.append("Hypotheses:")
        for i, h in enumerate(r["hypotheses"], 1):
            out.append(f"  {i}. [{h.get('confidence', '?')}] {h.get('cause', '')}")
    if r["evidence"]:
        out.append("Evidence:")
        for e in r["evidence"][:5]:
            out.append(f"  - {e.get('source', '')}: {e.get('detail', '')}")
    out.append(f"{TIER_LABEL.get(p['tier'], p['tier'])}:")
    if p["tier"] == "action":
        params = ", ".join(f"{k}={v}" for k, v in p["params"].items())
        out.append(f"  {p['action']}({params})")
    if p.get("rationale"):
        out.append(f"  Why: {p['rationale']}")
    for s in p.get("manual_steps", []):
        out.append(f"  - [{RISK_LABEL.get(s.get('risk'), s.get('risk'))}] {s.get('step', '')}")
    if result.proposal_note:
        out.append(f"Note: {result.proposal_note}")
    return out


def footer(result: TriageResult) -> str:
    cost = f"${result.cost_usd:.4f}" if result.cost_usd is not None else "n/a"
    return (
        f"{result.model} | {result.steps} turns, {len(result.tool_calls)} tool calls | "
        f"{result.usage.input_tokens}+{result.usage.output_tokens} tokens | {cost} | {result.duration_s:.1f}s | {result.finish}"
    )


def to_text(result: TriageResult) -> str:
    return "\n".join([f"AI triage: {result.check}", *_lines(result), f"({footer(result)})"])


def to_slack(result: TriageResult) -> dict:
    body = "\n".join(_lines(result))
    return {
        "text": f"AI triage: {result.check}",
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": f":mag: *AI triage: {result.check}*\n{body}"[:2900]}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": footer(result)}]},
        ],
    }
