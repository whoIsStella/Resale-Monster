from __future__ import annotations

from goliath.agents.base import AgentAdapter
from goliath.agents.registry import AgentRegistry
from goliath.core.schemas import JobSubmission


class NoEligibleAgentError(LookupError):
    pass


class AgentRouter:
    def __init__(self, registry: AgentRegistry) -> None:
        self._registry = registry

    async def route(self, submission: JobSubmission) -> AgentAdapter:
        if submission.requested_agent:
            adapter = self._registry.get(submission.requested_agent)
            if adapter is None:
                raise NoEligibleAgentError(
                    f"requested agent is not configured: {submission.requested_agent}"
                )
            reason = await self._ineligible_reason(adapter, submission)
            if reason:
                raise NoEligibleAgentError(
                    f"requested agent {submission.requested_agent} is not eligible: {reason}"
                )
            return adapter

        reasons: list[str] = []
        for name, entry in self._registry.entries():
            reason = await self._ineligible_reason(entry.adapter, submission)
            if reason is None:
                return entry.adapter
            reasons.append(f"{name}: {reason}")
        detail = "; ".join(reasons) or "no agents are configured"
        raise NoEligibleAgentError(f"no eligible agent found ({detail})")

    @staticmethod
    async def _ineligible_reason(adapter: AgentAdapter, submission: JobSubmission) -> str | None:
        health = await adapter.healthcheck()
        if not health.available:
            return health.detail or "unavailable"
        supported_tasks = {
            task_type for capability in adapter.capabilities for task_type in capability.task_types
        }
        if submission.task_type not in supported_tasks:
            return f"task type {submission.task_type.value} is unsupported"
        capability_names = {capability.name for capability in adapter.capabilities}
        missing = submission.required_capabilities - capability_names
        if missing:
            return f"missing capabilities: {', '.join(sorted(item.value for item in missing))}"
        allowed_permissions = {
            permission
            for capability in adapter.capabilities
            for permission in capability.permissions
        }
        denied = submission.permissions - allowed_permissions
        if denied:
            return f"permissions unavailable: {', '.join(sorted(item.value for item in denied))}"
        return None
