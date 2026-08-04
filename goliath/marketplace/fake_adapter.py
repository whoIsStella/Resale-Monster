"""Complete in-memory fake marketplace adapter for deterministic tests.

Performs no network I/O. Simulates listings, orders, offers, messages, tracking,
fees, and labels with idempotent writes and verifiable read-backs. Never touches
cookies or a real browser.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from goliath.marketplace.adapter import (
    BaseMarketplaceAdapter,
    LabelRequest,
    ListingRequest,
    MessageRequest,
    OfferResponseRequest,
    OperationResult,
)

_ALL_CAPS = {
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
}


class FakeMarketplaceAdapter(BaseMarketplaceAdapter):
    """Deterministic fake with injectable failures for tests."""

    def __init__(
        self,
        *,
        marketplace: str = "fake",
        capabilities: set[str] | None = None,
        authenticated: bool = True,
        fail_operations: set[str] | None = None,
        fail_category: str = "transient",
    ) -> None:
        self.marketplace = marketplace
        self._caps = capabilities if capabilities is not None else set(_ALL_CAPS)
        self._authenticated = authenticated
        self._fail_operations = fail_operations or set()
        self._fail_category = fail_category
        self._listings: dict[str, dict[str, Any]] = {}
        self._orders: dict[str, dict[str, Any]] = {}
        self._offers: dict[str, dict[str, Any]] = {}
        self._messages: dict[str, dict[str, Any]] = {}
        self._labels: dict[str, dict[str, Any]] = {}
        self._sent_messages: dict[str, str] = {}
        self._counter = 0
        self.fee_percent = Decimal("0.10")

    def capabilities(self) -> set[str]:
        return set(self._caps)

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{self.marketplace}-{prefix}-{self._counter}"

    def _maybe_fail(self, operation: str) -> OperationResult | None:
        if operation in self._fail_operations:
            return OperationResult.failure(operation, self._fail_category, "injected failure")
        if operation not in self._caps and operation not in {
            "health_check",
            "authentication_status",
        }:
            return self._unsupported(operation)
        return None

    # -- injection helpers for tests -------------------------------------------
    def seed_order(self, remote_order_id: str, **fields: Any) -> None:
        self._orders[remote_order_id] = {"remote_order_id": remote_order_id, **fields}

    def seed_offer(self, remote_offer_id: str, **fields: Any) -> None:
        self._offers[remote_offer_id] = {"remote_offer_id": remote_offer_id, **fields}

    def seed_message(self, remote_thread_id: str, body: str, **fields: Any) -> None:
        self._messages[remote_thread_id] = {
            "remote_thread_id": remote_thread_id,
            "body": body,
            **fields,
        }

    # -- operations ------------------------------------------------------------
    async def health_check(self) -> OperationResult:
        return OperationResult(ok=True, operation="health_check", data={"status": "healthy"})

    async def authentication_status(self) -> OperationResult:
        return OperationResult(
            ok=True,
            operation="authentication_status",
            data={"authenticated": self._authenticated},
        )

    async def read_account(self) -> OperationResult:
        if (failure := self._maybe_fail("read_account")) is not None:
            return failure
        return OperationResult(
            ok=True, operation="read_account", data={"marketplace": self.marketplace}
        )

    async def read_listings(self) -> OperationResult:
        if (failure := self._maybe_fail("read_listings")) is not None:
            return failure
        return OperationResult(
            ok=True, operation="read_listings", data={"listings": list(self._listings.values())}
        )

    async def read_listing(self, remote_listing_id: str) -> OperationResult:
        if (failure := self._maybe_fail("read_listing")) is not None:
            return failure
        listing = self._listings.get(remote_listing_id)
        if listing is None:
            return OperationResult.failure("read_listing", "not_found", remote_listing_id)
        return OperationResult(
            ok=True, operation="read_listing", data=dict(listing), remote_id=remote_listing_id
        )

    async def create_listing(self, request: ListingRequest) -> OperationResult:
        if (failure := self._maybe_fail("create_listing")) is not None:
            return failure
        # Idempotent replay by idempotency key.
        if request.idempotency_key:
            for existing in self._listings.values():
                if existing.get("idempotency_key") == request.idempotency_key:
                    return OperationResult(
                        ok=True,
                        operation="create_listing",
                        data=dict(existing),
                        remote_id=existing["remote_listing_id"],
                        remote_url=existing["remote_url"],
                        idempotent_replay=True,
                    )
        remote_id = self._next_id("listing")
        record = {
            "remote_listing_id": remote_id,
            "remote_url": f"https://{self.marketplace}.example.test/l/{remote_id}",
            "title": request.title,
            "price": str(request.price),
            "currency": request.currency,
            "status": "active",
            "idempotency_key": request.idempotency_key,
        }
        self._listings[remote_id] = record
        return OperationResult(
            ok=True,
            operation="create_listing",
            data=dict(record),
            remote_id=remote_id,
            remote_url=record["remote_url"],
        )

    async def update_listing(self, remote_listing_id: str, **fields: Any) -> OperationResult:
        if (failure := self._maybe_fail("update_listing")) is not None:
            return failure
        listing = self._listings.get(remote_listing_id)
        if listing is None:
            return OperationResult.failure("update_listing", "not_found", remote_listing_id)
        for key, value in fields.items():
            listing[key] = str(value) if isinstance(value, Decimal) else value
        return OperationResult(
            ok=True, operation="update_listing", data=dict(listing), remote_id=remote_listing_id
        )

    async def refresh_listing(self, remote_listing_id: str) -> OperationResult:
        if (failure := self._maybe_fail("refresh_listing")) is not None:
            return failure
        listing = self._listings.get(remote_listing_id)
        if listing is None:
            return OperationResult.failure("refresh_listing", "not_found", remote_listing_id)
        listing["refreshed"] = True
        return OperationResult(
            ok=True,
            operation="refresh_listing",
            data={"refreshed": True},
            remote_id=remote_listing_id,
        )

    async def promote_listing(self, remote_listing_id: str, **fields: Any) -> OperationResult:
        if (failure := self._maybe_fail("promote_listing")) is not None:
            return failure
        return OperationResult(
            ok=True,
            operation="promote_listing",
            data={"promoted": True},
            remote_id=remote_listing_id,
        )

    async def share_listing(self, remote_listing_id: str) -> OperationResult:
        if (failure := self._maybe_fail("share_listing")) is not None:
            return failure
        return OperationResult(
            ok=True, operation="share_listing", data={"shared": True}, remote_id=remote_listing_id
        )

    async def end_listing(self, remote_listing_id: str) -> OperationResult:
        if (failure := self._maybe_fail("end_listing")) is not None:
            return failure
        listing = self._listings.get(remote_listing_id)
        if listing is None:
            # Ending an unknown listing is treated as already inactive (safe).
            return OperationResult(
                ok=True,
                operation="end_listing",
                data={"status": "ended"},
                remote_id=remote_listing_id,
            )
        listing["status"] = "ended"
        return OperationResult(
            ok=True, operation="end_listing", data={"status": "ended"}, remote_id=remote_listing_id
        )

    async def read_orders(self) -> OperationResult:
        if (failure := self._maybe_fail("read_orders")) is not None:
            return failure
        return OperationResult(
            ok=True, operation="read_orders", data={"orders": list(self._orders.values())}
        )

    async def read_order(self, remote_order_id: str) -> OperationResult:
        if (failure := self._maybe_fail("read_order")) is not None:
            return failure
        order = self._orders.get(remote_order_id)
        if order is None:
            return OperationResult.failure("read_order", "not_found", remote_order_id)
        return OperationResult(ok=True, operation="read_order", data=dict(order))

    async def read_offers(self) -> OperationResult:
        if (failure := self._maybe_fail("read_offers")) is not None:
            return failure
        return OperationResult(
            ok=True, operation="read_offers", data={"offers": list(self._offers.values())}
        )

    async def accept_offer(self, request: OfferResponseRequest) -> OperationResult:
        return self._respond_offer(request.remote_offer_id, "accepted", "accept_offer")

    async def decline_offer(self, request: OfferResponseRequest) -> OperationResult:
        return self._respond_offer(request.remote_offer_id, "declined", "decline_offer")

    async def counter_offer(self, request: OfferResponseRequest) -> OperationResult:
        return self._respond_offer(
            request.remote_offer_id, "countered", "counter_offer", counter=request.counter_amount
        )

    def _respond_offer(self, offer_id, state, operation, counter=None) -> OperationResult:
        if (failure := self._maybe_fail(operation)) is not None:
            return failure
        offer = self._offers.setdefault(offer_id, {"remote_offer_id": offer_id})
        offer["state"] = state
        if counter is not None:
            offer["counter_amount"] = str(counter)
        return OperationResult(
            ok=True, operation=operation, data={"state": state}, remote_id=offer_id
        )

    async def send_offer(self, remote_listing_id: str, amount: Decimal) -> OperationResult:
        if (failure := self._maybe_fail("send_offer")) is not None:
            return failure
        return OperationResult(
            ok=True,
            operation="send_offer",
            data={"amount": str(amount), "sent": True},
            remote_id=remote_listing_id,
        )

    async def read_messages(self) -> OperationResult:
        if (failure := self._maybe_fail("read_messages")) is not None:
            return failure
        return OperationResult(
            ok=True,
            operation="read_messages",
            data={"messages": list(self._messages.values())},
        )

    async def send_message(self, request: MessageRequest) -> OperationResult:
        if (failure := self._maybe_fail("send_message")) is not None:
            return failure
        self._sent_messages[request.remote_thread_id] = request.body
        return OperationResult(
            ok=True,
            operation="send_message",
            data={"delivered": True, "category": request.category},
            remote_id=request.remote_thread_id,
        )

    async def read_notifications(self) -> OperationResult:
        if (failure := self._maybe_fail("read_notifications")) is not None:
            return failure
        return OperationResult(ok=True, operation="read_notifications", data={"notifications": []})

    async def update_tracking(
        self, remote_order_id: str, tracking_number: str, carrier: str
    ) -> OperationResult:
        if (failure := self._maybe_fail("update_tracking")) is not None:
            return failure
        order = self._orders.setdefault(remote_order_id, {"remote_order_id": remote_order_id})
        order["tracking_number"] = tracking_number
        order["carrier"] = carrier
        return OperationResult(
            ok=True,
            operation="update_tracking",
            data={"tracking_number": tracking_number},
            remote_id=remote_order_id,
        )

    async def estimate_fees(self, price: Decimal) -> OperationResult:
        if (failure := self._maybe_fail("estimate_fees")) is not None:
            return failure
        fee = (price * self.fee_percent).quantize(Decimal("0.01"))
        return OperationResult(ok=True, operation="estimate_fees", data={"fee": str(fee)})

    async def read_shipping_options(self, remote_order_id: str) -> OperationResult:
        if (failure := self._maybe_fail("read_shipping_options")) is not None:
            return failure
        return OperationResult(
            ok=True,
            operation="read_shipping_options",
            data={"options": [{"profile": "polymailer-medium", "cost": "6.50"}]},
        )

    async def purchase_label(self, request: LabelRequest) -> OperationResult:
        if (failure := self._maybe_fail("purchase_label")) is not None:
            return failure
        quoted_cost = Decimal("6.50")
        if quoted_cost > request.maximum_cost:
            return OperationResult.failure(
                "purchase_label", "invalid", "label quote exceeds configured ceiling"
            )
        if request.idempotency_key and request.idempotency_key in self._labels:
            record = self._labels[request.idempotency_key]
            return OperationResult(
                ok=True,
                operation="purchase_label",
                data=dict(record),
                remote_id=record["label_reference"],
                idempotent_replay=True,
            )
        label_ref = self._next_id("label")
        tracking = self._next_id("trk")
        record = {
            "label_reference": label_ref,
            "tracking_number": tracking,
            "carrier": "fakepost",
            "cost": str(quoted_cost),
        }
        if request.idempotency_key:
            self._labels[request.idempotency_key] = record
        return OperationResult(
            ok=True, operation="purchase_label", data=dict(record), remote_id=label_ref
        )

    async def sync_account(self) -> OperationResult:
        if (failure := self._maybe_fail("sync_account")) is not None:
            return failure
        return OperationResult(
            ok=True,
            operation="sync_account",
            data={
                "listings": len(self._listings),
                "orders": len(self._orders),
                "offers": len(self._offers),
            },
        )
