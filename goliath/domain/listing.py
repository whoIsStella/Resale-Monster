"""Deterministic listing-draft generation and marketplace validation."""

from __future__ import annotations

from typing import Any

from goliath.config import MarketplaceConstraintConfig
from goliath.db.models import InventoryItem, Marketplace


def build_master_draft_content(
    item: InventoryItem, *, measurements: list[dict[str, Any]], pricing_recommendation_id=None
) -> dict[str, Any]:
    """Generate marketplace-neutral master draft content from an inventory item."""
    keywords = [
        value
        for value in (item.brand, item.model_name, item.pattern, item.department)
        if value
    ]
    title_parts = [value for value in (item.brand, item.model_name, item.title) if value]
    title = " ".join(dict.fromkeys(" ".join(title_parts).split()))[:300] or item.title
    return {
        "title": title,
        "description": item.description or "",
        "brand": item.brand,
        "category": item.category,
        "subcategory": item.subcategory,
        "condition": item.condition.value,
        "condition_details": item.condition_notes,
        "size": item.size_label,
        "colors": list(item.colors),
        "materials": list(item.materials),
        "style_keywords": keywords,
        "measurements": measurements,
        "defects": list(item.defects),
        "pricing_recommendation_id": pricing_recommendation_id,
        "image_order": [],
        "shipping_assumptions": {
            "estimated_weight_grams": (
                str(item.estimated_weight_grams)
                if item.estimated_weight_grams is not None
                else None
            ),
            "package_dimensions": item.package_dimensions,
        },
    }


REQUIRED_FOR_DRAFT = ("title", "condition")


def required_data_missing(item: InventoryItem) -> list[str]:
    """Fields an item must have before a master draft may be generated."""
    missing = []
    for field in REQUIRED_FOR_DRAFT:
        value = getattr(item, field, None)
        if value is None or (isinstance(value, str) and not value):
            missing.append(field)
    return missing


def validate_master_draft(content: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return (warnings, missing_fields) for a marketplace-neutral master draft."""
    warnings: list[str] = []
    missing: list[str] = []
    if not content.get("title"):
        missing.append("title")
    if not content.get("description"):
        warnings.append("description is empty")
    if not content.get("size"):
        warnings.append("size is missing")
    if not content.get("colors"):
        warnings.append("colors are missing")
    if not content.get("measurements"):
        warnings.append("no measurements attached")
    return warnings, missing


def build_variant_content(
    content: dict[str, Any],
    *,
    marketplace: Marketplace,
    constraint: MarketplaceConstraintConfig,
    proposed_price=None,
) -> dict[str, Any]:
    """Generate a marketplace-specific variant from master content and constraints."""
    title = (content.get("title") or "")[: constraint.max_title_length]
    hashtags: list[str] = []
    if marketplace in {Marketplace.DEPOP, Marketplace.POSHMARK, Marketplace.GRAILED}:
        hashtags = [
            "#" + str(word).lower().replace(" ", "")
            for word in content.get("style_keywords", [])
            if word
        ][:10]
    warnings = validate_variant(title, content, constraint)
    return {
        "marketplace_title": title,
        "marketplace_description": content.get("description"),
        "category_mapping": content.get("category"),
        "item_specifics": {
            "brand": content.get("brand"),
            "size": content.get("size"),
            "materials": content.get("materials"),
        },
        "hashtags": hashtags,
        "proposed_price": proposed_price,
        "image_order": content.get("image_order", []),
        "validation_warnings": warnings,
    }


def validate_variant(
    title: str, content: dict[str, Any], constraint: MarketplaceConstraintConfig
) -> list[str]:
    """Enforce configurable, versioned platform constraints on a variant."""
    warnings: list[str] = []
    if len(content.get("title") or "") > constraint.max_title_length:
        warnings.append(
            f"title exceeds {constraint.max_title_length} characters and was truncated"
        )
    for field in constraint.required_fields:
        if field == "price":
            continue
        if not content.get(field):
            warnings.append(f"required field missing for marketplace: {field}")
    condition = content.get("condition")
    if condition and condition not in constraint.allowed_conditions:
        warnings.append(f"condition not allowed on this marketplace: {condition}")
    return warnings
