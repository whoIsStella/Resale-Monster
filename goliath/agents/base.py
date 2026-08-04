from __future__ import annotations

from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from goliath.core.schemas import (
    AgentCapability,
    AgentHealthResult,
    ExecutionRequest,
    ExecutionResult,
)


class AgentAdapter(Protocol):
    name: str

    @property
    def capabilities(self) -> tuple[AgentCapability, ...]: ...

    async def run(
        self,
        request: ExecutionRequest,
        on_started: Callable[[int], None] | None = None,
    ) -> ExecutionResult: ...

    async def cancel(self, job_id: UUID) -> bool: ...

    async def healthcheck(self) -> AgentHealthResult: ...
