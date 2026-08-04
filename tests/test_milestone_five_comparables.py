"""Milestone five: comparable import, review, pricing policy, analysis provider."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from goliath.config import OrchestrationConfig
from goliath.db.base import Base
from goliath.db.database import build_session_factory, create_test_engine
from goliath.db.models import (
    ComparableReviewStatus,
    ImportStatus,
    ReviewTaskType,
)
from goliath.domain.comparables import (
    ComparableImportService,
    parse_csv,
    parse_json,
    select_comparables_for_pricing,
)
from goliath.domain.providers import (
    DisabledImageAnalysisProvider,
    FakeComparableProvider,
    FakeImageAnalysisProvider,
    build_analysis_provider,
)
from goliath.domain.review_service import ReviewService
from goliath.domain.service import DomainService


@pytest.fixture
def env(tmp_path: Path):
    engine = create_test_engine()
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    config = OrchestrationConfig(workspace_roots=[tmp_path])
    config.domain.media.media_root = tmp_path / "m"
    config.domain.media.quarantine_root = tmp_path / "q"
    config.domain.media.temp_upload_root = tmp_path / "t"
    domain = DomainService(session_factory=factory, config=config)
    importer = ComparableImportService(session_factory=factory, config=config)
    review = ReviewService(session_factory=factory, config=config)
    item = domain.create_inventory(
        actor="human:op", sku="C1", title="Tee", condition="good", acquisition_cost=Decimal(5)
    )
    yield {
        "factory": factory,
        "config": config,
        "domain": domain,
        "importer": importer,
        "review": review,
        "item": item,
    }
    engine.dispose()


CSV = (
    "marketplace,listing_title,source_identity,is_sold,sold_price,currency,reliability_score,similarity_score\n"
    "ebay,Nike Tee,L1,true,40,USD,0.8,0.9\n"
    "poshmark,Nike Tee 2,L2,true,45,USD,0.7,0.85\n"
    "ebay,Bad row,,,notnum,USD,,\n"
)


def test_csv_import_records_every_row(env) -> None:
    rows = parse_csv(CSV)
    result = env["importer"].import_rows(
        env["item"].id, rows, source_format="csv", actor="human:cli"
    )
    assert result["status"] == ImportStatus.COMPLETED.value
    assert result["imported"] == 2
    assert result["failed"] == 1


def test_json_import(env) -> None:
    content = (
        '[{"marketplace":"ebay","listing_title":"X","source_identity":"J1",'
        '"is_sold":true,"sold_price":"30","currency":"USD"}]'
    )
    rows = parse_json(content)
    result = env["importer"].import_rows(
        env["item"].id, rows, source_format="json", actor="human:cli"
    )
    assert result["imported"] == 1


def test_dry_run_does_not_persist_comparables(env) -> None:
    rows = parse_csv(CSV)
    result = env["importer"].import_rows(
        env["item"].id, rows, source_format="csv", actor="human:cli", dry_run=True
    )
    assert result["status"] == ImportStatus.DRY_RUN.value
    assert env["domain"].find_comparables(env["item"].id) == []


def test_all_or_nothing_rolls_back_on_invalid_row(env) -> None:
    rows = parse_csv(CSV)
    result = env["importer"].import_rows(
        env["item"].id, rows, source_format="csv", actor="human:cli", all_or_nothing=True
    )
    assert result["status"] == ImportStatus.ROLLED_BACK.value
    assert env["domain"].find_comparables(env["item"].id) == []


def test_duplicate_detection(env) -> None:
    rows = parse_csv(CSV)
    env["importer"].import_rows(env["item"].id, rows, source_format="csv", actor="human:cli")
    result = env["importer"].import_rows(
        env["item"].id, rows, source_format="csv", actor="human:cli"
    )
    assert result["duplicates"] == 2


def test_import_creates_comparable_review_tasks(env) -> None:
    from goliath.db.ingestion_repositories import ReviewTaskRepository

    rows = parse_csv(CSV)
    env["importer"].import_rows(env["item"].id, rows, source_format="csv", actor="human:cli")
    with env["factory"]() as session:
        tasks = ReviewTaskRepository(session).list(task_type=ReviewTaskType.COMPARABLE_REVIEW)
    assert len(tasks) == 2


def test_reviewed_only_pricing_excludes_unreviewed(env) -> None:
    rows = parse_csv(CSV)
    env["importer"].import_rows(env["item"].id, rows, source_format="csv", actor="human:cli")
    rec, _ = env["domain"].recommend_price_with_policy(
        env["item"].id, actor="human:op", cost_basis=Decimal(5)
    )
    assert len(rec.included_comparables) == 0
    assert all(x["reason"] == "not_reviewed" for x in rec.excluded_comparables)
    assert rec.policy_version == "policy-v1"


def test_accepted_comparables_feed_pricing(env) -> None:
    rows = parse_csv(CSV)
    env["importer"].import_rows(env["item"].id, rows, source_format="csv", actor="human:cli")
    for comp in env["review"].list_comparables_pending():
        env["review"].review_comparable(
            comp.id, reviewer="human:op", decision=ComparableReviewStatus.ACCEPTED
        )
    rec, _ = env["domain"].recommend_price_with_policy(
        env["item"].id, actor="human:op", cost_basis=Decimal(5)
    )
    assert len(rec.included_comparables) == 2
    assert rec.recommended_price > 0


def test_comparable_review_closes_linked_task(env) -> None:
    from goliath.db.ingestion_repositories import ReviewTaskRepository
    from goliath.db.models import ReviewTaskStatus

    rows = parse_csv(CSV)
    env["importer"].import_rows(env["item"].id, rows, source_format="csv", actor="human:cli")
    comp = env["review"].list_comparables_pending()[0]
    env["review"].review_comparable(
        comp.id, reviewer="human:op", decision=ComparableReviewStatus.ACCEPTED
    )
    with env["factory"]() as session:
        tasks = ReviewTaskRepository(session).list(task_type=ReviewTaskType.COMPARABLE_REVIEW)
    closed = [t for t in tasks if t.resource_id == comp.id]
    assert closed[0].status is ReviewTaskStatus.COMPLETED


def test_pricing_policy_records_exclusion_reasons() -> None:
    from goliath.config import PricingSourcePolicy

    class _Comp:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    now_policy = PricingSourcePolicy(reviewed_only=False, reliability_threshold=0.6)
    comps = [
        _Comp(
            id="1",
            review_status=ComparableReviewStatus.ACCEPTED,
            invalidated_at=None,
            sold_price=Decimal(40),
            listed_price=None,
            currency="USD",
            reliability_score=Decimal("0.9"),
            similarity_score=Decimal("0.9"),
            sale_date=None,
            is_sold=True,
        ),
        _Comp(
            id="2",
            review_status=ComparableReviewStatus.ACCEPTED,
            invalidated_at=None,
            sold_price=Decimal(50),
            listed_price=None,
            currency="USD",
            reliability_score=Decimal("0.1"),
            similarity_score=Decimal("0.9"),
            sale_date=None,
            is_sold=True,
        ),
    ]
    selection = select_comparables_for_pricing(comps, policy=now_policy)
    assert len(selection.included) == 1
    assert selection.excluded_meta[0]["reason"] == "reliability_below_threshold"


def test_analysis_provider_disabled_by_default() -> None:
    config = OrchestrationConfig(workspace_roots=[Path("/tmp")]).domain
    provider = build_analysis_provider(config)
    assert isinstance(provider, DisabledImageAnalysisProvider)
    assert provider.analyze(b"x") == []


def test_fake_analysis_provider_is_deterministic_suggestion_only() -> None:
    provider = FakeImageAnalysisProvider()
    first = provider.analyze(b"image")
    second = provider.analyze(b"image")
    assert [s.value for s in first] == [s.value for s in second]
    assert all(0 <= float(s.confidence) <= 1 for s in first)


def test_fake_comparable_provider_no_network() -> None:
    from goliath.domain.providers import ComparableSearchRequest

    provider = FakeComparableProvider()
    page = provider.search(ComparableSearchRequest(query="nike"))
    assert page.provider == "data_provider"
    assert page.rate_limit_remaining == 100
