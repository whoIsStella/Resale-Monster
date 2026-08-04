"""Deterministic inventory completeness evaluation."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel

from goliath.config import CompletenessRuleConfig
from goliath.db.models import InventoryItem


class CompletenessResult(BaseModel):
    category: str | None
    score: float
    missing_required_fields: list[str]
    missing_recommended_fields: list[str]
    missing_measurements: list[str]
    blocking_errors: list[str]
    warnings: list[str]


_ALWAYS_REQUIRED = ("title", "condition")


def _has_value(item: InventoryItem, field: str) -> bool:
    value = getattr(item, field, None)
    if value is None:
        return False
    return not (isinstance(value, list | dict | str) and len(value) == 0)


def evaluate_completeness(
    item: InventoryItem,
    *,
    rule: CompletenessRuleConfig | None,
    measurement_types: Iterable[str],
) -> CompletenessResult:
    """Return a deterministic completeness assessment for one inventory item."""
    present_measurements = set(measurement_types)
    required_fields = list(_ALWAYS_REQUIRED)
    recommended_fields: list[str] = []
    required_measurements: list[str] = []
    if rule is not None:
        required_fields = list(dict.fromkeys(required_fields + rule.required_fields))
        recommended_fields = list(rule.recommended_fields)
        required_measurements = list(rule.required_measurements)

    missing_required = [field for field in required_fields if not _has_value(item, field)]
    missing_recommended = [field for field in recommended_fields if not _has_value(item, field)]
    missing_measurements = [
        name for name in required_measurements if name not in present_measurements
    ]

    total_checks = len(required_fields) + len(recommended_fields) + len(required_measurements)
    satisfied = total_checks - (
        len(missing_required) + len(missing_recommended) + len(missing_measurements)
    )
    score = round(satisfied / total_checks, 4) if total_checks else 1.0

    blocking_errors: list[str] = []
    if missing_required:
        blocking_errors.append(
            "missing required fields: " + ", ".join(missing_required)
        )
    if missing_measurements:
        blocking_errors.append(
            "missing required measurements: " + ", ".join(missing_measurements)
        )

    warnings: list[str] = []
    if missing_recommended:
        warnings.append("missing recommended fields: " + ", ".join(missing_recommended))
    if rule is None:
        warnings.append("no completeness rule configured for this category")

    return CompletenessResult(
        category=item.category,
        score=score,
        missing_required_fields=missing_required,
        missing_recommended_fields=missing_recommended,
        missing_measurements=missing_measurements,
        blocking_errors=blocking_errors,
        warnings=warnings,
    )


def completeness_payload(result: CompletenessResult) -> dict[str, Any]:
    return result.model_dump()
