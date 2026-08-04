from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from goliath.agents.supervisor import ProcessSupervisor
from goliath.core.schemas import (
    AgentCapability,
    AgentHealthResult,
    AgentJob,
    ExecutionRequest,
    ExecutionResult,
)


@dataclass(slots=True)
class SubprocessAgentAdapter:
    name: str
    command_template: list[str]
    supervisor: ProcessSupervisor = field(default_factory=ProcessSupervisor)
    environment_allowlist: set[str] = field(
        default_factory=lambda: {"PATH", "HOME", "LANG", "LC_ALL", "TERM"}
    )
    profile: str | None = None
    _capabilities: tuple[AgentCapability, ...] = ()

    @property
    def capabilities(self) -> tuple[AgentCapability, ...]:
        return self._capabilities

    def _prompt(self, request: ExecutionRequest | AgentJob) -> str:
        context = "\n".join(f"- {path}" for path in request.context_files) or "- none"
        permissions = ", ".join(sorted(str(item) for item in request.permissions)) or "none"
        forbidden = ", ".join(sorted(request.forbidden_actions)) or "none"
        return (
            f"Job ID: {request.job_id}\n"
            f"Task type: {request.task_type}\n"
            f"Objective: {request.objective}\n"
            f"Permissions: {permissions}\n"
            f"Forbidden actions: {forbidden}\n"
            f"Context files:\n{context}\n\n"
            "Work only inside the supplied workspace. Do not access marketplace credentials, "
            "production databases, payouts, buyer messaging, unrestricted SQL, or live listing "
            "mutations. Run relevant tests and return a concise structured summary."
        )

    def _command(self, request: ExecutionRequest | AgentJob) -> tuple[list[str], list[str]]:
        prompt = self._prompt(request)
        replacements = {
            "{prompt}": prompt,
            "{workspace}": str(request.workspace),
            "{profile}": self.profile or "",
        }
        command = [replacements.get(argument, argument) for argument in self.command_template]
        command = [argument for argument in command if argument]
        redacted = ["<redacted-prompt>" if argument == prompt else argument for argument in command]
        return command, redacted

    def _environment(self) -> dict[str, str]:
        return {name: os.environ[name] for name in self.environment_allowlist if name in os.environ}

    async def healthcheck(self) -> AgentHealthResult:
        executable = self.command_template[0]
        available = (
            Path(executable).is_file() and os.access(executable, os.X_OK)
            if os.sep in executable
            else shutil.which(executable, path=self._environment().get("PATH")) is not None
        )
        return AgentHealthResult(
            agent=self.name,
            available=available,
            executable=executable,
            detail=None if available else "executable not found",
        )

    async def run(
        self,
        request: ExecutionRequest,
        on_started: Callable[[int], None] | None = None,
    ) -> ExecutionResult:
        command, redacted = self._command(request)
        result = await self.supervisor.run(
            job_id=request.job_id,
            agent=self.name,
            command=command,
            redacted_command=redacted,
            workspace=request.workspace,
            environment=self._environment(),
            timeout_seconds=request.timeout_seconds,
            max_output_chars=request.max_output_chars,
            on_started=on_started,
        )
        if result.succeeded and result.stdout:
            try:
                structured = json.loads(result.stdout)
            except json.JSONDecodeError:
                structured = None
            if isinstance(structured, dict):
                result.structured_result = structured
        return result

    async def cancel(self, job_id: UUID) -> bool:
        return await self.supervisor.cancel(job_id)
