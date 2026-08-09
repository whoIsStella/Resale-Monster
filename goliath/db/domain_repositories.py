"""Bounded repositories for milestone-four resale-domain entities.

Each repository exposes only domain-specific methods. None exposes raw sessions,
generic model access, unrestricted filters, arbitrary SQL, or connection objects.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from goliath.db.models import (
    CategoryCompletenessRule,
    ComparableSale,
    DomainProposal,
    IdentificationCandidate,
    InventoryMedia,
    ListingDraftVersion,
    Marketplace,
    MarketplaceConstraintVersion,
    MarketplaceDraftVariant,
    MasterListingDraft,
    McpServicePrincipal,
    Measurement,
    MeasurementUnit,
    MediaRole,
    PricingRecommendation,
    ProposalStatus,
    ProposalType,
    ResearchRecord,
    ResearchSource,
    ResearchStatus,
    SourceReliability,
    utc_now,
)
from goliath.db.models import (
    ProposalEvidence as ProposalEvidenceModel,
)
from goliath.db.repositories import (
    InvalidStateTransitionError,
    RecordNotFoundError,
    VersionConflictError,
)

# Reasonable physical bound (cm) used to reject impossible measurements.
MAX_MEASUREMENT_CM = Decimal(500)


class DuplicateRecordError(ValueError):
    """Raised when checksum/identity-based duplicate detection rejects a record."""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class InventoryMediaRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        *,
        inventory_item_id: UUID,
        file_identifier: str,
        media_type: str,
        checksum: str,
        file_size: int,
        role: MediaRole,
        storage_key: str,
        original_filename: str | None = None,
        image_width: int | None = None,
        image_height: int | None = None,
        status: Any = None,
        detected_media_type: str | None = None,
        validation_result: dict[str, Any] | None = None,
    ) -> InventoryMedia:
        from goliath.db.models import MediaStatus

        if file_size < 0:
            raise ValueError("file_size must be nonnegative")
        existing = self._session.scalar(
            select(InventoryMedia).where(
                InventoryMedia.inventory_item_id == inventory_item_id,
                InventoryMedia.checksum == checksum,
            )
        )
        if existing is not None:
            raise DuplicateRecordError(f"duplicate media checksum for item: {checksum}")
        media = InventoryMedia(
            inventory_item_id=inventory_item_id,
            file_identifier=file_identifier,
            media_type=media_type,
            checksum=checksum,
            file_size=file_size,
            role=role,
            storage_key=storage_key,
            original_filename=original_filename,
            image_width=image_width,
            image_height=image_height,
            status=status or MediaStatus.PENDING,
            detected_media_type=detected_media_type,
            validation_result=validation_result or {},
        )
        self._session.add(media)
        self._session.flush()
        return media

    def get(self, media_id: UUID) -> InventoryMedia | None:
        return self._session.get(InventoryMedia, media_id)

    def get_by_checksum(self, inventory_item_id: UUID, checksum: str) -> InventoryMedia | None:
        return self._session.scalar(
            select(InventoryMedia).where(
                InventoryMedia.inventory_item_id == inventory_item_id,
                InventoryMedia.checksum == checksum,
            )
        )

    def list_for_item(self, inventory_item_id: UUID) -> Sequence[InventoryMedia]:
        return self._session.scalars(
            select(InventoryMedia)
            .where(InventoryMedia.inventory_item_id == inventory_item_id)
            .order_by(InventoryMedia.created_at)
        ).all()

    def list_by_status(self, status: Any, *, limit: int = 100) -> Sequence[InventoryMedia]:
        return self._session.scalars(
            select(InventoryMedia)
            .where(InventoryMedia.status == status)
            .order_by(InventoryMedia.created_at)
            .limit(limit)
        ).all()

    def update_status(
        self,
        media_id: UUID,
        *,
        expected_version: int,
        status: Any,
        detected_media_type: str | None = None,
        validation_result: dict[str, Any] | None = None,
        error_category: str | None = None,
        processing_error: str | None = None,
        touch: str | None = None,
    ) -> InventoryMedia:
        """Optimistic media-state transition using a versioned compare-and-swap."""
        from goliath.db.models import MediaStatus

        values: dict[str, Any] = {
            "status": status,
            "version": expected_version + 1,
        }
        if detected_media_type is not None:
            values["detected_media_type"] = detected_media_type
        if validation_result is not None:
            values["validation_result"] = validation_result
        if error_category is not None:
            values["processing_error_category"] = error_category
        if processing_error is not None:
            values["processing_error"] = processing_error
        if touch == "validated":
            values["validated_at"] = utc_now()
        elif touch == "processing":
            values["processing_started_at"] = utc_now()
        elif touch == "processed":
            values["processing_completed_at"] = utc_now()
        elif touch == "archived":
            values["archived_at"] = utc_now()
        if status is MediaStatus.PROCESSING:
            values["processing_attempts"] = InventoryMedia.processing_attempts + 1
        media = self.get(media_id)
        if media is None:
            raise RecordNotFoundError(f"media not found: {media_id}")
        from sqlalchemy import update as _update

        result = self._session.execute(
            _update(InventoryMedia)
            .where(
                InventoryMedia.id == media_id,
                InventoryMedia.version == expected_version,
            )
            .values(**values)
        )
        if result.rowcount != 1:
            self._session.expire_all()
            raise VersionConflictError(
                f"stale media version: expected {expected_version} for {media_id}"
            )
        self._session.expire(media)
        return media


class MeasurementRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        *,
        inventory_item_id: UUID,
        measurement_type: str,
        value: Decimal,
        unit: MeasurementUnit,
        method: str | None = None,
        confidence: Decimal | None = None,
        source: str | None = None,
        notes: str | None = None,
    ) -> Measurement:
        value = Decimal(value)
        if value <= 0:
            raise ValueError("measurement value must be positive")
        value_cm = value if unit is MeasurementUnit.CM else value * Decimal("2.54")
        if value_cm > MAX_MEASUREMENT_CM:
            raise ValueError("measurement value is physically impossible")
        if confidence is not None and not (Decimal(0) <= Decimal(confidence) <= Decimal(1)):
            raise ValueError("confidence must be within [0, 1]")
        measurement = Measurement(
            inventory_item_id=inventory_item_id,
            measurement_type=measurement_type,
            value=value,
            unit=unit,
            value_cm=value_cm.quantize(Decimal("0.001")),
            method=method,
            confidence=confidence,
            source=source,
            notes=notes,
        )
        self._session.add(measurement)
        self._session.flush()
        return measurement

    def list_for_item(self, inventory_item_id: UUID) -> Sequence[Measurement]:
        return self._session.scalars(
            select(Measurement)
            .where(Measurement.inventory_item_id == inventory_item_id)
            .order_by(Measurement.created_at)
        ).all()


class ResearchRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        inventory_item_id: UUID,
        research_question: str,
        researcher: str,
        search_terms: list[str] | None = None,
    ) -> ResearchRecord:
        record = ResearchRecord(
            inventory_item_id=inventory_item_id,
            research_question=research_question,
            researcher=researcher,
            search_terms=search_terms or [],
            status=ResearchStatus.PENDING,
        )
        self._session.add(record)
        self._session.flush()
        return record

    def get(self, research_id: UUID) -> ResearchRecord | None:
        return self._session.get(ResearchRecord, research_id)

    def list_for_item(self, inventory_item_id: UUID) -> Sequence[ResearchRecord]:
        return self._session.scalars(
            select(ResearchRecord)
            .where(ResearchRecord.inventory_item_id == inventory_item_id)
            .order_by(ResearchRecord.started_at)
        ).all()

    def list_by_status(self, status: ResearchStatus) -> Sequence[ResearchRecord]:
        return self._session.scalars(
            select(ResearchRecord).where(ResearchRecord.status == status)
        ).all()

    def mark_in_progress(self, research_id: UUID) -> ResearchRecord:
        record = self._require(research_id)
        if record.status not in {ResearchStatus.PENDING, ResearchStatus.IN_PROGRESS}:
            raise InvalidStateTransitionError("research is no longer open")
        record.status = ResearchStatus.IN_PROGRESS
        self._session.flush()
        return record

    def add_source(
        self,
        research_id: UUID,
        *,
        source_type: str,
        content_hash: str,
        title: str | None = None,
        url: str | None = None,
        excerpt: str | None = None,
        relevance_score: Decimal | None = None,
        reliability: SourceReliability = SourceReliability.UNKNOWN,
    ) -> ResearchSource:
        self._require(research_id)
        duplicate = self._session.scalar(
            select(ResearchSource).where(
                ResearchSource.research_id == research_id,
                ResearchSource.content_hash == content_hash,
            )
        )
        if duplicate is not None:
            raise DuplicateRecordError("duplicate research source content hash")
        source = ResearchSource(
            research_id=research_id,
            source_type=source_type,
            title=title,
            url=url,
            excerpt=excerpt,
            relevance_score=relevance_score,
            reliability=reliability,
            content_hash=content_hash,
        )
        self._session.add(source)
        self._session.flush()
        return source

    def list_sources(self, research_id: UUID) -> Sequence[ResearchSource]:
        return self._session.scalars(
            select(ResearchSource)
            .where(ResearchSource.research_id == research_id)
            .order_by(ResearchSource.created_at)
        ).all()

    def add_candidate(
        self,
        research_id: UUID,
        *,
        brand: str | None = None,
        model_name: str | None = None,
        attributes: dict[str, Any] | None = None,
        confidence: Decimal | None = None,
        evidence_source_ids: list[str] | None = None,
        rationale: str | None = None,
    ) -> IdentificationCandidate:
        self._require(research_id)
        candidate = IdentificationCandidate(
            research_id=research_id,
            brand=brand,
            model_name=model_name,
            attributes=attributes or {},
            confidence=confidence,
            evidence_source_ids=evidence_source_ids or [],
            rationale=rationale,
        )
        self._session.add(candidate)
        self._session.flush()
        return candidate

    def list_candidates(self, research_id: UUID) -> Sequence[IdentificationCandidate]:
        return self._session.scalars(
            select(IdentificationCandidate)
            .where(IdentificationCandidate.research_id == research_id)
            .order_by(IdentificationCandidate.created_at)
        ).all()

    def complete(
        self,
        research_id: UUID,
        *,
        status: ResearchStatus,
        confidence: Decimal | None = None,
        selected_identification: dict[str, Any] | None = None,
        observations: str | None = None,
        unresolved_questions: list[str] | None = None,
    ) -> ResearchRecord:
        if status not in {
            ResearchStatus.COMPLETED,
            ResearchStatus.INCONCLUSIVE,
            ResearchStatus.REJECTED,
        }:
            raise InvalidStateTransitionError("research can only be closed with a terminal status")
        record = self._require(research_id)
        if record.status in {
            ResearchStatus.COMPLETED,
            ResearchStatus.INCONCLUSIVE,
            ResearchStatus.REJECTED,
        }:
            raise InvalidStateTransitionError("research is already closed")
        record.status = status
        record.confidence = confidence
        record.selected_identification = selected_identification
        if observations is not None:
            record.observations = observations
        if unresolved_questions is not None:
            record.unresolved_questions = unresolved_questions
        record.completed_at = utc_now()
        record.version += 1
        self._session.flush()
        return record

    def _require(self, research_id: UUID) -> ResearchRecord:
        record = self.get(research_id)
        if record is None:
            raise RecordNotFoundError(f"research record not found: {research_id}")
        return record


class ComparableRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        *,
        inventory_item_id: UUID,
        marketplace: Marketplace,
        source_identity: str,
        listing_title: str,
        is_sold: bool = False,
        listed_price: Decimal | None = None,
        sold_price: Decimal | None = None,
        shipping_price: Decimal | None = None,
        currency: str = "USD",
        condition: str | None = None,
        size: str | None = None,
        sale_date: Any = None,
        source_url: str | None = None,
        similarity_score: Decimal | None = None,
        reliability_score: Decimal | None = None,
        notes: str | None = None,
    ) -> ComparableSale:
        duplicate = self._session.scalar(
            select(ComparableSale).where(
                ComparableSale.inventory_item_id == inventory_item_id,
                ComparableSale.marketplace == marketplace,
                ComparableSale.source_identity == source_identity,
            )
        )
        if duplicate is not None:
            raise DuplicateRecordError("duplicate comparable for marketplace and source identity")
        for label, price in (
            ("listed_price", listed_price),
            ("sold_price", sold_price),
            ("shipping_price", shipping_price),
        ):
            if price is not None and Decimal(price) < 0:
                raise ValueError(f"{label} must be nonnegative")
        comparable = ComparableSale(
            inventory_item_id=inventory_item_id,
            marketplace=marketplace,
            source_identity=source_identity,
            listing_title=listing_title,
            is_sold=is_sold,
            listed_price=listed_price,
            sold_price=sold_price,
            shipping_price=shipping_price,
            currency=currency.upper(),
            condition=condition,
            size=size,
            sale_date=sale_date,
            source_url=source_url,
            similarity_score=similarity_score,
            reliability_score=reliability_score,
            notes=notes,
        )
        self._session.add(comparable)
        self._session.flush()
        return comparable

    def get(self, comparable_id: UUID) -> ComparableSale | None:
        return self._session.get(ComparableSale, comparable_id)

    def list_for_item(
        self, inventory_item_id: UUID, *, include_invalidated: bool = False
    ) -> Sequence[ComparableSale]:
        statement = select(ComparableSale).where(
            ComparableSale.inventory_item_id == inventory_item_id
        )
        if not include_invalidated:
            statement = statement.where(ComparableSale.invalidated_at.is_(None))
        return self._session.scalars(statement.order_by(ComparableSale.captured_at)).all()

    def invalidate(self, comparable_id: UUID, *, reason: str) -> ComparableSale:
        comparable = self.get(comparable_id)
        if comparable is None:
            raise RecordNotFoundError(f"comparable not found: {comparable_id}")
        if comparable.invalidated_at is not None:
            raise InvalidStateTransitionError("comparable already invalidated")
        comparable.invalidated_at = utc_now()
        comparable.invalidation_reason = reason
        self._session.flush()
        return comparable


class PricingRecommendationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        inventory_item_id: UUID,
        created_by: str,
        fee_version: str,
        currency: str,
        recommended_price: Decimal,
        fast_sale_price: Decimal,
        minimum_price: Decimal,
        expected_net_proceeds: Decimal,
        expected_profit: Decimal,
        expected_margin: Decimal,
        confidence: Decimal,
        breakdown: dict[str, Any],
        warnings: list[str],
        inputs: dict[str, Any],
        policy_version: str | None = None,
        included_comparables: list[dict[str, Any]] | None = None,
        excluded_comparables: list[dict[str, Any]] | None = None,
    ) -> PricingRecommendation:
        recommendation = PricingRecommendation(
            inventory_item_id=inventory_item_id,
            created_by=created_by,
            fee_version=fee_version,
            currency=currency.upper(),
            recommended_price=recommended_price,
            fast_sale_price=fast_sale_price,
            minimum_price=minimum_price,
            expected_net_proceeds=expected_net_proceeds,
            expected_profit=expected_profit,
            expected_margin=expected_margin,
            confidence=confidence,
            breakdown=breakdown,
            warnings=warnings,
            inputs=inputs,
            policy_version=policy_version,
            included_comparables=included_comparables or [],
            excluded_comparables=excluded_comparables or [],
        )
        self._session.add(recommendation)
        self._session.flush()
        return recommendation

    def get(self, recommendation_id: UUID) -> PricingRecommendation | None:
        return self._session.get(PricingRecommendation, recommendation_id)

    def list_for_item(self, inventory_item_id: UUID) -> Sequence[PricingRecommendation]:
        return self._session.scalars(
            select(PricingRecommendation)
            .where(PricingRecommendation.inventory_item_id == inventory_item_id)
            .order_by(PricingRecommendation.created_at)
        ).all()


class ListingDraftRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self, *, inventory_item_id: UUID, created_by: str, content: dict[str, Any]
    ) -> MasterListingDraft:
        draft = MasterListingDraft(
            inventory_item_id=inventory_item_id,
            created_by=created_by,
            title=content.get("title", ""),
            description=content.get("description"),
            brand=content.get("brand"),
            category=content.get("category"),
            subcategory=content.get("subcategory"),
            condition=content.get("condition"),
            condition_details=content.get("condition_details"),
            size=content.get("size"),
            colors=content.get("colors", []),
            materials=content.get("materials", []),
            style_keywords=content.get("style_keywords", []),
            measurements=content.get("measurements", []),
            defects=content.get("defects", []),
            pricing_recommendation_id=content.get("pricing_recommendation_id"),
            image_order=content.get("image_order", []),
            shipping_assumptions=content.get("shipping_assumptions", {}),
        )
        self._session.add(draft)
        self._session.flush()
        self._snapshot(draft, created_by)
        return draft

    def get(self, draft_id: UUID) -> MasterListingDraft | None:
        return self._session.get(MasterListingDraft, draft_id)

    def list(
        self, *, status: Any = None, limit: int = 100, offset: int = 0
    ) -> Sequence[MasterListingDraft]:
        statement = select(MasterListingDraft)
        if status is not None:
            statement = statement.where(MasterListingDraft.status == status)
        statement = statement.order_by(MasterListingDraft.created_at).limit(limit).offset(offset)
        return self._session.scalars(statement).all()

    def list_versions(self, draft_id: UUID) -> Sequence[ListingDraftVersion]:
        return self._session.scalars(
            select(ListingDraftVersion)
            .where(ListingDraftVersion.draft_id == draft_id)
            .order_by(ListingDraftVersion.version)
        ).all()

    def store_validation(
        self, draft_id: UUID, *, warnings: list[str], missing_fields: list[str]
    ) -> MasterListingDraft:
        draft = self._require(draft_id)
        draft.validation_warnings = warnings
        draft.missing_fields = missing_fields
        self._session.flush()
        return draft

    def set_status(
        self, draft_id: UUID, *, status, created_by: str = "system"
    ) -> MasterListingDraft:
        from goliath.db.models import DraftStatus

        draft = self._require(draft_id)
        if draft.status is DraftStatus.APPROVED and status is not DraftStatus.SUPERSEDED:
            raise InvalidStateTransitionError("approved drafts may only be superseded")
        draft.status = status
        draft.version += 1
        self._session.flush()
        self._snapshot(draft, created_by)
        return draft

    def _snapshot(self, draft: MasterListingDraft, created_by: str) -> None:
        version = ListingDraftVersion(
            draft_id=draft.id,
            version=draft.version,
            status=draft.status,
            created_by=created_by,
            content={
                "title": draft.title,
                "description": draft.description,
                "size": draft.size,
                "colors": draft.colors,
                "materials": draft.materials,
                "defects": draft.defects,
            },
        )
        self._session.add(version)
        self._session.flush()

    def _require(self, draft_id: UUID) -> MasterListingDraft:
        draft = self.get(draft_id)
        if draft is None:
            raise RecordNotFoundError(f"listing draft not found: {draft_id}")
        return draft


class MarketplaceVariantRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        draft_id: UUID,
        marketplace: Marketplace,
        constraint_version: str,
        marketplace_title: str,
        marketplace_description: str | None = None,
        category_mapping: str | None = None,
        item_specifics: dict[str, Any] | None = None,
        hashtags: list[str] | None = None,
        proposed_price: Decimal | None = None,
        shipping_profile: str | None = None,
        image_order: list[str] | None = None,
        validation_warnings: list[str] | None = None,
    ) -> MarketplaceDraftVariant:
        existing = self._session.scalar(
            select(MarketplaceDraftVariant).where(
                MarketplaceDraftVariant.draft_id == draft_id,
                MarketplaceDraftVariant.marketplace == marketplace,
            )
        )
        if existing is not None:
            raise DuplicateRecordError(
                f"variant already exists for marketplace: {marketplace.value}"
            )
        variant = MarketplaceDraftVariant(
            draft_id=draft_id,
            marketplace=marketplace,
            constraint_version=constraint_version,
            marketplace_title=marketplace_title,
            marketplace_description=marketplace_description,
            category_mapping=category_mapping,
            item_specifics=item_specifics or {},
            hashtags=hashtags or [],
            proposed_price=proposed_price,
            shipping_profile=shipping_profile,
            image_order=image_order or [],
            validation_warnings=validation_warnings or [],
        )
        self._session.add(variant)
        self._session.flush()
        return variant

    def get(self, variant_id: UUID) -> MarketplaceDraftVariant | None:
        return self._session.get(MarketplaceDraftVariant, variant_id)

    def set_status(self, variant_id: UUID, *, status: Any) -> MarketplaceDraftVariant:
        variant = self.get(variant_id)
        if variant is None:
            raise RecordNotFoundError(f"marketplace variant not found: {variant_id}")
        from goliath.db.models import DraftStatus

        if variant.status is DraftStatus.APPROVED and status is not DraftStatus.SUPERSEDED:
            raise InvalidStateTransitionError(
                "approved marketplace variants may only be superseded"
            )
        variant.status = status
        self._session.flush()
        return variant

    def list_for_draft(self, draft_id: UUID) -> Sequence[MarketplaceDraftVariant]:
        return self._session.scalars(
            select(MarketplaceDraftVariant)
            .where(MarketplaceDraftVariant.draft_id == draft_id)
            .order_by(MarketplaceDraftVariant.marketplace)
        ).all()


class DomainProposalRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        proposal_type: ProposalType,
        resource_type: str,
        resource_id: UUID,
        current_version: int,
        proposed_payload: dict[str, Any],
        justification: str,
        risk_tier: str,
        requested_by: str,
        expires_at: datetime | None = None,
        evidence: list[dict[str, Any]] | None = None,
    ) -> DomainProposal:
        proposal = DomainProposal(
            proposal_type=proposal_type,
            resource_type=resource_type,
            resource_id=resource_id,
            current_version=current_version,
            proposed_payload=proposed_payload,
            justification=justification,
            risk_tier=risk_tier,
            requested_by=requested_by,
            expires_at=expires_at,
            status=ProposalStatus.PENDING,
        )
        self._session.add(proposal)
        self._session.flush()
        for item in evidence or []:
            self._session.add(
                ProposalEvidenceModel(
                    proposal_id=proposal.id,
                    evidence_type=item.get("evidence_type", "note"),
                    reference_type=item.get("reference_type", "note"),
                    reference_id=item.get("reference_id"),
                    detail=item.get("detail", {}),
                )
            )
        self._session.flush()
        return proposal

    def get(self, proposal_id: UUID) -> DomainProposal | None:
        return self._session.get(DomainProposal, proposal_id)

    def list(
        self,
        *,
        status: ProposalStatus | None = None,
        proposal_type: ProposalType | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[DomainProposal]:
        statement = select(DomainProposal)
        if status is not None:
            statement = statement.where(DomainProposal.status == status)
        if proposal_type is not None:
            statement = statement.where(DomainProposal.proposal_type == proposal_type)
        statement = statement.order_by(DomainProposal.requested_at).limit(limit).offset(offset)
        return self._session.scalars(statement).all()

    def list_evidence(self, proposal_id: UUID) -> Sequence[ProposalEvidenceModel]:
        return self._session.scalars(
            select(ProposalEvidenceModel)
            .where(ProposalEvidenceModel.proposal_id == proposal_id)
            .order_by(ProposalEvidenceModel.created_at)
        ).all()

    def expire_due(self, *, now: datetime | None = None) -> int:
        now = now or utc_now()
        pending = self._session.scalars(
            select(DomainProposal).where(DomainProposal.status == ProposalStatus.PENDING)
        ).all()
        expired = 0
        for proposal in pending:
            if proposal.expires_at is not None and _utc(proposal.expires_at) <= now:
                proposal.status = ProposalStatus.EXPIRED
                expired += 1
        self._session.flush()
        return expired

    def decide(
        self,
        proposal_id: UUID,
        *,
        approved: bool,
        reviewer: str,
        review_notes: str | None = None,
        rejection_reason: str | None = None,
        now: datetime | None = None,
    ) -> DomainProposal:
        now = now or utc_now()
        proposal = self._require(proposal_id)
        if proposal.status is not ProposalStatus.PENDING:
            raise InvalidStateTransitionError(f"proposal is not pending: {proposal.status.value}")
        if proposal.expires_at is not None and _utc(proposal.expires_at) <= now:
            proposal.status = ProposalStatus.EXPIRED
            self._session.flush()
            raise InvalidStateTransitionError("proposal has expired")
        proposal.status = ProposalStatus.APPROVED if approved else ProposalStatus.REJECTED
        proposal.reviewer = reviewer
        proposal.reviewed_at = now
        proposal.review_notes = review_notes
        if not approved:
            proposal.rejection_reason = rejection_reason
        self._session.flush()
        return proposal

    def mark_executed(self, proposal_id: UUID, *, error: str | None = None) -> DomainProposal:
        proposal = self._require(proposal_id)
        if proposal.status is not ProposalStatus.APPROVED:
            raise InvalidStateTransitionError("only approved proposals can be executed")
        proposal.status = (
            ProposalStatus.EXECUTED if error is None else ProposalStatus.EXECUTION_FAILED
        )
        proposal.execution_error = error
        self._session.flush()
        return proposal

    def supersede(self, proposal_id: UUID) -> DomainProposal:
        proposal = self._require(proposal_id)
        if proposal.status is not ProposalStatus.PENDING:
            raise InvalidStateTransitionError("only pending proposals can be superseded")
        proposal.status = ProposalStatus.SUPERSEDED
        self._session.flush()
        return proposal

    def check_current_version(self, proposal: DomainProposal, actual_version: int) -> None:
        if proposal.current_version != actual_version:
            raise VersionConflictError(
                "proposal is stale: resource version changed since it was created"
            )

    def _require(self, proposal_id: UUID) -> DomainProposal:
        proposal = self.get(proposal_id)
        if proposal is None:
            raise RecordNotFoundError(f"proposal not found: {proposal_id}")
        return proposal


class CompletenessRuleRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        category: str,
        version: str,
        required_fields: list[str],
        recommended_fields: list[str] | None = None,
        required_measurements: list[str] | None = None,
    ) -> CategoryCompletenessRule:
        rule = CategoryCompletenessRule(
            category=category,
            version=version,
            required_fields=required_fields,
            recommended_fields=recommended_fields or [],
            required_measurements=required_measurements or [],
        )
        self._session.add(rule)
        self._session.flush()
        return rule

    def get_active_for_category(self, category: str) -> CategoryCompletenessRule | None:
        return self._session.scalar(
            select(CategoryCompletenessRule)
            .where(
                CategoryCompletenessRule.category == category,
                CategoryCompletenessRule.is_active.is_(True),
            )
            .order_by(CategoryCompletenessRule.created_at.desc())
        )

    def list(self) -> Sequence[CategoryCompletenessRule]:
        return self._session.scalars(
            select(CategoryCompletenessRule).order_by(CategoryCompletenessRule.category)
        ).all()


class MarketplaceConstraintRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        marketplace: Marketplace,
        version: str,
        max_title_length: int,
        required_fields: list[str] | None = None,
        allowed_conditions: list[str] | None = None,
        category_attributes: dict[str, Any] | None = None,
    ) -> MarketplaceConstraintVersion:
        if max_title_length <= 0:
            raise ValueError("max_title_length must be positive")
        constraint = MarketplaceConstraintVersion(
            marketplace=marketplace,
            version=version,
            max_title_length=max_title_length,
            required_fields=required_fields or [],
            allowed_conditions=allowed_conditions or [],
            category_attributes=category_attributes or {},
        )
        self._session.add(constraint)
        self._session.flush()
        return constraint

    def get_active(self, marketplace: Marketplace) -> MarketplaceConstraintVersion | None:
        return self._session.scalar(
            select(MarketplaceConstraintVersion)
            .where(
                MarketplaceConstraintVersion.marketplace == marketplace,
                MarketplaceConstraintVersion.is_active.is_(True),
            )
            .order_by(MarketplaceConstraintVersion.created_at.desc())
        )

    def list(self) -> Sequence[MarketplaceConstraintVersion]:
        return self._session.scalars(
            select(MarketplaceConstraintVersion).order_by(MarketplaceConstraintVersion.marketplace)
        ).all()


class McpPrincipalRepository:
    """Service principals for MCP; only hashed credentials are stored."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self, *, name: str, scopes: list[str], expires_at: datetime | None = None
    ) -> tuple[McpServicePrincipal, str]:
        from goliath.core.schemas import HUMAN_ONLY_SCOPES

        forbidden = set(scopes) & HUMAN_ONLY_SCOPES
        if forbidden:
            raise ValueError(
                "MCP service principals may not hold human-only scopes: "
                + ", ".join(sorted(forbidden))
            )
        raw = "mcp_" + secrets.token_urlsafe(32)
        principal = McpServicePrincipal(
            name=name,
            credential_prefix=raw[:12],
            credential_hash=_hash(raw),
            scopes=scopes,
            expires_at=expires_at,
        )
        self._session.add(principal)
        self._session.flush()
        return principal, raw

    def authenticate(
        self, raw_credential: str, *, now: datetime | None = None
    ) -> McpServicePrincipal | None:
        now = now or utc_now()
        principal = self._session.scalar(
            select(McpServicePrincipal).where(
                McpServicePrincipal.credential_hash == _hash(raw_credential),
                McpServicePrincipal.revoked_at.is_(None),
            )
        )
        if principal is None:
            return None
        if principal.expires_at is not None and _utc(principal.expires_at) <= now:
            return None
        if not hmac.compare_digest(principal.credential_hash, _hash(raw_credential)):
            return None
        principal.last_used_at = now
        self._session.flush()
        return principal

    def revoke(self, principal_id: UUID) -> None:
        principal = self._session.get(McpServicePrincipal, principal_id)
        if principal is None:
            raise RecordNotFoundError(f"MCP principal not found: {principal_id}")
        principal.revoked_at = utc_now()
        self._session.flush()

    def list(self) -> Sequence[McpServicePrincipal]:
        return self._session.scalars(
            select(McpServicePrincipal).order_by(McpServicePrincipal.created_at)
        ).all()
