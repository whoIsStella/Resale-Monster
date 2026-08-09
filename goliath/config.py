from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AUTOMATION_MODES = (
    "fake",
    "simulation",
    "disabled",
    "observe",
    "shadow",
    "sandbox",
    "canary",
    "production",
    "autonomous_conservative",
    "autonomous_normal",
    "paused",
)
AutomationModeLiteral = Literal[
    "fake",
    "simulation",
    "disabled",
    "observe",
    "shadow",
    "sandbox",
    "canary",
    "production",
    "autonomous_conservative",
    "autonomous_normal",
    "paused",
]

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


class MediaConfig(BaseModel):
    """Milestone-five media ingestion and image-processing configuration."""

    model_config = ConfigDict(extra="forbid")

    media_root: Path = Field(default=Path("/srv/resale-goliath/media"))
    quarantine_root: Path = Field(default=Path("/srv/resale-goliath/quarantine"))
    temp_upload_root: Path = Field(default=Path("/srv/resale-goliath/tmp"))
    allowed_mime_types: set[str] = Field(
        default_factory=lambda: {"image/jpeg", "image/png", "image/webp"}
    )
    allowed_extensions: set[str] = Field(default_factory=lambda: {".jpg", ".jpeg", ".png", ".webp"})
    max_file_bytes: int = Field(default=25_000_000, ge=1, le=1_000_000_000)
    max_image_dimension: int = Field(default=12_000, ge=16, le=100_000)
    min_image_dimension: int = Field(default=200, ge=1, le=100_000)
    thumbnail_dimension: int = Field(default=256, ge=16, le=4_096)
    preview_dimension: int = Field(default=1024, ge=32, le=8_192)
    jpeg_quality: int = Field(default=85, ge=1, le=100)
    pad_square_canvas: bool = False
    processing_max_attempts: int = Field(default=3, ge=1, le=20)
    processing_backoff_seconds: float = Field(default=5.0, ge=0, le=3_600)
    blur_variance_threshold: float = Field(default=50.0, ge=0, le=100_000)
    max_aspect_ratio: float = Field(default=4.0, gt=1, le=100)

    @model_validator(mode="after")
    def roots_are_distinct(self) -> MediaConfig:
        roots = {
            "media_root": self.media_root.expanduser().resolve(),
            "quarantine_root": self.quarantine_root.expanduser().resolve(),
            "temp_upload_root": self.temp_upload_root.expanduser().resolve(),
        }
        resolved = list(roots.values())
        if len(set(resolved)) != len(resolved):
            raise ValueError("media, quarantine, and temp roots must be distinct")
        # Reject nesting one storage root inside another (unsafe overlap).
        for name_a, path_a in roots.items():
            for name_b, path_b in roots.items():
                if name_a != name_b and path_a.is_relative_to(path_b):
                    raise ValueError(f"{name_a} must not be nested inside {name_b}")
        if self.min_image_dimension >= self.max_image_dimension:
            raise ValueError("min_image_dimension must be below max_image_dimension")
        return self


class AnalysisProviderConfig(BaseModel):
    """Optional image-analysis provider; disabled by default, no vendor hard-coded."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    provider: str = "fake"
    min_confidence: float = Field(default=0.5, ge=0, le=1)


class ComparableImportConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_rows: int = Field(default=1_000, ge=1, le=100_000)
    allowed_currencies: set[str] = Field(default_factory=lambda: {"USD", "EUR", "GBP", "CAD"})


class PricingSourcePolicy(BaseModel):
    """Configurable, versioned policy governing which comparables feed pricing."""

    model_config = ConfigDict(extra="forbid")

    version: str = "policy-v1"
    reviewed_only: bool = True
    min_comparable_count: int = Field(default=1, ge=0, le=1_000)
    max_comparable_age_days: int = Field(default=180, ge=1, le=3_650)
    sold_weight: float = Field(default=0.7, ge=0, le=1)
    active_weight: float = Field(default=0.3, ge=0, le=1)
    reliability_threshold: float = Field(default=0.3, ge=0, le=1)
    similarity_threshold: float = Field(default=0.3, ge=0, le=1)
    outlier_z_threshold: float = Field(default=3.0, gt=0, le=10)
    allowed_currencies: set[str] = Field(default_factory=lambda: {"USD"})
    marketplace_weighting: dict[str, float] = Field(default_factory=dict)
    fallback_to_cost_plus: bool = True

    @model_validator(mode="after")
    def weights_positive(self) -> PricingSourcePolicy:
        if self.sold_weight + self.active_weight <= 0:
            raise ValueError("comparable weights must sum to a positive value")
        return self


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
    approval_risk_tiers: set[str] = Field(default_factory=lambda: {"low", "medium", "high"})
    mcp_host: str = "127.0.0.1"
    mcp_transport: Literal["stdio", "http"] = "stdio"
    mcp_http_port: int = Field(default=8900, ge=1, le=65_535)
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
            "media:read",
            "media:write",
            "comparables:read",
            "comparables:write",
        }
    )
    # Milestone five.
    media: MediaConfig = Field(default_factory=MediaConfig)
    analysis_provider: AnalysisProviderConfig = Field(default_factory=AnalysisProviderConfig)
    comparable_import: ComparableImportConfig = Field(default_factory=ComparableImportConfig)
    pricing_source_policy: PricingSourcePolicy = Field(default_factory=PricingSourcePolicy)
    review_task_expiration_seconds: int = Field(default=1_209_600, ge=60, le=31_536_000)
    dashboard_max_page_size: int = Field(default=200, ge=1, le=1_000)
    bulk_operation_max_items: int = Field(default=200, ge=1, le=5_000)

    @model_validator(mode="after")
    def fee_version_exists(self) -> DomainConfig:
        if self.default_fee_version not in self.fee_versions:
            raise ValueError(f"default_fee_version not defined: {self.default_fee_version}")
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
        # Keep media_storage_root aligned with the media root by default.
        if self.media.media_root != Path("/srv/resale-goliath/media"):
            self.media_storage_root = self.media.media_root
        return self

    def resolve_quarantine_path(self, candidate: Path) -> Path:
        root = self.media.quarantine_root.expanduser().resolve()
        resolved = (
            (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        )
        if not resolved.is_relative_to(root):
            raise ValueError(f"quarantine path is outside the configured root: {candidate}")
        return resolved

    def resolve_media_path(self, candidate: Path) -> Path:
        """Reject media paths that escape the configured storage root."""
        root = self.media_storage_root.expanduser().resolve()
        resolved = (
            (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        )
        if not resolved.is_relative_to(root):
            raise ValueError(f"media path is outside the configured root: {candidate}")
        return resolved


class SessionEncryptionConfig(BaseModel):
    """Session-state-at-rest settings. Persistent sessions must be encrypted."""

    model_config = ConfigDict(extra="forbid")

    session_root: Path = Field(default=Path("data/sessions"))
    require_encryption: bool = True
    encryption_key: str | None = None
    session_ttl_seconds: int = Field(default=1_209_600, ge=60, le=31_536_000)
    browser_executable: str | None = None

    @model_validator(mode="after")
    def encryption_is_required(self) -> SessionEncryptionConfig:
        if not self.require_encryption:
            raise ValueError("persistent marketplace sessions must be encrypted at rest")
        return self


class PublishingRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minimum_expected_profit: Decimal = Field(default=Decimal("5.00"), ge=0)
    require_images: bool = True
    min_images: int = Field(default=1, ge=0, le=50)
    max_active_listings_per_item: int = Field(default=6, ge=1, le=50)


class CrossListingRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Literal["single", "preferred", "all_eligible", "category"] = "preferred"
    preferred_marketplaces: list[str] = Field(default_factory=lambda: ["ebay", "poshmark"])
    category_marketplaces: dict[str, list[str]] = Field(default_factory=dict)
    max_active_listings: int = Field(default=4, ge=1, le=20)
    delayed_secondary_seconds: int = Field(default=0, ge=0, le=604_800)


class OfferRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minimum_net_profit: Decimal = Field(default=Decimal("12.00"), ge=0)
    minimum_margin_percent: float = Field(default=20.0, ge=0, le=100)
    maximum_discount_percent: float = Field(default=35.0, ge=0, le=100)
    automatic_accept: bool = True
    automatic_counter: bool = True
    automatic_decline: bool = True
    counter_increment: Decimal = Field(default=Decimal("1.00"), ge=0)
    cooldown_minutes: int = Field(default=60, ge=0, le=100_000)


class MessagingRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_categories: list[str] = Field(
        default_factory=lambda: [
            "availability",
            "measurements",
            "condition_details",
            "included_accessories",
            "shipping_timing",
            "tracking_status",
            "bundle_requests",
            "price_questions",
            "offer_explanations",
            "thank_you",
            "cancellation_ack",
        ]
    )
    escalation_categories: list[str] = Field(
        default_factory=lambda: [
            "threat",
            "harassment",
            "legal_claim",
            "counterfeit_allegation",
            "chargeback",
            "fraud",
            "off_platform_payment",
            "personal_contact_request",
            "tracking_dispute",
            "unusual_refund_demand",
        ]
    )

    @model_validator(mode="after")
    def categories_present(self) -> MessagingRules:
        if not self.allowed_categories:
            raise ValueError("buyer messaging requires at least one allowed category")
        return self


class PricingAutomationRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minimum_net_profit: Decimal = Field(default=Decimal("12.00"), ge=0)
    maximum_daily_reduction_percent: float = Field(default=10.0, gt=0, le=100)
    maximum_total_reduction_percent: float = Field(default=40.0, gt=0, le=100)
    cooldown_hours: int = Field(default=24, ge=0, le=10_000)
    marketplace_rounding: Decimal = Field(default=Decimal("1.00"), gt=0)
    stale_thresholds: dict[int, float] = Field(
        default_factory=lambda: {14: 5.0, 30: 8.0, 60: 12.0, 90: 15.0}
    )


class RelistingRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    stale_after_days: int = Field(default=90, ge=1, le=3650)
    cooldown_hours: int = Field(default=24, ge=1, le=8760)
    maximum_relists: int = Field(default=3, ge=0, le=100)


class ShippingRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    automatic_label_purchase: bool = True
    maximum_label_cost: Decimal = Field(default=Decimal("18.00"), ge=0)
    require_confirmed_weight_above: float = Field(default=5.0, ge=0)
    default_package_profile: str = "polymailer-medium"

    @model_validator(mode="after")
    def label_ceiling_present(self) -> ShippingRules:
        if self.automatic_label_purchase and self.maximum_label_cost <= 0:
            raise ValueError("automatic label purchase requires a positive cost ceiling")
        return self


class RefundRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    automatic_limit: Decimal = Field(default=Decimal("30.00"), ge=0)
    allowed_reasons: list[str] = Field(
        default_factory=lambda: [
            "order_cancelled_before_shipping",
            "verified_inventory_error",
            "duplicate_sale",
        ]
    )
    require_exception_review_above: Decimal = Field(default=Decimal("30.00"), ge=0)

    @model_validator(mode="after")
    def ceiling_present(self) -> RefundRules:
        if self.enabled and self.automatic_limit <= 0:
            raise ValueError("automatic refunds require a positive amount ceiling")
        return self


class CircuitBreakerRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failure_threshold: int = Field(default=5, ge=1, le=1000)
    cooldown_seconds: int = Field(default=300, ge=1, le=86_400)
    half_open_probe_after_seconds: int = Field(default=300, ge=1, le=86_400)


class MarketplaceScheduleSetting(BaseModel):
    """One independently controllable durable marketplace schedule."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    interval_seconds: int = Field(ge=5, le=2_592_000)


class MarketplaceScheduleConfig(BaseModel):
    """Safe development defaults for the durable marketplace workflows."""

    model_config = ConfigDict(extra="forbid")

    account_sync: MarketplaceScheduleSetting = Field(
        default_factory=lambda: MarketplaceScheduleSetting(interval_seconds=300)
    )
    listing_sync: MarketplaceScheduleSetting = Field(
        default_factory=lambda: MarketplaceScheduleSetting(interval_seconds=600)
    )
    listing_reconciliation: MarketplaceScheduleSetting = Field(
        default_factory=lambda: MarketplaceScheduleSetting(interval_seconds=1800)
    )
    order_sync: MarketplaceScheduleSetting = Field(
        default_factory=lambda: MarketplaceScheduleSetting(interval_seconds=300)
    )
    offer_sync: MarketplaceScheduleSetting = Field(
        default_factory=lambda: MarketplaceScheduleSetting(interval_seconds=300)
    )
    message_sync: MarketplaceScheduleSetting = Field(
        default_factory=lambda: MarketplaceScheduleSetting(interval_seconds=300)
    )
    pricing: MarketplaceScheduleSetting = Field(
        default_factory=lambda: MarketplaceScheduleSetting(interval_seconds=21_600)
    )
    refresh: MarketplaceScheduleSetting = Field(
        default_factory=lambda: MarketplaceScheduleSetting(interval_seconds=43_200)
    )
    promotion_share: MarketplaceScheduleSetting = Field(
        default_factory=lambda: MarketplaceScheduleSetting(interval_seconds=10_800)
    )
    stale_inventory: MarketplaceScheduleSetting = Field(
        default_factory=lambda: MarketplaceScheduleSetting(interval_seconds=86_400)
    )
    shipping: MarketplaceScheduleSetting = Field(
        default_factory=lambda: MarketplaceScheduleSetting(interval_seconds=600)
    )
    tracking: MarketplaceScheduleSetting = Field(
        default_factory=lambda: MarketplaceScheduleSetting(interval_seconds=900)
    )
    financial_reconciliation: MarketplaceScheduleSetting = Field(
        default_factory=lambda: MarketplaceScheduleSetting(interval_seconds=3600)
    )
    reservation_cleanup: MarketplaceScheduleSetting = Field(
        default_factory=lambda: MarketplaceScheduleSetting(interval_seconds=3600)
    )
    circuit_breaker_probe: MarketplaceScheduleSetting = Field(
        default_factory=lambda: MarketplaceScheduleSetting(interval_seconds=300)
    )


class MarketplaceAccountConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    marketplace: str
    label: str
    automation_mode: AutomationModeLiteral = "disabled"
    currency: str = "USD"
    capabilities: set[str] = Field(default_factory=set)
    allowed_domains: list[str] = Field(default_factory=list)
    operation_modes: dict[str, AutomationModeLiteral] = Field(default_factory=dict)
    ebay_marketplace_id: str = "EBAY_US"
    merchant_location_key: str | None = None
    payment_policy_id: str | None = None
    fulfillment_policy_id: str | None = None
    return_policy_id: str | None = None

    @model_validator(mode="after")
    def domains_present(self) -> MarketplaceAccountConfig:
        from goliath.marketplace.adapter import MARKETPLACE_OPERATIONS

        unknown = (set(self.capabilities) | set(self.operation_modes)) - set(MARKETPLACE_OPERATIONS)
        if unknown:
            raise ValueError(f"unsupported marketplace operations: {', '.join(sorted(unknown))}")
        if not self.allowed_domains:
            raise ValueError(f"marketplace account {self.label} requires an allowed-domain list")
        expected_domains = {
            "ebay": ("ebay.com",),
            "poshmark": ("poshmark.com",),
            "depop": ("depop.com",),
            "mercari": ("mercari.com",),
            "grailed": ("grailed.com",),
            "facebook": ("facebook.com",),
        }.get(self.marketplace, ())
        for domain in self.allowed_domains:
            normalized = domain.strip().lower().rstrip(".")
            if "://" in normalized or "/" in normalized or not normalized:
                raise ValueError(f"invalid marketplace domain allowlist entry: {domain}")
            if expected_domains and not any(
                normalized == expected or normalized.endswith("." + expected)
                for expected in expected_domains
            ):
                raise ValueError(f"domain {domain} is outside the {self.marketplace} allowlist")
        return self


class EbayOAuthConfig(BaseModel):
    """References only: client secrets and tokens never belong in configuration files."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    client_id_env: str = "GOLIATH_EBAY_CLIENT_ID"
    client_secret_env: str = "GOLIATH_EBAY_CLIENT_SECRET"
    redirect_uri: str | None = None
    sandbox_authorization_url: str = "https://auth.sandbox.ebay.com/oauth2/authorize"
    production_authorization_url: str = "https://auth.ebay.com/oauth2/authorize"
    sandbox_token_url: str = "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
    production_token_url: str = "https://api.ebay.com/identity/v1/oauth2/token"
    scopes: list[str] = Field(
        default_factory=lambda: [
            "https://api.ebay.com/oauth/api_scope/sell.account.readonly",
            "https://api.ebay.com/oauth/api_scope/sell.inventory",
            "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
        ]
    )
    state_ttl_seconds: int = Field(default=600, ge=60, le=1800)
    token_refresh_margin_seconds: int = Field(default=300, ge=30, le=3600)

    @model_validator(mode="after")
    def validate_oauth(self) -> EbayOAuthConfig:
        for name in (self.client_id_env, self.client_secret_env):
            if not name.startswith("GOLIATH_EBAY_") or not name.replace("_", "").isalnum():
                raise ValueError("invalid eBay credential environment reference")
        if self.enabled and not self.redirect_uri:
            raise ValueError("enabled eBay OAuth requires a redirect URI")
        if self.redirect_uri:
            from urllib.parse import urlsplit

            parsed = urlsplit(self.redirect_uri)
            loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            if parsed.scheme != "https" and not (loopback and parsed.scheme == "http"):
                raise ValueError("eBay OAuth redirect must use HTTPS or loopback HTTP")
            if parsed.fragment or parsed.username or parsed.password:
                raise ValueError("invalid eBay OAuth redirect URI")
        return self


class MarketplaceTransportConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connect_timeout_seconds: float = Field(default=5, gt=0, le=60)
    read_timeout_seconds: float = Field(default=20, gt=0, le=120)
    total_timeout_seconds: float = Field(default=30, gt=0, le=180)
    max_response_bytes: int = Field(default=2_000_000, ge=1024, le=20_000_000)
    max_retries: int = Field(default=2, ge=0, le=5)


class CanaryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_account_ids: set[str] = Field(default_factory=set)
    allowed_inventory_ids: set[str] = Field(default_factory=set)
    allowed_operations: set[str] = Field(default_factory=set)
    maximum_writes_per_hour: int = Field(default=10, ge=1, le=1000)
    maximum_listings_per_day: int = Field(default=5, ge=1, le=1000)
    maximum_label_cost: Decimal = Field(default=Decimal(0), ge=0)
    maximum_price_change_percent: Decimal = Field(default=Decimal(10), gt=0, le=100)
    maximum_offer_value: Decimal = Field(default=Decimal(0), ge=0)
    mandatory_verification: bool = True
    automatic_stop_failures: int = Field(default=2, ge=1, le=100)


class MarketplaceAutomationConfig(BaseModel):
    """Typed configuration for milestone-six autonomous marketplace operations."""

    model_config = ConfigDict(extra="forbid")

    default_mode: AutomationModeLiteral = "disabled"
    operation_modes: dict[str, AutomationModeLiteral] = Field(default_factory=dict)
    session: SessionEncryptionConfig = Field(default_factory=SessionEncryptionConfig)
    ebay_oauth: EbayOAuthConfig = Field(default_factory=EbayOAuthConfig)
    transport: MarketplaceTransportConfig = Field(default_factory=MarketplaceTransportConfig)
    canary: CanaryConfig = Field(default_factory=CanaryConfig)
    accounts: dict[str, MarketplaceAccountConfig] = Field(default_factory=dict)
    publishing: PublishingRules = Field(default_factory=PublishingRules)
    cross_listing: CrossListingRules = Field(default_factory=CrossListingRules)
    pricing: PricingAutomationRules = Field(default_factory=PricingAutomationRules)
    relisting: RelistingRules = Field(default_factory=RelistingRules)
    offers: OfferRules = Field(default_factory=OfferRules)
    messaging: MessagingRules = Field(default_factory=MessagingRules)
    shipping: ShippingRules = Field(default_factory=ShippingRules)
    refunds: RefundRules = Field(default_factory=RefundRules)
    circuit_breaker: CircuitBreakerRules = Field(default_factory=CircuitBreakerRules)
    schedules: MarketplaceScheduleConfig = Field(default_factory=MarketplaceScheduleConfig)
    operation_rate_limit_per_minute: int = Field(default=60, ge=1, le=100_000)
    max_publish_per_hour: int = Field(default=100, ge=1, le=100_000)
    max_delisting_latency_seconds: int = Field(default=900, ge=1, le=86_400)
    reservation_ttl_seconds: int = Field(default=86_400, ge=60, le=2_592_000)
    max_operation_retries: int = Field(default=5, ge=1, le=100)
    high_value_threshold: Decimal = Field(default=Decimal("500.00"), ge=0)
    conservative_profit_multiplier: Decimal = Field(default=Decimal("1.50"), ge=1, le=10)
    marketplace_scopes: set[str] = Field(
        default_factory=lambda: {
            "marketplace:read",
            "marketplace:listing:create",
            "marketplace:listing:update",
            "marketplace:listing:refresh",
            "marketplace:listing:end",
            "marketplace:listing:promote",
            "marketplace:listing:share",
            "marketplace:offer:read",
            "marketplace:offer:respond",
            "marketplace:message:read",
            "marketplace:message:routine",
            "marketplace:order:read",
            "marketplace:tracking:update",
            "marketplace:label:purchase",
            "marketplace:sync",
        }
    )

    @model_validator(mode="after")
    def autonomous_requires_profit_rules(self) -> MarketplaceAutomationConfig:
        from goliath.marketplace.adapter import MARKETPLACE_OPERATIONS

        unknown = set(self.operation_modes) - set(MARKETPLACE_OPERATIONS)
        if unknown:
            raise ValueError(f"unsupported marketplace operations: {', '.join(sorted(unknown))}")
        autonomous = {
            "sandbox",
            "canary",
            "production",
            "autonomous_conservative",
            "autonomous_normal",
        }
        modes = {self.default_mode} | {a.automation_mode for a in self.accounts.values()}
        if modes & autonomous:
            if self.publishing.minimum_expected_profit <= 0:
                raise ValueError("autonomous mode requires a positive minimum expected profit")
            if self.offers.minimum_net_profit <= 0 or self.pricing.minimum_net_profit <= 0:
                raise ValueError("autonomous mode requires minimum-profit rules")
            if not self.session.encryption_key and not os.environ.get("GOLIATH_SESSION_KEY"):
                raise ValueError("autonomous mode requires a persistent session encryption key")
        if "production" in modes:
            if not self.ebay_oauth.enabled:
                raise ValueError("production mode requires eBay authentication")
            if not self.canary.allowed_operations:
                raise ValueError("production rollout requires explicitly allowed operations")
        return self


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
    marketplace: MarketplaceAutomationConfig = Field(default_factory=MarketplaceAutomationConfig)

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
        # Marketplace session state must never live inside an agent workspace.
        session_root = self.marketplace.session.session_root.expanduser().resolve()
        for root in self.workspace_roots:
            if session_root == root or session_root.is_relative_to(root):
                raise ValueError("marketplace session root must be outside all agent workspaces")
        # General coding agents must not inherit marketplace scopes.
        marketplace_scopes = self.marketplace.marketplace_scopes
        for name, agent in self.agents.items():
            if agent.adapter == "codex" and set(agent.mcp_scopes) & marketplace_scopes:
                raise ValueError(f"coding agent '{name}' must not hold marketplace scopes")
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
