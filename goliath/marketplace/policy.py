"""Deterministic marketplace automation policies.

Every policy is a pure function returning a versioned outcome (decision, reasons,
risk score, warnings, numeric outputs). A language model may classify or explain,
but may never override these numeric boundaries. All money math uses Decimal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from goliath.config import (
    OfferRules,
    PricingAutomationRules,
    PublishingRules,
    RefundRules,
)

POLICY_VERSION = "automation-v1"
CENTS = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


@dataclass(slots=True)
class PolicyOutcome:
    policy_type: str
    decision: str
    reasons: list[str] = field(default_factory=list)
    risk_score: Decimal = Decimal(0)
    warnings: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    policy_version: str = POLICY_VERSION


def net_proceeds(
    price: Decimal, *, fee_percent: Decimal, fixed_fee: Decimal, shipping_cost: Decimal
) -> Decimal:
    """Deterministic net proceeds for a sale price."""
    return _money(price - price * fee_percent - fixed_fee - shipping_cost)


@dataclass(slots=True)
class OfferInputs:
    offer_amount: Decimal
    list_price: Decimal
    cost_basis: Decimal
    fee_percent: Decimal = Decimal("0.10")
    fixed_fee: Decimal = Decimal("0.30")
    shipping_cost: Decimal = Decimal(0)
    days_listed: int = 0
    prior_offers: int = 0
    reserved: bool = False
    high_value: bool = False


def evaluate_offer(inputs: OfferInputs, rules: OfferRules) -> PolicyOutcome:
    """Decide accept/decline/counter/escalate. Numeric boundaries are authoritative."""
    reasons: list[str] = []
    if inputs.reserved:
        return PolicyOutcome(
            "offer", "escalate", ["item is reserved"], Decimal("0.7")
        )
    if inputs.high_value:
        return PolicyOutcome(
            "offer", "escalate", ["high-value item requires human review"], Decimal("0.6")
        )

    def profit_at(price: Decimal) -> Decimal:
        return net_proceeds(
            price,
            fee_percent=inputs.fee_percent,
            fixed_fee=inputs.fixed_fee,
            shipping_cost=inputs.shipping_cost,
        ) - inputs.cost_basis

    offer_profit = profit_at(inputs.offer_amount)
    min_profit = Decimal(str(rules.minimum_net_profit))
    # Minimum acceptable price that clears the minimum net profit.
    denominator = Decimal(1) - inputs.fee_percent
    min_price = _money(
        (inputs.cost_basis + min_profit + inputs.fixed_fee + inputs.shipping_cost) / denominator
    )
    margin = (offer_profit / inputs.offer_amount) if inputs.offer_amount > 0 else Decimal(0)
    discount_percent = (
        (inputs.list_price - inputs.offer_amount) / inputs.list_price * 100
        if inputs.list_price > 0
        else Decimal(0)
    )

    data = {
        "offer_profit": str(offer_profit),
        "minimum_price": str(min_price),
        "margin_percent": str(_money(margin * 100)),
        "discount_percent": str(_money(discount_percent)),
    }

    max_discount = Decimal(str(rules.maximum_discount_percent))
    min_margin = Decimal(str(rules.minimum_margin_percent))

    if (
        rules.automatic_accept
        and offer_profit >= min_profit
        and margin * 100 >= min_margin
        and discount_percent <= max_discount
    ):
        reasons.append("offer clears minimum profit and margin")
        return PolicyOutcome("offer", "accept", reasons, Decimal("0.1"), data=data)

    if inputs.offer_amount < min_price and rules.automatic_counter:
        counter = _money(max(min_price, inputs.offer_amount + Decimal(str(rules.counter_increment))))
        reasons.append("offer below minimum acceptable price; countering")
        data["counter_amount"] = str(counter)
        return PolicyOutcome("offer", "counter", reasons, Decimal("0.3"), data=data)

    if discount_percent > max_discount and rules.automatic_decline:
        reasons.append("discount exceeds maximum allowed")
        return PolicyOutcome("offer", "decline", reasons, Decimal("0.2"), data=data)

    reasons.append("no automatic rule matched; deferring")
    return PolicyOutcome("offer", "defer", reasons, Decimal("0.2"), data=data)


@dataclass(slots=True)
class PublishingInputs:
    item_status: str
    draft_approved: bool
    variants_approved: bool
    image_count: int
    pricing_current: bool
    account_healthy: bool
    expected_profit: Decimal
    duplicate_active: bool
    reserved: bool
    emergency_stopped: bool
    breaker_open: bool


def evaluate_publishing(inputs: PublishingInputs, rules: PublishingRules) -> PolicyOutcome:
    reasons: list[str] = []
    if inputs.emergency_stopped:
        reasons.append("emergency stop active")
    if inputs.breaker_open:
        reasons.append("circuit breaker open")
    if inputs.item_status != "ready_for_listing":
        reasons.append(f"item not ready_for_listing: {inputs.item_status}")
    if not inputs.draft_approved:
        reasons.append("no approved master draft")
    if not inputs.variants_approved:
        reasons.append("no approved marketplace variants")
    if rules.require_images and inputs.image_count < rules.min_images:
        reasons.append("insufficient images")
    if not inputs.pricing_current:
        reasons.append("pricing recommendation is not current")
    if not inputs.account_healthy:
        reasons.append("marketplace account is not healthy")
    if inputs.expected_profit < rules.minimum_expected_profit:
        reasons.append("insufficient expected profit")
    if inputs.duplicate_active:
        reasons.append("duplicate active remote listing exists")
    if inputs.reserved:
        reasons.append("item has an open reservation")
    decision = "allow" if not reasons else "block"
    return PolicyOutcome(
        "publishing", decision, reasons or ["all publishing checks passed"], Decimal("0.1")
    )


@dataclass(slots=True)
class MarkdownInputs:
    current_price: Decimal
    minimum_price: Decimal
    days_listed: int
    hours_since_last_change: float
    total_reduction_percent: float


def evaluate_markdown(inputs: MarkdownInputs, rules: PricingAutomationRules) -> PolicyOutcome:
    reasons: list[str] = []
    if inputs.hours_since_last_change < rules.cooldown_hours:
        return PolicyOutcome(
            "pricing", "hold", ["within pricing cooldown"], Decimal("0.1"),
            data={"new_price": str(inputs.current_price)},
        )
    # Choose the markdown percent for the current age bracket.
    percent = 0.0
    for threshold in sorted(rules.stale_thresholds):
        if inputs.days_listed >= threshold:
            percent = rules.stale_thresholds[threshold]
    if percent == 0.0:
        return PolicyOutcome(
            "pricing", "hold", ["item not yet stale"], Decimal("0.1"),
            data={"new_price": str(inputs.current_price)},
        )
    percent = min(percent, rules.maximum_daily_reduction_percent)
    if inputs.total_reduction_percent + percent > rules.maximum_total_reduction_percent:
        percent = max(0.0, rules.maximum_total_reduction_percent - inputs.total_reduction_percent)
        reasons.append("clamped to maximum total reduction")
    proposed = inputs.current_price * (Decimal(1) - Decimal(str(percent)) / 100)
    # Round to the marketplace increment.
    rounding = Decimal(str(rules.marketplace_rounding))
    proposed = (proposed / rounding).quantize(Decimal(1), rounding=ROUND_HALF_UP) * rounding
    if proposed < inputs.minimum_price:
        proposed = inputs.minimum_price
        reasons.append("clamped to minimum price")
    if proposed >= inputs.current_price:
        return PolicyOutcome(
            "pricing", "hold", reasons or ["no reduction available"], Decimal("0.1"),
            data={"new_price": str(inputs.current_price)},
        )
    reasons.append(f"stale markdown at {inputs.days_listed} days")
    return PolicyOutcome(
        "pricing", "markdown", reasons, Decimal("0.2"),
        data={"new_price": str(_money(proposed)), "percent": str(percent)},
    )


@dataclass(slots=True)
class RefundInputs:
    amount: Decimal
    reason: str
    order_status: str
    disputed: bool
    prior_refunds: Decimal = Decimal(0)


def evaluate_refund(inputs: RefundInputs, rules: RefundRules) -> PolicyOutcome:
    if not rules.enabled:
        return PolicyOutcome("refund", "escalate", ["automatic refunds disabled"], Decimal("0.5"))
    if inputs.disputed or inputs.order_status in {"disputed"}:
        return PolicyOutcome("refund", "escalate", ["order is disputed"], Decimal("0.9"))
    if inputs.reason not in rules.allowed_reasons:
        return PolicyOutcome("refund", "escalate", ["reason not auto-approvable"], Decimal("0.6"))
    if inputs.amount > rules.automatic_limit or inputs.amount > rules.require_exception_review_above:
        return PolicyOutcome(
            "refund", "escalate", ["amount above automatic limit"], Decimal("0.7")
        )
    return PolicyOutcome("refund", "approve", ["within automatic refund policy"], Decimal("0.2"))


# Keyword-based deterministic message classification.
_MESSAGE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "availability": ("available", "still for sale", "in stock"),
    "measurements": ("measure", "size", "dimensions", "length", "width"),
    "condition_details": ("condition", "flaw", "damage", "wear", "stain"),
    "included_accessories": ("include", "comes with", "accessor", "box"),
    "shipping_timing": ("ship", "when will", "how long", "arrive"),
    "tracking_status": ("tracking", "where is my", "shipped yet"),
    "bundle_requests": ("bundle", "discount for", "multiple"),
    "price_questions": ("price", "lowest", "best price", "negotiable"),
    "offer_explanations": ("offer", "why declined", "counter"),
    "thank_you": ("thank", "thanks", "appreciate"),
    "cancellation_ack": ("cancel", "cancelled"),
}
_ESCALATION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "threat": ("threat", "hurt you", "report you"),
    "harassment": ("idiot", "scam", "liar"),
    "legal_claim": ("lawyer", "legal", "sue", "attorney"),
    "counterfeit_allegation": ("fake", "counterfeit", "replica", "not authentic"),
    "chargeback": ("chargeback", "dispute", "bank"),
    "fraud": ("fraud", "stolen"),
    "off_platform_payment": ("venmo", "cash app", "paypal friends", "zelle", "off platform"),
    "personal_contact_request": ("phone number", "text me", "call me", "email me"),
    "tracking_dispute": ("never arrived", "didn't receive", "not delivered"),
    "unusual_refund_demand": ("full refund and keep", "refund now or"),
}


def classify_message(body: str, rules) -> PolicyOutcome:
    text = body.lower()
    for category, keywords in _ESCALATION_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return PolicyOutcome(
                "message", "escalate", [f"matched escalation category: {category}"],
                Decimal("0.8"), data={"category": category},
            )
    for category, keywords in _MESSAGE_KEYWORDS.items():
        if category in rules.allowed_categories and any(k in text for k in keywords):
            return PolicyOutcome(
                "message", "respond", [f"routine category: {category}"],
                Decimal("0.1"), data={"category": category},
            )
    return PolicyOutcome(
        "message", "escalate", ["unclassified message"], Decimal("0.4"),
        data={"category": "unknown"},
    )


def build_response(category: str, facts: dict[str, Any]) -> str:
    """Deterministic factual response templates; no costs, notes, or credentials."""
    templates = {
        "availability": "Yes, this item is still available.",
        "measurements": "Measurements: {measurements}.",
        "condition_details": "Condition: {condition}. {condition_notes}",
        "included_accessories": "Included: {accessories}.",
        "shipping_timing": "This ships within {ship_days} business day(s) of payment.",
        "tracking_status": "Your tracking number is {tracking}.",
        "bundle_requests": "I can prepare a bundle — please add the items and I'll review.",
        "price_questions": "The current price is {price}. Feel free to send an offer.",
        "offer_explanations": "The offer was outside the acceptable range for this item.",
        "thank_you": "Thank you for your purchase!",
        "cancellation_ack": "Your cancellation has been acknowledged.",
    }
    template = templates.get(category, "Thanks for your message.")
    try:
        return template.format(**{k: facts.get(k, "n/a") for k in _template_keys(template)})
    except (KeyError, IndexError):
        return template


def _template_keys(template: str) -> list[str]:
    import string

    return [name for _, name, _, _ in string.Formatter().parse(template) if name]
