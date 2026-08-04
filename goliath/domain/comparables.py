"""Comparable import (CSV/JSON) and pricing-source-policy selection.

Import never silently discards rows: every row gets a recorded status and reason.
No marketplace is scraped; providers supply data. Pricing-source policy decides
which comparables feed deterministic pricing and records inclusions/exclusions.
"""

from __future__ import annotations

import csv
import io
import json
import statistics
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from goliath.config import OrchestrationConfig, PricingSourcePolicy
from goliath.db.domain_repositories import ComparableRepository, DuplicateRecordError
from goliath.db.ingestion_repositories import ComparableImportRepository, ReviewTaskRepository
from goliath.db.models import (
    ComparableProviderType,
    ComparableReviewStatus,
    ImportStatus,
    Marketplace,
    ReviewTaskType,
    utc_now,
)
from goliath.db.repositories import AuditEventRepository
from goliath.domain.pricing import ComparableObservation


class ImportError_(ValueError):
    pass


def parse_csv(text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text))
    return [dict(row) for row in reader]


def parse_json(text: str) -> list[dict[str, Any]]:
    data = json.loads(text)
    if isinstance(data, dict) and "comparables" in data:
        data = data["comparables"]
    if not isinstance(data, list):
        raise ImportError_("JSON import must be a list or an object with a 'comparables' list")
    return list(data)


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError) as error:
        raise ImportError_(f"invalid number: {value}") from error


def _bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "sold", "y"}


def _date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError as error:
        raise ImportError_(f"invalid date: {value}") from error


@dataclass(slots=True)
class ValidatedRow:
    row_number: int
    fields: dict[str, Any]
    raw: dict[str, Any]
    error: str | None = None


class ComparableImportService:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session] | sessionmaker[Session],
        config: OrchestrationConfig,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._limits = config.domain.comparable_import

    def import_rows(
        self,
        inventory_item_id: UUID,
        rows: list[dict[str, Any]],
        *,
        source_format: str,
        actor: str,
        dry_run: bool = False,
        all_or_nothing: bool = False,
    ) -> dict[str, Any]:
        if len(rows) > self._limits.max_rows:
            raise ImportError_(
                f"import exceeds max rows ({len(rows)} > {self._limits.max_rows})"
            )
        validated = [self._validate_row(index + 1, row) for index, row in enumerate(rows)]
        invalid = [row for row in validated if row.error is not None]

        with self._session_factory() as session:
            import_repo = ComparableImportRepository(session)
            record = import_repo.create(
                inventory_item_id=inventory_item_id,
                source_format=source_format,
                requested_by=actor,
                dry_run=dry_run,
                total_rows=len(rows),
            )
            import_id = record.id
            comp_repo = ComparableRepository(session)
            imported = duplicates = failed = 0
            do_write = not dry_run and not (all_or_nothing and invalid)

            for row in validated:
                if row.error is not None:
                    failed += 1
                    import_repo.add_row(
                        import_id,
                        row_number=row.row_number,
                        raw=row.raw,
                        status="failed",
                        error=row.error,
                    )
                    continue
                if not do_write:
                    import_repo.add_row(
                        import_id,
                        row_number=row.row_number,
                        raw=row.raw,
                        status="validated" if dry_run else "skipped",
                    )
                    continue
                try:
                    comparable = comp_repo.add(
                        inventory_item_id=inventory_item_id,
                        marketplace=row.fields["marketplace"],
                        source_identity=row.fields["source_identity"],
                        listing_title=row.fields["listing_title"],
                        is_sold=row.fields["is_sold"],
                        listed_price=row.fields.get("listed_price"),
                        sold_price=row.fields.get("sold_price"),
                        shipping_price=row.fields.get("shipping_price"),
                        currency=row.fields["currency"],
                        condition=row.fields.get("condition"),
                        size=row.fields.get("size"),
                        sale_date=row.fields.get("sale_date"),
                        source_url=row.fields.get("source_url"),
                        similarity_score=row.fields.get("similarity_score"),
                        reliability_score=row.fields.get("reliability_score"),
                    )
                    comparable.provider_type = ComparableProviderType.IMPORTED_CSV
                    comparable.provider_identity = source_format
                    comparable.import_id = import_id
                    session.flush()
                    imported += 1
                    import_repo.add_row(
                        import_id,
                        row_number=row.row_number,
                        raw=row.raw,
                        status="imported",
                        comparable_id=comparable.id,
                    )
                    ReviewTaskRepository(session).create_idempotent(
                        task_type=ReviewTaskType.COMPARABLE_REVIEW,
                        resource_type="comparable_sale",
                        resource_id=comparable.id,
                        reason="imported comparable awaiting review",
                    )
                except DuplicateRecordError:
                    duplicates += 1
                    import_repo.add_row(
                        import_id,
                        row_number=row.row_number,
                        raw=row.raw,
                        status="duplicate",
                        error="duplicate marketplace + source identity",
                    )

            if dry_run:
                status = ImportStatus.DRY_RUN
            elif all_or_nothing and invalid:
                status = ImportStatus.ROLLED_BACK
            elif failed and not imported:
                status = ImportStatus.FAILED
            else:
                status = ImportStatus.COMPLETED

            summary = {
                "total": len(rows),
                "imported": imported,
                "failed": failed,
                "duplicates": duplicates,
                "invalid": len(invalid),
                "all_or_nothing_rolled_back": bool(all_or_nothing and invalid),
            }
            import_repo.finalize(
                import_id,
                status=status,
                imported_rows=imported,
                failed_rows=failed,
                duplicate_rows=duplicates,
                summary=summary,
            )
            actor_type, _, actor_id = actor.partition(":")
            AuditEventRepository(session).append(
                event_type="comparable.imported",
                actor_type=actor_type or "system",
                actor_id=actor_id or actor,
                resource_type="comparable_import",
                resource_id=import_id,
                details={"status": status.value, **summary},
            )
            session.commit()
            return {"import_id": str(import_id), "status": status.value, **summary}

    def _validate_row(self, row_number: int, row: dict[str, Any]) -> ValidatedRow:
        fields: dict[str, Any] = dict(row)
        try:
            marketplace = str(row.get("marketplace", "")).strip().lower()
            if marketplace not in {m.value for m in Marketplace}:
                raise ImportError_(f"unknown marketplace: {marketplace}")
            title = str(row.get("listing_title") or row.get("title") or "").strip()
            if not title:
                raise ImportError_("missing listing_title")
            source_identity = str(
                row.get("source_identity") or row.get("source_url") or ""
            ).strip()
            if not source_identity:
                raise ImportError_("missing source identity")
            currency = str(row.get("currency", "USD")).strip().upper()
            if currency not in self._limits.allowed_currencies:
                raise ImportError_(f"disallowed currency: {currency}")
            sold_price = _decimal(row.get("sold_price"))
            listed_price = _decimal(row.get("listed_price"))
            for label, price in (("sold_price", sold_price), ("listed_price", listed_price)):
                if price is not None and price < 0:
                    raise ImportError_(f"{label} must be nonnegative")
            is_sold = _bool(row.get("is_sold") or row.get("sold"))
            if is_sold and sold_price is None:
                raise ImportError_("sold comparable requires sold_price")
            fields = {
                "marketplace": Marketplace(marketplace),
                "listing_title": title[:500],
                "source_identity": source_identity[:300],
                "currency": currency,
                "is_sold": is_sold,
                "sold_price": sold_price,
                "listed_price": listed_price,
                "shipping_price": _decimal(row.get("shipping_price")),
                "condition": (str(row["condition"]).strip() if row.get("condition") else None),
                "size": (str(row["size"]).strip() if row.get("size") else None),
                "sale_date": _date(row.get("sale_date")),
                "source_url": (str(row["source_url"]).strip() if row.get("source_url") else None),
                "similarity_score": _decimal(row.get("similarity_score")),
                "reliability_score": _decimal(row.get("reliability_score")),
            }
            return ValidatedRow(row_number=row_number, fields=fields, raw=_stringify(row))
        except ImportError_ as error:
            return ValidatedRow(row_number=row_number, fields={}, raw=_stringify(row), error=str(error))


def _stringify(row: dict[str, Any]) -> dict[str, Any]:
    return {key: (str(value) if value is not None else None) for key, value in row.items()}


# --------------------------------------------------------------------------- #
# Pricing-source policy selection
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ComparableSelection:
    included: list[ComparableObservation]
    included_meta: list[dict[str, Any]]
    excluded_meta: list[dict[str, Any]]
    policy_version: str


def select_comparables_for_pricing(
    comparables: list[Any], *, policy: PricingSourcePolicy, now: datetime | None = None
) -> ComparableSelection:
    """Deterministically apply the pricing-source policy, recording every exclusion."""
    now = now or utc_now()
    included: list[ComparableObservation] = []
    included_meta: list[dict[str, Any]] = []
    excluded_meta: list[dict[str, Any]] = []

    prices: list[float] = []
    prescreened: list[tuple[Any, Decimal]] = []
    for comp in comparables:
        reason = _exclusion_reason(comp, policy, now)
        if reason is not None:
            excluded_meta.append({"id": str(comp.id), "reason": reason})
            continue
        price = comp.sold_price or comp.listed_price
        prescreened.append((comp, Decimal(price)))
        prices.append(float(price))

    # Deterministic outlier handling via z-score against the prescreened set.
    mean = statistics.fmean(prices) if prices else 0.0
    stdev = statistics.pstdev(prices) if len(prices) > 1 else 0.0
    for comp, price in prescreened:
        if stdev > 0:
            z = abs(float(price) - mean) / stdev
            if z > policy.outlier_z_threshold:
                excluded_meta.append({"id": str(comp.id), "reason": "price_outlier"})
                continue
        included.append(
            ComparableObservation(
                price=price,
                is_sold=comp.is_sold,
                reliability=float(comp.reliability_score or Decimal("0.5")),
            )
        )
        included_meta.append(
            {"id": str(comp.id), "price": str(price), "is_sold": comp.is_sold}
        )
    return ComparableSelection(
        included=included,
        included_meta=included_meta,
        excluded_meta=excluded_meta,
        policy_version=policy.version,
    )


def _exclusion_reason(comp: Any, policy: PricingSourcePolicy, now: datetime) -> str | None:
    if policy.reviewed_only and comp.review_status is not ComparableReviewStatus.ACCEPTED:
        return "not_reviewed"
    if comp.invalidated_at is not None:
        return "invalidated"
    price = comp.sold_price or comp.listed_price
    if price is None:
        return "no_price"
    if comp.currency.upper() not in {c.upper() for c in policy.allowed_currencies}:
        return "currency_restricted"
    if comp.reliability_score is not None and float(comp.reliability_score) < policy.reliability_threshold:
        return "reliability_below_threshold"
    if comp.similarity_score is not None and float(comp.similarity_score) < policy.similarity_threshold:
        return "similarity_below_threshold"
    if comp.sale_date is not None:
        age_days = (now.date() - comp.sale_date).days
        if age_days > policy.max_comparable_age_days:
            return "too_old"
    return None
