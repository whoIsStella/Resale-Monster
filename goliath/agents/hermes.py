from goliath.agents.subprocess_adapter import SubprocessAgentAdapter
from goliath.agents.supervisor import ProcessSupervisor
from goliath.config import AgentConfig
from goliath.core.schemas import AgentCapability


def build_hermes_adapter(
    config: AgentConfig | None = None,
    *,
    cancellation_grace_seconds: float = 5.0,
) -> SubprocessAgentAdapter:
    settings = config or AgentConfig(
        adapter="hermes",
        command_template=["hermes", "{prompt}"],
        capabilities={"research", "operations", "analysis"},
        task_types={"research", "operations", "analysis"},
        permissions={"read_files"},
    )
    capabilities = tuple(
        AgentCapability(
            name=name,
            task_types=settings.task_types,
            permissions=settings.permissions,
        )
        for name in settings.capabilities
    )
    return SubprocessAgentAdapter(
        name="hermes",
        command_template=settings.command_template,
        supervisor=ProcessSupervisor(cancellation_grace_seconds=cancellation_grace_seconds),
        environment_allowlist=settings.environment_allowlist,
        profile=settings.profile,
        _capabilities=capabilities,
    )
