"""Provider interfaces for optional image analysis and comparable acquisition.

Providers are interfaces only. Concrete providers are configuration-selected and
disabled by default. No external vendor is hard-coded, no network is required, and
fakes are supplied for tests. Provider output is always treated as a suggestion:
it is persisted with confidence and requires human review, never mutating final
inventory fields directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from goliath.db.models import utc_now

# --------------------------------------------------------------------------- #
# Image analysis
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class AnalysisSuggestion:
    kind: str  # label_text | dominant_color | category | defect | material | background_removal
    value: str
    confidence: Decimal


@runtime_checkable
class ImageAnalysisProvider(Protocol):
    name: str

    def analyze(self, image_bytes: bytes) -> list[AnalysisSuggestion]: ...


class DisabledImageAnalysisProvider:
    """Default provider: performs no analysis and returns no suggestions."""

    name = "disabled"

    def analyze(self, image_bytes: bytes) -> list[AnalysisSuggestion]:
        return []


class FakeImageAnalysisProvider:
    """Deterministic fake provider for tests; makes no network calls."""

    name = "fake"

    def __init__(self, suggestions: list[AnalysisSuggestion] | None = None) -> None:
        self._suggestions = suggestions or [
            AnalysisSuggestion("category", "clothing", Decimal("0.6")),
            AnalysisSuggestion("dominant_color", "indigo", Decimal("0.8")),
            AnalysisSuggestion("material", "cotton", Decimal("0.55")),
        ]

    def analyze(self, image_bytes: bytes) -> list[AnalysisSuggestion]:
        # Deterministic: identical bytes always yield identical suggestions.
        return list(self._suggestions)


def build_analysis_provider(config) -> ImageAnalysisProvider:
    """Select an analysis provider from configuration (disabled by default)."""
    if not config.analysis_provider.enabled:
        return DisabledImageAnalysisProvider()
    if config.analysis_provider.provider == "fake":
        return FakeImageAnalysisProvider()
    raise ValueError(f"unknown analysis provider: {config.analysis_provider.provider}")


# --------------------------------------------------------------------------- #
# Comparable acquisition
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ComparableRecord:
    marketplace: str
    source_identity: str
    listing_title: str
    is_sold: bool = False
    listed_price: Decimal | None = None
    sold_price: Decimal | None = None
    shipping_price: Decimal | None = None
    currency: str = "USD"
    condition: str | None = None
    size: str | None = None
    sale_date: str | None = None
    source_url: str | None = None
    similarity_score: Decimal | None = None
    reliability_score: Decimal | None = None
    notes: str | None = None


@dataclass(slots=True)
class ProviderPage:
    records: list[ComparableRecord]
    provider: str
    captured_at: datetime = field(default_factory=utc_now)
    next_cursor: str | None = None
    rate_limit_remaining: int | None = None
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ComparableSearchRequest:
    query: str
    limit: int = 25
    cursor: str | None = None


@runtime_checkable
class ComparableProvider(Protocol):
    provider_type: str
    provider_identity: str

    def search(self, request: ComparableSearchRequest) -> ProviderPage: ...


class ManualComparableProvider:
    """Human-entered comparables. Does not scrape or contact any marketplace."""

    provider_type = "manual"

    def __init__(self, records: list[ComparableRecord], *, identity: str = "manual") -> None:
        self.provider_identity = identity
        self._records = records

    def search(self, request: ComparableSearchRequest) -> ProviderPage:
        page = self._records[: request.limit]
        return ProviderPage(records=list(page), provider=self.provider_type)


class FakeComparableProvider:
    """Deterministic fake provider for tests; no network, no scraping."""

    provider_type = "data_provider"

    def __init__(self, records: list[ComparableRecord] | None = None) -> None:
        self.provider_identity = "fake-provider"
        self._records = records or [
            ComparableRecord(
                marketplace="ebay",
                source_identity="fake-1",
                listing_title="Sample sold listing",
                is_sold=True,
                sold_price=Decimal("42.00"),
                reliability_score=Decimal("0.7"),
                similarity_score=Decimal("0.8"),
            )
        ]

    def search(self, request: ComparableSearchRequest) -> ProviderPage:
        return ProviderPage(
            records=list(self._records[: request.limit]),
            provider=self.provider_type,
            rate_limit_remaining=100,
        )
