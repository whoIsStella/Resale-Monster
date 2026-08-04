"""Deterministic handlers for persisted marketplace automation jobs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select

from goliath.db.marketplace_repositories import (
    CircuitBreakerRepository,
    EmergencyStopRepository,
    MarketplaceAccountRepository,
    MarketplaceOrderRepository,
    ReservationRepository,
)
from goliath.db.models import (
    CircuitBreakerState,
    InventoryReservation,
    MarketplaceOffer,
    MarketplaceOrder,
    OfferStatus,
    OrderStatus,
    RemoteListing,
    RemoteListingStatus,
    ShippingTask,
    ShippingTaskStatus,
    utc_now,
)
from goliath.db.repositories import AuditEventRepository


class MarketplaceAutomationWorker:
    """Executes one account-scoped workflow after the durable worker claims its job."""

    def __init__(self, service) -> None:
        self._service = service
        self._session_factory = service._session_factory

    async def execute_job(self, metadata: dict[str, Any], *, job_id: UUID) -> dict[str, Any]:
        account_id = UUID(str(metadata["marketplace_account_id"]))
        operation = str(metadata["operation_type"])
        write_capable = metadata.get("write_classification") == "write_capable"
        with self._session_factory() as session:
            account = MarketplaceAccountRepository(session).require(account_id)
            capability = metadata.get("requested_capability")
            if (
                capability
                and capability != "health_check"
                and capability not in account.capabilities
            ):
                return self._controlled(
                    session,
                    job_id,
                    account_id,
                    operation,
                    "skipped",
                    "unsupported_capability",
                )
            if write_capable:
                stops = EmergencyStopRepository(session)
                stop_keys = {str(account_id), operation, f"{account_id}:{operation}"}
                if any(stops.is_active(key) for key in stop_keys):
                    return self._controlled(
                        session, job_id, account_id, operation, "blocked", "emergency_stop"
                    )
                breakers = CircuitBreakerRepository(session).list()
                blocked = any(
                    breaker.state in {CircuitBreakerState.OPEN, CircuitBreakerState.HALF_OPEN}
                    and (
                        (breaker.scope == "account" and breaker.scope_key == str(account_id))
                        or (
                            breaker.scope == "operation"
                            and breaker.scope_key
                            in {
                                f"{account_id}:{operation}",
                                operation,
                            }
                        )
                    )
                    for breaker in breakers
                )
                if blocked:
                    return self._controlled(
                        session, job_id, account_id, operation, "blocked", "circuit_breaker_open"
                    )

        handler = getattr(self, f"_run_{operation}", None)
        if handler is None:
            return self._audit_result(
                job_id, account_id, operation, "skipped", reason="unsupported_workflow"
            )
        result = await handler(account_id)
        status = str(result.get("status", "succeeded"))
        return self._audit_result(
            job_id,
            account_id,
            operation,
            status,
            reason=result.get("reason"),
            result=result,
        )

    def _controlled(
        self,
        session,
        job_id: UUID,
        account_id: UUID,
        operation: str,
        status: str,
        reason: str,
    ) -> dict[str, Any]:
        payload = {"status": status, "operation": operation, "reason": reason, "executed": False}
        AuditEventRepository(session).append(
            event_type=f"marketplace.job.{status}",
            actor_type="worker",
            actor_id="marketplace-automation",
            resource_type="agent_job",
            resource_id=job_id,
            details={"account_id": str(account_id), "operation": operation, "reason": reason},
        )
        session.commit()
        return payload

    def _audit_result(
        self,
        job_id: UUID,
        account_id: UUID,
        operation: str,
        status: str,
        *,
        reason: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "status": status,
            "operation": operation,
            "reason": reason,
            "executed": status == "succeeded",
            **(result or {}),
        }
        with self._session_factory() as session:
            AuditEventRepository(session).append(
                event_type=f"marketplace.job.{status}",
                actor_type="worker",
                actor_id="marketplace-automation",
                resource_type="agent_job",
                resource_id=job_id,
                details={
                    "account_id": str(account_id),
                    "operation": operation,
                    "reason": reason,
                },
            )
            session.commit()
        return payload

    @staticmethod
    def _receipt(receipt) -> dict[str, Any]:
        status = (
            "succeeded"
            if receipt.ok
            else (
                "skipped"
                if receipt.error_category == "not_supported"
                else "blocked"
                if receipt.error_category == "blocked"
                else "failed"
            )
        )
        return {
            "status": status,
            "ok": receipt.ok,
            "executed": receipt.executed,
            "verified": receipt.verified,
            "error_category": receipt.error_category,
            "reason": "; ".join(receipt.reasons) if receipt.reasons else None,
        }

    def _account_listing_ids(self, account_id: UUID, *, active: bool = False) -> list[UUID]:
        with self._session_factory() as session:
            statement = select(RemoteListing.id).where(RemoteListing.account_id == account_id)
            if active:
                statement = statement.where(RemoteListing.status == RemoteListingStatus.ACTIVE)
            return list(session.scalars(statement).all())

    def _account_order_ids(
        self, account_id: UUID, statuses: set[OrderStatus] | None = None
    ) -> list[UUID]:
        with self._session_factory() as session:
            statement = select(MarketplaceOrder.id).where(MarketplaceOrder.account_id == account_id)
            if statuses:
                statement = statement.where(MarketplaceOrder.status.in_(statuses))
            return list(session.scalars(statement).all())

    async def _run_account_sync(self, account_id: UUID) -> dict[str, Any]:
        return self._receipt(
            await self._service.sync_account(
                account_id, principal_scopes={"admin"}, actor="system:marketplace-job"
            )
        )

    async def _run_listing_state_sync(self, account_id: UUID) -> dict[str, Any]:
        return self._receipt(
            await self._service.read_listings_remote(
                account_id, principal_scopes={"admin"}, actor="system:marketplace-job"
            )
        )

    async def _run_listing_reconciliation(self, account_id: UUID) -> dict[str, Any]:
        results = []
        for listing_id in self._account_listing_ids(account_id):
            results.append(
                self._receipt(
                    await self._service.read_listing(
                        listing_id,
                        principal_scopes={"admin"},
                        actor="system:marketplace-job",
                    )
                )
            )
        return {"status": "succeeded", "checked": len(results), "results": results}

    async def _run_order_sync(self, account_id: UUID) -> dict[str, Any]:
        result = await self._service.sync_orders(
            account_id, principal_scopes={"admin"}, actor="system:marketplace-job"
        )
        return {
            "status": (
                "succeeded"
                if result.get("ok")
                else "skipped"
                if result.get("error_category") == "not_supported"
                else "failed"
            ),
            "reason": result.get("reason"),
            **result,
        }

    async def _process_sales(self, account_id: UUID) -> dict[str, Any]:
        order_ids = self._account_order_ids(
            account_id, {OrderStatus.PAID, OrderStatus.READY_TO_SHIP}
        )
        processed = 0
        for order_id in order_ids:
            with self._session_factory() as session:
                order = MarketplaceOrderRepository(session).get(order_id)
                item_id = order.inventory_item_id if order else None
            await self._service._on_sale(order_id, item_id, "system:marketplace-job")
            processed += 1
        return {"status": "succeeded", "processed": processed}

    async def _run_paid_sale_detection(self, account_id: UUID) -> dict[str, Any]:
        return await self._process_sales(account_id)

    async def _run_inventory_reservation(self, account_id: UUID) -> dict[str, Any]:
        return await self._process_sales(account_id)

    async def _run_cross_marketplace_delisting(self, account_id: UUID) -> dict[str, Any]:
        return await self._process_sales(account_id)

    async def _run_offer_sync(self, account_id: UUID) -> dict[str, Any]:
        return await self._service.sync_offers(
            account_id, principal_scopes={"admin"}, actor="system:marketplace-job"
        )

    async def _run_offer_processing(self, account_id: UUID) -> dict[str, Any]:
        with self._session_factory() as session:
            offer_ids = list(
                session.scalars(
                    select(MarketplaceOffer.id).where(
                        MarketplaceOffer.account_id == account_id,
                        MarketplaceOffer.status == OfferStatus.PENDING,
                    )
                ).all()
            )
        results = [
            await self._service.handle_offer(
                offer_id, principal_scopes={"admin"}, actor="system:marketplace-job"
            )
            for offer_id in offer_ids
        ]
        return {"status": "succeeded", "processed": len(results), "results": results}

    async def _run_message_sync(self, account_id: UUID) -> dict[str, Any]:
        return self._receipt(
            await self._service.read_messages_remote(
                account_id, principal_scopes={"admin"}, actor="system:marketplace-job"
            )
        )

    async def _run_routine_message_response(self, account_id: UUID) -> dict[str, Any]:
        return await self._service.process_routine_messages(
            account_id, principal_scopes={"admin"}, actor="system:marketplace-job"
        )

    async def _run_price_automation(self, account_id: UUID) -> dict[str, Any]:
        ids = self._account_listing_ids(account_id, active=True)
        results = [
            await self._service.run_price_automation(
                item, principal_scopes={"admin"}, actor="system:marketplace-job"
            )
            for item in ids
        ]
        return {"status": "succeeded", "processed": len(results), "results": results}

    async def _run_listing_refresh(self, account_id: UUID) -> dict[str, Any]:
        ids = self._account_listing_ids(account_id, active=True)
        results = [
            self._receipt(
                await self._service.refresh_listing(
                    item, principal_scopes={"admin"}, actor="system:marketplace-job"
                )
            )
            for item in ids
        ]
        return {"status": "succeeded", "processed": len(results), "results": results}

    async def _run_listing_share_or_promotion(self, account_id: UUID) -> dict[str, Any]:
        account = self._service.get_account(account_id)
        operation = "share" if "share_listing" in account.capabilities else "promote"
        if f"{operation}_listing" not in account.capabilities:
            return {"status": "skipped", "reason": "unsupported_capability"}
        results = []
        for listing_id in self._account_listing_ids(account_id, active=True):
            method = getattr(self._service, f"{operation}_listing")
            results.append(
                self._receipt(
                    await method(
                        listing_id,
                        principal_scopes={"admin"},
                        actor="system:marketplace-job",
                    )
                )
            )
        return {"status": "succeeded", "processed": len(results), "results": results}

    async def _run_stale_inventory(self, account_id: UUID) -> dict[str, Any]:
        results = [
            await self._service.relist_listing(
                listing_id, principal_scopes={"admin"}, actor="system:marketplace-job"
            )
            for listing_id in self._account_listing_ids(account_id, active=True)
        ]
        return {"status": "succeeded", "processed": len(results), "results": results}

    async def _run_shipping_task_generation(self, account_id: UUID) -> dict[str, Any]:
        return await self._process_sales(account_id)

    async def _run_shipping_label_purchase(self, account_id: UUID) -> dict[str, Any]:
        with self._session_factory() as session:
            task_ids = list(
                session.scalars(
                    select(ShippingTask.id)
                    .join(MarketplaceOrder, MarketplaceOrder.id == ShippingTask.order_id)
                    .where(
                        MarketplaceOrder.account_id == account_id,
                        ShippingTask.status == ShippingTaskStatus.READY,
                    )
                ).all()
            )
        results = [
            await self._service.purchase_label(
                task_id, principal_scopes={"admin"}, actor="system:marketplace-job"
            )
            for task_id in task_ids
        ]
        return {"status": "succeeded", "processed": len(results), "results": results}

    async def _run_tracking_sync(self, account_id: UUID) -> dict[str, Any]:
        with self._session_factory() as session:
            rows = list(
                session.execute(
                    select(
                        MarketplaceOrder.id,
                        ShippingTask.tracking_number,
                        ShippingTask.carrier,
                    )
                    .join(ShippingTask, ShippingTask.order_id == MarketplaceOrder.id)
                    .where(
                        MarketplaceOrder.account_id == account_id,
                        ShippingTask.tracking_number.is_not(None),
                        ShippingTask.carrier.is_not(None),
                    )
                ).all()
            )
        results = [
            self._receipt(
                await self._service.update_tracking(
                    order_id,
                    tracking,
                    carrier,
                    principal_scopes={"admin"},
                    actor="system:marketplace-job",
                )
            )
            for order_id, tracking, carrier in rows
        ]
        return {"status": "succeeded", "processed": len(results), "results": results}

    async def _run_financial_reconciliation(self, account_id: UUID) -> dict[str, Any]:
        order_ids = self._account_order_ids(account_id)
        results = [self._service.reconcile_order(order_id) for order_id in order_ids]
        return {"status": "succeeded", "processed": len(results)}

    async def _run_expired_reservation_release(self, account_id: UUID) -> dict[str, Any]:
        now = utc_now()
        order_ids = {str(value) for value in self._account_order_ids(account_id)}
        with self._session_factory() as session:
            reservations = list(
                session.scalars(
                    select(InventoryReservation).where(
                        InventoryReservation.released_at.is_(None),
                        InventoryReservation.expires_at.is_not(None),
                        InventoryReservation.expires_at <= now,
                    )
                ).all()
            )
            released = 0
            for reservation in reservations:
                if reservation.source_order_id in order_ids:
                    ReservationRepository(session).release(
                        reservation.id, reason="scheduled expiration"
                    )
                    released += 1
            session.commit()
        return {"status": "succeeded", "released": released}

    async def _run_circuit_breaker_probe(self, account_id: UUID) -> dict[str, Any]:
        now = utc_now()
        with self._session_factory() as session:
            candidates = [
                breaker
                for breaker in CircuitBreakerRepository(session).list()
                if breaker.state in {CircuitBreakerState.OPEN, CircuitBreakerState.HALF_OPEN}
                and (
                    (breaker.scope == "account" and breaker.scope_key == str(account_id))
                    or (
                        breaker.scope == "operation"
                        and breaker.scope_key.startswith(f"{account_id}:")
                    )
                )
                and (breaker.next_probe_at is None or self._aware(breaker.next_probe_at) <= now)
            ]
            candidate_ids = [breaker.id for breaker in candidates]
            for breaker in candidates:
                breaker.state = CircuitBreakerState.HALF_OPEN
            session.commit()
        if not candidate_ids:
            return {"status": "skipped", "reason": "no_probe_due"}
        receipt = await self._service.health_check_remote(
            account_id, principal_scopes={"admin"}, actor="system:marketplace-probe"
        )
        rules = self._service._marketplace.circuit_breaker
        with self._session_factory() as session:
            repo = CircuitBreakerRepository(session)
            for breaker_id in candidate_ids:
                breaker = next((x for x in repo.list() if x.id == breaker_id), None)
                if breaker is None:
                    continue
                if receipt.ok:
                    repo.record_success(scope=breaker.scope, scope_key=breaker.scope_key)
                else:
                    repo.record_failure(
                        scope=breaker.scope,
                        scope_key=breaker.scope_key,
                        threshold=breaker.threshold,
                        cooldown_seconds=rules.cooldown_seconds,
                        reason=receipt.error_category or "probe_failed",
                    )
            session.commit()
        return {
            "status": "succeeded" if receipt.ok else "failed",
            "probed": len(candidate_ids),
            "reason": None if receipt.ok else receipt.error_category,
        }

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value

    async def run_once(self) -> dict[str, Any]:
        """Compatibility shim; production execution uses execute_job exclusively."""
        raise RuntimeError("marketplace maintenance must run as a persisted scheduled job")
