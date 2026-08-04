from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from goliath.core.schemas import AgentPermission, CapabilityName, TaskType

SAFE_ENVIRONMENT_NAMES = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR"})
SENSITIVE_ENVIRONMENT_FRAGMENTS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
    "PAYOUT",
    "DATABASE_URL",
    "API_KEY",
)


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    adapter: Literal["codex", "hermes", "subprocess"] = "subprocess"
    command_template: list[str] = Field(min_length=1)
    profile: str | None = None
    capabilities: set[CapabilityName] = Field(default_factory=set)
    task_types: set[TaskType] = Field(default_factory=set)
    permissions: set[AgentPermission] = Field(default_factory=lambda: {AgentPermission.READ_FILES})
    priority: int = Field(default=100, ge=0, le=10_000)
    environment_allowlist: set[str] = Field(default_factory=lambda: set(SAFE_ENVIRONMENT_NAMES))
    # MCP scopes this agent's service principal holds; gates commerce-domain routing.
    mcp_scopes: set[str] = Field(default_factory=set)
    preferred_domain_task_types: set[TaskType] = Field(default_factory=set)

    @field_validator("command_template")
    @classmethod
    def command_is_argument_sequence(cls, value: list[str]) -> list[str]:
        if any(not argument for argument in value):
            raise ValueError("command template arguments must not be empty")
        return value

    @field_validator("environment_allowlist")
    @classmethod
    def environment_names_are_safe(cls, value: set[str]) -> set[str]:
        unsafe = {
            name
            for name in value
            if name not in SAFE_ENVIRONMENT_NAMES
            or any(fragment in name.upper() for fragment in SENSITIVE_ENVIRONMENT_FRAGMENTS)
        }
        if unsafe:
            raise ValueError(f"unsafe environment variables: {', '.join(sorted(unsafe))}")
        return value


class MarketplaceFeeVersion(BaseModel):
    """Versioned, configuration-driven marketplace economics. No values are hard-coded."""

    model_config = ConfigDict(extra="forbid")

    fee_percent: float = Field(ge=0, le=1)
    payment_percent: float = Field(default=0.0, ge=0, le=1)
    fixed_fee: float = Field(default=0.0, ge=0)

    @model_validator(mode="after")
    def percents_are_valid(self) -> MarketplaceFeeVersion:
        if self.fee_percent + self.payment_percent >= 1:
            raise ValueError("combined fee and payment percentages must be below 100%")
        return self


class PricingRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_margin: float = Field(default=0.40, ge=0, lt=1)
    minimum_profit: float = Field(default=5.0, ge=0)
    sold_comparable_weight: float = Field(default=0.7, ge=0, le=1)
    active_comparable_weight: float = Field(default=0.3, ge=0, le=1)
    fast_sale_factor: float = Field(default=0.85, gt=0, le=1)
    condition_adjustments: dict[str, float] = Field(
        default_factory=lambda: {
            "new": 1.0,
            "like_new": 0.92,
            "good": 0.8,
            "fair": 0.65,
            "poor": 0.45,
        }
    )
    stale_inventory_adjustment: float = Field(default=0.9, gt=0, le=1)
    size_demand_adjustment: float = Field(default=1.0, gt=0, le=2)

    @model_validator(mode="after")
    def weights_are_valid(self) -> PricingRules:
        total = self.sold_comparable_weight + self.active_comparable_weight
        if total <= 0:
            raise ValueError("comparable weights must sum to a positive value")
        return self


class MarketplaceConstraintConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = "v1"
    max_title_length: int = Field(default=80, ge=1, le=500)
    required_fields: list[str] = Field(default_factory=lambda: ["title", "condition", "price"])
    allowed_conditions: list[str] = Field(
        default_factory=lambda: ["new", "like_new", "good", "fair", "poor"]
    )


class CompletenessRuleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required_fields: list[str] = Field(default_factory=list)
    recommended_fields: list[str] = Field(default_factory=list)
    required_measurements: list[str] = Field(default_factory=list)


class DomainConfig(BaseModel):
    """Typed configuration for milestone-four resale-domain behavior."""

    model_config = ConfigDict(extra="forbid")

    media_storage_root: Path = Field(default=Path("/srv/resale-goliath/media"))
    supported_image_types: set[str] = Field(
        default_factory=lambda: {"image/jpeg", "image/png", "image/webp"}
    )
    max_image_bytes: int = Field(default=25_000_000, ge=1, le=1_000_000_000)
    identification_auto_threshold: float = Field(default=0.85, ge=0, le=1)
    research_min_confidence: float = Field(default=0.5, ge=0, le=1)
    max_research_sources: int = Field(default=25, ge=1, le=1000)
    max_comparables: int = Field(default=50, ge=1, le=1000)
    pricing_rules: PricingRules = Field(default_factory=PricingRules)
    default_fee_version: str = "generic-v1"
    fee_versions: dict[str, MarketplaceFeeVersion] = Field(
        default_factory=lambda: {
            "generic-v1": MarketplaceFeeVersion(
                fee_percent=0.10, payment_percent=0.03, fixed_fee=0.30
            ),
            "ebay-2026-01": MarketplaceFeeVersion(
                fee_percent=0.1335, payment_percent=0.0, fixed_fee=0.30
            ),
            "poshmark-2026-01": MarketplaceFeeVersion(
                fee_percent=0.20, payment_percent=0.0, fixed_fee=0.0
            ),
        }
    )
    completeness_rules: dict[str, CompletenessRuleConfig] = Field(
        default_factory=lambda: {
            "clothing": CompletenessRuleConfig(
                required_fields=["size_label", "condition", "materials"],
                recommended_fields=["brand", "colors", "pattern"],
                required_measurements=["chest_flat"],
            ),
            "shoes": CompletenessRuleConfig(
                required_fields=["size_label", "condition"],
                recommended_fields=["brand", "colors"],
                required_measurements=["outsole_length"],
            ),
            "bags": CompletenessRuleConfig(
                required_fields=["materials", "condition"],
                recommended_fields=["brand", "colors"],
                required_measurements=[],
            ),
            "electronics": CompletenessRuleConfig(
                required_fields=["model_name", "condition"],
                recommended_fields=["brand"],
                required_measurements=[],
            ),
        }
    )
    marketplace_constraints: dict[str, MarketplaceConstraintConfig] = Field(
        default_factory=lambda: {
            "ebay": MarketplaceConstraintConfig(version="ebay-v1", max_title_length=80),
            "poshmark": MarketplaceConstraintConfig(version="poshmark-v1", max_title_length=50),
            "depop": MarketplaceConstraintConfig(version="depop-v1", max_title_length=65),
            "mercari": MarketplaceConstraintConfig(version="mercari-v1", max_title_length=80),
            "grailed": MarketplaceConstraintConfig(version="grailed-v1", max_title_length=60),
            "facebook": MarketplaceConstraintConfig(version="facebook-v1", max_title_length=100),
            "generic": MarketplaceConstraintConfig(version="generic-v1", max_title_length=140),
        }
    )
    proposal_expiration_seconds: int = Field(default=604_800, ge=60, le=31_536_000)
    approval_risk_tiers: set[str] = Field(
        default_factory=lambda: {"low", "medium", "high"}
    )
    mcp_host: str = "127.0.0.1"
    mcp_transport: Literal["stdio", "http"] = "stdio"
    mcp_authentication_required: bool = True
    mcp_allow_unauthenticated_non_loopback: bool = False
    mcp_scopes: set[str] = Field(
        default_factory=lambda: {
            "inventory:read",
            "inventory:write_draft",
            "research:read",
            "research:write",
            "pricing:calculate",
            "listing:read",
            "listing:write_draft",
            "approval:read",
            "approval:request",
        }
    )

    @model_validator(mode="after")
    def fee_version_exists(self) -> DomainConfig:
        if self.default_fee_version not in self.fee_versions:
            raise ValueError(
                f"default_fee_version not defined: {self.default_fee_version}"
            )
        if not self.approval_risk_tiers:
            raise ValueError("at least one approval risk tier is required")
        if (
            self.mcp_authentication_required is False
            and self.mcp_transport == "http"
            and self.mcp_host not in {"127.0.0.1", "localhost", "::1"}
            and not self.mcp_allow_unauthenticated_non_loopback
        ):
            raise ValueError(
                "unauthenticated non-loopback MCP exposure requires an explicit override"
            )
        return self

    def resolve_media_path(self, candidate: Path) -> Path:
        """Reject media paths that escape the configured storage root."""
        root = self.media_storage_root.expanduser().resolve()
        resolved = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"media path is outside the configured root: {candidate}")
        return resolved


class OrchestrationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_timeout_seconds: int = Field(default=600, ge=1, le=86_400)
    maximum_timeout_seconds: int = Field(default=3_600, ge=1, le=86_400)
    default_output_limit_chars: int = Field(default=100_000, ge=1, le=10_000_000)
    maximum_output_limit_chars: int = Field(default=1_000_000, ge=1, le=10_000_000)
    cancellation_grace_seconds: float = Field(default=5.0, ge=0.05, le=60)
    worker_concurrency: int = Field(default=1, ge=1, le=128)
    worker_poll_interval_seconds: float = Field(default=1.0, ge=0.05, le=300)
    lease_duration_seconds: float = Field(default=60.0, gt=0, le=86_400)
    heartbeat_interval_seconds: float = Field(default=15.0, gt=0, le=86_399)
    shutdown_grace_seconds: float = Field(default=30.0, ge=0.1, le=300)
    recovery_policy: Literal["fail", "requeue", "retry"] = "retry"
    stale_worker_threshold_seconds: float = Field(default=120.0, gt=0, le=86_400)
    retry_max_attempts: int = Field(default=3, ge=1, le=100)
    retry_fixed_delay_seconds: float = Field(default=5.0, ge=0, le=86_400)
    retry_exponential: bool = True
    retry_max_delay_seconds: float = Field(default=3_600, ge=0, le=86_400)
    retryable_exit_codes: set[int] = Field(default_factory=lambda: {1, 2, 124})
    retryable_failure_categories: set[str] = Field(default_factory=lambda: {"adapter", "process"})
    scheduler_timezone: str = "UTC"
    missed_run_policy: Literal["skip", "catch_up"] = "skip"
    max_catch_up_runs: int = Field(default=1, ge=0, le=100)
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65_535)
    api_authentication_required: bool = True
    allow_unauthenticated_non_loopback: bool = False
    metrics_enabled: bool = False
    metrics_public: bool = False
    idempotency_expiration_seconds: int = Field(default=86_400, ge=1, le=2_592_000)
    workspace_roots: list[Path] = Field(min_length=1)
    agents: dict[str, AgentConfig] = Field(default_factory=dict)
    domain: DomainConfig = Field(default_factory=DomainConfig)

    @field_validator("workspace_roots")
    @classmethod
    def roots_must_exist(cls, value: list[Path]) -> list[Path]:
        roots = [path.expanduser().resolve() for path in value]
        missing = [str(path) for path in roots if not path.exists() or not path.is_dir()]
        if missing:
            raise ValueError(f"workspace roots do not exist: {', '.join(missing)}")
        return roots

    @model_validator(mode="after")
    def defaults_do_not_exceed_maximums(self) -> OrchestrationConfig:
        if self.default_timeout_seconds > self.maximum_timeout_seconds:
            raise ValueError("default timeout exceeds maximum timeout")
        if self.default_output_limit_chars > self.maximum_output_limit_chars:
            raise ValueError("default output limit exceeds maximum output limit")
        if self.heartbeat_interval_seconds >= self.lease_duration_seconds:
            raise ValueError("heartbeat interval must be shorter than lease duration")
        if (
            not self.api_authentication_required
            and self.api_host not in {"127.0.0.1", "localhost", "::1"}
            and not self.allow_unauthenticated_non_loopback
        ):
            raise ValueError("unauthenticated non-loopback API binding requires explicit override")
        if self.metrics_public and not self.metrics_enabled:
            raise ValueError("public metrics require metrics_enabled")
        return self

    def validate_workspace(self, workspace: Path) -> Path:
        resolved = workspace.expanduser().resolve()
        if not any(resolved.is_relative_to(root) for root in self.workspace_roots):
            raise ValueError(f"workspace is outside configured roots: {resolved}")
        return resolved


def load_config(path: Path | None = None) -> OrchestrationConfig:
    config_path = path or Path(os.getenv("GOLIATH_CONFIG", "config/agents.yaml"))
    if not config_path.exists():
        raise FileNotFoundError(f"configuration file not found: {config_path}")
    with config_path.open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream) or {}
    return OrchestrationConfig.model_validate(data)
