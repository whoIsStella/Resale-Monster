"""Capability and MCP-scope routing for resale commerce-domain tasks.

Hermes is the configurable default for research/identification/review tasks;
Codex remains preferred for engineering tasks. Commerce-domain tasks are never
silently routed to an agent that lacks the required MCP scopes.
"""

from __future__ import annotations

from goliath.config import AgentConfig
from goliath.core.schemas import DOMAIN_TASK_TYPES, TaskType

# Preferred agent per commerce-domain task type. Configuration may override by
# setting ``preferred_domain_task_types`` on an agent.
DEFAULT_PREFERRED_AGENT: dict[TaskType, str] = {
    TaskType.PRODUCT_IDENTIFICATION: "hermes",
    TaskType.COMPARABLE_RESEARCH: "hermes",
    TaskType.STALE_INVENTORY_REVIEW: "hermes",
    TaskType.INVENTORY_QUALITY_REVIEW: "hermes",
    TaskType.LISTING_GENERATION: "hermes",
    TaskType.PRICING_ANALYSIS: "hermes",
}

# Codex remains preferred for engineering task types.
CODEX_PREFERRED_TASK_TYPES: frozenset[TaskType] = frozenset(
    {TaskType.CODE_CHANGE, TaskType.TEST, TaskType.REVIEW}
)

# Minimum MCP scopes required to execute each commerce-domain task type.
TASK_REQUIRED_MCP_SCOPES: dict[TaskType, frozenset[str]] = {
    TaskType.PRODUCT_IDENTIFICATION: frozenset(
        {"inventory:read", "research:read", "research:write", "approval:request"}
    ),
    TaskType.COMPARABLE_RESEARCH: frozenset(
        {"inventory:read", "research:read", "research:write"}
    ),
    TaskType.LISTING_GENERATION: frozenset(
        {"inventory:read", "listing:read", "listing:write_draft", "approval:request"}
    ),
    TaskType.PRICING_ANALYSIS: frozenset({"inventory:read", "pricing:calculate"}),
    TaskType.INVENTORY_QUALITY_REVIEW: frozenset({"inventory:read"}),
    TaskType.STALE_INVENTORY_REVIEW: frozenset({"inventory:read", "approval:request"}),
}


def required_mcp_scopes(task_type: TaskType) -> frozenset[str]:
    return TASK_REQUIRED_MCP_SCOPES.get(task_type, frozenset())


def is_domain_task(task_type: TaskType) -> bool:
    return task_type in DOMAIN_TASK_TYPES


def missing_mcp_scopes(agent: AgentConfig, task_type: TaskType) -> set[str]:
    """Return the MCP scopes an agent lacks for a commerce-domain task type."""
    if not is_domain_task(task_type):
        return set()
    return set(required_mcp_scopes(task_type)) - set(agent.mcp_scopes)


class InsufficientMcpScopesError(RuntimeError):
    pass


def select_domain_agent(
    task_type: TaskType, agents: dict[str, AgentConfig]
) -> str:
    """Choose an eligible agent for a commerce-domain task.

    Prefers a configured preference, then the default preferred agent, then any
    enabled agent that holds every required MCP scope. Never returns an agent
    that is missing a required scope.
    """
    if not is_domain_task(task_type):
        raise ValueError(f"not a commerce-domain task type: {task_type.value}")

    def _eligible(name: str) -> bool:
        agent = agents.get(name)
        return (
            agent is not None
            and agent.enabled
            and not missing_mcp_scopes(agent, task_type)
        )

    preferred_by_config = [
        name
        for name, agent in agents.items()
        if task_type in agent.preferred_domain_task_types and _eligible(name)
    ]
    if preferred_by_config:
        return min(preferred_by_config, key=lambda name: agents[name].priority)

    default = DEFAULT_PREFERRED_AGENT.get(task_type)
    if default is not None and _eligible(default):
        return default

    fallback = sorted(
        (name for name in agents if _eligible(name)),
        key=lambda name: agents[name].priority,
    )
    if fallback:
        return fallback[0]

    raise InsufficientMcpScopesError(
        f"no enabled agent holds the required MCP scopes for {task_type.value}: "
        f"{sorted(required_mcp_scopes(task_type))}"
    )
