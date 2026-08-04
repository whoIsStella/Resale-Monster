"""Typed asynchronous marketplace adapter protocol and base class.

Adapters expose real read/write marketplace operations behind typed requests and
results. They never expose raw cookies, credentials, or browser objects; the
opaque session bytes are held privately and used only to authenticate the
isolated context. Unsupported operations fail safely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

# Every operation an adapter may declare and the gateway may invoke.
MARKETPLACE_OPERATIONS: tuple[str, ...] = (
    "health_check",
    "authentication_status",
    "read_account",
    "read_listings",
    "read_listing",
    "create_listing",
    "update_listing",
    "refresh_listing",
    "promote_listing",
    "share_listing",
    "end_listing",
    "read_orders",
    "read_order",
    "read_offers",
    "accept_offer",
    "decline_offer",
    "counter_offer",
    "send_offer",
    "read_messages",
    "send_message",
    "read_notifications",
    "update_tracking",
    "estimate_fees",
    "read_shipping_options",
    "purchase_label",
    "sync_account",
)

# Error categories used for classification and circuit-breaker decisions.
ERROR_CATEGORIES = frozenset(
    {
        "auth_required",
        "rate_limited",
        "not_supported",
        "not_found",
        "challenge",
        "transient",
        "verification_failed",
        "invalid",
        "conflict",
    }
)


@dataclass(slots=True)
class OperationResult:
    ok: bool
    operation: str
    data: dict[str, Any] = field(default_factory=dict)
    remote_id: str | None = None
    remote_url: str | None = None
    error_category: str | None = None
    rate_limit_remaining: int | None = None
    idempotent_replay: bool = False

    @classmethod
    def failure(cls, operation: str, category: str, detail: str = "") -> OperationResult:
        return cls(ok=False, operation=operation, error_category=category, data={"detail": detail})


@dataclass(slots=True)
class ListingRequest:
    inventory_item_id: str
    title: str
    description: str
    price: Decimal
    currency: str = "USD"
    quantity: int = 1
    category: str | None = None
    image_keys: list[str] = field(default_factory=list)
    idempotency_key: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OfferResponseRequest:
    remote_offer_id: str
    action: str  # accept | decline | counter
    counter_amount: Decimal | None = None
    idempotency_key: str | None = None


@dataclass(slots=True)
class MessageRequest:
    remote_thread_id: str
    body: str
    category: str
    idempotency_key: str | None = None


@dataclass(slots=True)
class LabelRequest:
    remote_order_id: str
    package_profile: str
    weight_grams: Decimal
    maximum_cost: Decimal
    idempotency_key: str | None = None


@runtime_checkable
class MarketplaceAdapter(Protocol):
    marketplace: str

    def capabilities(self) -> set[str]: ...

    async def health_check(self) -> OperationResult: ...

    async def authentication_status(self) -> OperationResult: ...

    async def read_account(self) -> OperationResult: ...

    async def read_listings(self) -> OperationResult: ...

    async def read_listing(self, remote_listing_id: str) -> OperationResult: ...

    async def create_listing(self, request: ListingRequest) -> OperationResult: ...

    async def update_listing(self, remote_listing_id: str, **fields: Any) -> OperationResult: ...

    async def refresh_listing(self, remote_listing_id: str) -> OperationResult: ...

    async def promote_listing(self, remote_listing_id: str, **fields: Any) -> OperationResult: ...

    async def share_listing(self, remote_listing_id: str) -> OperationResult: ...

    async def end_listing(self, remote_listing_id: str) -> OperationResult: ...

    async def read_orders(self) -> OperationResult: ...

    async def read_order(self, remote_order_id: str) -> OperationResult: ...

    async def read_offers(self) -> OperationResult: ...

    async def accept_offer(self, request: OfferResponseRequest) -> OperationResult: ...

    async def decline_offer(self, request: OfferResponseRequest) -> OperationResult: ...

    async def counter_offer(self, request: OfferResponseRequest) -> OperationResult: ...

    async def send_offer(self, remote_listing_id: str, amount: Decimal) -> OperationResult: ...

    async def read_messages(self) -> OperationResult: ...

    async def send_message(self, request: MessageRequest) -> OperationResult: ...

    async def read_notifications(self) -> OperationResult: ...

    async def update_tracking(
        self, remote_order_id: str, tracking_number: str, carrier: str
    ) -> OperationResult: ...

    async def estimate_fees(self, price: Decimal) -> OperationResult: ...

    async def read_shipping_options(self, remote_order_id: str) -> OperationResult: ...

    async def purchase_label(self, request: LabelRequest) -> OperationResult: ...

    async def sync_account(self) -> OperationResult: ...


class BaseMarketplaceAdapter:
    """Default adapter: declares no write capability and fails unsupported operations safely."""

    marketplace: str = "generic"

    def capabilities(self) -> set[str]:
        return set()

    def _unsupported(self, operation: str) -> OperationResult:
        return OperationResult.failure(operation, "not_supported", f"{operation} not supported")

    async def health_check(self) -> OperationResult:
        return OperationResult(ok=True, operation="health_check", data={"status": "unknown"})

    async def authentication_status(self) -> OperationResult:
        return OperationResult(
            ok=True, operation="authentication_status", data={"authenticated": False}
        )

    async def read_account(self) -> OperationResult:
        return self._unsupported("read_account")

    async def read_listings(self) -> OperationResult:
        return self._unsupported("read_listings")

    async def read_listing(self, remote_listing_id: str) -> OperationResult:
        return self._unsupported("read_listing")

    async def create_listing(self, request: ListingRequest) -> OperationResult:
        return self._unsupported("create_listing")

    async def update_listing(self, remote_listing_id: str, **fields: Any) -> OperationResult:
        return self._unsupported("update_listing")

    async def refresh_listing(self, remote_listing_id: str) -> OperationResult:
        return self._unsupported("refresh_listing")

    async def promote_listing(self, remote_listing_id: str, **fields: Any) -> OperationResult:
        return self._unsupported("promote_listing")

    async def share_listing(self, remote_listing_id: str) -> OperationResult:
        return self._unsupported("share_listing")

    async def end_listing(self, remote_listing_id: str) -> OperationResult:
        return self._unsupported("end_listing")

    async def read_orders(self) -> OperationResult:
        return self._unsupported("read_orders")

    async def read_order(self, remote_order_id: str) -> OperationResult:
        return self._unsupported("read_order")

    async def read_offers(self) -> OperationResult:
        return self._unsupported("read_offers")

    async def accept_offer(self, request: OfferResponseRequest) -> OperationResult:
        return self._unsupported("accept_offer")

    async def decline_offer(self, request: OfferResponseRequest) -> OperationResult:
        return self._unsupported("decline_offer")

    async def counter_offer(self, request: OfferResponseRequest) -> OperationResult:
        return self._unsupported("counter_offer")

    async def send_offer(self, remote_listing_id: str, amount: Decimal) -> OperationResult:
        return self._unsupported("send_offer")

    async def read_messages(self) -> OperationResult:
        return self._unsupported("read_messages")

    async def send_message(self, request: MessageRequest) -> OperationResult:
        return self._unsupported("send_message")

    async def read_notifications(self) -> OperationResult:
        return self._unsupported("read_notifications")

    async def update_tracking(
        self, remote_order_id: str, tracking_number: str, carrier: str
    ) -> OperationResult:
        return self._unsupported("update_tracking")

    async def estimate_fees(self, price: Decimal) -> OperationResult:
        return self._unsupported("estimate_fees")

    async def read_shipping_options(self, remote_order_id: str) -> OperationResult:
        return self._unsupported("read_shipping_options")

    async def purchase_label(self, request: LabelRequest) -> OperationResult:
        return self._unsupported("purchase_label")

    async def sync_account(self) -> OperationResult:
        return self._unsupported("sync_account")
