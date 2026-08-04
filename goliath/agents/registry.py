from __future__ import annotations

from dataclasses import dataclass

from goliath.agents.base import AgentAdapter
from goliath.agents.codex import build_codex_adapter
from goliath.agents.hermes import build_hermes_adapter
from goliath.agents.subprocess_adapter import SubprocessAgentAdapter
from goliath.agents.supervisor import ProcessSupervisor
from goliath.config import AgentConfig, OrchestrationConfig
from goliath.core.schemas import AgentCapability, AgentHealthResult


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    adapter: AgentAdapter
    priority: int


class AgentRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, RegistryEntry] = {}

    def register(self, name: str, adapter: AgentAdapter, *, priority: int = 100) -> None:
        if name in self._entries:
            raise ValueError(f"agent already registered: {name}")
        self._entries[name] = RegistryEntry(adapter=adapter, priority=priority)

    def get(self, name: str) -> AgentAdapter | None:
        entry = self._entries.get(name)
        return entry.adapter if entry else None

    def entries(self) -> list[tuple[str, RegistryEntry]]:
        return sorted(self._entries.items(), key=lambda item: (item[1].priority, item[0]))

    async def health(self) -> list[AgentHealthResult]:
        return [await entry.adapter.healthcheck() for _, entry in self.entries()]


def _generic_adapter(
    name: str, settings: AgentConfig, cancellation_grace_seconds: float
) -> SubprocessAgentAdapter:
    capabilities = tuple(
        AgentCapability(
            name=capability,
            task_types=settings.task_types,
            permissions=settings.permissions,
        )
        for capability in settings.capabilities
    )
    return SubprocessAgentAdapter(
        name=name,
        command_template=settings.command_template,
        supervisor=ProcessSupervisor(cancellation_grace_seconds=cancellation_grace_seconds),
        environment_allowlist=settings.environment_allowlist,
        profile=settings.profile,
        _capabilities=capabilities,
    )


def build_registry(config: OrchestrationConfig | None = None) -> AgentRegistry:
    registry = AgentRegistry()
    if config is None:
        from pathlib import Path

        config = OrchestrationConfig(
            workspace_roots=[Path.cwd()],
            agents={
                "codex": AgentConfig(
                    adapter="codex",
                    command_template=["codex", "exec", "{prompt}"],
                    capabilities={"coding", "testing", "review"},
                    task_types={"code_change", "test", "review"},
                    permissions={"read_files", "write_files", "run_commands"},
                ),
                "hermes": AgentConfig(
                    adapter="hermes",
                    command_template=["hermes", "{prompt}"],
                    capabilities={"research", "operations", "analysis"},
                    task_types={"research", "operations", "analysis"},
                ),
            },
        )
    for name, settings in config.agents.items():
        if not settings.enabled:
            continue
        if settings.adapter == "codex":
            adapter = build_codex_adapter(
                settings,
                cancellation_grace_seconds=config.cancellation_grace_seconds,
            )
        elif settings.adapter == "hermes":
            adapter = build_hermes_adapter(
                settings,
                cancellation_grace_seconds=config.cancellation_grace_seconds,
            )
        else:
            adapter = _generic_adapter(name, settings, config.cancellation_grace_seconds)
        registry.register(name, adapter, priority=settings.priority)
    return registry
