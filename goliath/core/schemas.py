from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RiskTier(StrEnum):
    READ_ONLY = "read_only"
    DRAFT = "draft"
    APPROVAL_REQUIRED = "approval_required"
    PROHIBITED = "prohibited"


class TaskType(StrEnum):
    CODE_CHANGE = "code_change"
    TEST = "test"
    REVIEW = "review"
    RESEARCH = "research"
    ANALYSIS = "analysis"
    OPERATIONS = "operations"
    # Milestone four resale-domain task types.
    PRODUCT_IDENTIFICATION = "product_identification"
    COMPARABLE_RESEARCH = "comparable_research"
    LISTING_GENERATION = "listing_generation"
    PRICING_ANALYSIS = "pricing_analysis"
    INVENTORY_QUALITY_REVIEW = "inventory_quality_review"
    STALE_INVENTORY_REVIEW = "stale_inventory_review"


# Commerce-domain task types that require MCP scopes to execute safely.
DOMAIN_TASK_TYPES: frozenset[TaskType] = frozenset(
    {
        TaskType.PRODUCT_IDENTIFICATION,
        TaskType.COMPARABLE_RESEARCH,
        TaskType.LISTING_GENERATION,
        TaskType.PRICING_ANALYSIS,
        TaskType.INVENTORY_QUALITY_REVIEW,
        TaskType.STALE_INVENTORY_REVIEW,
    }
)


class AgentPermission(StrEnum):
    READ_FILES = "read_files"
    WRITE_FILES = "write_files"
    RUN_COMMANDS = "run_commands"


class JobStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class CapabilityName(StrEnum):
    CODING = "coding"
    TESTING = "testing"
    REVIEW = "review"
    RESEARCH = "research"
    OPERATIONS = "operations"
    ANALYSIS = "analysis"
    # Milestone four domain capabilities.
    WEB_RESEARCH = "web_research"
    INVENTORY_READ = "inventory_read"
    RESEARCH_WRITE = "research_write"
    PRICING_CALCULATION = "pricing_calculation"
    LISTING_DRAFT_GENERATION = "listing_draft_generation"
    APPROVAL_REQUEST_CREATION = "approval_request_creation"


# Scopes that only human review principals may hold. MCP service principals and
# worker identities must never be granted these; enforced at principal creation.
HUMAN_ONLY_SCOPES: frozenset[str] = frozenset({"reviews:write", "comparables:review"})

# Full set of milestone-five API scopes.
DASHBOARD_API_SCOPES: frozenset[str] = frozenset(
    {
        "media:read",
        "media:write",
        "media:process",
        "comparables:read",
        "comparables:write",
        "comparables:review",
        "dashboard:read",
        "reviews:read",
        "reviews:write",
    }
)


KNOWN_FORBIDDEN_ACTIONS = frozenset(
    {
        "publish_listing",
        "change_live_price",
        "accept_offer",
        "issue_refund",
        "contact_buyer",
        "access_payouts",
        "access_marketplace_credentials",
        "access_production_database",
        "execute_unrestricted_sql",
    }
)
REQUIRED_FORBIDDEN_ACTIONS = KNOWN_FORBIDDEN_ACTIONS


def _resolve_workspace(value: Path) -> Path:
    resolved = value.expanduser().resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise ValueError(f"workspace does not exist: {resolved}")
    return resolved


class JobSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requested_agent: str | None = None
    task_type: TaskType
    objective: str = Field(min_length=1, max_length=20_000)
    workspace: Path
    permissions: set[AgentPermission] = Field(default_factory=lambda: {AgentPermission.READ_FILES})
    context_files: list[Path] = Field(default_factory=list)
    forbidden_actions: set[str] = Field(default_factory=lambda: set(REQUIRED_FORBIDDEN_ACTIONS))
    required_capabilities: set[CapabilityName] = Field(default_factory=set)
    risk_tier: RiskTier = RiskTier.READ_ONLY
    timeout_seconds: int = Field(default=600, ge=1, le=86_400)
    max_output_chars: int = Field(default=100_000, ge=1, le=10_000_000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("workspace")
    @classmethod
    def workspace_must_exist(cls, value: Path) -> Path:
        return _resolve_workspace(value)

    @field_validator("forbidden_actions")
    @classmethod
    def forbidden_actions_are_known_and_complete(cls, value: set[str]) -> set[str]:
        unknown = value - KNOWN_FORBIDDEN_ACTIONS
        if unknown:
            raise ValueError(f"unknown forbidden actions: {', '.join(sorted(unknown))}")
        missing = REQUIRED_FORBIDDEN_ACTIONS - value
        if missing:
            raise ValueError(f"required forbidden actions missing: {', '.join(sorted(missing))}")
        return value

    @model_validator(mode="after")
    def resolve_context_files(self) -> JobSubmission:
        resolved_files: list[Path] = []
        for path in self.context_files:
            candidate = path.expanduser()
            if not candidate.is_absolute():
                candidate = self.workspace / candidate
            candidate = candidate.resolve()
            if not candidate.is_relative_to(self.workspace):
                raise ValueError(f"context file is outside workspace: {path}")
            if not candidate.exists() or not candidate.is_file():
                raise ValueError(f"context file does not exist: {candidate}")
            resolved_files.append(candidate)
        self.context_files = resolved_files
        return self


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    agent: str
    task_type: TaskType
    objective: str
    workspace: Path
    permissions: set[AgentPermission]
    context_files: list[Path]
    forbidden_actions: set[str]
    timeout_seconds: int = Field(ge=1, le=86_400)
    max_output_chars: int = Field(ge=1, le=10_000_000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("workspace")
    @classmethod
    def workspace_must_exist(cls, value: Path) -> Path:
        return _resolve_workspace(value)

    @model_validator(mode="after")
    def context_is_contained(self) -> ExecutionRequest:
        for path in self.context_files:
            if not path.resolve().is_relative_to(self.workspace):
                raise ValueError(f"context file is outside workspace: {path}")
        return self


class ExecutionResult(BaseModel):
    job_id: UUID
    agent: str
    process_id: int | None = None
    command: list[str] = Field(default_factory=list)
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = Field(ge=0)
    timed_out: bool = False
    cancelled: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_total_chars: int = Field(default=0, ge=0)
    stderr_total_chars: int = Field(default=0, ge=0)
    structured_result: dict[str, Any] | None = None
    failure_reason: str | None = None

    @property
    def succeeded(self) -> bool:
        return (
            self.exit_code == 0
            and not self.timed_out
            and not self.cancelled
            and self.failure_reason is None
        )


class CancellationRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2_000)
    requested_by: str = Field(default="human:operator", min_length=1, max_length=200)


class AgentCapability(BaseModel):
    name: CapabilityName
    task_types: set[TaskType] = Field(default_factory=set)
    permissions: set[AgentPermission] = Field(default_factory=set)


class AgentHealthResult(BaseModel):
    agent: str
    available: bool
    executable: str
    detail: str | None = None


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    requested_agent: str | None
    agent_name: str | None
    task_type: str
    objective: str
    workspace_path: str
    permissions: list[str]
    context_files: list[str]
    forbidden_actions: list[str]
    status: JobStatus
    timeout_seconds: int
    max_output_chars: int
    exit_code: int | None
    stdout: str
    stderr: str
    structured_result: dict[str, Any] | None
    metadata: dict[str, Any] = Field(validation_alias="job_metadata")
    failure_reason: str | None
    cancellation_reason: str | None
    output_truncated: bool
    stdout_total_chars: int
    stderr_total_chars: int
    process_id: int | None
    version: int
    created_at: datetime
    queued_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None


# Backward-compatible names used by the original direct adapter entry point.
class AgentJob(JobSubmission):
    job_id: UUID = Field(default_factory=uuid4)
    allowed_tools: list[str] = Field(default_factory=list)

    @field_validator("forbidden_actions")
    @classmethod
    def forbidden_actions_are_known_and_complete(cls, value: set[str]) -> set[str]:
        unknown = value - KNOWN_FORBIDDEN_ACTIONS
        if unknown:
            raise ValueError(f"unknown forbidden actions: {', '.join(sorted(unknown))}")
        return value | REQUIRED_FORBIDDEN_ACTIONS


AgentRun = ExecutionResult
