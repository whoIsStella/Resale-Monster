from goliath.agents.subprocess_adapter import SubprocessAgentAdapter
from goliath.agents.supervisor import ProcessSupervisor
from goliath.config import AgentConfig
from goliath.core.schemas import AgentCapability


def build_codex_adapter(
    config: AgentConfig | None = None,
    *,
    cancellation_grace_seconds: float = 5.0,
) -> SubprocessAgentAdapter:
    settings = config or AgentConfig(
        adapter="codex",
        command_template=["codex", "exec", "{prompt}"],
        capabilities={"coding", "testing", "review"},
        task_types={"code_change", "test", "review"},
        permissions={"read_files", "write_files", "run_commands"},
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
        name="codex",
        command_template=settings.command_template,
        supervisor=ProcessSupervisor(cancellation_grace_seconds=cancellation_grace_seconds),
        environment_allowlist=settings.environment_allowlist,
        profile=settings.profile,
        _capabilities=capabilities,
    )
