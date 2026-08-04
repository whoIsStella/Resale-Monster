"""Resale-domain application service.

Ties bounded repositories, deterministic engines, the proposal/approval system,
and append-only audit events into the milestone-four workflows. Shared by the
API, CLI, and MCP server so authorization and auditing stay consistent.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from goliath.config import OrchestrationConfig
from goliath.db.domain_repositories import (
    ComparableRepository,
    DomainProposalRepository,
    InventoryMediaRepository,
    ListingDraftRepository,
    MarketplaceVariantRepository,
    MeasurementRepository,
    PricingRecommendationRepository,
    ResearchRepository,
)
from goliath.db.models import (
    DraftStatus,
    InventoryCondition,
    InventoryItem,
    InventoryStatus,
    Marketplace,
    Measurement,
    MeasurementUnit,
    MediaRole,
    ProposalStatus,
    ProposalType,
    ResearchStatus,
    SourceReliability,
    utc_now,
)
from goliath.db.repositories import (
    AuditEventRepository,
    InventoryRepository,
    RecordNotFoundError,
    VersionConflictError,
)
from goliath.domain.completeness import evaluate_completeness
from goliath.domain.listing import (
    build_master_draft_content,
    build_variant_content,
    required_data_missing,
    validate_master_draft,
)
from goliath.domain.pricing import ComparableObservation, PricingInputs, calculate_pricing


class DomainError(RuntimeError):
    pass


class DomainService:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session] | sessionmaker[Session],
        config: OrchestrationConfig,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._domain = config.domain

    # ----------------------------------------------------------------- audit
    @staticmethod
    def _audit(
        session: Session,
        event_type: str,
        *,
        actor: str,
        resource_type: str,
        resource_id: UUID,
        details: dict[str, Any] | None = None,
    ) -> None:
        actor_type, _, actor_id = actor.partition(":")
        AuditEventRepository(session).append(
            event_type=event_type,
            actor_type=actor_type or "system",
            actor_id=actor_id or actor,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details or {},
        )

    # ------------------------------------------------------------- inventory
    def create_inventory(self, *, actor: str, **fields: Any) -> InventoryItem:
        with self._session_factory() as session:
            condition = fields.pop("condition", InventoryCondition.GOOD)
            if isinstance(condition, str):
                condition = InventoryCondition(condition)
            status = fields.pop("status", InventoryStatus.DRAFT)
            if isinstance(status, str):
                status = InventoryStatus(status)
            for decimal_field in (
                "acquisition_cost",
                "cost_basis",
                "estimated_weight_grams",
                "packed_weight_grams",
                "research_confidence",
                "identification_confidence",
            ):
                if decimal_field in fields and fields[decimal_field] is not None:
                    fields[decimal_field] = Decimal(str(fields[decimal_field]))
            item = InventoryRepository(session).create(
                condition=condition, status=status, **fields
            )
            self._audit(
                session,
                "inventory.created",
                actor=actor,
                resource_type="inventory_item",
                resource_id=item.id,
                details={"sku": item.sku, "status": item.status.value},
            )
            session.commit()
            session.refresh(item)
            return item

    def get_inventory(self, item_id: UUID) -> InventoryItem:
        with self._session_factory() as session:
            item = InventoryRepository(session).get(item_id)
            if item is None:
                raise RecordNotFoundError(f"inventory item not found: {item_id}")
            session.expunge(item)
            return item

    def list_inventory(
        self,
        *,
        status: InventoryStatus | None = None,
        category: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[InventoryItem]:
        with self._session_factory() as session:
            items = list(
                InventoryRepository(session).list(
                    status=status, category=category, limit=limit, offset=offset
                )
            )
            for item in items:
                session.expunge(item)
            return items

    def search_inventory(self, query: str, *, limit: int = 50) -> list[InventoryItem]:
        with self._session_factory() as session:
            items = list(InventoryRepository(session).search(query, limit=limit))
            for item in items:
                session.expunge(item)
            return items

    def update_inventory_draft(
        self,
        item_id: UUID,
        *,
        expected_version: int,
        changes: dict[str, Any],
        actor: str,
        allow_human_only_status: bool = False,
    ) -> InventoryItem:
        with self._session_factory() as session:
            item = InventoryRepository(session).update_draft(
                item_id,
                expected_version=expected_version,
                changes=changes,
                allow_human_only_status=allow_human_only_status,
            )
            self._audit(
                session,
                "inventory.draft_updated",
                actor=actor,
                resource_type="inventory_item",
                resource_id=item_id,
                details={"fields": sorted(changes), "version": item.version},
            )
            session.commit()
            session.refresh(item)
            return item

    def evaluate_completeness(self, item_id: UUID, *, actor: str = "system:completeness"):
        with self._session_factory() as session:
            item = InventoryRepository(session).get(item_id)
            if item is None:
                raise RecordNotFoundError(f"inventory item not found: {item_id}")
            rule_config = (
                self._domain.completeness_rules.get(item.category) if item.category else None
            )
            measurement_types = [
                m.measurement_type for m in MeasurementRepository(session).list_for_item(item_id)
            ]
            result = evaluate_completeness(
                item, rule=rule_config, measurement_types=measurement_types
            )
            self._audit(
                session,
                "inventory.completeness_evaluated",
                actor=actor,
                resource_type="inventory_item",
                resource_id=item_id,
                details={"score": result.score, "blocking": len(result.blocking_errors)},
            )
            session.commit()
            return result

    # ----------------------------------------------------------------- media
    def add_media(
        self,
        item_id: UUID,
        *,
        actor: str,
        media_type: str,
        checksum: str,
        file_size: int,
        role: MediaRole,
        storage_path: str,
        file_identifier: str,
        original_filename: str | None = None,
        image_width: int | None = None,
        image_height: int | None = None,
    ):
        if media_type.startswith("image/") and media_type not in self._domain.supported_image_types:
            raise DomainError(f"unsupported image type: {media_type}")
        if file_size > self._domain.max_image_bytes:
            raise DomainError("file exceeds configured maximum size")
        storage_key = str(self._domain.resolve_media_path(__import__("pathlib").Path(storage_path)))
        with self._session_factory() as session:
            media = InventoryMediaRepository(session).add(
                inventory_item_id=item_id,
                file_identifier=file_identifier,
                media_type=media_type,
                checksum=checksum,
                file_size=file_size,
                role=role,
                storage_key=storage_key,
                original_filename=original_filename,
                image_width=image_width,
                image_height=image_height,
            )
            self._audit(
                session,
                "image.metadata_added",
                actor=actor,
                resource_type="inventory_item",
                resource_id=item_id,
                details={"role": role.value, "media_id": str(media.id)},
            )
            session.commit()
            session.refresh(media)
            return media

    def list_media(self, item_id: UUID):
        with self._session_factory() as session:
            media = list(InventoryMediaRepository(session).list_for_item(item_id))
            for record in media:
                session.expunge(record)
            return media

    # ---------------------------------------------------------- measurements
    def add_measurement(
        self,
        item_id: UUID,
        *,
        actor: str,
        measurement_type: str,
        value: Decimal,
        unit: MeasurementUnit,
        method: str | None = None,
        confidence: Decimal | None = None,
        source: str | None = None,
        notes: str | None = None,
    ) -> Measurement:
        with self._session_factory() as session:
            measurement = MeasurementRepository(session).add(
                inventory_item_id=item_id,
                measurement_type=measurement_type,
                value=value,
                unit=unit,
                method=method,
                confidence=confidence,
                source=source,
                notes=notes,
            )
            self._audit(
                session,
                "measurement.added",
                actor=actor,
                resource_type="inventory_item",
                resource_id=item_id,
                details={"type": measurement_type, "value_cm": str(measurement.value_cm)},
            )
            session.commit()
            session.refresh(measurement)
            return measurement

    def list_measurements(self, item_id: UUID):
        with self._session_factory() as session:
            values = list(MeasurementRepository(session).list_for_item(item_id))
            for value in values:
                session.expunge(value)
            return values

    # -------------------------------------------------------------- research
    def create_research(
        self,
        item_id: UUID,
        *,
        actor: str,
        research_question: str,
        researcher: str,
        search_terms: list[str] | None = None,
    ):
        with self._session_factory() as session:
            record = ResearchRepository(session).create(
                inventory_item_id=item_id,
                research_question=research_question,
                researcher=researcher,
                search_terms=search_terms,
            )
            self._audit(
                session,
                "research.created",
                actor=actor,
                resource_type="research_record",
                resource_id=record.id,
                details={"item": str(item_id)},
            )
            session.commit()
            session.refresh(record)
            return record

    def get_research(self, research_id: UUID):
        with self._session_factory() as session:
            record = ResearchRepository(session).get(research_id)
            if record is None:
                raise RecordNotFoundError(f"research not found: {research_id}")
            session.expunge(record)
            return record

    def list_research_for_item(self, item_id: UUID):
        with self._session_factory() as session:
            values = list(ResearchRepository(session).list_for_item(item_id))
            for value in values:
                session.expunge(value)
            return values

    def add_research_source(
        self,
        research_id: UUID,
        *,
        actor: str,
        source_type: str,
        content_hash: str,
        title: str | None = None,
        url: str | None = None,
        excerpt: str | None = None,
        relevance_score: Decimal | None = None,
        reliability: SourceReliability = SourceReliability.UNKNOWN,
    ):
        with self._session_factory() as session:
            repo = ResearchRepository(session)
            if len(repo.list_sources(research_id)) >= self._domain.max_research_sources:
                raise DomainError("research source limit reached")
            repo.mark_in_progress(research_id)
            source = repo.add_source(
                research_id,
                source_type=source_type,
                content_hash=content_hash,
                title=title,
                url=url,
                excerpt=excerpt,
                relevance_score=relevance_score,
                reliability=reliability,
            )
            self._audit(
                session,
                "research.source_added",
                actor=actor,
                resource_type="research_record",
                resource_id=research_id,
                details={"source_type": source_type, "reliability": reliability.value},
            )
            session.commit()
            session.refresh(source)
            return source

    def add_research_candidate(
        self,
        research_id: UUID,
        *,
        actor: str,
        brand: str | None = None,
        model_name: str | None = None,
        attributes: dict[str, Any] | None = None,
        confidence: Decimal | None = None,
        evidence_source_ids: list[str] | None = None,
        rationale: str | None = None,
    ):
        with self._session_factory() as session:
            candidate = ResearchRepository(session).add_candidate(
                research_id,
                brand=brand,
                model_name=model_name,
                attributes=attributes,
                confidence=confidence,
                evidence_source_ids=evidence_source_ids,
                rationale=rationale,
            )
            self._audit(
                session,
                "research.candidate_added",
                actor=actor,
                resource_type="research_record",
                resource_id=research_id,
                details={"brand": brand, "model": model_name},
            )
            session.commit()
            session.refresh(candidate)
            return candidate

    def complete_research(
        self,
        research_id: UUID,
        *,
        actor: str,
        status: ResearchStatus,
        confidence: Decimal | None = None,
        selected_identification: dict[str, Any] | None = None,
        observations: str | None = None,
        unresolved_questions: list[str] | None = None,
    ):
        with self._session_factory() as session:
            repo = ResearchRepository(session)
            record = repo.get(research_id)
            if record is None:
                raise RecordNotFoundError(f"research not found: {research_id}")
            if (
                status is ResearchStatus.COMPLETED
                and selected_identification is not None
                and not repo.list_sources(research_id)
            ):
                raise DomainError(
                    "exact identification requires at least one source-backed evidence record"
                )
            record = repo.complete(
                research_id,
                status=status,
                confidence=confidence,
                selected_identification=selected_identification,
                observations=observations,
                unresolved_questions=unresolved_questions,
            )
            proposal_id: UUID | None = None
            if status is ResearchStatus.COMPLETED and selected_identification is not None:
                item = InventoryRepository(session).get(record.inventory_item_id)
                threshold = Decimal(str(self._domain.identification_auto_threshold))
                below = confidence is None or confidence < threshold
                risk = "high" if below else "low"
                proposal = DomainProposalRepository(session).create(
                    proposal_type=ProposalType.IDENTIFICATION_SELECTION,
                    resource_type="inventory_item",
                    resource_id=record.inventory_item_id,
                    current_version=item.version if item else 0,
                    proposed_payload=self._identification_changes(selected_identification),
                    justification=(
                        f"Research {research_id} identification "
                        f"(confidence={confidence}, threshold={threshold})"
                    ),
                    risk_tier=risk,
                    requested_by=actor,
                    expires_at=utc_now()
                    + timedelta(seconds=self._domain.proposal_expiration_seconds),
                    evidence=[
                        {
                            "evidence_type": "research",
                            "reference_type": "research_record",
                            "reference_id": research_id,
                            "detail": {"confidence": str(confidence)},
                        }
                    ],
                )
                proposal_id = proposal.id
                self._audit(
                    session,
                    "approval.requested",
                    actor=actor,
                    resource_type="domain_proposal",
                    resource_id=proposal.id,
                    details={"type": proposal.proposal_type.value, "risk": risk},
                )
            self._audit(
                session,
                "research.completed",
                actor=actor,
                resource_type="research_record",
                resource_id=research_id,
                details={"status": status.value, "proposal": str(proposal_id) if proposal_id else None},
            )
            session.commit()
            session.refresh(record)
            return record, proposal_id

    @staticmethod
    def _identification_changes(identification: dict[str, Any]) -> dict[str, Any]:
        allowed = {"brand", "model_name", "category", "subcategory", "pattern"}
        changes = {key: value for key, value in identification.items() if key in allowed}
        if "confidence" in identification:
            changes["identification_confidence"] = identification["confidence"]
        return changes

    # ----------------------------------------------------------- comparables
    def add_comparable(self, item_id: UUID, *, actor: str, marketplace: Marketplace, **fields: Any):
        with self._session_factory() as session:
            repo = ComparableRepository(session)
            if len(repo.list_for_item(item_id, include_invalidated=True)) >= (
                self._domain.max_comparables
            ):
                raise DomainError("comparable limit reached")
            comparable = repo.add(inventory_item_id=item_id, marketplace=marketplace, **fields)
            self._audit(
                session,
                "comparable.added",
                actor=actor,
                resource_type="inventory_item",
                resource_id=item_id,
                details={"marketplace": marketplace.value, "sold": comparable.is_sold},
            )
            session.commit()
            session.refresh(comparable)
            return comparable

    def find_comparables(self, item_id: UUID):
        with self._session_factory() as session:
            values = list(ComparableRepository(session).list_for_item(item_id))
            for value in values:
                session.expunge(value)
            return values

    # -------------------------------------------------------------- pricing
    def calculate_pricing(
        self,
        item_id: UUID,
        *,
        actor: str,
        inputs: PricingInputs,
        fee_version: str | None = None,
    ):
        fee_version = fee_version or self._domain.default_fee_version
        fees = self._domain.fee_versions.get(fee_version)
        if fees is None:
            raise DomainError(f"unknown fee version: {fee_version}")
        result = calculate_pricing(
            inputs, rules=self._domain.pricing_rules, fees=fees, fee_version=fee_version
        )
        with self._session_factory() as session:
            self._audit(
                session,
                "pricing.calculated",
                actor=actor,
                resource_type="inventory_item",
                resource_id=item_id,
                details={"recommended": str(result.recommended_price), "fee_version": fee_version},
            )
            session.commit()
        return result

    def calculate_pricing_from_comparables(
        self, item_id: UUID, *, actor: str, cost_basis: Decimal, fee_version: str | None = None
    ):
        comparables = self.find_comparables(item_id)
        item = self.get_inventory(item_id)
        observations = [
            ComparableObservation(
                price=comp.sold_price or comp.listed_price or Decimal(0),
                is_sold=comp.is_sold,
                reliability=float(comp.reliability_score or Decimal("0.5")),
            )
            for comp in comparables
            if (comp.sold_price or comp.listed_price)
        ]
        inputs = PricingInputs(
            cost_basis=cost_basis,
            condition=item.condition.value,
            comparables=observations,
        )
        return self.calculate_pricing(
            item_id, actor=actor, inputs=inputs, fee_version=fee_version
        )

    def create_pricing_recommendation(
        self, item_id: UUID, *, actor: str, inputs: PricingInputs, fee_version: str | None = None
    ):
        fee_version = fee_version or self._domain.default_fee_version
        fees = self._domain.fee_versions.get(fee_version)
        if fees is None:
            raise DomainError(f"unknown fee version: {fee_version}")
        result = calculate_pricing(
            inputs, rules=self._domain.pricing_rules, fees=fees, fee_version=fee_version
        )
        with self._session_factory() as session:
            recommendation = PricingRecommendationRepository(session).create(
                inventory_item_id=item_id,
                created_by=actor,
                fee_version=fee_version,
                currency=result.currency,
                recommended_price=result.recommended_price,
                fast_sale_price=result.fast_sale_price,
                minimum_price=result.minimum_price,
                expected_net_proceeds=result.expected_net_proceeds,
                expected_profit=result.expected_profit,
                expected_margin=result.expected_margin,
                confidence=result.confidence,
                breakdown=result.breakdown,
                warnings=result.warnings,
                inputs=inputs.model_dump(mode="json"),
            )
            self._audit(
                session,
                "pricing.calculated",
                actor=actor,
                resource_type="inventory_item",
                resource_id=item_id,
                details={"recommendation_id": str(recommendation.id)},
            )
            session.commit()
            session.refresh(recommendation)
            return recommendation, result

    # --------------------------------------------------------- listing drafts
    def create_master_draft(self, item_id: UUID, *, actor: str):
        with self._session_factory() as session:
            item = InventoryRepository(session).get(item_id)
            if item is None:
                raise RecordNotFoundError(f"inventory item not found: {item_id}")
            missing = required_data_missing(item)
            if missing:
                raise DomainError(f"item missing required data for a draft: {', '.join(missing)}")
            measurements = [
                {"type": m.measurement_type, "value_cm": str(m.value_cm)}
                for m in MeasurementRepository(session).list_for_item(item_id)
            ]
            content = build_master_draft_content(item, measurements=measurements)
            warnings, missing_fields = validate_master_draft(content)
            repo = ListingDraftRepository(session)
            draft = repo.create(inventory_item_id=item_id, created_by=actor, content=content)
            repo.store_validation(draft.id, warnings=warnings, missing_fields=missing_fields)
            self._audit(
                session,
                "listing.draft_created",
                actor=actor,
                resource_type="master_listing_draft",
                resource_id=draft.id,
                details={"item": str(item_id)},
            )
            self._audit(
                session,
                "listing.draft_validated",
                actor=actor,
                resource_type="master_listing_draft",
                resource_id=draft.id,
                details={"warnings": len(warnings), "missing": len(missing_fields)},
            )
            session.commit()
            session.refresh(draft)
            return draft

    def get_draft(self, draft_id: UUID):
        with self._session_factory() as session:
            draft = ListingDraftRepository(session).get(draft_id)
            if draft is None:
                raise RecordNotFoundError(f"listing draft not found: {draft_id}")
            session.expunge(draft)
            return draft

    def list_drafts(self, *, status: DraftStatus | None = None, limit: int = 100, offset: int = 0):
        with self._session_factory() as session:
            drafts = list(ListingDraftRepository(session).list(status=status, limit=limit, offset=offset))
            for draft in drafts:
                session.expunge(draft)
            return drafts

    def validate_draft(self, draft_id: UUID, *, actor: str):
        with self._session_factory() as session:
            repo = ListingDraftRepository(session)
            draft = repo.get(draft_id)
            if draft is None:
                raise RecordNotFoundError(f"listing draft not found: {draft_id}")
            content = {
                "title": draft.title,
                "description": draft.description,
                "size": draft.size,
                "colors": draft.colors,
                "measurements": draft.measurements,
                "condition": draft.condition,
                "brand": draft.brand,
                "category": draft.category,
            }
            warnings, missing = validate_master_draft(content)
            repo.store_validation(draft_id, warnings=warnings, missing_fields=missing)
            self._audit(
                session,
                "listing.draft_validated",
                actor=actor,
                resource_type="master_listing_draft",
                resource_id=draft_id,
                details={"warnings": len(warnings), "missing": len(missing)},
            )
            session.commit()
            return {"warnings": warnings, "missing_fields": missing}

    def create_variant(self, draft_id: UUID, *, actor: str, marketplace: Marketplace, proposed_price: Decimal | None = None):
        constraint = self._domain.marketplace_constraints.get(marketplace.value)
        if constraint is None:
            raise DomainError(f"no configured constraints for marketplace: {marketplace.value}")
        with self._session_factory() as session:
            draft = ListingDraftRepository(session).get(draft_id)
            if draft is None:
                raise RecordNotFoundError(f"listing draft not found: {draft_id}")
            content = {
                "title": draft.title,
                "description": draft.description,
                "brand": draft.brand,
                "category": draft.category,
                "size": draft.size,
                "materials": draft.materials,
                "condition": draft.condition,
                "style_keywords": draft.style_keywords,
                "image_order": draft.image_order,
            }
            variant_content = build_variant_content(
                content,
                marketplace=marketplace,
                constraint=constraint,
                proposed_price=proposed_price,
            )
            variant = MarketplaceVariantRepository(session).create(
                draft_id=draft_id,
                marketplace=marketplace,
                constraint_version=constraint.version,
                **variant_content,
            )
            self._audit(
                session,
                "variant.created",
                actor=actor,
                resource_type="master_listing_draft",
                resource_id=draft_id,
                details={"marketplace": marketplace.value},
            )
            session.commit()
            session.refresh(variant)
            return variant

    def list_variants(self, draft_id: UUID):
        with self._session_factory() as session:
            values = list(MarketplaceVariantRepository(session).list_for_draft(draft_id))
            for value in values:
                session.expunge(value)
            return values

    def request_listing_approval(self, draft_id: UUID, *, actor: str):
        with self._session_factory() as session:
            draft = ListingDraftRepository(session).get(draft_id)
            if draft is None:
                raise RecordNotFoundError(f"listing draft not found: {draft_id}")
            ListingDraftRepository(session).set_status(
                draft_id, status=DraftStatus.NEEDS_REVIEW, created_by=actor
            )
            proposal = DomainProposalRepository(session).create(
                proposal_type=ProposalType.LISTING_DRAFT_APPROVAL,
                resource_type="master_listing_draft",
                resource_id=draft_id,
                current_version=draft.version,
                proposed_payload={"status": "approved"},
                justification=f"Listing draft {draft_id} ready for review",
                risk_tier="low",
                requested_by=actor,
                expires_at=utc_now()
                + timedelta(seconds=self._domain.proposal_expiration_seconds),
            )
            self._audit(
                session,
                "approval.requested",
                actor=actor,
                resource_type="domain_proposal",
                resource_id=proposal.id,
                details={"type": proposal.proposal_type.value},
            )
            session.commit()
            session.refresh(proposal)
            return proposal

    # ------------------------------------------------------------- proposals
    def submit_proposal(
        self,
        *,
        actor: str,
        proposal_type: ProposalType,
        resource_type: str,
        resource_id: UUID,
        current_version: int,
        proposed_payload: dict[str, Any],
        justification: str,
        risk_tier: str = "medium",
        evidence: list[dict[str, Any]] | None = None,
    ):
        if risk_tier not in self._domain.approval_risk_tiers:
            raise DomainError(f"unknown risk tier: {risk_tier}")
        with self._session_factory() as session:
            proposal = DomainProposalRepository(session).create(
                proposal_type=proposal_type,
                resource_type=resource_type,
                resource_id=resource_id,
                current_version=current_version,
                proposed_payload=proposed_payload,
                justification=justification,
                risk_tier=risk_tier,
                requested_by=actor,
                expires_at=utc_now()
                + timedelta(seconds=self._domain.proposal_expiration_seconds),
                evidence=evidence,
            )
            self._audit(
                session,
                "approval.requested",
                actor=actor,
                resource_type="domain_proposal",
                resource_id=proposal.id,
                details={"type": proposal_type.value, "risk": risk_tier},
            )
            session.commit()
            session.refresh(proposal)
            return proposal

    def get_proposal(self, proposal_id: UUID):
        with self._session_factory() as session:
            proposal = DomainProposalRepository(session).get(proposal_id)
            if proposal is None:
                raise RecordNotFoundError(f"proposal not found: {proposal_id}")
            session.expunge(proposal)
            return proposal

    def list_proposals(
        self,
        *,
        status: ProposalStatus | None = None,
        proposal_type: ProposalType | None = None,
        limit: int = 100,
        offset: int = 0,
    ):
        with self._session_factory() as session:
            proposals = list(
                DomainProposalRepository(session).list(
                    status=status, proposal_type=proposal_type, limit=limit, offset=offset
                )
            )
            for proposal in proposals:
                session.expunge(proposal)
            return proposals

    def reject_proposal(self, proposal_id: UUID, *, reviewer: str, reason: str | None = None):
        with self._session_factory() as session:
            proposal = DomainProposalRepository(session).decide(
                proposal_id, approved=False, reviewer=reviewer, rejection_reason=reason
            )
            self._audit(
                session,
                "approval.rejected",
                actor=reviewer,
                resource_type="domain_proposal",
                resource_id=proposal_id,
                details={"reason": reason},
            )
            session.commit()
            session.refresh(proposal)
            return proposal

    def approve_proposal(self, proposal_id: UUID, *, reviewer: str, notes: str | None = None):
        with self._session_factory() as session:
            proposals = DomainProposalRepository(session)
            proposal = proposals.decide(
                proposal_id, approved=True, reviewer=reviewer, review_notes=notes
            )
            self._audit(
                session,
                "approval.approved",
                actor=reviewer,
                resource_type="domain_proposal",
                resource_id=proposal_id,
                details={"type": proposal.proposal_type.value},
            )
            error = self._execute_proposal(session, proposal, reviewer=reviewer)
            proposals.mark_executed(proposal_id, error=error)
            session.commit()
            session.refresh(proposal)
            return proposal

    def _execute_proposal(self, session: Session, proposal, *, reviewer: str) -> str | None:
        """Deterministically apply an approved, inventory-safe proposal. No live actions."""
        try:
            if proposal.proposal_type in {
                ProposalType.INVENTORY_UPDATE,
                ProposalType.IDENTIFICATION_SELECTION,
                ProposalType.RESEARCH_RESOLUTION,
            }:
                inventory = InventoryRepository(session)
                item = inventory.get(proposal.resource_id)
                if item is None:
                    return "inventory item no longer exists"
                if item.version != proposal.current_version:
                    raise VersionConflictError(
                        "proposal is stale: inventory version changed since request"
                    )
                inventory.update_draft(
                    proposal.resource_id,
                    expected_version=proposal.current_version,
                    changes=proposal.proposed_payload,
                    allow_human_only_status=True,
                )
                self._audit(
                    session,
                    "inventory.approved_change_executed",
                    actor=reviewer,
                    resource_type="inventory_item",
                    resource_id=proposal.resource_id,
                    details={"proposal": str(proposal.id)},
                )
            elif proposal.proposal_type is ProposalType.LISTING_DRAFT_APPROVAL:
                drafts = ListingDraftRepository(session)
                draft = drafts.get(proposal.resource_id)
                if draft is None:
                    return "listing draft no longer exists"
                if draft.version != proposal.current_version:
                    raise VersionConflictError("proposal is stale: draft version changed")
                drafts.set_status(
                    proposal.resource_id, status=DraftStatus.APPROVED, created_by=reviewer
                )
            # Other proposal types record approval only; no live marketplace action.
            return None
        except VersionConflictError as error:
            return str(error)
