"""Bounded repositories for milestone-six marketplace entities.

Expose only domain-specific methods. Never expose raw sessions, cookies, browser
contexts, arbitrary SQL, HTTP clients, credentials, or unrestricted filters.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from goliath.db.models import (
    AutomationMode,
    AutomationPolicyVersion,
    CircuitBreaker,
    CircuitBreakerState,
    EmergencyStopState,
    ExceptionStatus,
    ExceptionTask,
    InventoryReservation,
    Marketplace,
    MarketplaceAccount,
    MarketplaceAccountStatus,
    MarketplaceOffer,
    MarketplaceOperationAttempt,
    MarketplaceOrder,
    MessageRecord,
    MessageThread,
    OAuthStateRecord,
    OfferDecision,
    OfferStatus,
    OperationStatus,
    OrderStatus,
    PolicyDecision,
    ReconciliationRecord,
    ReconciliationStatus,
    RemoteListing,
    RemoteListingStatus,
    ReservationReason,
    SessionReference,
    ShippingTask,
    SynchronizationConflict,
    VerificationStatus,
    WebhookEvent,
    utc_now,
)
from goliath.db.repositories import (
    InvalidStateTransitionError,
    RecordNotFoundError,
    VersionConflictError,
)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class MarketplaceAccountRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        marketplace: Marketplace,
        account_label: str,
        currency: str = "USD",
        capabilities: list[str] | None = None,
        seller_region: str | None = None,
    ) -> MarketplaceAccount:
        account = MarketplaceAccount(
            marketplace=marketplace,
            account_label=account_label,
            currency=currency.upper(),
            capabilities=capabilities or [],
            seller_region=seller_region,
            automation_mode=AutomationMode.DISABLED,
            status=MarketplaceAccountStatus.DISCONNECTED,
        )
        self._session.add(account)
        self._session.flush()
        return account

    def get(self, account_id: UUID) -> MarketplaceAccount | None:
        return self._session.get(MarketplaceAccount, account_id)

    def require(self, account_id: UUID) -> MarketplaceAccount:
        account = self.get(account_id)
        if account is None:
            raise RecordNotFoundError(f"marketplace account not found: {account_id}")
        return account

    def list(self) -> Sequence[MarketplaceAccount]:
        return self._session.scalars(
            select(MarketplaceAccount).order_by(MarketplaceAccount.created_at)
        ).all()

    def set_mode(
        self, account_id: UUID, *, mode: AutomationMode, expected_version: int
    ) -> MarketplaceAccount:
        account = self.require(account_id)
        result = self._session.execute(
            update(MarketplaceAccount)
            .where(
                MarketplaceAccount.id == account_id,
                MarketplaceAccount.version == expected_version,
            )
            .values(
                automation_mode=mode,
                mode_version=account.mode_version + 1,
                version=expected_version + 1,
                updated_at=utc_now(),
            )
        )
        if result.rowcount != 1:
            self._session.expire_all()
            raise VersionConflictError("stale marketplace account version")
        self._session.expire(account)
        return account

    def set_status(
        self, account_id: UUID, *, status: MarketplaceAccountStatus
    ) -> MarketplaceAccount:
        account = self.require(account_id)
        account.status = status
        account.version += 1
        account.updated_at = utc_now()
        self._session.flush()
        return account

    def set_session(
        self, account_id: UUID, *, reference: str | None, expires_at: datetime | None
    ) -> MarketplaceAccount:
        account = self.require(account_id)
        account.session_reference = reference
        account.session_expires_at = expires_at
        account.version += 1
        self._session.flush()
        return account

    def record_sync(self, account_id: UUID, *, success: bool) -> MarketplaceAccount:
        account = self.require(account_id)
        now = utc_now()
        if success:
            account.last_sync_at = now
            account.consecutive_failures = 0
            if account.status is MarketplaceAccountStatus.DEGRADED:
                account.status = MarketplaceAccountStatus.HEALTHY
        else:
            account.last_failed_sync_at = now
            account.consecutive_failures += 1
        account.version += 1
        self._session.flush()
        return account


class SessionReferenceRepository:
    """Stores only encrypted ciphertext. Never returns raw cookies to callers."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def store(
        self,
        *,
        account_id: UUID,
        reference_key: str,
        ciphertext: str,
        key_id: str,
        storage_dir: str,
        allowed_domains: list[str],
        expires_at: datetime | None,
    ) -> SessionReference:
        reference = SessionReference(
            account_id=account_id,
            reference_key=reference_key,
            ciphertext=ciphertext,
            key_id=key_id,
            storage_dir=storage_dir,
            allowed_domains=allowed_domains,
            expires_at=expires_at,
        )
        self._session.add(reference)
        self._session.flush()
        return reference

    def get_by_key(self, reference_key: str) -> SessionReference | None:
        return self._session.scalar(
            select(SessionReference).where(
                SessionReference.reference_key == reference_key,
                SessionReference.revoked_at.is_(None),
            )
        )

    def get_active(
        self, account_id: UUID, *, now: datetime | None = None
    ) -> SessionReference | None:
        now = now or utc_now()
        reference = self._session.scalar(
            select(SessionReference)
            .where(
                SessionReference.account_id == account_id,
                SessionReference.revoked_at.is_(None),
            )
            .order_by(SessionReference.created_at.desc())
        )
        if reference is None:
            return None
        if reference.expires_at is not None and _utc(reference.expires_at) <= now:
            return None
        return reference

    def revoke(self, reference_key: str) -> None:
        reference = self.get_by_key(reference_key)
        if reference is None:
            raise RecordNotFoundError(f"session reference not found: {reference_key}")
        reference.revoked_at = utc_now()
        self._session.flush()

    def record_auth_failure(self, reference_key: str) -> None:
        reference = self.get_by_key(reference_key)
        if reference is not None:
            reference.last_auth_failure_at = utc_now()
            self._session.flush()


class OAuthStateRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        account_id: UUID,
        state: str,
        redirect_uri: str,
        requested_scopes: list[str],
        expires_at: datetime,
    ) -> OAuthStateRecord:
        record = OAuthStateRecord(
            account_id=account_id,
            state_digest=hashlib.sha256(state.encode()).hexdigest(),
            redirect_uri=redirect_uri,
            requested_scopes=requested_scopes,
            expires_at=expires_at,
        )
        self._session.add(record)
        self._session.flush()
        return record

    def consume(
        self, *, state: str, redirect_uri: str, now: datetime | None = None
    ) -> OAuthStateRecord:
        now = now or utc_now()
        digest = hashlib.sha256(state.encode()).hexdigest()
        record = self._session.scalar(
            select(OAuthStateRecord).where(OAuthStateRecord.state_digest == digest)
        )
        if record is None or record.consumed_at is not None:
            raise RecordNotFoundError("invalid or already-used OAuth state")
        if _utc(record.expires_at) <= now:
            raise InvalidStateTransitionError("OAuth state expired")
        if record.redirect_uri != redirect_uri:
            raise InvalidStateTransitionError("OAuth redirect URI mismatch")
        record.consumed_at = now
        self._session.flush()
        return record


class RemoteListingRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        inventory_item_id: UUID,
        account_id: UUID,
        idempotency_key: str | None = None,
        status: RemoteListingStatus = RemoteListingStatus.PENDING_PUBLISH,
        current_price: Decimal | None = None,
        automation_mode_used: str | None = None,
    ) -> RemoteListing:
        listing = RemoteListing(
            inventory_item_id=inventory_item_id,
            account_id=account_id,
            idempotency_key=idempotency_key,
            status=status,
            current_price=current_price,
            automation_mode_used=automation_mode_used,
        )
        self._session.add(listing)
        self._session.flush()
        return listing

    def get(self, listing_id: UUID) -> RemoteListing | None:
        return self._session.get(RemoteListing, listing_id)

    def get_by_idempotency(self, idempotency_key: str) -> RemoteListing | None:
        return self._session.scalar(
            select(RemoteListing).where(RemoteListing.idempotency_key == idempotency_key)
        )

    def get_by_remote_identity(
        self, *, account_id: UUID, remote_listing_id: str
    ) -> RemoteListing | None:
        return self._session.scalar(
            select(RemoteListing).where(
                RemoteListing.account_id == account_id,
                RemoteListing.remote_listing_id == remote_listing_id,
            )
        )

    def find_active_on_account(
        self, inventory_item_id: UUID, account_id: UUID
    ) -> RemoteListing | None:
        return self._session.scalar(
            select(RemoteListing).where(
                RemoteListing.inventory_item_id == inventory_item_id,
                RemoteListing.account_id == account_id,
                RemoteListing.status.in_(
                    [
                        RemoteListingStatus.PENDING_PUBLISH,
                        RemoteListingStatus.PUBLISHING,
                        RemoteListingStatus.ACTIVE,
                        RemoteListingStatus.RESERVED,
                    ]
                ),
            )
        )

    def list_for_item(self, inventory_item_id: UUID) -> Sequence[RemoteListing]:
        return self._session.scalars(
            select(RemoteListing)
            .where(RemoteListing.inventory_item_id == inventory_item_id)
            .order_by(RemoteListing.created_at)
        ).all()

    def list_active_for_item(self, inventory_item_id: UUID) -> Sequence[RemoteListing]:
        return self._session.scalars(
            select(RemoteListing).where(
                RemoteListing.inventory_item_id == inventory_item_id,
                RemoteListing.status.in_(
                    [RemoteListingStatus.ACTIVE, RemoteListingStatus.RESERVED]
                ),
            )
        ).all()

    def list(self, *, status: RemoteListingStatus | None = None, limit: int = 100, offset: int = 0):
        statement = select(RemoteListing)
        if status is not None:
            statement = statement.where(RemoteListing.status == status)
        return self._session.scalars(
            statement.order_by(RemoteListing.created_at).limit(limit).offset(offset)
        ).all()

    def apply_publish_result(
        self,
        listing_id: UUID,
        *,
        expected_version: int,
        remote_listing_id: str,
        remote_url: str | None,
        status: RemoteListingStatus,
        verified: bool,
    ) -> RemoteListing:
        listing = self.get(listing_id)
        if listing is None:
            raise RecordNotFoundError(f"remote listing not found: {listing_id}")
        result = self._session.execute(
            update(RemoteListing)
            .where(
                RemoteListing.id == listing_id,
                RemoteListing.local_version == expected_version,
            )
            .values(
                remote_listing_id=remote_listing_id,
                remote_url=remote_url,
                status=status,
                published_at=utc_now(),
                last_synced_at=utc_now(),
                last_verified_action="publish" if verified else None,
                local_version=expected_version + 1,
            )
        )
        if result.rowcount != 1:
            self._session.expire_all()
            raise VersionConflictError("stale remote listing version")
        self._session.expire(listing)
        return listing

    def set_status(
        self,
        listing_id: UUID,
        *,
        status: RemoteListingStatus,
        verified_action: str | None = None,
        error_category: str | None = None,
        price: Decimal | None = None,
    ) -> RemoteListing:
        listing = self.get(listing_id)
        if listing is None:
            raise RecordNotFoundError(f"remote listing not found: {listing_id}")
        listing.status = status
        listing.local_version += 1
        if verified_action is not None:
            listing.last_verified_action = verified_action
            listing.last_synced_at = utc_now()
        # Refreshes and price updates both reset the engagement/markdown clock.
        if status is RemoteListingStatus.ACTIVE and verified_action in {"refresh", "update"}:
            listing.refreshed_at = utc_now()
        if error_category is not None:
            listing.last_error_category = error_category
        if price is not None:
            listing.current_price = price
        self._session.flush()
        return listing


class OperationAttemptRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_idempotency(self, idempotency_key: str) -> MarketplaceOperationAttempt | None:
        return self._session.scalar(
            select(MarketplaceOperationAttempt).where(
                MarketplaceOperationAttempt.idempotency_key == idempotency_key
            )
        )

    def get(self, operation_id: UUID) -> MarketplaceOperationAttempt | None:
        return self._session.get(MarketplaceOperationAttempt, operation_id)

    def count_since(
        self,
        *,
        since: datetime,
        account_id: UUID | None = None,
        operation: str | None = None,
    ) -> int:
        statement = select(func.count(MarketplaceOperationAttempt.id)).where(
            MarketplaceOperationAttempt.created_at >= since
        )
        if account_id is not None:
            statement = statement.where(MarketplaceOperationAttempt.account_id == account_id)
        if operation is not None:
            statement = statement.where(MarketplaceOperationAttempt.operation == operation)
        return int(self._session.scalar(statement) or 0)

    def start(
        self,
        *,
        account_id: UUID,
        operation: str,
        idempotency_key: str,
        automation_mode: str,
        agent_identity: str | None = None,
        resource_type: str | None = None,
        resource_id: UUID | None = None,
        request_summary: dict[str, Any] | None = None,
        job_id: UUID | None = None,
        requested_payload_hash: str | None = None,
    ) -> MarketplaceOperationAttempt:
        attempt = MarketplaceOperationAttempt(
            account_id=account_id,
            operation=operation,
            idempotency_key=idempotency_key,
            automation_mode=automation_mode,
            agent_identity=agent_identity,
            resource_type=resource_type,
            resource_id=resource_id,
            request_summary=request_summary or {},
            job_id=job_id,
            requested_payload_hash=requested_payload_hash,
            status=OperationStatus.RUNNING,
        )
        self._session.add(attempt)
        self._session.flush()
        return attempt

    def complete(
        self,
        attempt_id: UUID,
        *,
        status: OperationStatus,
        result_summary: dict[str, Any] | None = None,
        remote_identifier: str | None = None,
        verified: bool = False,
        error_category: str | None = None,
        remote_request_id: str | None = None,
        verification_status: VerificationStatus | None = None,
    ) -> MarketplaceOperationAttempt:
        attempt = self._session.get(MarketplaceOperationAttempt, attempt_id)
        if attempt is None:
            raise RecordNotFoundError(f"operation attempt not found: {attempt_id}")
        attempt.status = status
        attempt.result_summary = result_summary or {}
        attempt.remote_identifier = remote_identifier
        attempt.verified = verified
        attempt.error_category = error_category
        attempt.remote_request_id = remote_request_id
        attempt.verification_status = verification_status or (
            VerificationStatus.VERIFIED if verified else VerificationStatus.UNAVAILABLE
        )
        attempt.completed_at = utc_now()
        self._session.flush()
        return attempt


class MarketplaceOrderRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(
        self,
        *,
        account_id: UUID,
        remote_order_id: str,
        sale_price: Decimal,
        status: OrderStatus,
        payload_checksum: str,
        **fields: Any,
    ) -> tuple[MarketplaceOrder, bool]:
        existing = self._session.scalar(
            select(MarketplaceOrder).where(
                MarketplaceOrder.account_id == account_id,
                MarketplaceOrder.remote_order_id == remote_order_id,
            )
        )
        if existing is not None:
            if existing.payload_checksum == payload_checksum:
                return existing, False
            existing.status = status
            existing.sale_price = sale_price
            existing.payload_checksum = payload_checksum
            existing.last_synced_at = utc_now()
            existing.version += 1
            for key, value in fields.items():
                setattr(existing, key, value)
            self._session.flush()
            return existing, False
        order = MarketplaceOrder(
            account_id=account_id,
            remote_order_id=remote_order_id,
            sale_price=sale_price,
            status=status,
            payload_checksum=payload_checksum,
            last_synced_at=utc_now(),
            **fields,
        )
        self._session.add(order)
        self._session.flush()
        return order, True

    def get(self, order_id: UUID) -> MarketplaceOrder | None:
        return self._session.get(MarketplaceOrder, order_id)

    def list(self, *, status: OrderStatus | None = None, limit: int = 100, offset: int = 0):
        statement = select(MarketplaceOrder)
        if status is not None:
            statement = statement.where(MarketplaceOrder.status == status)
        return self._session.scalars(
            statement.order_by(MarketplaceOrder.created_at).limit(limit).offset(offset)
        ).all()

    def set_status(self, order_id: UUID, *, status: OrderStatus) -> MarketplaceOrder:
        order = self.get(order_id)
        if order is None:
            raise RecordNotFoundError(f"order not found: {order_id}")
        order.status = status
        order.version += 1
        self._session.flush()
        return order


class MarketplaceOfferRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(
        self,
        *,
        account_id: UUID,
        remote_offer_id: str,
        offer_amount: Decimal,
        list_price: Decimal | None = None,
        inventory_item_id: UUID | None = None,
        prior_offer_count: int = 0,
    ) -> MarketplaceOffer:
        existing = self._session.scalar(
            select(MarketplaceOffer).where(
                MarketplaceOffer.account_id == account_id,
                MarketplaceOffer.remote_offer_id == remote_offer_id,
            )
        )
        if existing is not None:
            return existing
        offer = MarketplaceOffer(
            account_id=account_id,
            remote_offer_id=remote_offer_id,
            offer_amount=offer_amount,
            list_price=list_price,
            inventory_item_id=inventory_item_id,
            prior_offer_count=prior_offer_count,
        )
        self._session.add(offer)
        self._session.flush()
        return offer

    def get(self, offer_id: UUID) -> MarketplaceOffer | None:
        return self._session.get(MarketplaceOffer, offer_id)

    def list(self, *, status: OfferStatus | None = None, limit: int = 100, offset: int = 0):
        statement = select(MarketplaceOffer)
        if status is not None:
            statement = statement.where(MarketplaceOffer.status == status)
        return self._session.scalars(
            statement.order_by(MarketplaceOffer.created_at).limit(limit).offset(offset)
        ).all()

    def record_decision(
        self,
        offer_id: UUID,
        *,
        decision: OfferDecision,
        status: OfferStatus,
        counter_amount: Decimal | None = None,
        requested_action: str | None = None,
        executed_action: str | None = None,
        responded: bool = False,
    ) -> MarketplaceOffer:
        offer = self.get(offer_id)
        if offer is None:
            raise RecordNotFoundError(f"offer not found: {offer_id}")
        offer.decision = decision
        offer.status = status
        if requested_action is not None:
            offer.requested_action = requested_action
        if executed_action is not None:
            offer.executed_action = executed_action
        offer.counter_amount = counter_amount
        offer.evaluated_at = utc_now()
        if responded:
            offer.responded_at = utc_now()
        offer.version += 1
        self._session.flush()
        return offer


class MessageRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_thread(
        self,
        *,
        account_id: UUID,
        remote_thread_id: str,
        inventory_item_id: UUID | None = None,
        buyer_reference: str | None = None,
    ) -> MessageThread:
        existing = self._session.scalar(
            select(MessageThread).where(
                MessageThread.account_id == account_id,
                MessageThread.remote_thread_id == remote_thread_id,
            )
        )
        if existing is not None:
            return existing
        thread = MessageThread(
            account_id=account_id,
            remote_thread_id=remote_thread_id,
            inventory_item_id=inventory_item_id,
            buyer_reference=buyer_reference,
        )
        self._session.add(thread)
        self._session.flush()
        return thread

    def get_thread(self, thread_id: UUID) -> MessageThread | None:
        return self._session.get(MessageThread, thread_id)

    def has_message_checksum(self, thread_id: UUID, checksum: str) -> bool:
        return (
            self._session.scalar(
                select(MessageRecord.id)
                .where(
                    MessageRecord.thread_id == thread_id,
                    MessageRecord.body_checksum == checksum,
                )
                .limit(1)
            )
            is not None
        )

    def add_message(
        self,
        thread_id: UUID,
        *,
        direction: str,
        category: str | None,
        body_checksum: str,
        delivered: bool = False,
        escalated: bool = False,
    ) -> MessageRecord:
        thread = self.get_thread(thread_id)
        if thread is None:
            raise RecordNotFoundError(f"thread not found: {thread_id}")
        record = MessageRecord(
            thread_id=thread_id,
            direction=direction,
            category=category,
            body_checksum=body_checksum,
            delivered=delivered,
            escalated=escalated,
        )
        self._session.add(record)
        thread.last_category = category
        thread.last_message_at = utc_now()
        if escalated:
            thread.escalated = True
        self._session.flush()
        return record

    def list_threads(self, *, limit: int = 100, offset: int = 0) -> Sequence[MessageThread]:
        return self._session.scalars(
            select(MessageThread).order_by(MessageThread.created_at).limit(limit).offset(offset)
        ).all()


class ReservationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def active_for_item(self, inventory_item_id: UUID) -> Sequence[InventoryReservation]:
        return self._session.scalars(
            select(InventoryReservation).where(
                InventoryReservation.inventory_item_id == inventory_item_id,
                InventoryReservation.released_at.is_(None),
            )
        ).all()

    def reserve(
        self,
        *,
        inventory_item_id: UUID,
        reason: ReservationReason,
        created_by: str,
        source_marketplace: str | None = None,
        source_listing_id: str | None = None,
        source_order_id: str | None = None,
        expires_at: datetime | None = None,
    ) -> InventoryReservation:
        reservation = InventoryReservation(
            inventory_item_id=inventory_item_id,
            reason=reason,
            created_by=created_by,
            source_marketplace=source_marketplace,
            source_listing_id=source_listing_id,
            source_order_id=source_order_id,
            expires_at=expires_at,
        )
        self._session.add(reservation)
        self._session.flush()
        return reservation

    def release(self, reservation_id: UUID, *, reason: str) -> InventoryReservation:
        reservation = self._session.get(InventoryReservation, reservation_id)
        if reservation is None:
            raise RecordNotFoundError(f"reservation not found: {reservation_id}")
        if reservation.released_at is not None:
            raise InvalidStateTransitionError("reservation already released")
        reservation.released_at = utc_now()
        reservation.release_reason = reason
        reservation.version += 1
        self._session.flush()
        return reservation


class ShippingTaskRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create_for_order(
        self,
        *,
        order_id: UUID,
        inventory_item_id: UUID | None,
        package_profile: str,
        ship_by_at: datetime | None = None,
        estimated_weight_grams: Decimal | None = None,
        storage_location: str | None = None,
    ) -> ShippingTask:
        existing = self._session.scalar(
            select(ShippingTask).where(ShippingTask.order_id == order_id)
        )
        if existing is not None:
            return existing
        task = ShippingTask(
            order_id=order_id,
            inventory_item_id=inventory_item_id,
            package_profile=package_profile,
            ship_by_at=ship_by_at,
            estimated_weight_grams=estimated_weight_grams,
            storage_location=storage_location,
        )
        self._session.add(task)
        self._session.flush()
        return task

    def get(self, task_id: UUID) -> ShippingTask | None:
        return self._session.get(ShippingTask, task_id)

    def list(self, *, status: Any = None, limit: int = 100, offset: int = 0):
        statement = select(ShippingTask)
        if status is not None:
            statement = statement.where(ShippingTask.status == status)
        return self._session.scalars(
            statement.order_by(ShippingTask.created_at).limit(limit).offset(offset)
        ).all()

    def update(self, task_id: UUID, *, expected_version: int, **fields: Any) -> ShippingTask:
        task = self.get(task_id)
        if task is None:
            raise RecordNotFoundError(f"shipping task not found: {task_id}")
        if task.version != expected_version:
            raise VersionConflictError("stale shipping task version")
        for key, value in fields.items():
            setattr(task, key, value)
        task.version += 1
        self._session.flush()
        return task


class ReconciliationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, *, order_id: UUID, status: ReconciliationStatus, **fields: Any):
        record = ReconciliationRecord(order_id=order_id, status=status, **fields)
        self._session.add(record)
        self._session.flush()
        return record

    def list_for_order(self, order_id: UUID) -> Sequence[ReconciliationRecord]:
        return self._session.scalars(
            select(ReconciliationRecord)
            .where(ReconciliationRecord.order_id == order_id)
            .order_by(ReconciliationRecord.created_at)
        ).all()

    def list_discrepancies(self) -> Sequence[ReconciliationRecord]:
        return self._session.scalars(
            select(ReconciliationRecord).where(
                ReconciliationRecord.status == ReconciliationStatus.DISCREPANCY
            )
        ).all()


class PolicyDecisionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        *,
        policy_type: str,
        policy_version: str,
        decision: str,
        inputs: dict[str, Any],
        reasons: list[str],
        risk_score: Decimal = Decimal(0),
        warnings: list[str] | None = None,
        confidence_requirements: dict[str, Any] | None = None,
        resource_type: str | None = None,
        resource_id: UUID | None = None,
        expires_at: datetime | None = None,
    ) -> PolicyDecision:
        record = PolicyDecision(
            policy_type=policy_type,
            policy_version=policy_version,
            decision=decision,
            inputs=inputs,
            reasons=reasons,
            risk_score=risk_score,
            warnings=warnings or [],
            confidence_requirements=confidence_requirements or {},
            resource_type=resource_type,
            resource_id=resource_id,
            expires_at=expires_at,
        )
        self._session.add(record)
        self._session.flush()
        return record

    def record_execution(self, decision_id: UUID, result: str) -> PolicyDecision:
        record = self._session.get(PolicyDecision, decision_id)
        if record is None:
            raise RecordNotFoundError(f"policy decision not found: {decision_id}")
        record.execution_result = result
        self._session.flush()
        return record


class PolicyVersionRepository:
    """Versioned deterministic policy settings; activation is explicit and bounded."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self, *, policy_type: str, version: str, settings: dict[str, Any]
    ) -> AutomationPolicyVersion:
        record = AutomationPolicyVersion(
            policy_type=policy_type, version=version, settings=settings, is_active=False
        )
        self._session.add(record)
        self._session.flush()
        return record

    def activate(self, policy_id: UUID) -> AutomationPolicyVersion:
        record = self._session.get(AutomationPolicyVersion, policy_id)
        if record is None:
            raise RecordNotFoundError(f"policy version not found: {policy_id}")
        active = self._session.scalars(
            select(AutomationPolicyVersion).where(
                AutomationPolicyVersion.policy_type == record.policy_type,
                AutomationPolicyVersion.is_active.is_(True),
            )
        ).all()
        for current in active:
            current.is_active = False
        record.is_active = True
        self._session.flush()
        return record

    def get_active(self, policy_type: str) -> AutomationPolicyVersion | None:
        return self._session.scalar(
            select(AutomationPolicyVersion)
            .where(
                AutomationPolicyVersion.policy_type == policy_type,
                AutomationPolicyVersion.is_active.is_(True),
            )
            .order_by(AutomationPolicyVersion.created_at.desc())
        )


class CircuitBreakerRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_or_create(self, *, scope: str, scope_key: str, threshold: int) -> CircuitBreaker:
        breaker = self._session.scalar(
            select(CircuitBreaker).where(
                CircuitBreaker.scope == scope, CircuitBreaker.scope_key == scope_key
            )
        )
        if breaker is None:
            breaker = CircuitBreaker(scope=scope, scope_key=scope_key, threshold=threshold)
            self._session.add(breaker)
            self._session.flush()
        return breaker

    def is_open(self, *, scope: str, scope_key: str, now: datetime | None = None) -> bool:
        now = now or utc_now()
        breaker = self._session.scalar(
            select(CircuitBreaker).where(
                CircuitBreaker.scope == scope, CircuitBreaker.scope_key == scope_key
            )
        )
        if breaker is None or breaker.state is CircuitBreakerState.CLOSED:
            return False
        if breaker.state is CircuitBreakerState.OPEN:
            if breaker.next_probe_at is not None and _utc(breaker.next_probe_at) <= now:
                breaker.state = CircuitBreakerState.HALF_OPEN
                self._session.flush()
                return False
            return True
        return False

    def record_failure(
        self, *, scope: str, scope_key: str, threshold: int, cooldown_seconds: int, reason: str
    ) -> CircuitBreaker:
        breaker = self.get_or_create(scope=scope, scope_key=scope_key, threshold=threshold)
        breaker.failure_count += 1
        breaker.reason = reason
        if breaker.failure_count >= breaker.threshold and breaker.state is not (
            CircuitBreakerState.OPEN
        ):
            breaker.state = CircuitBreakerState.OPEN
            breaker.opened_at = utc_now()
            breaker.next_probe_at = utc_now() + timedelta(seconds=cooldown_seconds)
        breaker.version += 1
        self._session.flush()
        return breaker

    def record_success(self, *, scope: str, scope_key: str) -> None:
        breaker = self._session.scalar(
            select(CircuitBreaker).where(
                CircuitBreaker.scope == scope, CircuitBreaker.scope_key == scope_key
            )
        )
        if breaker is not None and breaker.state is CircuitBreakerState.HALF_OPEN:
            breaker.state = CircuitBreakerState.CLOSED
            breaker.failure_count = 0
            breaker.reset_at = utc_now()
            breaker.version += 1
            self._session.flush()

    def reset(self, breaker_id: UUID) -> CircuitBreaker:
        breaker = self._session.get(CircuitBreaker, breaker_id)
        if breaker is None:
            raise RecordNotFoundError(f"circuit breaker not found: {breaker_id}")
        breaker.state = CircuitBreakerState.CLOSED
        breaker.failure_count = 0
        breaker.reset_at = utc_now()
        breaker.version += 1
        self._session.flush()
        return breaker

    def list(self) -> Sequence[CircuitBreaker]:
        return self._session.scalars(
            select(CircuitBreaker).order_by(CircuitBreaker.scope, CircuitBreaker.scope_key)
        ).all()


class EmergencyStopRepository:
    GLOBAL = "*"

    def __init__(self, session: Session) -> None:
        self._session = session

    def _get(self, scope_key: str) -> EmergencyStopState | None:
        return self._session.scalar(
            select(EmergencyStopState).where(EmergencyStopState.scope_key == scope_key)
        )

    def is_active(self, scope_key: str = GLOBAL) -> bool:
        for key in {scope_key, self.GLOBAL}:
            state = self._get(key)
            if state is not None and state.active:
                return True
        return False

    def activate(self, *, scope_key: str, reason: str, actor: str) -> EmergencyStopState:
        state = self._get(scope_key)
        if state is None:
            state = EmergencyStopState(scope_key=scope_key, version=1)
            self._session.add(state)
            self._session.flush()
        state.active = True
        state.reason = reason
        state.activated_by = actor
        state.activated_at = utc_now()
        state.released_at = None
        state.version = (state.version or 0) + 1
        self._session.flush()
        return state

    def release(self, *, scope_key: str, reason: str) -> EmergencyStopState:
        state = self._get(scope_key)
        if state is None or not state.active:
            raise InvalidStateTransitionError("emergency stop is not active")
        state.active = False
        state.reason = reason
        state.released_at = utc_now()
        state.version = (state.version or 0) + 1
        self._session.flush()
        return state

    def list_active(self) -> Sequence[EmergencyStopState]:
        return self._session.scalars(
            select(EmergencyStopState).where(EmergencyStopState.active.is_(True))
        ).all()


class SyncConflictRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        conflict_type: str,
        resource_type: str,
        detail: dict[str, Any],
        resource_id: UUID | None = None,
        account_id: UUID | None = None,
        resolution_policy: str | None = None,
    ) -> SynchronizationConflict:
        conflict = SynchronizationConflict(
            conflict_type=conflict_type,
            resource_type=resource_type,
            detail=detail,
            resource_id=resource_id,
            account_id=account_id,
            resolution_policy=resolution_policy,
        )
        self._session.add(conflict)
        self._session.flush()
        return conflict


class WebhookEventRepository:
    """Checksum-only webhook inbox; complete remote payloads are never persisted."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def ingest(
        self, *, source: str, external_id: str, event_type: str, payload_checksum: str
    ) -> tuple[WebhookEvent, bool]:
        existing = self._session.scalar(
            select(WebhookEvent).where(
                WebhookEvent.source == source, WebhookEvent.external_id == external_id
            )
        )
        if existing is not None:
            if existing.payload_checksum != payload_checksum:
                raise VersionConflictError("webhook identity reused with a different checksum")
            return existing, False
        event = WebhookEvent(
            source=source,
            external_id=external_id,
            event_type=event_type,
            payload_checksum=payload_checksum,
        )
        self._session.add(event)
        self._session.flush()
        return event, True

    def mark_processed(self, event_id: UUID) -> WebhookEvent:
        event = self._session.get(WebhookEvent, event_id)
        if event is None:
            raise RecordNotFoundError(f"webhook event not found: {event_id}")
        event.processed = True
        self._session.flush()
        return event

    def list_pending(self, *, limit: int = 100) -> Sequence[WebhookEvent]:
        if limit < 1 or limit > 1_000:
            raise ValueError("webhook limit must be between 1 and 1000")
        return self._session.scalars(
            select(WebhookEvent)
            .where(WebhookEvent.processed.is_(False))
            .order_by(WebhookEvent.created_at)
            .limit(limit)
        ).all()

    def list_open(self) -> Sequence[SynchronizationConflict]:
        return self._session.scalars(
            select(SynchronizationConflict)
            .where(SynchronizationConflict.resolved.is_(False))
            .order_by(SynchronizationConflict.created_at)
        ).all()

    def resolve(self, conflict_id: UUID) -> SynchronizationConflict:
        conflict = self._session.get(SynchronizationConflict, conflict_id)
        if conflict is None:
            raise RecordNotFoundError(f"conflict not found: {conflict_id}")
        conflict.resolved = True
        conflict.resolved_at = utc_now()
        self._session.flush()
        return conflict


class ExceptionTaskRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        exception_type: str,
        reason: str,
        severity: str = "normal",
        resource_type: str | None = None,
        resource_id: UUID | None = None,
        account_id: UUID | None = None,
        detail: dict[str, Any] | None = None,
    ) -> ExceptionTask:
        # Idempotent per (type, resource, open status): avoid duplicate open exceptions.
        if resource_id is not None:
            existing = self._session.scalar(
                select(ExceptionTask).where(
                    ExceptionTask.exception_type == exception_type,
                    ExceptionTask.resource_id == resource_id,
                    ExceptionTask.status == ExceptionStatus.OPEN,
                )
            )
            if existing is not None:
                return existing
        task = ExceptionTask(
            exception_type=exception_type,
            reason=reason,
            severity=severity,
            resource_type=resource_type,
            resource_id=resource_id,
            account_id=account_id,
            detail=detail or {},
        )
        self._session.add(task)
        self._session.flush()
        return task

    def get(self, task_id: UUID) -> ExceptionTask | None:
        return self._session.get(ExceptionTask, task_id)

    def list(self, *, status: ExceptionStatus | None = None, limit: int = 100, offset: int = 0):
        statement = select(ExceptionTask)
        if status is not None:
            statement = statement.where(ExceptionTask.status == status)
        return self._session.scalars(
            statement.order_by(ExceptionTask.created_at.desc()).limit(limit).offset(offset)
        ).all()

    def resolve(self, task_id: UUID, *, notes: str | None = None) -> ExceptionTask:
        task = self.get(task_id)
        if task is None:
            raise RecordNotFoundError(f"exception task not found: {task_id}")
        task.status = ExceptionStatus.RESOLVED
        task.resolved_at = utc_now()
        task.resolution_notes = notes
        task.version += 1
        self._session.flush()
        return task

    def count_open(self) -> int:
        return (
            self._session.scalar(
                select(func.count())
                .select_from(ExceptionTask)
                .where(ExceptionTask.status == ExceptionStatus.OPEN)
            )
            or 0
        )
