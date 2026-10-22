"""The investigation loop: Claude calls read-only tools, then submits a report."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from . import actions
from .llm import LLM, LLMError, Usage
from .tools import Toolbox, ToolInputError

DEFAULT_MAX_STEPS = 8
MAX_TOKENS_PER_CALL = 2048

SYSTEM_PROMPT = """You are an on-call SRE assistant doing first-response triage for a monitoring alert.

Your job: find out what is broken, since when, and the most likely cause, using ONLY the read-only tools.
Then call submit_report.

Rules:
- You cannot change anything. You can only propose.
- Base every claim on tool output. Cite timestamps. If evidence is thin, say so and use low confidence.
- A recent change is a suspect, not proof: check that its timing matches when the failures started.
- Prefer a proposal that stops user impact quickly, even if the root cause is still uncertain.
- Choose the proposal tier:
  * "action": exactly one allowlisted action below fits AND you have strong evidence for it.
    Use only the listed action ids and only the listed parameter values.
  * "pull_request": the fix is a code or configuration change. Describe it; a human will review.
  * "escalate": anything else, including external dependencies, unclear causes, or risky fixes.
- manual_steps are text for a human. Label each with its risk honestly.
- Be efficient: you have at most {max_steps} tool-calling turns. Do not repeat the same query.

Allowlisted actions (with the values currently allowed):
{actions}
"""


@dataclass
class ToolCall:
    name: str
    input: dict
    ok: bool
    ms: float


@dataclass
class TriageResult:
    check: str
    report: dict
    model: str
    usage: Usage
    steps: int
    tool_calls: list[ToolCall] = field(default_factory=list)
    duration_s: float = 0.0
    finish: str = "report"  # report | forced | fallback | error
    proposal_note: str = ""

    @property
    def cost_usd(self) -> Optional[float]:
        return self.usage.cost_usd(self.model)

    def to_dict(self) -> dict:
        return {
            "check": self.check,
            "model": self.model,
            "finish": self.finish,
            "steps": self.steps,
            "duration_s": round(self.duration_s, 2),
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "cost_usd": self.cost_usd,
            "tool_calls": [{"name": t.name, "input": t.input, "ok": t.ok} for t in self.tool_calls],
            "proposal_note": self.proposal_note,
            "report": self.report,
        }


def alarm_prompt(alarm: dict, now: float) -> str:
    when = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"Alert: check '{alarm['check']}' is FAILING.",
        f"Current time: {when}",
    ]
    if alarm.get("description"):
        lines.append(f"Check description: {alarm['description']}")
    if alarm.get("reason"):
        lines.append(f"Alarm reason: {alarm['reason']}")
    lines.append("Investigate and submit your report.")
    return "\n".join(lines)


def fallback_report(reason: str) -> dict:
    return {
        "summary": f"Automated triage could not finish: {reason}",
        "impact": "Unknown.",
        "evidence": [],
        "hypotheses": [],
        "proposal": {"tier": "escalate", "rationale": "Investigate manually.", "manual_steps": []},
    }


def normalize(report: Any, context: dict[str, list[str]]) -> tuple[dict, str]:
    """Make the report well-formed and enforce the action allowlist."""
    if not isinstance(report, dict):
        return fallback_report("the model returned a malformed report"), "malformed report"
    out = {
        "summary": str(report.get("summary", "")).strip() or "(no summary)",
        "impact": str(report.get("impact", "")).strip(),
        "evidence": [e for e in report.get("evidence") or [] if isinstance(e, dict)][:10],
        "hypotheses": [h for h in report.get("hypotheses") or [] if isinstance(h, dict)][:5],
        "proposal": dict(report.get("proposal") or {}),
    }
    proposal = out["proposal"]
    proposal.setdefault("rationale", "")
    proposal["manual_steps"] = [s for s in proposal.get("manual_steps") or [] if isinstance(s, dict)][:10]

    note = ""
    tier = proposal.get("tier")
    if tier not in ("action", "pull_request", "escalate"):
        note = f"unknown tier '{tier}', treated as escalate"
        proposal["tier"] = "escalate"
    elif tier == "action":
        try:
            proposal["params"] = actions.validate(proposal.get("action"), proposal.get("params") or {}, context)
        except actions.ProposalError as exc:
            note = f"proposed action rejected by validator: {exc}"
            proposal["rejected_action"] = {"action": proposal.pop("action", None), "params": proposal.pop("params", None)}
            proposal["tier"] = "escalate"
    if proposal["tier"] != "action":
        proposal.pop("action", None)
        proposal.pop("params", None)
    return out, note


def investigate(
    alarm: dict,
    toolbox: Toolbox,
    llm: LLM,
    *,
    max_steps: int = DEFAULT_MAX_STEPS,
    clock=time.monotonic,
) -> TriageResult:
    started = clock()
    context = toolbox.backend.action_context()
    system = SYSTEM_PROMPT.format(max_steps=max_steps, actions=actions.describe(context))
    tools = toolbox.definitions()
    messages: list[dict] = [{"role": "user", "content": alarm_prompt(alarm, toolbox.backend.now())}]
    usage = Usage()
    calls: list[ToolCall] = []
    report: Optional[dict] = None
    finish = "report"
    force = False
    steps = 0

    try:
        for steps in range(1, max_steps + 1):
            if steps == max_steps:
                force = True
            choice = {"type": "tool", "name": "submit_report"} if force else {"type": "auto"}
            resp = llm.create(
                system=system, messages=messages, tools=tools, tool_choice=choice, max_tokens=MAX_TOKENS_PER_CALL
            )
            usage.add(resp.get("usage", {}))
            content = resp.get("content", [])
            messages.append({"role": "assistant", "content": content})

            tool_uses = [b for b in content if b.get("type") == "tool_use"]
            submitted = next((b for b in tool_uses if b["name"] == "submit_report"), None)
            if submitted:
                report = submitted.get("input")
                finish = "forced" if force else "report"
                break
            if not tool_uses:
                # The model answered in prose. Ask once more, this time requiring the report.
                messages.append({"role": "user", "content": "Submit your findings with the submit_report tool now."})
                force = True
                continue

            results = []
            for block in tool_uses:
                t0 = clock()
                try:
                    text, ok = toolbox.call(block["name"], block.get("input") or {}), True
                except ToolInputError as exc:
                    text, ok = f"Invalid tool input: {exc}", False
                except Exception as exc:  # noqa: BLE001 - a failing data source must not end the triage
                    text, ok = f"Tool failed: {type(exc).__name__}: {exc}", False
                calls.append(ToolCall(block["name"], block.get("input") or {}, ok, (clock() - t0) * 1000))
                result = {"type": "tool_result", "tool_use_id": block["id"], "content": text}
                if not ok:
                    result["is_error"] = True
                results.append(result)
            if steps == max_steps - 1:
                results.append({"type": "text", "text": "Last turn: submit your report now."})
            messages.append({"role": "user", "content": results})
    except LLMError as exc:
        report, finish = fallback_report(str(exc)), "error"

    if report is None:
        report, finish = fallback_report("step limit reached without a report"), "fallback"

    clean, note = normalize(report, context)
    return TriageResult(
        check=alarm["check"],
        report=clean,
        model=getattr(llm, "model", "unknown"),
        usage=usage,
        steps=steps,
        tool_calls=calls,
        duration_s=clock() - started,
        finish=finish,
        proposal_note=note,
    )


def dumps(result: TriageResult) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
