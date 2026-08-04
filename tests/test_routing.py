from pathlib import Path
from uuid import UUID

import pytest

from goliath.agents.registry import AgentRegistry
from goliath.agents.router import AgentRouter, NoEligibleAgentError
from goliath.core.schemas import (
    REQUIRED_FORBIDDEN_ACTIONS,
    AgentCapability,
    AgentHealthResult,
    ExecutionRequest,
    ExecutionResult,
    JobSubmission,
)


class RoutingAdapter:
    def __init__(
        self, name: str, *, available: bool = True, capabilities: tuple[AgentCapability, ...] = ()
    ) -> None:
        self.name = name
        self.available = available
        self._capabilities = capabilities

    @property
    def capabilities(self) -> tuple[AgentCapability, ...]:
        return self._capabilities

    async def healthcheck(self) -> AgentHealthResult:
        return AgentHealthResult(
            agent=self.name,
            available=self.available,
            executable=self.name,
            detail=None if self.available else "missing",
        )

    async def run(self, request: ExecutionRequest) -> ExecutionResult:
        raise NotImplementedError

    async def cancel(self, job_id: UUID) -> bool:
        return False


def submission(tmp_path: Path, *, agent: str | None = None) -> JobSubmission:
    return JobSubmission(
        requested_agent=agent,
        task_type="code_change",
        objective="Implement feature",
        workspace=tmp_path,
        permissions={"read_files", "write_files"},
        required_capabilities={"coding"},
        forbidden_actions=set(REQUIRED_FORBIDDEN_ACTIONS),
    )


def coding_capability() -> tuple[AgentCapability, ...]:
    return (
        AgentCapability(
            name="coding",
            task_types={"code_change"},
            permissions={"read_files", "write_files"},
        ),
    )


@pytest.mark.asyncio
async def test_explicit_agent_is_selected_without_fallback(tmp_path: Path) -> None:
    registry = AgentRegistry()
    registry.register("requested", RoutingAdapter("requested", available=False), priority=1)
    registry.register(
        "other", RoutingAdapter("other", capabilities=coding_capability()), priority=2
    )
    with pytest.raises(NoEligibleAgentError, match="requested agent requested"):
        await AgentRouter(registry).route(submission(tmp_path, agent="requested"))


@pytest.mark.asyncio
async def test_automatic_routing_uses_eligible_priority(tmp_path: Path) -> None:
    registry = AgentRegistry()
    registry.register(
        "later", RoutingAdapter("later", capabilities=coding_capability()), priority=20
    )
    registry.register(
        "first", RoutingAdapter("first", capabilities=coding_capability()), priority=10
    )
    selected = await AgentRouter(registry).route(submission(tmp_path))
    assert selected.name == "first"


@pytest.mark.asyncio
async def test_unavailable_agents_produce_clear_error(tmp_path: Path) -> None:
    registry = AgentRegistry()
    registry.register("missing", RoutingAdapter("missing", available=False))
    with pytest.raises(NoEligibleAgentError, match="no eligible agent found"):
        await AgentRouter(registry).route(submission(tmp_path))
