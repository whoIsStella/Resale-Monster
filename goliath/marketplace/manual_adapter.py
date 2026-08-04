"""Manual export/import adapter.

Produces export payloads a human pastes into a marketplace and ingests results a
human pastes back. No network, no browser, no credentials — the safe fallback for
marketplaces without an automated adapter.
"""

from __future__ import annotations

import hashlib

from goliath.marketplace.adapter import BaseMarketplaceAdapter, ListingRequest, OperationResult


class ManualMarketplaceAdapter(BaseMarketplaceAdapter):
    marketplace = "manual"

    def capabilities(self) -> set[str]:
        # Manual adapter can "prepare" a listing export and read back a confirmation.
        return {"create_listing", "read_listing", "end_listing", "sync_account"}

    async def authentication_status(self) -> OperationResult:
        return OperationResult(
            ok=True, operation="authentication_status", data={"authenticated": True}
        )

    async def create_listing(self, request: ListingRequest) -> OperationResult:
        payload = {
            "title": request.title,
            "description": request.description,
            "price": str(request.price),
            "currency": request.currency,
            "category": request.category,
        }
        digest = hashlib.sha256(str(payload).encode()).hexdigest()[:16]
        remote_id = f"manual-{digest}"
        return OperationResult(
            ok=True,
            operation="create_listing",
            data={"export": payload, "instructions": "paste into the marketplace listing form"},
            remote_id=remote_id,
            remote_url=None,
        )

    async def read_listing(self, remote_listing_id: str) -> OperationResult:
        return OperationResult(
            ok=True,
            operation="read_listing",
            data={"status": "active", "manual": True},
            remote_id=remote_listing_id,
        )

    async def end_listing(self, remote_listing_id: str) -> OperationResult:
        return OperationResult(
            ok=True,
            operation="end_listing",
            data={"status": "ended", "instructions": "end the listing manually"},
            remote_id=remote_listing_id,
        )

    async def sync_account(self) -> OperationResult:
        return OperationResult(ok=True, operation="sync_account", data={"manual": True})
