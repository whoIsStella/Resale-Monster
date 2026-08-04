"""Deterministic pricing engine.

All arithmetic is pure and testable. Agents may supply inputs and observations
(comparables) but may never override the arithmetic. Marketplace fees come only
from versioned configuration.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from goliath.config import MarketplaceFeeVersion, PricingRules

CENTS = Decimal("0.01")
RATE = Decimal("0.0001")


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def _rate(value: Decimal) -> Decimal:
    return value.quantize(RATE, rounding=ROUND_HALF_UP)


class ComparableObservation(BaseModel):
    """An agent-supplied observation used only as pricing input, never as authority."""

    model_config = ConfigDict(extra="forbid")

    price: Decimal = Field(gt=0)
    is_sold: bool = False
    reliability: float = Field(default=0.5, ge=0, le=1)


class PricingInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cost_basis: Decimal = Field(ge=0)
    currency: str = "USD"
    shipping_cost: Decimal = Field(default=Decimal(0), ge=0)
    promotion_cost: Decimal = Field(default=Decimal(0), ge=0)
    expected_offer_discount: float = Field(default=0.0, ge=0, lt=1)
    minimum_acceptable_profit: Decimal | None = Field(default=None, ge=0)
    target_margin: float | None = Field(default=None, ge=0, lt=1)
    condition: str = "good"
    is_stale: bool = False
    comparables: list[ComparableObservation] = Field(default_factory=list)


class PricingResult(BaseModel):
    recommended_price: Decimal
    fast_sale_price: Decimal
    minimum_price: Decimal
    expected_net_proceeds: Decimal
    expected_profit: Decimal
    expected_margin: Decimal
    confidence: Decimal
    fee_version: str
    currency: str
    breakdown: dict[str, Any]
    warnings: list[str]


def _net_proceeds(
    price: Decimal, inputs: PricingInputs, fees: MarketplaceFeeVersion
) -> Decimal:
    fee_percent = Decimal(str(fees.fee_percent))
    payment_percent = Decimal(str(fees.payment_percent))
    offer = Decimal(str(inputs.expected_offer_discount))
    effective_price = price * (Decimal(1) - offer)
    variable = effective_price * (fee_percent + payment_percent)
    return (
        effective_price
        - variable
        - Decimal(str(fees.fixed_fee))
        - inputs.shipping_cost
        - inputs.promotion_cost
    )


def calculate_pricing(
    inputs: PricingInputs,
    *,
    rules: PricingRules,
    fees: MarketplaceFeeVersion,
    fee_version: str,
) -> PricingResult:
    warnings: list[str] = []
    fee_percent = Decimal(str(fees.fee_percent))
    payment_percent = Decimal(str(fees.payment_percent))
    offer = Decimal(str(inputs.expected_offer_discount))

    sold = [c for c in inputs.comparables if c.is_sold]
    active = [c for c in inputs.comparables if not c.is_sold]

    def _mean(values: list[ComparableObservation]) -> Decimal | None:
        if not values:
            return None
        return sum((c.price for c in values), Decimal(0)) / Decimal(len(values))

    sold_mean = _mean(sold)
    active_mean = _mean(active)
    sold_weight = Decimal(str(rules.sold_comparable_weight))
    active_weight = Decimal(str(rules.active_comparable_weight))

    if sold_mean is not None and active_mean is not None:
        total = sold_weight + active_weight
        market = (sold_mean * sold_weight + active_mean * active_weight) / total
    elif sold_mean is not None:
        market = sold_mean
    elif active_mean is not None:
        market = active_mean
    else:
        market = None

    condition_adj = Decimal(str(rules.condition_adjustments.get(inputs.condition, 1.0)))
    size_adj = Decimal(str(rules.size_demand_adjustment))
    stale_adj = Decimal(str(rules.stale_inventory_adjustment)) if inputs.is_stale else Decimal(1)

    if market is None:
        # No comparables: fall back to a cost-plus-target-margin base price.
        target_margin = Decimal(str(inputs.target_margin or rules.target_margin))
        denominator = Decimal(1) - target_margin
        base = inputs.cost_basis / denominator if denominator > 0 else inputs.cost_basis
        warnings.append("no comparables supplied; using cost-plus-margin base price")
    else:
        base = market

    recommended = _money(base * condition_adj * size_adj * stale_adj)

    # Minimum price that still clears the minimum acceptable profit after fees.
    min_profit = (
        inputs.minimum_acceptable_profit
        if inputs.minimum_acceptable_profit is not None
        else Decimal(str(rules.minimum_profit))
    )
    variable_rate = (Decimal(1) - offer) * (fee_percent + payment_percent)
    net_rate = (Decimal(1) - offer) - variable_rate
    fixed_costs = (
        Decimal(str(fees.fixed_fee))
        + inputs.shipping_cost
        + inputs.promotion_cost
        + inputs.cost_basis
        + min_profit
    )
    if net_rate <= 0:
        raise ValueError("fee configuration leaves no net proceeds")
    minimum_price = _money(fixed_costs / net_rate)

    if recommended < minimum_price:
        warnings.append("recommended price raised to satisfy minimum acceptable profit")
        recommended = minimum_price

    fast_sale = _money(
        max(recommended * Decimal(str(rules.fast_sale_factor)), minimum_price)
    )

    net = _net_proceeds(recommended, inputs, fees)
    profit = net - inputs.cost_basis
    margin = _rate(profit / recommended) if recommended > 0 else Decimal(0)

    # Confidence grows with the count and reliability of comparables.
    if inputs.comparables:
        reliability = sum(Decimal(str(c.reliability)) for c in inputs.comparables) / Decimal(
            len(inputs.comparables)
        )
        count_factor = min(Decimal(len(inputs.comparables)) / Decimal(5), Decimal(1))
        confidence = _rate(reliability * Decimal("0.6") + count_factor * Decimal("0.4"))
    else:
        confidence = Decimal("0.200")

    breakdown = {
        "market_reference": str(market) if market is not None else None,
        "sold_mean": str(sold_mean) if sold_mean is not None else None,
        "active_mean": str(active_mean) if active_mean is not None else None,
        "condition_adjustment": str(condition_adj),
        "size_demand_adjustment": str(size_adj),
        "stale_adjustment": str(stale_adj),
        "fee_percent": str(fee_percent),
        "payment_percent": str(payment_percent),
        "fixed_fee": str(Decimal(str(fees.fixed_fee))),
        "expected_offer_discount": str(offer),
        "cost_basis": str(inputs.cost_basis),
        "minimum_acceptable_profit": str(min_profit),
    }

    return PricingResult(
        recommended_price=recommended,
        fast_sale_price=fast_sale,
        minimum_price=minimum_price,
        expected_net_proceeds=_money(net),
        expected_profit=_money(profit),
        expected_margin=margin,
        confidence=confidence,
        fee_version=fee_version,
        currency=inputs.currency,
        breakdown=breakdown,
        warnings=warnings,
    )
