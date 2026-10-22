"""Human-defined registry of remediation actions the agent may *propose*.

Nothing here executes anything. The agent picks an action id and parameters;
`validate` checks them against values observed in the live system (for
example, a rollback target must be one of the recent successful deploys).
Anything that fails validation is downgraded to an escalation.

Adding an action is a deliberate, reviewed change: write the definition here,
add its executor (phase 6) and tests. Irreversible operations never belong here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Action:
    id: str
    description: str
    # parameter name -> key in the action context listing the allowed values
    params: dict[str, str]


REGISTRY: dict[str, Action] = {
    a.id: a
    for a in [
        Action(
            "rerun_job",
            "Run a scheduled job once more. Use when a job failed or was skipped and is safe to repeat.",
            {"job": "jobs"},
        ),
        Action(
            "resume_schedule",
            "Re-enable a job's schedule that was disabled.",
            {"schedule": "schedules"},
        ),
        Action(
            "rollback_deploy",
            "Return a service to an earlier successful deployment. Use when a recent deploy clearly caused the failure.",
            {"deployment_id": "deployments"},
        ),
        Action(
            "disable_feature_flag",
            "Turn off a feature flag to stop a broken feature without a code change.",
            {"flag": "flags"},
        ),
    ]
}


class ProposalError(ValueError):
    pass


def validate(action_id: Any, params: Any, context: dict[str, list[str]]) -> dict[str, str]:
    """Return clean params, or raise ProposalError explaining why the proposal is invalid."""
    action = REGISTRY.get(str(action_id))
    if action is None:
        raise ProposalError(f"'{action_id}' is not an allowlisted action")
    if not isinstance(params, dict):
        raise ProposalError("params must be an object")

    extra = set(params) - set(action.params)
    if extra:
        raise ProposalError(f"unexpected parameter(s) for {action.id}: {sorted(extra)}")

    clean = {}
    for name, context_key in action.params.items():
        value = params.get(name)
        allowed = context.get(context_key, [])
        if value is None:
            raise ProposalError(f"{action.id} requires '{name}'")
        if str(value) not in allowed:
            raise ProposalError(f"{name}='{value}' is not one of the allowed values {allowed}")
        clean[name] = str(value)
    return clean


def describe(context: dict[str, list[str]]) -> str:
    """Text for the system prompt: each action with the values it accepts right now."""
    lines = []
    for action in REGISTRY.values():
        params = ", ".join(
            f"{name} in {context.get(key) or '[] (none available)'}" for name, key in action.params.items()
        )
        lines.append(f"- {action.id}({params}): {action.description}")
    return "\n".join(lines)
