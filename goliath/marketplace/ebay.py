"""Official eBay REST adapter with bounded transport and safe projections.

Only this module performs eBay HTTP calls.  Callers receive typed projections,
never OAuth material or raw eBay response bodies.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from goliath.marketplace.adapter import (
    BaseMarketplaceAdapter,
    Capability,
    CapabilityStatus,
    ListingRequest,
    OperationResult,
)

EBAY_PRODUCTION_API = "https://api.ebay.com"
EBAY_SANDBOX_API = "https://api.sandbox.ebay.com"
EBAY_MARKETPLACE_IDS = frozenset(
    {
        "EBAY_US",
        "EBAY_CA",
        "EBAY_GB",
        "EBAY_AU",
        "EBAY_AT",
        "EBAY_BE",
        "EBAY_CH",
        "EBAY_DE",
        "EBAY_ES",
        "EBAY_FR",
        "EBAY_HK",
        "EBAY_IE",
        "EBAY_IT",
        "EBAY_MY",
        "EBAY_NL",
        "EBAY_PH",
        "EBAY_PL",
        "EBAY_SG",
    }
)


class EbayTransportError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        category: str,
        status_code: int | None = None,
        retry_after_seconds: int | None = None,
        correlation_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.correlation_id = correlation_id


class EbayTransportProtocol(Protocol):
    async def get_identity(self) -> dict[str, Any]: ...
    async def get_privileges(self) -> dict[str, Any]: ...
    async def get_policies(self, policy_type: str, marketplace_id: str) -> dict[str, Any]: ...
    async def get_inventory_item(self, sku: str) -> dict[str, Any]: ...
    async def put_inventory_item(self, sku: str, payload: dict[str, Any]) -> dict[str, Any]: ...
    async def list_inventory_items(self, *, limit: int, offset: int) -> dict[str, Any]: ...
    async def get_offer(self, offer_id: str) -> dict[str, Any]: ...
    async def list_offers(self, *, sku: str | None = None, limit: int = 100) -> dict[str, Any]: ...
    async def create_offer(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    async def update_offer(self, offer_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...
    async def publish_offer(self, offer_id: str) -> dict[str, Any]: ...
    async def withdraw_offer(self, offer_id: str) -> dict[str, Any]: ...
    async def list_orders(
        self, *, filter_value: str | None = None, limit: int = 50
    ) -> dict[str, Any]: ...
    async def get_order(self, order_id: str) -> dict[str, Any]: ...
    async def create_fulfillment(
        self, order_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]: ...
    async def list_fulfillments(self, order_id: str) -> dict[str, Any]: ...


class EbayTransport:
    """Allowlisted eBay client. There is intentionally no public arbitrary request API."""

    def __init__(
        self,
        *,
        access_token: str,
        base_url: str,
        marketplace_id: str = "EBAY_US",
        connect_timeout: float = 5.0,
        read_timeout: float = 20.0,
        total_timeout: float = 30.0,
        max_response_bytes: int = 2_000_000,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if base_url not in {EBAY_PRODUCTION_API, EBAY_SANDBOX_API}:
            raise ValueError("eBay base URL is not allowlisted")
        if not access_token:
            raise ValueError("eBay access token is required")
        if marketplace_id not in EBAY_MARKETPLACE_IDS:
            raise ValueError("unsupported eBay marketplace ID")
        if max_response_bytes < 1 or max_retries < 0 or max_retries > 5:
            raise ValueError("unsafe eBay transport bounds")
        self._access_token = access_token
        self._base_url = base_url.rstrip("/")
        self._marketplace_id = marketplace_id
        self._max_response_bytes = max_response_bytes
        self._max_retries = max_retries
        timeout = httpx.Timeout(total_timeout, connect=connect_timeout, read=read_timeout)
        self._client = client or httpx.AsyncClient(
            timeout=timeout, verify=True, follow_redirects=False
        )
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        payload: dict[str, Any] | None = None,
        retry_safe: bool = False,
    ) -> dict[str, Any]:
        if not path.startswith(("/sell/", "/commerce/identity/")) or ".." in path:
            raise EbayTransportError("blocked eBay path", category="validation")
        correlation_id = hashlib.sha256(f"{method}:{path}:{random.random()}".encode()).hexdigest()[
            :24
        ]
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-EBAY-C-MARKETPLACE-ID": self._marketplace_id,
            "X-GOLIATH-CORRELATION-ID": correlation_id,
        }
        attempts = self._max_retries + 1 if retry_safe else 1
        for attempt in range(attempts):
            try:
                response = await self._client.request(
                    method, self._base_url + path, params=params, json=payload, headers=headers
                )
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                if attempt + 1 < attempts:
                    await asyncio.sleep(min(0.05 * (2**attempt), 0.2) + random.random() * 0.01)
                    continue
                raise EbayTransportError(
                    "eBay transport unavailable",
                    category="transport",
                    correlation_id=correlation_id,
                ) from error
            retry_after = _retry_after(response.headers.get("Retry-After"))
            if response.status_code == 429:
                raise EbayTransportError(
                    "eBay rate limit reached",
                    category="rate_limit",
                    status_code=429,
                    retry_after_seconds=retry_after,
                    correlation_id=correlation_id,
                )
            if response.status_code in {500, 502, 503, 504} and attempt + 1 < attempts:
                await asyncio.sleep(min(0.05 * (2**attempt), 0.2) + random.random() * 0.01)
                continue
            if response.status_code >= 400:
                raise EbayTransportError(
                    _safe_error_message(response.status_code),
                    category=_status_category(response.status_code),
                    status_code=response.status_code,
                    retry_after_seconds=retry_after,
                    correlation_id=correlation_id,
                )
            body = response.content
            if len(body) > self._max_response_bytes:
                raise EbayTransportError(
                    "eBay response exceeded configured limit", category="transport"
                )
            content_type = response.headers.get("content-type", "").partition(";")[0].lower()
            if not body and response.status_code in {200, 201, 204}:
                return {"_request_id": _request_id(response.headers)}
            if content_type != "application/json":
                raise EbayTransportError(
                    "unexpected eBay response content type", category="transport"
                )
            try:
                value = response.json()
            except json.JSONDecodeError as error:
                raise EbayTransportError(
                    "malformed eBay JSON response", category="transport"
                ) from error
            if not isinstance(value, dict):
                raise EbayTransportError("invalid eBay response shape", category="transport")
            value["_request_id"] = _request_id(response.headers)
            return value
        raise EbayTransportError("eBay transport unavailable", category="transport")

    async def get_identity(self) -> dict[str, Any]:
        return await self._request("GET", "/commerce/identity/v1/user/", retry_safe=True)

    async def get_privileges(self) -> dict[str, Any]:
        return await self._request("GET", "/sell/account/v1/privilege", retry_safe=True)

    async def get_policies(self, policy_type: str, marketplace_id: str) -> dict[str, Any]:
        allowed = {"fulfillment_policy", "payment_policy", "return_policy"}
        if policy_type not in allowed or marketplace_id not in EBAY_MARKETPLACE_IDS:
            raise EbayTransportError("invalid policy request", category="validation")
        return await self._request(
            "GET",
            f"/sell/account/v1/{policy_type}",
            params={"marketplace_id": marketplace_id},
            retry_safe=True,
        )

    async def get_inventory_item(self, sku: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"/sell/inventory/v1/inventory_item/{quote(sku, safe='')}", retry_safe=True
        )

    async def put_inventory_item(self, sku: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request(
            "PUT",
            f"/sell/inventory/v1/inventory_item/{quote(sku, safe='')}",
            payload=payload,
            retry_safe=True,
        )

    async def list_inventory_items(self, *, limit: int, offset: int) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/sell/inventory/v1/inventory_item",
            params={"limit": limit, "offset": offset},
            retry_safe=True,
        )

    async def get_offer(self, offer_id: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"/sell/inventory/v1/offer/{quote(offer_id, safe='')}", retry_safe=True
        )

    async def list_offers(self, *, sku: str | None = None, limit: int = 100) -> dict[str, Any]:
        params: dict[str, str | int] = {"limit": min(max(limit, 1), 200)}
        if sku:
            params["sku"] = sku
        return await self._request(
            "GET", "/sell/inventory/v1/offer", params=params, retry_safe=True
        )

    async def create_offer(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/sell/inventory/v1/offer", payload=payload)

    async def update_offer(self, offer_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request(
            "PUT",
            f"/sell/inventory/v1/offer/{quote(offer_id, safe='')}",
            payload=payload,
            retry_safe=True,
        )

    async def publish_offer(self, offer_id: str) -> dict[str, Any]:
        return await self._request(
            "POST", f"/sell/inventory/v1/offer/{quote(offer_id, safe='')}/publish"
        )

    async def withdraw_offer(self, offer_id: str) -> dict[str, Any]:
        return await self._request(
            "POST", f"/sell/inventory/v1/offer/{quote(offer_id, safe='')}/withdraw"
        )

    async def list_orders(
        self, *, filter_value: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        params: dict[str, str | int] = {"limit": min(max(limit, 1), 200)}
        if filter_value:
            params["filter"] = filter_value
        return await self._request(
            "GET", "/sell/fulfillment/v1/order", params=params, retry_safe=True
        )

    async def get_order(self, order_id: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"/sell/fulfillment/v1/order/{quote(order_id, safe='')}", retry_safe=True
        )

    async def create_fulfillment(self, order_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/sell/fulfillment/v1/order/{quote(order_id, safe='')}/shipping_fulfillment",
            payload=payload,
        )

    async def list_fulfillments(self, order_id: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/sell/fulfillment/v1/order/{quote(order_id, safe='')}/shipping_fulfillment",
            retry_safe=True,
        )


class SafeAccount(BaseModel):
    model_config = ConfigDict(extra="ignore")
    remote_account_id: str
    seller_name: str
    status: str = "active"


class SafeListing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    remote_listing_id: str
    offer_id: str
    listing_id: str | None = None
    sku: str
    title: str | None = None
    price: Decimal | None = None
    currency: str = "USD"
    quantity: int = 0
    status: str
    revision: str | None = None


class SafeOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    remote_order_id: str
    status: str
    payment_status: str
    paid: bool
    total: Decimal
    sale_price: Decimal
    currency: str
    remote_listing_ids: list[str] = Field(default_factory=list)
    line_items: list[dict[str, Any]] = Field(default_factory=list)
    fees: Decimal = Decimal(0)
    marketplace_fees: Decimal = Decimal(0)
    payment_state: str
    remote_listing_id: str | None = None
    quantity: int = 1
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class EbayAdapterSettings:
    marketplace_id: str = "EBAY_US"
    merchant_location_key: str | None = None
    payment_policy_id: str | None = None
    fulfillment_policy_id: str | None = None
    return_policy_id: str | None = None


class EbayMarketplaceAdapter(BaseMarketplaceAdapter):
    marketplace = "ebay"
    _SUPPORTED = frozenset(
        {
            "health_check",
            "authentication_status",
            "read_account",
            "read_account_status",
            "read_seller_limits",
            "read_business_policies",
            "read_listings",
            "read_listing",
            "read_ended_listings",
            "create_listing",
            "update_listing",
            "update_price",
            "update_quantity",
            "end_listing",
            "reconcile_listing",
            "read_orders",
            "read_order",
            "update_tracking",
            "estimate_fees",
            "read_fees",
            "sync_account",
        }
    )

    def __init__(self, *, transport: EbayTransportProtocol, settings: EbayAdapterSettings) -> None:
        self._transport = transport
        self._settings = settings

    def capabilities(self) -> set[str]:
        return set(self._SUPPORTED)

    def capability_report(self) -> tuple[Capability, ...]:
        from goliath.marketplace.adapter import MARKETPLACE_OPERATIONS, _capability_group

        manual = {"purchase_label", "read_shipping_options"}
        readonly = {"read_fees"}
        return tuple(
            Capability(
                operation=op,
                group=_capability_group(op),
                status=(
                    CapabilityStatus.READ_ONLY
                    if op in readonly
                    else CapabilityStatus.REQUIRES_MANUAL_STEP
                    if op in manual
                    else CapabilityStatus.SUPPORTED
                    if op in self._SUPPORTED
                    else CapabilityStatus.UNSUPPORTED
                ),
            )
            for op in MARKETPLACE_OPERATIONS
        )

    async def health_check(self) -> OperationResult:
        return await self.authentication_status()

    async def authentication_status(self) -> OperationResult:
        result = await self.read_account()
        return OperationResult(
            ok=result.ok,
            operation="authentication_status",
            data={"authenticated": result.ok, "status": result.data.get("status", "unknown")},
            error_category=result.error_category,
        )

    async def read_account(self) -> OperationResult:
        try:
            raw = await self._transport.get_identity()
            account = _project_account(raw)
            return OperationResult(ok=True, operation="read_account", data=account.model_dump())
        except (EbayTransportError, ValidationError, ValueError) as error:
            return _failure("read_account", error)

    async def read_account_status(self) -> OperationResult:
        return await self.read_account()

    async def read_seller_limits(self) -> OperationResult:
        try:
            raw = await self._transport.get_privileges()
            limits = [
                {
                    "type": str(item.get("limitType", "unknown")),
                    "remaining": item.get("quantity") or item.get("amount"),
                }
                for item in raw.get("sellingLimit", raw.get("sellingLimits", []))
                if isinstance(item, dict)
            ]
            return OperationResult(ok=True, operation="read_seller_limits", data={"limits": limits})
        except EbayTransportError as error:
            return _failure("read_seller_limits", error)

    async def read_business_policies(self) -> OperationResult:
        try:
            safe: dict[str, list[dict[str, str]]] = {}
            for kind in ("fulfillment_policy", "payment_policy", "return_policy"):
                raw = await self._transport.get_policies(kind, self._settings.marketplace_id)
                plural = {
                    "fulfillment_policy": "fulfillmentPolicies",
                    "payment_policy": "paymentPolicies",
                    "return_policy": "returnPolicies",
                }[kind]
                safe[kind] = [
                    {
                        "id": str(
                            row.get(kind.replace("_", "") + "Id")
                            or row.get(_policy_id_key(kind))
                            or ""
                        ),
                        "name": str(row.get("name", ""))[:200],
                    }
                    for row in raw.get(plural, [])
                    if isinstance(row, dict)
                ]
            return OperationResult(
                ok=True, operation="read_business_policies", data={"policies": safe}
            )
        except EbayTransportError as error:
            return _failure("read_business_policies", error)

    async def read_listings(self) -> OperationResult:
        try:
            raw = await self._transport.list_offers(limit=100)
            values = [_project_offer(row).model_dump(mode="json") for row in raw.get("offers", [])]
            return OperationResult(ok=True, operation="read_listings", data={"listings": values})
        except (EbayTransportError, ValidationError, ValueError) as error:
            return _failure("read_listings", error)

    async def read_ended_listings(self) -> OperationResult:
        result = await self.read_listings()
        if result.ok:
            result.operation = "read_ended_listings"
            result.data["listings"] = [x for x in result.data["listings"] if x["status"] == "ended"]
        return result

    async def read_listing(self, remote_listing_id: str) -> OperationResult:
        try:
            offer_id = await self._resolve_offer_id(remote_listing_id)
            listing = _project_offer(await self._transport.get_offer(offer_id))
            return OperationResult(
                ok=True,
                operation="read_listing",
                data=listing.model_dump(mode="json"),
                remote_id=listing.remote_listing_id,
            )
        except (EbayTransportError, ValidationError, ValueError) as error:
            return _failure("read_listing", error)

    async def create_listing(self, request: ListingRequest) -> OperationResult:
        try:
            _validate_listing_request(request, self._settings)
            sku = f"goliath-{request.inventory_item_id}"
            existing = await self._find_offer_by_sku(sku)
            if existing is not None:
                projected = _project_offer(existing)
                return OperationResult(
                    ok=True,
                    operation="create_listing",
                    data=projected.model_dump(mode="json"),
                    remote_id=projected.remote_listing_id,
                    idempotent_replay=True,
                )
            image_urls = [str(value) for value in request.attributes.get("image_urls", [])]
            inventory_payload = {
                "availability": {"shipToLocationAvailability": {"quantity": request.quantity}},
                "condition": str(request.attributes["condition"]),
                "product": {
                    "title": request.title,
                    "description": request.description,
                    "imageUrls": image_urls,
                    "aspects": request.attributes.get("aspects", {}),
                },
            }
            await self._transport.put_inventory_item(sku, inventory_payload)
            offer_payload = {
                "sku": sku,
                "marketplaceId": self._settings.marketplace_id,
                "format": "FIXED_PRICE",
                "availableQuantity": request.quantity,
                "categoryId": request.category,
                "merchantLocationKey": self._settings.merchant_location_key,
                "listingDescription": request.description,
                "pricingSummary": {
                    "price": {"value": str(request.price), "currency": request.currency}
                },
                "listingPolicies": {
                    "paymentPolicyId": self._settings.payment_policy_id,
                    "fulfillmentPolicyId": self._settings.fulfillment_policy_id,
                    "returnPolicyId": self._settings.return_policy_id,
                },
            }
            created = await self._transport.create_offer(offer_payload)
            offer_id = str(created.get("offerId", ""))
            if not offer_id:
                raise EbayTransportError("eBay omitted offer identity", category="permanent_remote")
            published = await self._transport.publish_offer(offer_id)
            readback = _project_offer(await self._transport.get_offer(offer_id))
            if str(readback.price) != str(request.price) or readback.quantity != request.quantity:
                return OperationResult.failure(
                    "create_listing", "verification", "eBay listing read-back mismatch"
                )
            data = readback.model_dump(mode="json")
            data["listing_id"] = published.get("listingId") or data.get("listing_id")
            return OperationResult(
                ok=True,
                operation="create_listing",
                data=data,
                remote_id=readback.remote_listing_id,
                remote_url=_listing_url(data.get("listing_id")),
                remote_request_id=created.get("_request_id"),
            )
        except (EbayTransportError, ValidationError, ValueError, KeyError) as error:
            return _failure("create_listing", error)

    async def update_listing(self, remote_listing_id: str, **fields: Any) -> OperationResult:
        try:
            offer_id = await self._resolve_offer_id(remote_listing_id)
            current = await self._transport.get_offer(offer_id)
            allowed = {"price", "quantity", "description", "title"}
            if set(fields) - allowed:
                raise ValueError("unsupported eBay listing update field")
            if "price" in fields:
                price = Decimal(str(fields["price"]))
                if price <= 0:
                    raise ValueError("price must be positive")
                current.setdefault("pricingSummary", {})["price"] = {
                    "value": str(price),
                    "currency": current.get("pricingSummary", {})
                    .get("price", {})
                    .get("currency", "USD"),
                }
            if "quantity" in fields:
                quantity = int(fields["quantity"])
                if quantity < 0:
                    raise ValueError("quantity must be nonnegative")
                current["availableQuantity"] = quantity
            safe_update = {
                key: value for key, value in current.items() if key in _OFFER_UPDATE_FIELDS
            }
            await self._transport.update_offer(offer_id, safe_update)
            readback = _project_offer(await self._transport.get_offer(offer_id))
            return OperationResult(
                ok=True,
                operation="update_listing",
                data=readback.model_dump(mode="json"),
                remote_id=readback.remote_listing_id,
            )
        except (EbayTransportError, ValidationError, ValueError) as error:
            return _failure("update_listing", error)

    async def update_price(self, remote_listing_id: str, price: Decimal) -> OperationResult:
        result = await self.update_listing(remote_listing_id, price=price)
        result.operation = "update_price"
        return result

    async def update_quantity(self, remote_listing_id: str, quantity: int) -> OperationResult:
        result = await self.update_listing(remote_listing_id, quantity=quantity)
        result.operation = "update_quantity"
        return result

    async def end_listing(self, remote_listing_id: str) -> OperationResult:
        try:
            offer_id = await self._resolve_offer_id(remote_listing_id)
            result = await self._transport.withdraw_offer(offer_id)
            readback = _project_offer(await self._transport.get_offer(offer_id))
            if readback.status != "ended":
                return OperationResult.failure(
                    "end_listing", "verification", "withdrawal not confirmed"
                )
            return OperationResult(
                ok=True,
                operation="end_listing",
                data=readback.model_dump(mode="json"),
                remote_id=readback.remote_listing_id,
                remote_request_id=result.get("_request_id"),
            )
        except (EbayTransportError, ValidationError, ValueError) as error:
            return _failure("end_listing", error)

    async def reconcile_listing(self, remote_listing_id: str) -> OperationResult:
        result = await self.read_listing(remote_listing_id)
        result.operation = "reconcile_listing"
        return result

    async def read_orders(self) -> OperationResult:
        try:
            raw = await self._transport.list_orders(limit=50)
            values = [_project_order(row).model_dump(mode="json") for row in raw.get("orders", [])]
            return OperationResult(ok=True, operation="read_orders", data={"orders": values})
        except (EbayTransportError, ValidationError, ValueError) as error:
            return _failure("read_orders", error)

    async def read_order(self, remote_order_id: str) -> OperationResult:
        try:
            order = _project_order(await self._transport.get_order(remote_order_id))
            return OperationResult(
                ok=True,
                operation="read_order",
                data=order.model_dump(mode="json"),
                remote_id=remote_order_id,
            )
        except (EbayTransportError, ValidationError, ValueError) as error:
            return _failure("read_order", error)

    async def update_tracking(
        self, remote_order_id: str, tracking_number: str, carrier: str
    ) -> OperationResult:
        if not tracking_number.strip() or not carrier.strip():
            return OperationResult.failure(
                "update_tracking", "validation", "tracking and carrier are required"
            )
        try:
            order = await self._transport.get_order(remote_order_id)
            lines = [
                {"lineItemId": str(row["lineItemId"]), "quantity": int(row.get("quantity", 1))}
                for row in order.get("lineItems", [])
                if row.get("lineItemId")
            ]
            if not lines:
                raise ValueError("order has no fulfillable line items")
            result = await self._transport.create_fulfillment(
                remote_order_id,
                {
                    "lineItems": lines,
                    "shippedDate": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "shippingCarrierCode": carrier,
                    "trackingNumber": tracking_number,
                },
            )
            fulfillment_id = str(result.get("fulfillmentId", ""))
            readback = await self._transport.list_fulfillments(remote_order_id)
            verified = any(
                str(row.get("shipmentTrackingNumber", "")) == tracking_number
                for row in readback.get("fulfillments", [])
            )
            if not verified:
                return OperationResult.failure(
                    "update_tracking", "verification", "tracking read-back mismatch"
                )
            return OperationResult(
                ok=True,
                operation="update_tracking",
                data={
                    "status": "uploaded",
                    "carrier": carrier,
                    "tracking_reference": hashlib.sha256(tracking_number.encode()).hexdigest()[:16],
                },
                remote_id=fulfillment_id,
            )
        except (EbayTransportError, ValueError, KeyError) as error:
            return _failure("update_tracking", error)

    async def estimate_fees(self, price: Decimal) -> OperationResult:
        return OperationResult.failure(
            "estimate_fees",
            "requires_manual_step",
            "fee estimate requires a prepared unpublished offer",
        )

    async def read_fees(self, remote_order_id: str) -> OperationResult:
        result = await self.read_order(remote_order_id)
        result.operation = "read_fees"
        result.data = (
            {"fees": result.data.get("fees"), "currency": result.data.get("currency")}
            if result.ok
            else result.data
        )
        return result

    async def sync_account(self) -> OperationResult:
        account, listings, orders = await asyncio.gather(
            self.read_account(), self.read_listings(), self.read_orders()
        )
        if not all(value.ok for value in (account, listings, orders)):
            failed = next(value for value in (account, listings, orders) if not value.ok)
            return OperationResult.failure(
                "sync_account",
                failed.error_category or "temporary_remote",
                "one or more eBay reads failed",
            )
        return OperationResult(
            ok=True,
            operation="sync_account",
            data={
                "account": account.data,
                "listings": listings.data["listings"],
                "orders": orders.data["orders"],
            },
        )

    async def _find_offer_by_sku(self, sku: str) -> dict[str, Any] | None:
        try:
            raw = await self._transport.list_offers(sku=sku, limit=10)
        except EbayTransportError as error:
            if error.status_code == 404:
                return None
            raise
        values = [row for row in raw.get("offers", []) if row.get("sku") == sku]
        if len(values) > 1:
            raise EbayTransportError(
                "duplicate remote offer for deterministic SKU", category="conflict"
            )
        return values[0] if values else None

    async def _resolve_offer_id(self, remote_listing_id: str) -> str:
        try:
            raw = await self._transport.get_offer(remote_listing_id)
            if raw.get("offerId"):
                return str(raw["offerId"])
        except EbayTransportError as error:
            if error.status_code != 404:
                raise
        raw = await self._transport.list_offers(limit=200)
        matches = [
            row
            for row in raw.get("offers", [])
            if str(row.get("listing", {}).get("listingId") or row.get("listingId") or "")
            == remote_listing_id
        ]
        if len(matches) != 1:
            raise EbayTransportError("eBay listing-to-offer mapping unavailable", category="conflict")
        return str(matches[0]["offerId"])


_OFFER_UPDATE_FIELDS = frozenset(
    {
        "sku",
        "marketplaceId",
        "format",
        "availableQuantity",
        "categoryId",
        "merchantLocationKey",
        "listingDescription",
        "listingDuration",
        "listingPolicies",
        "pricingSummary",
        "quantityLimitPerBuyer",
        "tax",
    }
)


def _project_account(raw: dict[str, Any]) -> SafeAccount:
    identity = raw.get("user") if isinstance(raw.get("user"), dict) else raw
    remote_id = str(identity.get("userId") or identity.get("username") or "")
    seller_name = str(identity.get("username") or identity.get("userId") or "")
    if not remote_id:
        raise ValueError("eBay identity response omitted account identity")
    return SafeAccount(remote_account_id=remote_id, seller_name=seller_name[:100])


def _project_offer(raw: dict[str, Any]) -> SafeListing:
    offer_id = str(raw.get("offerId", ""))
    sku = str(raw.get("sku", ""))
    if not offer_id or not sku:
        raise ValueError("eBay offer response omitted identity")
    price = raw.get("pricingSummary", {}).get("price", {})
    raw_status = str(raw.get("status", "UNPUBLISHED")).upper()
    status = (
        "active"
        if raw_status in {"PUBLISHED", "ACTIVE"}
        else "ended"
        if raw_status in {"WITHDRAWN", "ENDED"}
        else "pending"
    )
    listing_id = raw.get("listing", {}).get("listingId") or raw.get("listingId")
    return SafeListing(
        remote_listing_id=str(listing_id or offer_id),
        offer_id=offer_id,
        listing_id=listing_id,
        sku=sku,
        title=(str(raw.get("listingDescription", ""))[:500] or None),
        price=Decimal(str(price["value"])) if price.get("value") is not None else None,
        currency=str(price.get("currency", "USD")),
        quantity=int(raw.get("availableQuantity", 0)),
        status=status,
        revision=str(raw.get("lastModifiedDate")) if raw.get("lastModifiedDate") else None,
    )


def _project_order(raw: dict[str, Any]) -> SafeOrder:
    order_id = str(raw.get("orderId", ""))
    if not order_id:
        raise ValueError("eBay order response omitted identity")
    total = raw.get("pricingSummary", {}).get("total", {})
    payment = str(raw.get("orderPaymentStatus", "UNKNOWN"))
    lines = []
    listing_ids = []
    fees = Decimal(0)
    for row in raw.get("lineItems", []):
        listing_id = str(row.get("legacyItemId") or row.get("listingId") or "")
        if listing_id:
            listing_ids.append(listing_id)
        line_total = row.get("lineItemCost", {})
        lines.append(
            {
                "line_item_id": str(row.get("lineItemId", "")),
                "listing_id": listing_id,
                "sku": str(row.get("sku", "")),
                "quantity": int(row.get("quantity", 1)),
                "total": str(line_total.get("value", "0")),
            }
        )
        for fee in row.get("totalMarketplaceFee", []):
            if isinstance(fee, dict):
                fees += Decimal(str(fee.get("value", "0")))
    paid = payment in {"PAID", "FULLY_PAID"}
    fulfillment = str(raw.get("orderFulfillmentStatus", "UNKNOWN"))
    status = "shipped" if fulfillment == "FULFILLED" else "paid" if paid else "pending_payment"
    total_value = Decimal(str(total.get("value", "0")))
    return SafeOrder(
        remote_order_id=order_id,
        status=status,
        payment_status=payment.lower(),
        payment_state=payment.lower(),
        paid=paid,
        total=total_value,
        sale_price=total_value,
        currency=str(total.get("currency", "USD")),
        remote_listing_ids=listing_ids,
        remote_listing_id=listing_ids[0] if listing_ids else None,
        quantity=sum(int(row.get("quantity", 1)) for row in raw.get("lineItems", [])) or 1,
        line_items=lines,
        fees=fees,
        marketplace_fees=fees,
        created_at=raw.get("creationDate"),
    )


def _validate_listing_request(request: ListingRequest, settings: EbayAdapterSettings) -> None:
    missing = [
        name
        for name, value in {
            "category": request.category,
            "merchant_location_key": settings.merchant_location_key,
            "payment_policy_id": settings.payment_policy_id,
            "fulfillment_policy_id": settings.fulfillment_policy_id,
            "return_policy_id": settings.return_policy_id,
            "condition": request.attributes.get("condition"),
        }.items()
        if not value
    ]
    if missing:
        raise ValueError("missing eBay publication fields: " + ", ".join(missing))
    if (
        request.price <= 0
        or request.quantity < 1
        or not request.title.strip()
        or not request.description.strip()
    ):
        raise ValueError("invalid eBay listing values")
    image_urls = request.attributes.get("image_urls")
    if (
        not isinstance(image_urls, list)
        or not image_urls
        or any(not str(url).startswith("https://") for url in image_urls)
    ):
        raise ValueError("eBay publication requires HTTPS image URLs")


def _failure(operation: str, error: Exception) -> OperationResult:
    if isinstance(error, EbayTransportError):
        return OperationResult(
            ok=False,
            operation=operation,
            error_category=error.category,
            data={"detail": str(error)},
            retry_after_seconds=error.retry_after_seconds,
            remote_request_id=error.correlation_id,
        )
    category = (
        "validation"
        if isinstance(error, (ValueError, KeyError, ValidationError))
        else "permanent_remote"
    )
    return OperationResult.failure(operation, category, str(error))


def _retry_after(value: str | None) -> int | None:
    if value and value.isdigit():
        return min(int(value), 86_400)
    return None


def _request_id(headers: Mapping[str, str]) -> str | None:
    return headers.get("x-ebay-c-request-id") or headers.get("x-ebay-correlation-id")


def _safe_error_message(status: int) -> str:
    return f"eBay request failed with HTTP {status}"


def _status_category(status: int) -> str:
    if status == 401:
        return "authentication"
    if status == 403:
        return "authorization"
    if status == 404:
        return "not_found"
    if status == 409:
        return "conflict"
    if status in {408, 425, 500, 502, 503, 504}:
        return "temporary_remote"
    return "validation" if status in {400, 405, 406, 415, 422} else "permanent_remote"


def _policy_id_key(kind: str) -> str:
    return {
        "fulfillment_policy": "fulfillmentPolicyId",
        "payment_policy": "paymentPolicyId",
        "return_policy": "returnPolicyId",
    }[kind]


def _listing_url(listing_id: str | None) -> str | None:
    return f"https://www.ebay.com/itm/{quote(str(listing_id), safe='')}" if listing_id else None
