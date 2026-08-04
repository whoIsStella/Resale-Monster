"""Bounded MCP prompt templates.

Prompts contain no secrets and no unrestricted instructions. Each renders into a
single user message with the supplied, escaped arguments. Agents remain bound by
tool scopes; prompts only shape the request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class PromptArgument:
    name: str
    description: str
    required: bool = True


@dataclass(slots=True)
class PromptTemplate:
    name: str
    description: str
    arguments: list[PromptArgument]
    template: str
    scope: str = "inventory:read"


PROMPTS: dict[str, PromptTemplate] = {
    "product_identification": PromptTemplate(
        name="product_identification",
        description="Guide identification of an inventory item from its metadata and images.",
        arguments=[PromptArgument("item_id", "Inventory item id")],
        template=(
            "Identify the product for inventory item {item_id}. Use only the "
            "inventory.get, inventory.list_images, and research tools. Record every "
            "source with research.add_source and propose an identification via a "
            "proposal. Do not assert a brand or model without source-backed evidence."
        ),
    ),
    "comparable_research": PromptTemplate(
        name="comparable_research",
        description="Guide gathering of comparable sales for pricing.",
        arguments=[PromptArgument("item_id", "Inventory item id")],
        template=(
            "Gather comparable sales for inventory item {item_id}. Use "
            "research.find_comparables and record observations. Do not scrape "
            "marketplaces or bypass any terms of service; only use approved providers."
        ),
        scope="research:read",
    ),
    "listing_draft_generation": PromptTemplate(
        name="listing_draft_generation",
        description="Guide generation of a marketplace-neutral listing draft.",
        arguments=[PromptArgument("item_id", "Inventory item id")],
        template=(
            "Generate a master listing draft for inventory item {item_id} using "
            "listing.create_master_draft, then request human approval. Never publish "
            "to a live marketplace."
        ),
        scope="listing:read",
    ),
    "stale_inventory_review": PromptTemplate(
        name="stale_inventory_review",
        description="Guide review of stale inventory for possible archival recommendation.",
        arguments=[PromptArgument("item_id", "Inventory item id")],
        template=(
            "Review inventory item {item_id} for staleness. Summarize findings and, if "
            "warranted, submit an archive_recommendation proposal for human approval. "
            "Do not change item status directly."
        ),
    ),
    "inventory_completeness_review": PromptTemplate(
        name="inventory_completeness_review",
        description="Guide a completeness review of an inventory item.",
        arguments=[PromptArgument("item_id", "Inventory item id")],
        template=(
            "Assess completeness for inventory item {item_id}. Read the completeness "
            "resource, list missing required fields, and propose safe draft updates for "
            "human approval."
        ),
    ),
}


class PromptError(ValueError):
    pass


def list_prompts() -> list[dict[str, Any]]:
    return [
        {
            "name": prompt.name,
            "description": prompt.description,
            "arguments": [
                {"name": arg.name, "description": arg.description, "required": arg.required}
                for arg in prompt.arguments
            ],
        }
        for prompt in PROMPTS.values()
    ]


def get_prompt(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    prompt = PROMPTS.get(name)
    if prompt is None:
        raise PromptError(f"unknown prompt: {name}")
    arguments = arguments or {}
    missing = [arg.name for arg in prompt.arguments if arg.required and arg.name not in arguments]
    if missing:
        raise PromptError(f"missing prompt arguments: {', '.join(missing)}")
    safe = {key: _escape(str(value)) for key, value in arguments.items()}
    try:
        text = prompt.template.format(**safe)
    except KeyError as error:
        raise PromptError(f"unknown prompt argument: {error}") from error
    return {
        "description": prompt.description,
        "messages": [
            {"role": "user", "content": {"type": "text", "text": text}}
        ],
    }


def _escape(value: str) -> str:
    # Prevent format-string / brace injection in the rendered prompt.
    return value.replace("{", "").replace("}", "")[:500]
