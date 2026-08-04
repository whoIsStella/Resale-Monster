"""Deterministic marketplace maintenance loop; it never invokes a language model."""

from __future__ import annotations

from typing import Any

from goliath.db.models import OfferStatus, RemoteListingStatus, ShippingTaskStatus


class MarketplaceAutomationWorker:
    def __init__(self, service) -> None:
        self._service = service

    async def run_once(self) -> dict[str, Any]:
        """Synchronize safe reads, then run bounded listing and shipping maintenance."""
        results: dict[str, Any] = {"accounts": {}, "pricing": 0, "labels": 0, "offers": 0, "errors": []}
        for account in self._service.list_accounts():
            try:
                results["accounts"][account.account_label] = await self._service.sync_account(
                    account.id, actor="system:marketplace-worker"
                )
            except Exception as error:  # noqa: BLE001 - isolate account failures
                results["errors"].append({"account": account.account_label, "error": str(error)})
        for listing in self._service.list_listings(status=RemoteListingStatus.ACTIVE, limit=200):
            outcome = await self._service.run_price_automation(
                listing.id, actor="system:marketplace-worker"
            )
            if outcome.get("decision") == "markdown" and outcome.get("ok"):
                results["pricing"] += 1
        for offer in self._service.list_offers(status=OfferStatus.PENDING, limit=200):
            outcome = await self._service.handle_offer(
                offer.id, actor="system:marketplace-worker"
            )
            if outcome.get("ok"):
                results["offers"] += 1
        if self._service._marketplace.shipping.automatic_label_purchase:
            for task in self._service.list_shipping_tasks(
                status=ShippingTaskStatus.READY, limit=200
            ):
                outcome = await self._service.purchase_label(
                    task.id, actor="system:marketplace-worker"
                )
                if outcome.get("ok"):
                    results["labels"] += 1
        self._service.run_reconciliation()
        return results
