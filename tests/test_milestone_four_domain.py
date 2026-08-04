"""Milestone four: domain models, repositories, engines, and workflows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from goliath.config import (
    MarketplaceFeeVersion,
    OrchestrationConfig,
    PricingRules,
)
from goliath.db.base import Base
from goliath.db.database import build_session_factory, create_test_engine
from goliath.db.domain_repositories import (
    ComparableRepository,
    CompletenessRuleRepository,
    DomainProposalRepository,
    DuplicateRecordError,
    InventoryMediaRepository,
    ListingDraftRepository,
    MarketplaceVariantRepository,
    McpPrincipalRepository,
    MeasurementRepository,
    ResearchRepository,
)
from goliath.db.models import (
    DraftStatus,
    InventoryCondition,
    InventoryStatus,
    Marketplace,
    MeasurementUnit,
    MediaRole,
    ProposalStatus,
    ProposalType,
    ResearchStatus,
    SourceReliability,
)
from goliath.db.repositories import (
    ForbiddenStatusError,
    InvalidStateTransitionError,
    InventoryRepository,
    VersionConflictError,
)
from goliath.domain.pricing import (
    ComparableObservation,
    PricingInputs,
    calculate_pricing,
)
from goliath.domain.service import DomainError, DomainService


def _item(session: Session, *, sku: str = "SKU-1", category: str | None = "clothing"):
    return InventoryRepository(session).create(
        sku=sku,
        title="Vintage jacket",
        condition=InventoryCondition.GOOD,
        acquisition_cost=Decimal("24.50"),
        category=category,
        size_label="M",
        materials=["wool"],
    )


# --------------------------------------------------------------- inventory core


def test_inventory_defaults_to_draft_with_version_one(session: Session) -> None:
    item = _item(session)
    assert item.status is InventoryStatus.DRAFT
    assert item.version == 1


def test_optimistic_inventory_update_bumps_version(session: Session) -> None:
    repo = InventoryRepository(session)
    item = _item(session)
    updated = repo.update_draft(item.id, expected_version=1, changes={"brand": "Levi"})
    assert updated.version == 2
    assert updated.brand == "Levi"
    with pytest.raises(VersionConflictError):
        repo.update_draft(item.id, expected_version=1, changes={"brand": "Other"})


def test_agent_cannot_move_item_to_human_only_status(session: Session) -> None:
    repo = InventoryRepository(session)
    item = _item(session)
    for forbidden in ("listed", "sold", "donated", "archived"):
        with pytest.raises(ForbiddenStatusError):
            repo.update_draft(
                item.id, expected_version=item.version, changes={"status": forbidden}
            )
    # Human-authorized path is allowed.
    updated = repo.update_draft(
        item.id,
        expected_version=item.version,
        changes={"status": "archived"},
        allow_human_only_status=True,
    )
    assert updated.status is InventoryStatus.ARCHIVED


def test_inventory_update_rejects_non_draft_fields(session: Session) -> None:
    repo = InventoryRepository(session)
    item = _item(session)
    with pytest.raises(ValueError, match="draft-writable"):
        repo.update_draft(item.id, expected_version=1, changes={"sku": "NEW"})


# ------------------------------------------------------------------- media


def test_media_checksum_duplicate_detection(session: Session) -> None:
    item = _item(session)
    repo = InventoryMediaRepository(session)
    repo.add(
        inventory_item_id=item.id,
        file_identifier="f1",
        media_type="image/jpeg",
        checksum="deadbeef",
        file_size=100,
        role=MediaRole.ORIGINAL,
        storage_key="/media/f1.jpg",
    )
    with pytest.raises(DuplicateRecordError):
        repo.add(
            inventory_item_id=item.id,
            file_identifier="f2",
            media_type="image/jpeg",
            checksum="deadbeef",
            file_size=200,
            role=MediaRole.PROCESSED,
            storage_key="/media/f2.jpg",
        )


# ---------------------------------------------------------------- measurements


def test_measurement_normalizes_inches_to_cm(session: Session) -> None:
    item = _item(session)
    repo = MeasurementRepository(session)
    m = repo.add(
        inventory_item_id=item.id,
        measurement_type="chest_flat",
        value=Decimal(10),
        unit=MeasurementUnit.IN,
    )
    assert m.value == Decimal(10)
    assert m.unit is MeasurementUnit.IN
    assert m.value_cm == Decimal("25.400")


@pytest.mark.parametrize("value", [Decimal(0), Decimal(-1), Decimal(100000)])
def test_measurement_rejects_impossible_values(session: Session, value: Decimal) -> None:
    item = _item(session)
    with pytest.raises(ValueError):
        MeasurementRepository(session).add(
            inventory_item_id=item.id,
            measurement_type="chest_flat",
            value=value,
            unit=MeasurementUnit.CM,
        )


# ------------------------------------------------------------------- research


def test_research_lifecycle_and_source_duplicate_detection(session: Session) -> None:
    item = _item(session)
    repo = ResearchRepository(session)
    record = repo.create(
        inventory_item_id=item.id, research_question="what model", researcher="hermes"
    )
    assert record.status is ResearchStatus.PENDING
    repo.add_source(record.id, source_type="web", content_hash="h1", reliability=SourceReliability.HIGH)
    with pytest.raises(DuplicateRecordError):
        repo.add_source(record.id, source_type="web", content_hash="h1")
    repo.add_candidate(record.id, brand="Nike", confidence=Decimal("0.9"))
    completed = repo.complete(
        record.id,
        status=ResearchStatus.COMPLETED,
        confidence=Decimal("0.9"),
        selected_identification={"brand": "Nike"},
    )
    assert completed.status is ResearchStatus.COMPLETED
    with pytest.raises(InvalidStateTransitionError):
        repo.complete(record.id, status=ResearchStatus.COMPLETED)


# ---------------------------------------------------------------- comparables


def test_comparable_immutability_and_duplicate_detection(session: Session) -> None:
    item = _item(session)
    repo = ComparableRepository(session)
    comp = repo.add(
        inventory_item_id=item.id,
        marketplace=Marketplace.EBAY,
        source_identity="listing-1",
        listing_title="Nike tee",
        is_sold=True,
        sold_price=Decimal(30),
    )
    assert not hasattr(repo, "update")
    with pytest.raises(DuplicateRecordError):
        repo.add(
            inventory_item_id=item.id,
            marketplace=Marketplace.EBAY,
            source_identity="listing-1",
            listing_title="Nike tee dup",
        )
    invalidated = repo.invalidate(comp.id, reason="wrong item")
    assert invalidated.invalidated_at is not None
    assert repo.list_for_item(item.id) == []
    with pytest.raises(InvalidStateTransitionError):
        repo.invalidate(comp.id, reason="again")


# ------------------------------------------------------------------- pricing


def _fees() -> MarketplaceFeeVersion:
    return MarketplaceFeeVersion(fee_percent=0.10, payment_percent=0.03, fixed_fee=0.30)


def test_pricing_is_deterministic() -> None:
    inputs = PricingInputs(
        cost_basis=Decimal(20),
        comparables=[
            ComparableObservation(price=Decimal(80), is_sold=True, reliability=0.9),
            ComparableObservation(price=Decimal(100), is_sold=False, reliability=0.6),
        ],
    )
    first = calculate_pricing(inputs, rules=PricingRules(), fees=_fees(), fee_version="v1")
    second = calculate_pricing(inputs, rules=PricingRules(), fees=_fees(), fee_version="v1")
    assert first.model_dump() == second.model_dump()
    assert first.recommended_price == Decimal("68.80")


def test_pricing_enforces_minimum_price() -> None:
    inputs = PricingInputs(
        cost_basis=Decimal(50),
        minimum_acceptable_profit=Decimal(40),
        comparables=[ComparableObservation(price=Decimal(55), is_sold=True)],
    )
    result = calculate_pricing(inputs, rules=PricingRules(), fees=_fees(), fee_version="v1")
    assert result.recommended_price >= result.minimum_price
    assert any("minimum" in w for w in result.warnings)


def test_pricing_fee_version_selection_changes_result() -> None:
    inputs = PricingInputs(
        cost_basis=Decimal(20),
        comparables=[ComparableObservation(price=Decimal(100), is_sold=True)],
    )
    low = calculate_pricing(
        inputs, rules=PricingRules(), fees=MarketplaceFeeVersion(fee_percent=0.05), fee_version="low"
    )
    high = calculate_pricing(
        inputs, rules=PricingRules(), fees=MarketplaceFeeVersion(fee_percent=0.25), fee_version="high"
    )
    assert low.expected_net_proceeds > high.expected_net_proceeds


# --------------------------------------------------------------- listing drafts


def test_listing_draft_versioning_and_approved_protection(session: Session) -> None:
    item = _item(session)
    repo = ListingDraftRepository(session)
    draft = repo.create(
        inventory_item_id=item.id, created_by="agent:codex", content={"title": "T"}
    )
    assert draft.version == 1
    assert len(repo.list_versions(draft.id)) == 1
    repo.set_status(draft.id, status=DraftStatus.APPROVED, created_by="human:op")
    assert len(repo.list_versions(draft.id)) == 2
    with pytest.raises(InvalidStateTransitionError):
        repo.set_status(draft.id, status=DraftStatus.DRAFT, created_by="agent:codex")


def test_marketplace_variant_duplicate_detection(session: Session) -> None:
    item = _item(session)
    draft = ListingDraftRepository(session).create(
        inventory_item_id=item.id, created_by="agent:codex", content={"title": "T"}
    )
    repo = MarketplaceVariantRepository(session)
    repo.create(
        draft_id=draft.id,
        marketplace=Marketplace.EBAY,
        constraint_version="ebay-v1",
        marketplace_title="T",
    )
    with pytest.raises(DuplicateRecordError):
        repo.create(
            draft_id=draft.id,
            marketplace=Marketplace.EBAY,
            constraint_version="ebay-v1",
            marketplace_title="T2",
        )


# --------------------------------------------------------------- completeness


def _service(tmp_path: Path) -> tuple[DomainService, object]:
    engine = create_test_engine()
    Base.metadata.create_all(engine)
    media_root = tmp_path / "media"
    media_root.mkdir()
    config = OrchestrationConfig(workspace_roots=[tmp_path])
    config.domain.media_storage_root = media_root
    return DomainService(session_factory=build_session_factory(engine), config=config), engine


def test_completeness_scoring_is_deterministic(tmp_path: Path) -> None:
    service, engine = _service(tmp_path)
    item = service.create_inventory(
        actor="human:op",
        sku="C1",
        title="Tee",
        condition="good",
        acquisition_cost=Decimal(5),
        category="clothing",
    )
    first = service.evaluate_completeness(item.id)
    second = service.evaluate_completeness(item.id)
    assert first.model_dump() == second.model_dump()
    assert "materials" in first.missing_required_fields or first.score < 1.0
    assert any("measurement" in e for e in first.blocking_errors)
    engine.dispose()


# ----------------------------------------------------------- proposals/approval


def test_proposal_version_conflict_is_rejected(tmp_path: Path) -> None:
    service, engine = _service(tmp_path)
    item = service.create_inventory(
        actor="human:op", sku="P1", title="Tee", condition="good", acquisition_cost=Decimal(5)
    )
    proposal = service.submit_proposal(
        actor="service_principal:hermes",
        proposal_type=ProposalType.INVENTORY_UPDATE,
        resource_type="inventory_item",
        resource_id=item.id,
        current_version=item.version,
        proposed_payload={"brand": "Nike"},
        justification="identified brand",
    )
    # A concurrent human edit bumps the version, making the proposal stale.
    service.update_inventory_draft(
        item.id, expected_version=item.version, changes={"pattern": "solid"}, actor="human:op"
    )
    decided = service.approve_proposal(proposal.id, reviewer="human:op")
    assert decided.status is ProposalStatus.EXECUTION_FAILED
    assert "stale" in (decided.execution_error or "")
    engine.dispose()


def test_approved_safe_inventory_update_executes(tmp_path: Path) -> None:
    service, engine = _service(tmp_path)
    item = service.create_inventory(
        actor="human:op", sku="P2", title="Tee", condition="good", acquisition_cost=Decimal(5)
    )
    proposal = service.submit_proposal(
        actor="service_principal:hermes",
        proposal_type=ProposalType.INVENTORY_UPDATE,
        resource_type="inventory_item",
        resource_id=item.id,
        current_version=item.version,
        proposed_payload={"brand": "Nike"},
        justification="identified brand",
    )
    decided = service.approve_proposal(proposal.id, reviewer="human:op")
    assert decided.status is ProposalStatus.EXECUTED
    assert service.get_inventory(item.id).brand == "Nike"
    engine.dispose()


def test_proposal_expiration_blocks_decision(session: Session) -> None:
    item = _item(session)
    repo = DomainProposalRepository(session)
    proposal = repo.create(
        proposal_type=ProposalType.INVENTORY_UPDATE,
        resource_type="inventory_item",
        resource_id=item.id,
        current_version=item.version,
        proposed_payload={"brand": "Nike"},
        justification="x",
        risk_tier="low",
        requested_by="agent:hermes",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert repo.expire_due() == 1
    with pytest.raises(InvalidStateTransitionError):
        repo.decide(proposal.id, approved=True, reviewer="human:op")


def test_completeness_rule_repository_scopes_active_version(session: Session) -> None:
    repo = CompletenessRuleRepository(session)
    repo.create(category="shoes", version="v1", required_fields=["size_label"])
    active = repo.get_active_for_category("shoes")
    assert active is not None
    assert active.version == "v1"


def test_mcp_principal_hashes_credentials_and_supports_revocation(session: Session) -> None:
    repo = McpPrincipalRepository(session)
    principal, raw = repo.create(name="hermes", scopes=["inventory:read"])
    assert raw not in principal.credential_hash
    assert repo.authenticate(raw) is not None
    repo.revoke(principal.id)
    assert repo.authenticate(raw) is None


def test_mcp_principal_expiration(session: Session) -> None:
    repo = McpPrincipalRepository(session)
    _, raw = repo.create(
        name="temp", scopes=["inventory:read"], expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    assert repo.authenticate(raw) is None


def test_research_requires_source_for_exact_identification(tmp_path: Path) -> None:
    service, engine = _service(tmp_path)
    item = service.create_inventory(
        actor="human:op", sku="R1", title="Tee", condition="good", acquisition_cost=Decimal(5)
    )
    research = service.create_research(
        item.id, actor="agent:hermes", research_question="model?", researcher="hermes"
    )
    with pytest.raises(DomainError, match="source-backed"):
        service.complete_research(
            research.id,
            actor="agent:hermes",
            status=ResearchStatus.COMPLETED,
            selected_identification={"brand": "Nike"},
        )
    engine.dispose()


def test_domain_workflow_appends_audit_events(tmp_path: Path) -> None:
    from goliath.db.models import AuditEvent

    service, engine = _service(tmp_path)
    factory = service._session_factory
    item = service.create_inventory(
        actor="human:op", sku="A1", title="Tee", condition="good", acquisition_cost=Decimal(5)
    )
    service.evaluate_completeness(item.id)
    service.update_inventory_draft(
        item.id, expected_version=item.version, changes={"brand": "Nike"}, actor="human:op"
    )
    with factory() as session:
        events = {e.event_type for e in session.query(AuditEvent).all()}
    assert {"inventory.created", "inventory.completeness_evaluated", "inventory.draft_updated"} <= events
    engine.dispose()


def test_identification_below_threshold_is_high_risk(tmp_path: Path) -> None:
    service, engine = _service(tmp_path)
    item = service.create_inventory(
        actor="human:op", sku="R2", title="Tee", condition="good", acquisition_cost=Decimal(5)
    )
    research = service.create_research(
        item.id, actor="agent:hermes", research_question="model?", researcher="hermes"
    )
    service.add_research_source(
        research.id, actor="agent:hermes", source_type="web", content_hash="h1"
    )
    _, proposal_id = service.complete_research(
        research.id,
        actor="agent:hermes",
        status=ResearchStatus.COMPLETED,
        confidence=Decimal("0.4"),
        selected_identification={"brand": "Nike", "confidence": "0.4"},
    )
    proposal = service.get_proposal(proposal_id)
    assert proposal.risk_tier == "high"
    engine.dispose()
