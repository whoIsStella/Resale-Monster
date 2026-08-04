"""Autonomous marketplace operations service.

Ties the gateway, session broker, deterministic policies, and bounded repositories
into the zero-touch workflows: account management, automation modes, emergency
stop, publishing and cross-posting, refresh/promote, order sync, sale detection
with reservation and automatic delisting, offer automation, buyer messaging,
price automation, shipping/labels, refunds, and financial reconciliation.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from goliath.config import OrchestrationConfig
from goliath.db.marketplace_repositories import (
    CircuitBreakerRepository,
    EmergencyStopRepository,
    ExceptionTaskRepository,
    MarketplaceAccountRepository,
    MarketplaceOfferRepository,
    MarketplaceOrderRepository,
    MessageRepository,
    PolicyDecisionRepository,
    ReconciliationRepository,
    RemoteListingRepository,
    ReservationRepository,
    ShippingTaskRepository,
    SyncConflictRepository,
)
from goliath.db.models import (
    AutomationMode,
    InventoryStatus,
    Marketplace,
    MarketplaceAccountStatus,
    OfferDecision,
    OfferStatus,
    OrderStatus,
    RemoteListingStatus,
    ReservationReason,
    ShippingTaskStatus,
    utc_now,
)
from goliath.db.repositories import AuditEventRepository, InventoryRepository, RecordNotFoundError
from goliath.marketplace import policy as P
from goliath.marketplace.adapter import (
    LabelRequest,
    ListingRequest,
    MessageRequest,
    OfferResponseRequest,
)
from goliath.marketplace.broker import SessionBroker
from goliath.marketplace.gateway import MarketplaceGateway, MarketplaceMetrics

CENTS = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(CENTS, rounding=ROUND_HALF_UP)


def _aware(value: datetime | None) -> datetime | None:
    """Normalize a possibly-naive datetime (SQLite) to UTC-aware."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class MarketplaceServiceError(RuntimeError):
    pass


class MarketplaceService:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session] | sessionmaker[Session],
        config: OrchestrationConfig,
        broker: SessionBroker | None = None,
        gateway: MarketplaceGateway | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._marketplace = config.marketplace
        self._broker = broker or SessionBroker(session_factory=session_factory, config=config)
        self._gateway = gateway or MarketplaceGateway(
            session_factory=session_factory, config=config, broker=self._broker
        )

    @property
    def metrics(self) -> MarketplaceMetrics:
        return self._gateway.metrics

    # ----------------------------------------------------------------- audit
    def _audit(self, event_type, resource_type, resource_id, details, actor="system:marketplace"):
        with self._session_factory() as session:
            actor_type, _, actor_id = actor.partition(":")
            AuditEventRepository(session).append(
                event_type=event_type,
                actor_type=actor_type or "system",
                actor_id=actor_id or actor,
                resource_type=resource_type,
                resource_id=resource_id,
                details=details,
            )
            session.commit()

    # -------------------------------------------------------------- accounts
    def create_account(
        self,
        *,
        marketplace: str,
        label: str,
        currency: str = "USD",
        capabilities: list[str] | None = None,
        actor: str = "human:operator",
    ):
        with self._session_factory() as session:
            account = MarketplaceAccountRepository(session).create(
                marketplace=Marketplace(marketplace),
                account_label=label,
                currency=currency,
                capabilities=capabilities or [],
            )
            AuditEventRepository(session).append(
                event_type="account.created",
                actor_type="human",
                actor_id=actor.partition(":")[2] or actor,
                resource_type="marketplace_account",
                resource_id=account.id,
                details={"marketplace": marketplace, "mode": account.automation_mode.value},
            )
            session.commit()
            session.refresh(account)
            session.expunge(account)
            return account

    def get_account(self, account_id: UUID):
        with self._session_factory() as session:
            account = MarketplaceAccountRepository(session).get(account_id)
            if account is None:
                raise RecordNotFoundError(f"marketplace account not found: {account_id}")
            session.expunge(account)
            return account

    def list_accounts(self):
        with self._session_factory() as session:
            accounts = list(MarketplaceAccountRepository(session).list())
            for account in accounts:
                session.expunge(account)
            return accounts

    def set_mode(
        self,
        account_id: UUID,
        *,
        mode: str,
        expected_version: int | None = None,
        actor: str = "human:operator",
    ):
        target = AutomationMode(mode)
        with self._session_factory() as session:
            repo = MarketplaceAccountRepository(session)
            account = repo.require(account_id)
            previous = account.automation_mode.value
            if expected_version is not None and expected_version != account.version:
                from goliath.db.repositories import VersionConflictError

                raise VersionConflictError(
                    f"marketplace account version conflict: expected {expected_version}, "
                    f"found {account.version}"
                )
            account = repo.set_mode(account_id, mode=target, expected_version=account.version)
            AuditEventRepository(session).append(
                event_type="automation.mode_changed",
                actor_type="human",
                actor_id=actor.partition(":")[2] or actor,
                resource_type="marketplace_account",
                resource_id=account_id,
                details={
                    "from": previous,
                    "to": target.value,
                    "mode_version": account.mode_version,
                },
            )
            session.commit()
            session.refresh(account)
            session.expunge(account)
            return account

    def authenticate_account(
        self, account_id: UUID, session_state: bytes, *, actor: str = "human:operator"
    ) -> str:
        account = self.get_account(account_id)
        config_account = self._find_config_account(account)
        allowed = config_account.allowed_domains if config_account else []
        return self._broker.store_session(
            account_id, session_state, allowed_domains=allowed, actor=actor
        )

    def _find_config_account(self, account):
        for cfg in self._marketplace.accounts.values():
            if cfg.marketplace == account.marketplace.value and cfg.label == account.account_label:
                return cfg
        return None

    # ------------------------------------------------------ emergency stop
    def emergency_stop(self, *, reason: str, scope_key: str = "*", actor: str = "human:admin"):
        with self._session_factory() as session:
            state = EmergencyStopRepository(session).activate(
                scope_key=scope_key, reason=reason, actor=actor
            )
            AuditEventRepository(session).append(
                event_type="emergency_stop.activated",
                actor_type="human",
                actor_id=actor.partition(":")[2] or actor,
                resource_type="emergency_stop",
                resource_id=state.id,
                details={"scope": scope_key},
            )
            session.commit()
            return {"active": True, "scope": scope_key, "reason": reason}

    def emergency_start(self, *, reason: str, scope_key: str = "*", actor: str = "human:admin"):
        with self._session_factory() as session:
            state = EmergencyStopRepository(session).release(scope_key=scope_key, reason=reason)
            AuditEventRepository(session).append(
                event_type="emergency_stop.released",
                actor_type="human",
                actor_id=actor.partition(":")[2] or actor,
                resource_type="emergency_stop",
                resource_id=state.id,
                details={"scope": scope_key},
            )
            session.commit()
            return {"active": False, "scope": scope_key}

    def automation_status(self):
        with self._session_factory() as session:
            active = list(EmergencyStopRepository(session).list_active())
            return {
                "emergency_stopped": any(s.scope_key == "*" for s in active),
                "active_stops": [s.scope_key for s in active],
            }

    async def read_account_remote(
        self, account_id: UUID, *, principal_scopes=None, actor="system:sync"
    ):
        return await self._read_account_operation(
            account_id, "read_account", "marketplace:read", principal_scopes, actor
        )

    async def health_check_remote(
        self, account_id: UUID, *, principal_scopes=None, actor="system:health"
    ):
        return await self._read_account_operation(
            account_id, "health_check", "marketplace:read", principal_scopes, actor
        )

    async def read_listings_remote(
        self, account_id: UUID, *, principal_scopes=None, actor="system:sync"
    ):
        return await self._read_account_operation(
            account_id, "read_listings", "marketplace:read", principal_scopes, actor
        )

    async def read_offers_remote(
        self, account_id: UUID, *, principal_scopes=None, actor="system:sync"
    ):
        return await self._read_account_operation(
            account_id, "read_offers", "marketplace:offer:read", principal_scopes, actor
        )

    async def sync_offers(
        self, account_id: UUID, *, principal_scopes=None, actor="system:offers"
    ) -> dict[str, Any]:
        receipt = await self.read_offers_remote(
            account_id, principal_scopes=principal_scopes, actor=actor
        )
        if not receipt.ok:
            return {
                "status": "skipped" if receipt.error_category == "not_supported" else "failed",
                "reason": receipt.error_category or "offer_sync_failed",
                "synced": 0,
            }
        synced = 0
        with self._session_factory() as session:
            repo = MarketplaceOfferRepository(session)
            for raw in receipt.data.get("offers", []):
                remote_offer_id = str(raw.get("remote_offer_id", "")).strip()
                if not remote_offer_id or raw.get("offer_amount") is None:
                    continue
                item_id = (
                    UUID(str(raw["inventory_item_id"])) if raw.get("inventory_item_id") else None
                )
                repo.upsert(
                    account_id=account_id,
                    remote_offer_id=remote_offer_id,
                    offer_amount=Decimal(str(raw["offer_amount"])),
                    list_price=(
                        Decimal(str(raw["list_price"]))
                        if raw.get("list_price") is not None
                        else None
                    ),
                    inventory_item_id=item_id,
                    prior_offer_count=int(raw.get("prior_offer_count", 0)),
                )
                synced += 1
            session.commit()
        return {"status": "succeeded", "synced": synced}

    async def read_orders_remote(
        self, account_id: UUID, *, principal_scopes=None, actor="system:sync"
    ):
        return await self._read_account_operation(
            account_id, "read_orders", "marketplace:order:read", principal_scopes, actor
        )

    async def read_messages_remote(
        self, account_id: UUID, *, principal_scopes=None, actor="system:sync"
    ):
        return await self._read_account_operation(
            account_id, "read_messages", "marketplace:message:read", principal_scopes, actor
        )

    async def process_routine_messages(
        self, account_id: UUID, *, principal_scopes=None, actor="system:messaging"
    ) -> dict[str, Any]:
        """Synchronize and respond without persisting plaintext buyer messages."""
        receipt = await self.read_messages_remote(
            account_id, principal_scopes=principal_scopes, actor=actor
        )
        if not receipt.ok:
            return {
                "status": "skipped" if receipt.error_category == "not_supported" else "failed",
                "reason": receipt.error_category or "message_sync_failed",
                "processed": 0,
            }
        processed = 0
        skipped = 0
        for raw in receipt.data.get("messages", []):
            remote_thread_id = str(raw.get("remote_thread_id", "")).strip()
            body = str(raw.get("body", "")).strip()
            if not remote_thread_id or not body:
                skipped += 1
                continue
            checksum = hashlib.sha256(body.encode()).hexdigest()
            buyer_reference = (
                hashlib.sha256(str(raw["buyer"]).encode()).hexdigest()[:16]
                if raw.get("buyer")
                else None
            )
            item_id = UUID(str(raw["inventory_item_id"])) if raw.get("inventory_item_id") else None
            with self._session_factory() as session:
                messages = MessageRepository(session)
                thread = messages.upsert_thread(
                    account_id=account_id,
                    remote_thread_id=remote_thread_id,
                    inventory_item_id=item_id,
                    buyer_reference=buyer_reference,
                )
                duplicate = messages.has_message_checksum(thread.id, checksum)
                thread_id = thread.id
                session.commit()
            if duplicate:
                skipped += 1
                continue
            await self.respond_message(
                thread_id,
                body,
                facts=dict(raw.get("facts") or {}),
                principal_scopes=principal_scopes,
                actor=actor,
            )
            processed += 1
        return {"status": "succeeded", "processed": processed, "skipped": skipped}

    async def read_notifications_remote(
        self, account_id: UUID, *, principal_scopes=None, actor="system:sync"
    ):
        return await self._read_account_operation(
            account_id, "read_notifications", "marketplace:read", principal_scopes, actor
        )

    async def _read_account_operation(
        self, account_id: UUID, operation: str, scope: str, principal_scopes, actor: str
    ):
        return await self._gateway.execute(
            account_id=account_id,
            operation=operation,
            scope=scope,
            principal_scopes=principal_scopes,
            adapter_call=lambda adapter: getattr(adapter, operation)(),
            agent_identity=actor,
            resource_type="marketplace_account",
            resource_id=account_id,
        )

    async def sync_account(self, account_id: UUID, *, principal_scopes=None, actor="system:sync"):
        """Run account synchronization; order ingestion has its own durable workflow."""
        receipt = await self._gateway.execute(
            account_id=account_id,
            operation="sync_account",
            scope="marketplace:sync",
            principal_scopes=principal_scopes,
            adapter_call=lambda adapter: adapter.sync_account(),
            agent_identity=actor,
            resource_type="marketplace_account",
            resource_id=account_id,
        )
        with self._session_factory() as session:
            MarketplaceAccountRepository(session).record_sync(account_id, success=receipt.ok)
            session.commit()
        return receipt

    # ---------------------------------------------------------------- publish
    def _publishing_eligibility(self, session, item, account) -> P.PolicyOutcome:
        from goliath.db.domain_repositories import (
            InventoryMediaRepository,
            ListingDraftRepository,
            PricingRecommendationRepository,
        )
        from goliath.db.models import DraftStatus

        approved_draft = any(
            d.inventory_item_id == item.id and d.status is DraftStatus.APPROVED
            for d in ListingDraftRepository(session).list(limit=200)
        )
        recs = PricingRecommendationRepository(session).list_for_item(item.id)
        expected_profit = recs[-1].expected_profit if recs else Decimal(0)
        image_count = len(list(InventoryMediaRepository(session).list_for_item(item.id)))
        reservations = ReservationRepository(session).active_for_item(item.id)
        duplicate = RemoteListingRepository(session).find_active_on_account(item.id, account.id)
        stopped = EmergencyStopRepository(session).is_active(str(account.id))
        breaker_open = CircuitBreakerRepository(session).is_open(
            scope="account", scope_key=str(account.id)
        ) or CircuitBreakerRepository(session).is_open(scope="item", scope_key=str(item.id))
        rules = self._marketplace.publishing
        if account.automation_mode is AutomationMode.AUTONOMOUS_CONSERVATIVE:
            rules = rules.model_copy(
                update={
                    "minimum_expected_profit": rules.minimum_expected_profit
                    * self._marketplace.conservative_profit_multiplier
                }
            )
        return P.evaluate_publishing(
            P.PublishingInputs(
                item_status=item.status.value,
                draft_approved=approved_draft,
                variants_approved=approved_draft,
                image_count=image_count,
                pricing_current=bool(recs),
                account_healthy=account.status is MarketplaceAccountStatus.HEALTHY,
                expected_profit=expected_profit,
                duplicate_active=duplicate is not None,
                reserved=bool(reservations),
                emergency_stopped=stopped,
                breaker_open=breaker_open,
            ),
            rules,
        )

    def _select_accounts(self, accounts, requested: list[UUID] | None) -> list:
        if requested:
            requested_set = set(requested)
            return [a for a in accounts if a.id in requested_set]
        strategy = self._marketplace.cross_listing.strategy
        healthy = [a for a in accounts if a.status is MarketplaceAccountStatus.HEALTHY]
        if strategy == "single":
            return healthy[:1]
        if strategy == "preferred":
            preferred = self._marketplace.cross_listing.preferred_marketplaces
            chosen = [a for a in healthy if a.marketplace.value in preferred]
            return chosen[: self._marketplace.cross_listing.max_active_listings]
        return healthy[: self._marketplace.cross_listing.max_active_listings]

    async def publish_item(
        self,
        item_id: UUID,
        *,
        account_ids: list[UUID] | None = None,
        principal_scopes: set[str] | None = None,
        actor: str = "system:publisher",
    ) -> dict[str, Any]:
        with self._session_factory() as session:
            item = InventoryRepository(session).get(item_id)
            if item is None:
                raise RecordNotFoundError(f"inventory item not found: {item_id}")
            accounts = list(MarketplaceAccountRepository(session).list())
            for account in accounts:
                session.expunge(account)
            session.expunge(item)

        selected = self._select_accounts(accounts, account_ids)
        if not selected:
            raise MarketplaceServiceError("no eligible marketplace accounts")

        results: dict[str, str] = {}
        published_any = False
        for account in selected:
            label = account.account_label
            with self._session_factory() as session:
                fresh_item = InventoryRepository(session).get(item_id)
                outcome = self._publishing_eligibility(session, fresh_item, account)
                PolicyDecisionRepository(session).record(
                    policy_type="publishing",
                    policy_version=outcome.policy_version,
                    decision=outcome.decision,
                    inputs={"account": label},
                    reasons=outcome.reasons,
                    resource_type="inventory_item",
                    resource_id=item_id,
                )
                price = fresh_item.acquisition_cost
                from goliath.db.domain_repositories import PricingRecommendationRepository

                recs = PricingRecommendationRepository(session).list_for_item(item_id)
                if recs:
                    price = recs[-1].recommended_price
                session.commit()
            if outcome.decision != "allow":
                results[label] = "blocked"
                continue

            idem = self._idem("publish", item_id, account.id)
            with self._session_factory() as session:
                listing = RemoteListingRepository(session).create(
                    inventory_item_id=item_id,
                    account_id=account.id,
                    idempotency_key=idem,
                    status=RemoteListingStatus.PUBLISHING,
                    current_price=price,
                    automation_mode_used=account.automation_mode.value,
                )
                listing_id = listing.id
                listing_version = listing.local_version
                session.commit()

            request = ListingRequest(
                inventory_item_id=str(item_id),
                title=fresh_item.title,
                description=fresh_item.description or "",
                price=price,
                currency=account.currency,
                idempotency_key=idem,
            )
            receipt = await self._gateway.execute(
                account_id=account.id,
                operation="create_listing",
                scope="marketplace:listing:create",
                principal_scopes=principal_scopes,
                idempotency_key=idem,
                adapter_call=lambda adapter, r=request: adapter.create_listing(r),
                verify_call=lambda adapter, key=idem: self._verify_listing_active(adapter, key),
                agent_identity=actor,
                resource_type="remote_listing",
                resource_id=listing_id,
            )
            if receipt.ok and receipt.remote_id:
                with self._session_factory() as session:
                    RemoteListingRepository(session).apply_publish_result(
                        listing_id,
                        expected_version=listing_version,
                        remote_listing_id=receipt.remote_id,
                        remote_url=receipt.remote_url,
                        status=RemoteListingStatus.ACTIVE,
                        verified=receipt.verified,
                    )
                    session.commit()
                self._audit(
                    "listing.published",
                    "remote_listing",
                    listing_id,
                    {"account": label, "verified": receipt.verified},
                )
                results[label] = "published"
                published_any = True
            else:
                with self._session_factory() as session:
                    RemoteListingRepository(session).set_status(
                        listing_id,
                        status=RemoteListingStatus.FAILED,
                        error_category=receipt.error_category,
                    )
                    session.commit()
                if receipt.error_category in {"blocked", "unauthorized"}:
                    results[label] = "blocked"
                elif receipt.error_category in {"transient", "rate_limited"}:
                    results[label] = "retry_scheduled"
                else:
                    results[label] = "failed"

        if published_any:
            with self._session_factory() as session:
                item = InventoryRepository(session).get(item_id)
                if item.status is not InventoryStatus.LISTED:
                    InventoryRepository(session).update_draft(
                        item_id,
                        expected_version=item.version,
                        changes={"status": "listed"},
                        allow_human_only_status=True,
                    )
                session.commit()
        return {"item_id": str(item_id), "results": results}

    async def publish_draft(self, draft_id: UUID, **kwargs):
        from goliath.db.domain_repositories import ListingDraftRepository
        from goliath.db.models import DraftStatus

        with self._session_factory() as session:
            draft = ListingDraftRepository(session).get(draft_id)
            if draft is None:
                raise RecordNotFoundError(f"listing draft not found: {draft_id}")
            if draft.status is not DraftStatus.APPROVED:
                raise MarketplaceServiceError("listing draft is not approved")
            item_id = draft.inventory_item_id
        return await self.publish_item(item_id, **kwargs)

    @staticmethod
    async def _verify_listing_active(adapter, idempotency_key: str) -> bool:
        # Verify the exact idempotent publish, not merely any active listing.
        result = await adapter.read_listings()
        if not result.ok:
            return False
        listings = result.data.get("listings", [])
        return any(
            str(x.get("status")) == "active" and x.get("idempotency_key") == idempotency_key
            for x in listings
        )

    # ------------------------------------------------------- refresh/promote
    async def refresh_listing(
        self, listing_id: UUID, *, principal_scopes=None, actor="system:scheduler"
    ):
        return await self._simple_listing_op(
            listing_id,
            "refresh_listing",
            "marketplace:listing:refresh",
            lambda adapter, rid: adapter.refresh_listing(rid),
            principal_scopes,
            actor,
            verified_action="refresh",
        )

    async def promote_listing(
        self, listing_id: UUID, *, principal_scopes=None, actor="system:scheduler"
    ):
        return await self._simple_listing_op(
            listing_id,
            "promote_listing",
            "marketplace:listing:promote",
            lambda adapter, rid: adapter.promote_listing(rid),
            principal_scopes,
            actor,
        )

    async def share_listing(
        self, listing_id: UUID, *, principal_scopes=None, actor="system:scheduler"
    ):
        return await self._simple_listing_op(
            listing_id,
            "share_listing",
            "marketplace:listing:share",
            lambda adapter, rid: adapter.share_listing(rid),
            principal_scopes,
            actor,
        )

    async def update_listing(
        self,
        listing_id: UUID,
        *,
        fields: dict[str, Any],
        principal_scopes=None,
        actor="system:listing",
    ):
        allowed = {"title", "description", "price", "quantity", "category"}
        if not fields or set(fields) - allowed:
            raise MarketplaceServiceError("listing update contains unsupported fields")
        with self._session_factory() as session:
            listing = RemoteListingRepository(session).get(listing_id)
            if listing is None:
                raise RecordNotFoundError(f"remote listing not found: {listing_id}")
            if ReservationRepository(session).active_for_item(listing.inventory_item_id):
                raise MarketplaceServiceError("reserved inventory cannot be updated")
            account_id = listing.account_id
            remote_id = listing.remote_listing_id
        if remote_id is None:
            raise MarketplaceServiceError("listing has no remote id")
        idem = hashlib.sha256(
            f"update:{listing_id}:{sorted((key, str(value)) for key, value in fields.items())}".encode()
        ).hexdigest()[:32]
        receipt = await self._gateway.execute(
            account_id=account_id,
            operation="update_listing",
            scope="marketplace:listing:update",
            principal_scopes=principal_scopes,
            idempotency_key=idem,
            adapter_call=lambda adapter: adapter.update_listing(remote_id, **fields),
            verify_call=lambda adapter: self._verify_listing_fields(adapter, remote_id, fields),
            agent_identity=actor,
            resource_type="remote_listing",
            resource_id=listing_id,
            request_summary={"fields": sorted(fields)},
        )
        if receipt.ok:
            with self._session_factory() as session:
                RemoteListingRepository(session).set_status(
                    listing_id,
                    status=RemoteListingStatus.ACTIVE,
                    verified_action="update",
                    price=Decimal(str(fields["price"])) if "price" in fields else None,
                )
                session.commit()
            self._audit("listing.updated", "remote_listing", listing_id, {"fields": sorted(fields)})
        return receipt

    @staticmethod
    async def _verify_listing_fields(adapter, remote_id: str, fields: dict[str, Any]) -> bool:
        result = await adapter.read_listing(remote_id)
        if not result.ok:
            return False
        return all(str(result.data.get(key)) == str(value) for key, value in fields.items())

    async def read_listing(self, listing_id: UUID, *, principal_scopes=None, actor="system:sync"):
        with self._session_factory() as session:
            listing = RemoteListingRepository(session).get(listing_id)
            if listing is None:
                raise RecordNotFoundError(f"remote listing not found: {listing_id}")
            account_id, remote_id = listing.account_id, listing.remote_listing_id
        if remote_id is None:
            raise MarketplaceServiceError("listing has no remote id")
        receipt = await self._gateway.execute(
            account_id=account_id,
            operation="read_listing",
            scope="marketplace:read",
            principal_scopes=principal_scopes,
            adapter_call=lambda adapter: adapter.read_listing(remote_id),
            agent_identity=actor,
            resource_type="remote_listing",
            resource_id=listing_id,
        )
        if receipt.ok:
            with self._session_factory() as session:
                listing = RemoteListingRepository(session).get(listing_id)
                if listing is not None:
                    listing.last_synced_at = utc_now()
                    remote_status = receipt.data.get("status")
                    if remote_status in {value.value for value in RemoteListingStatus}:
                        listing.status = RemoteListingStatus(remote_status)
                    listing.local_version += 1
                session.commit()
        return receipt

    async def send_offer(
        self,
        listing_id: UUID,
        amount: Decimal,
        *,
        principal_scopes=None,
        actor="system:offers",
    ):
        with self._session_factory() as session:
            listing = RemoteListingRepository(session).get(listing_id)
            if listing is None:
                raise RecordNotFoundError(f"remote listing not found: {listing_id}")
            account_id, remote_id = listing.account_id, listing.remote_listing_id
            item = InventoryRepository(session).get(listing.inventory_item_id)
            reserved = bool(
                ReservationRepository(session).active_for_item(listing.inventory_item_id)
            )
            cost = item.acquisition_cost if item else Decimal(0)
        minimum = cost + self._marketplace.offers.minimum_net_profit
        if reserved or amount < minimum:
            raise MarketplaceServiceError(
                "outbound offer violates reservation or minimum-profit policy"
            )
        idem = hashlib.sha256(f"send-offer:{listing_id}:{amount}".encode()).hexdigest()[:32]
        return await self._gateway.execute(
            account_id=account_id,
            operation="send_offer",
            scope="marketplace:offer:respond",
            principal_scopes=principal_scopes,
            idempotency_key=idem,
            adapter_call=lambda adapter: adapter.send_offer(remote_id or "", amount),
            agent_identity=actor,
            resource_type="remote_listing",
            resource_id=listing_id,
            request_summary={"amount": str(amount)},
        )

    async def end_listing(
        self, listing_id: UUID, *, principal_scopes=None, actor="system:scheduler"
    ):
        return await self._end_listing(listing_id, principal_scopes=principal_scopes, actor=actor)

    async def _simple_listing_op(
        self, listing_id, operation, scope, call, principal_scopes, actor, verified_action=None
    ):
        with self._session_factory() as session:
            listing = RemoteListingRepository(session).get(listing_id)
            if listing is None:
                raise RecordNotFoundError(f"remote listing not found: {listing_id}")
            account_id = listing.account_id
            remote_id = listing.remote_listing_id
            session.expunge(listing)
        if remote_id is None:
            raise MarketplaceServiceError("listing has no remote id")
        receipt = await self._gateway.execute(
            account_id=account_id,
            operation=operation,
            scope=scope,
            principal_scopes=principal_scopes,
            idempotency_key=self._idem(operation, listing_id, account_id),
            adapter_call=lambda adapter: call(adapter, remote_id),
            agent_identity=actor,
            resource_type="remote_listing",
            resource_id=listing_id,
        )
        if receipt.ok and verified_action:
            with self._session_factory() as session:
                RemoteListingRepository(session).set_status(
                    listing_id, status=RemoteListingStatus.ACTIVE, verified_action=verified_action
                )
                session.commit()
        if receipt.ok:
            event = "listing.refreshed" if operation == "refresh_listing" else "listing.promoted"
            self._audit(event, "remote_listing", listing_id, {"ok": True})
        return receipt

    # ---------------------------------------------------- order sync + sale
    async def sync_orders(self, account_id: UUID, *, principal_scopes=None, actor="system:sync"):
        receipt = await self._gateway.execute(
            account_id=account_id,
            operation="read_orders",
            scope="marketplace:order:read",
            principal_scopes=principal_scopes,
            adapter_call=lambda adapter: adapter.read_orders(),
            agent_identity=actor,
        )
        if not receipt.ok:
            with self._session_factory() as session:
                MarketplaceAccountRepository(session).record_sync(account_id, success=False)
                session.commit()
            return {
                "ok": False,
                "reason": receipt.reasons,
                "error_category": receipt.error_category,
            }
        detected = 0
        for raw in receipt.data.get("orders", []):
            order, is_new = self._upsert_order(account_id, raw)
            if is_new:
                detected += 1
                self._audit(
                    "sale.detected",
                    "marketplace_order",
                    UUID(order["id"]),
                    {"account": str(account_id)},
                )
                if order["status"] in {OrderStatus.PAID.value, OrderStatus.READY_TO_SHIP.value}:
                    await self._on_sale(
                        UUID(order["id"]),
                        UUID(order["item_id"]) if order.get("item_id") else None,
                        actor,
                    )
        with self._session_factory() as session:
            MarketplaceAccountRepository(session).record_sync(account_id, success=True)
            session.commit()
        return {"ok": True, "orders_detected": detected}

    def _upsert_order(self, account_id, raw) -> tuple[dict, bool]:
        checksum = hashlib.sha256(str(sorted(raw.items())).encode()).hexdigest()
        buyer_ref = None
        if raw.get("buyer"):
            buyer_ref = hashlib.sha256(str(raw["buyer"]).encode()).hexdigest()[:16]
        item_id = raw.get("inventory_item_id")
        with self._session_factory() as session:
            order, is_new = MarketplaceOrderRepository(session).upsert(
                account_id=account_id,
                remote_order_id=str(raw["remote_order_id"]),
                sale_price=Decimal(str(raw.get("sale_price", "0"))),
                status=OrderStatus(raw.get("status", "unknown")),
                payload_checksum=checksum,
                remote_listing_id=raw.get("remote_listing_id"),
                inventory_item_id=UUID(item_id) if item_id else None,
                buyer_reference=buyer_ref,
                shipping_charged=Decimal(str(raw.get("shipping_charged", "0"))),
                marketplace_fees=Decimal(str(raw.get("marketplace_fees", "0"))),
                quantity=int(raw.get("quantity", 1)),
                payment_state=raw.get("payment_state", "unknown"),
            )
            session.commit()
            return (
                {
                    "id": str(order.id),
                    "status": order.status.value,
                    "item_id": str(order.inventory_item_id) if order.inventory_item_id else None,
                },
                is_new,
            )

    async def _on_sale(self, order_id: UUID, item_id: UUID | None, actor: str) -> None:
        """Reserve inventory and automatically delist every other active listing."""
        started = utc_now()
        if item_id is None:
            return
        with self._session_factory() as session:
            reservations = ReservationRepository(session)
            if not reservations.active_for_item(item_id):
                reservations.reserve(
                    inventory_item_id=item_id,
                    reason=ReservationReason.PAID_SALE,
                    created_by=actor,
                    source_order_id=str(order_id),
                    expires_at=utc_now()
                    + timedelta(seconds=self._marketplace.reservation_ttl_seconds),
                )
            inventory = InventoryRepository(session)
            item = inventory.get(item_id)
            if item and item.status not in {InventoryStatus.SOLD}:
                inventory.update_draft(
                    item_id,
                    expected_version=item.version,
                    changes={"status": "reserved"},
                    allow_human_only_status=True,
                )
            listings = list(RemoteListingRepository(session).list_active_for_item(item_id))
            listing_ids = [listing.id for listing in listings]
            AuditEventRepository(session).append(
                event_type="inventory.reserved",
                actor_type="system",
                actor_id="marketplace",
                resource_type="inventory_item",
                resource_id=item_id,
                details={"order": str(order_id)},
            )
            self._create_shipping_task(session, order_id, item_id)
            session.commit()

        self._audit("delisting.started", "inventory_item", item_id, {"count": len(listing_ids)})
        failed = []
        for listing_id in listing_ids:
            receipt = await self._end_listing(listing_id, actor=actor, on_sale=True)
            if not receipt.ok:
                failed.append(listing_id)

        latency = (utc_now() - started).total_seconds()
        if failed:
            for listing_id in failed:  # one retry
                receipt = await self._end_listing(listing_id, actor=actor, on_sale=True)
                if receipt.ok:
                    failed.remove(listing_id)
        if failed or latency > self._marketplace.max_delisting_latency_seconds:
            with self._session_factory() as session:
                ExceptionTaskRepository(session).create(
                    exception_type="duplicate_sale_risk",
                    reason="delisting incomplete or exceeded latency target",
                    severity="urgent",
                    resource_type="inventory_item",
                    resource_id=item_id,
                    detail={"failed": [str(x) for x in failed], "latency_seconds": latency},
                )
                CircuitBreakerRepository(session).record_failure(
                    scope="item",
                    scope_key=str(item_id),
                    threshold=1,
                    cooldown_seconds=self._marketplace.circuit_breaker.cooldown_seconds,
                    reason="delisting_failure",
                )
                session.commit()
            self._audit("delisting.failed", "inventory_item", item_id, {"failed": len(failed)})
        else:
            with self._session_factory() as session:
                item = InventoryRepository(session).get(item_id)
                if item and item.status is not InventoryStatus.SOLD:
                    InventoryRepository(session).update_draft(
                        item_id,
                        expected_version=item.version,
                        changes={"status": "sold"},
                        allow_human_only_status=True,
                    )
                session.commit()
            self._audit(
                "delisting.verified", "inventory_item", item_id, {"latency_seconds": latency}
            )

    async def _end_listing(
        self, listing_id, *, principal_scopes=None, actor="system", on_sale=False
    ):
        with self._session_factory() as session:
            listing = RemoteListingRepository(session).get(listing_id)
            if listing is None:
                raise RecordNotFoundError(f"remote listing not found: {listing_id}")
            account_id = listing.account_id
            remote_id = listing.remote_listing_id
            session.expunge(listing)
        receipt = await self._gateway.execute(
            account_id=account_id,
            operation="end_listing",
            scope="marketplace:listing:end",
            principal_scopes=principal_scopes,
            idempotency_key=self._idem("end_listing", listing_id, account_id),
            adapter_call=lambda adapter: adapter.end_listing(remote_id or ""),
            verify_call=lambda adapter: self._verify_ended(adapter, remote_id or ""),
            agent_identity=actor,
            resource_type="remote_listing",
            resource_id=listing_id,
        )
        if receipt.ok:
            with self._session_factory() as session:
                RemoteListingRepository(session).set_status(
                    listing_id, status=RemoteListingStatus.ENDED, verified_action="end"
                )
                session.commit()
            self._audit("delisting.verified", "remote_listing", listing_id, {"on_sale": on_sale})
        return receipt

    @staticmethod
    async def _verify_ended(adapter, remote_id) -> bool:
        result = await adapter.read_listing(remote_id)
        if not result.ok:
            return True  # not found means already inactive
        return str(result.data.get("status")) == "ended"

    def _create_shipping_task(self, session, order_id, item_id) -> None:
        item = InventoryRepository(session).get(item_id) if item_id else None
        task = ShippingTaskRepository(session).create_for_order(
            order_id=order_id,
            inventory_item_id=item_id,
            package_profile=self._marketplace.shipping.default_package_profile,
            estimated_weight_grams=item.packed_weight_grams if item else None,
            storage_location=item.storage_location if item else None,
        )
        if item is not None and item.package_dimensions:
            task.package_dimensions = dict(item.package_dimensions)
        dimensions = task.package_dimensions or {}
        if (
            task.package_profile
            and task.estimated_weight_grams is not None
            and task.estimated_weight_grams > 0
            and all(
                Decimal(str(dimensions.get(key, 0))) > 0 for key in ("length", "width", "height")
            )
        ):
            task.status = ShippingTaskStatus.READY

    # -------------------------------------------------------------- offers
    async def handle_offer(
        self,
        offer_id: UUID,
        *,
        requested_action: str | None = None,
        requested_counter_amount: Decimal | None = None,
        principal_scopes=None,
        actor="system:offers",
    ):
        if requested_action is not None and requested_action not in {
            "accept",
            "decline",
            "counter",
        }:
            raise MarketplaceServiceError("unsupported requested offer action")
        if requested_action == "counter":
            if requested_counter_amount is None or requested_counter_amount <= 0:
                raise MarketplaceServiceError("counter offers require a positive counter amount")
        elif requested_counter_amount is not None:
            raise MarketplaceServiceError("counter amount is only valid for counter offers")
        with self._session_factory() as session:
            offer = MarketplaceOfferRepository(session).get(offer_id)
            if offer is None:
                raise RecordNotFoundError(f"offer not found: {offer_id}")
            account_id = offer.account_id
            item_id = offer.inventory_item_id
            offer_amount = offer.offer_amount
            list_price = offer.list_price or offer_amount
            remote_offer_id = offer.remote_offer_id
            cost_basis = Decimal(0)
            reserved = False
            if item_id:
                item = InventoryRepository(session).get(item_id)
                cost_basis = item.acquisition_cost if item else Decimal(0)
                reserved = bool(ReservationRepository(session).active_for_item(item_id))
            session.expunge(offer)

        offer_rules = self._marketplace.offers
        if self.get_account(account_id).automation_mode is AutomationMode.AUTONOMOUS_CONSERVATIVE:
            offer_rules = offer_rules.model_copy(
                update={
                    "minimum_net_profit": offer_rules.minimum_net_profit
                    * self._marketplace.conservative_profit_multiplier,
                    "maximum_discount_percent": offer_rules.maximum_discount_percent / 2,
                }
            )
        outcome = P.evaluate_offer(
            P.OfferInputs(
                offer_amount=offer_amount,
                list_price=list_price,
                cost_basis=cost_basis,
                reserved=reserved,
                high_value=list_price >= self._marketplace.high_value_threshold,
            ),
            offer_rules,
        )
        if requested_action is not None and outcome.decision != requested_action:
            with self._session_factory() as session:
                MarketplaceOfferRepository(session).record_decision(
                    offer_id,
                    decision=OfferDecision.ESCALATE,
                    status=OfferStatus.ESCALATED,
                    requested_action=requested_action,
                    executed_action=None,
                )
                session.commit()
            return {
                "ok": False,
                "decision": "rejected",
                "requested_action": requested_action,
                "executed_action": None,
                "reasons": [
                    f"policy selected {outcome.decision}; requested action was not executed"
                ],
            }
        if requested_action == "counter":
            policy_counter = _money(Decimal(outcome.data["counter_amount"]))
            if _money(requested_counter_amount) != policy_counter:
                return {
                    "ok": False,
                    "decision": "rejected",
                    "requested_action": requested_action,
                    "executed_action": None,
                    "reasons": ["counter amount does not match deterministic policy"],
                }
        with self._session_factory() as session:
            PolicyDecisionRepository(session).record(
                policy_type="offer",
                policy_version=outcome.policy_version,
                decision=outcome.decision,
                inputs={"offer": str(offer_amount), "list_price": str(list_price)},
                reasons=outcome.reasons,
                risk_score=outcome.risk_score,
                resource_type="marketplace_offer",
                resource_id=offer_id,
            )
            session.commit()
        self._audit(
            "offer.evaluated", "marketplace_offer", offer_id, {"decision": outcome.decision}
        )

        if outcome.decision in {"escalate", "defer"}:
            if outcome.decision == "escalate":
                with self._session_factory() as session:
                    ExceptionTaskRepository(session).create(
                        exception_type="offer_escalation",
                        reason="; ".join(outcome.reasons),
                        resource_type="marketplace_offer",
                        resource_id=offer_id,
                        account_id=account_id,
                    )
                    MarketplaceOfferRepository(session).record_decision(
                        offer_id,
                        decision=OfferDecision.ESCALATE,
                        status=OfferStatus.ESCALATED,
                        requested_action=requested_action,
                        executed_action=None,
                    )
                    session.commit()
            return {"decision": outcome.decision, "reasons": outcome.reasons}

        action_map = {
            "accept": ("accept_offer", OfferDecision.ACCEPT, OfferStatus.ACCEPTED),
            "decline": ("decline_offer", OfferDecision.DECLINE, OfferStatus.DECLINED),
            "counter": ("counter_offer", OfferDecision.COUNTER, OfferStatus.COUNTERED),
        }
        operation, decision_enum, status_enum = action_map[outcome.decision]
        counter_amount = (
            _money(
                requested_counter_amount
                if requested_counter_amount is not None
                else Decimal(outcome.data["counter_amount"])
            )
            if outcome.decision == "counter"
            else None
        )
        request = OfferResponseRequest(
            remote_offer_id=remote_offer_id,
            action=outcome.decision,
            counter_amount=counter_amount,
            idempotency_key=self._idem(operation, offer_id, account_id),
        )
        receipt = await self._gateway.execute(
            account_id=account_id,
            operation=operation,
            scope="marketplace:offer:respond",
            principal_scopes=principal_scopes,
            idempotency_key=request.idempotency_key,
            adapter_call=lambda adapter, r=request: getattr(adapter, operation)(r),
            agent_identity=actor,
            resource_type="marketplace_offer",
            resource_id=offer_id,
        )
        if receipt.ok:
            with self._session_factory() as session:
                MarketplaceOfferRepository(session).record_decision(
                    offer_id,
                    decision=decision_enum,
                    status=status_enum,
                    counter_amount=counter_amount,
                    requested_action=requested_action or outcome.decision,
                    executed_action=outcome.decision,
                    responded=True,
                )
                session.commit()
            self._audit(
                "offer.response_sent", "marketplace_offer", offer_id, {"decision": outcome.decision}
            )
        return {
            "decision": outcome.decision,
            "ok": receipt.ok,
            "requested_action": requested_action or outcome.decision,
            "executed_action": outcome.decision if receipt.ok else None,
            "counter_amount": str(counter_amount) if counter_amount else None,
        }

    # ------------------------------------------------------------- messaging
    async def respond_message(
        self,
        thread_id: UUID,
        body: str,
        *,
        facts: dict[str, Any] | None = None,
        principal_scopes=None,
        actor="system:messaging",
    ):
        with self._session_factory() as session:
            thread = MessageRepository(session).get_thread(thread_id)
            if thread is None:
                raise RecordNotFoundError(f"thread not found: {thread_id}")
            account_id = thread.account_id
            remote_thread_id = thread.remote_thread_id
            MessageRepository(session).add_message(
                thread_id,
                direction="inbound",
                category=None,
                body_checksum=hashlib.sha256(body.encode()).hexdigest(),
            )
            session.commit()
        self._audit("buyer.message_read", "message_thread", thread_id, {})

        outcome = P.classify_message(body, self._marketplace.messaging)
        category = outcome.data["category"]
        if outcome.decision == "escalate":
            with self._session_factory() as session:
                MessageRepository(session).add_message(
                    thread_id,
                    direction="system",
                    category=category,
                    body_checksum="escalated",
                    escalated=True,
                )
                ExceptionTaskRepository(session).create(
                    exception_type="message_escalation",
                    reason=f"category: {category}",
                    resource_type="message_thread",
                    resource_id=thread_id,
                    account_id=account_id,
                )
                session.commit()
            self._audit("message.escalated", "message_thread", thread_id, {"category": category})
            return {"decision": "escalate", "category": category}

        response = P.build_response(category, facts or {})
        message_idem = hashlib.sha256(
            f"message:{thread_id}:{category}:{response}".encode()
        ).hexdigest()[:32]
        request = MessageRequest(
            remote_thread_id=remote_thread_id,
            body=response,
            category=category,
            idempotency_key=message_idem,
        )
        receipt = await self._gateway.execute(
            account_id=account_id,
            operation="send_message",
            scope="marketplace:message:routine",
            principal_scopes=principal_scopes,
            idempotency_key=message_idem,
            adapter_call=lambda adapter, r=request: adapter.send_message(r),
            agent_identity=actor,
            resource_type="message_thread",
            resource_id=thread_id,
        )
        with self._session_factory() as session:
            MessageRepository(session).add_message(
                thread_id,
                direction="outbound",
                category=category,
                body_checksum=hashlib.sha256(response.encode()).hexdigest(),
                delivered=receipt.ok and receipt.data.get("delivered", False),
            )
            session.commit()
        self._audit("buyer.response_sent", "message_thread", thread_id, {"category": category})
        return {"decision": "respond", "category": category, "ok": receipt.ok}

    # ------------------------------------------------------- price automation
    async def run_price_automation(
        self, listing_id: UUID, *, principal_scopes=None, actor="system:pricing"
    ):
        with self._session_factory() as session:
            listing = RemoteListingRepository(session).get(listing_id)
            if listing is None:
                raise RecordNotFoundError(f"remote listing not found: {listing_id}")
            account_id = listing.account_id
            remote_id = listing.remote_listing_id
            current_price = listing.current_price or Decimal(0)
            item = InventoryRepository(session).get(listing.inventory_item_id)
            reserved = bool(
                ReservationRepository(session).active_for_item(listing.inventory_item_id)
            )
            anchor = _aware(listing.published_at) or _aware(listing.created_at)
            days_listed = (utc_now() - anchor).days
            hours_since = (
                (utc_now() - _aware(listing.refreshed_at)).total_seconds() / 3600
                if listing.refreshed_at
                else 999.0
            )
            min_price = (
                item.acquisition_cost + self._marketplace.pricing.minimum_net_profit
                if item
                else current_price
            )
            session.expunge(listing)
        if reserved:
            return {"decision": "hold", "reason": "reserved"}
        outcome = P.evaluate_markdown(
            P.MarkdownInputs(
                current_price=current_price,
                minimum_price=_money(min_price),
                days_listed=days_listed,
                hours_since_last_change=hours_since,
                total_reduction_percent=0.0,
            ),
            self._marketplace.pricing,
        )
        with self._session_factory() as session:
            PolicyDecisionRepository(session).record(
                policy_type="pricing",
                policy_version=outcome.policy_version,
                decision=outcome.decision,
                inputs={"current": str(current_price)},
                reasons=outcome.reasons,
                resource_type="remote_listing",
                resource_id=listing_id,
            )
            session.commit()
        if outcome.decision != "markdown":
            return {"decision": outcome.decision, "new_price": outcome.data.get("new_price")}
        new_price = Decimal(outcome.data["new_price"])
        idem = hashlib.sha256(f"price:{listing_id}:{new_price}".encode()).hexdigest()[:32]
        receipt = await self._gateway.execute(
            account_id=account_id,
            operation="update_listing",
            scope="marketplace:listing:update",
            principal_scopes=principal_scopes,
            idempotency_key=idem,
            adapter_call=lambda adapter: adapter.update_listing(remote_id or "", price=new_price),
            verify_call=lambda adapter: self._verify_listing_fields(
                adapter, remote_id or "", {"price": new_price}
            ),
            agent_identity=actor,
            resource_type="remote_listing",
            resource_id=listing_id,
        )
        if receipt.ok:
            with self._session_factory() as session:
                RemoteListingRepository(session).set_status(
                    listing_id,
                    status=RemoteListingStatus.ACTIVE,
                    verified_action="update",
                    price=new_price,
                )
                session.commit()
            self._audit(
                "price.changed", "remote_listing", listing_id, {"new_price": str(new_price)}
            )
        return {"decision": "markdown", "new_price": str(new_price), "ok": receipt.ok}

    async def relist_listing(
        self, listing_id: UUID, *, principal_scopes=None, actor="system:relisting"
    ) -> dict[str, Any]:
        rules = self._marketplace.relisting
        with self._session_factory() as session:
            old = RemoteListingRepository(session).get(listing_id)
            if old is None:
                raise RecordNotFoundError(f"remote listing not found: {listing_id}")
            if not rules.enabled or old.relist_count >= rules.maximum_relists:
                return {"ok": False, "reason": "relisting policy limit reached"}
            if ReservationRepository(session).active_for_item(old.inventory_item_id):
                return {"ok": False, "reason": "inventory is reserved"}
            published = _aware(old.published_at) or _aware(old.created_at)
            if (utc_now() - published).days < rules.stale_after_days:
                return {"ok": False, "reason": "listing is not stale"}
            item = InventoryRepository(session).get(old.inventory_item_id)
            account = MarketplaceAccountRepository(session).require(old.account_id)
            item_id, account_id = old.inventory_item_id, old.account_id
            price = old.current_price or Decimal(0)
            relist_count = old.relist_count + 1
            title, description = item.title, item.description or ""
            currency = account.currency
        ended = await self._end_listing(listing_id, principal_scopes=principal_scopes, actor=actor)
        if not ended.ok or not ended.verified:
            return {"ok": False, "reason": "old listing could not be verified ended"}
        idem = hashlib.sha256(
            f"relist:{listing_id}:{relist_count}:{account_id}".encode()
        ).hexdigest()[:32]
        with self._session_factory() as session:
            replacement = RemoteListingRepository(session).create(
                inventory_item_id=item_id,
                account_id=account_id,
                idempotency_key=idem,
                status=RemoteListingStatus.PUBLISHING,
                current_price=price,
                automation_mode_used=self.get_account(account_id).automation_mode.value,
            )
            replacement.relist_count = relist_count
            replacement_id, version = replacement.id, replacement.local_version
            session.commit()
        request = ListingRequest(
            inventory_item_id=str(item_id),
            title=title,
            description=description,
            price=price,
            currency=currency,
            idempotency_key=idem,
        )
        receipt = await self._gateway.execute(
            account_id=account_id,
            operation="create_listing",
            scope="marketplace:listing:create",
            principal_scopes=principal_scopes,
            idempotency_key=idem,
            adapter_call=lambda adapter: adapter.create_listing(request),
            verify_call=lambda adapter: self._verify_listing_active(adapter, idem),
            agent_identity=actor,
            resource_type="remote_listing",
            resource_id=replacement_id,
        )
        with self._session_factory() as session:
            if receipt.ok and receipt.remote_id:
                RemoteListingRepository(session).apply_publish_result(
                    replacement_id,
                    expected_version=version,
                    remote_listing_id=receipt.remote_id,
                    remote_url=receipt.remote_url,
                    status=RemoteListingStatus.ACTIVE,
                    verified=receipt.verified,
                )
            else:
                RemoteListingRepository(session).set_status(
                    replacement_id,
                    status=RemoteListingStatus.FAILED,
                    error_category=receipt.error_category,
                )
            session.commit()
        if receipt.ok:
            self._audit(
                "relist.executed", "remote_listing", replacement_id, {"count": relist_count}
            )
        return {"ok": receipt.ok, "listing_id": str(replacement_id)}

    # ------------------------------------------------------------- shipping
    async def purchase_label(
        self, task_id: UUID, *, principal_scopes=None, actor="system:shipping"
    ):
        rules = self._marketplace.shipping
        with self._session_factory() as session:
            task = ShippingTaskRepository(session).get(task_id)
            if task is None:
                raise RecordNotFoundError(f"shipping task not found: {task_id}")
            order = MarketplaceOrderRepository(session).get(task.order_id)
            account_id = order.account_id
            remote_order_id = order.remote_order_id
            paid = order.status in {OrderStatus.PAID, OrderStatus.READY_TO_SHIP}
            weight = task.confirmed_weight_grams or task.estimated_weight_grams
            profile = task.package_profile or rules.default_package_profile
            dimensions = dict(task.package_dimensions or {})
            version = task.version
            has_label = task.label_reference is not None
            session.expunge(task)
        if has_label:
            return {"ok": True, "reason": "label already purchased"}
        if not rules.automatic_label_purchase:
            return {"ok": False, "reason": "automatic label purchase disabled"}
        if not paid:
            return {"ok": False, "reason": "order not paid"}
        if not profile or weight is None or weight <= 0:
            return await self._shipping_block(
                task_id, "complete positive package weight is required"
            )
        if not all(
            isinstance(dimensions.get(key), (int, float, Decimal))
            and Decimal(str(dimensions[key])) > 0
            for key in ("length", "width", "height")
        ):
            return await self._shipping_block(task_id, "positive package dimensions are required")
        confirmed_threshold_grams = Decimal(str(rules.require_confirmed_weight_above)) * Decimal(
            "453.59237"
        )
        if weight > confirmed_threshold_grams and task.confirmed_weight_grams is None:
            return {"ok": False, "reason": "confirmed packed weight required"}
        idem = self._idem("label", task_id, account_id)
        request = LabelRequest(
            remote_order_id=remote_order_id,
            package_profile=profile,
            weight_grams=weight,
            maximum_cost=rules.maximum_label_cost,
            idempotency_key=idem,
        )
        receipt = await self._gateway.execute(
            account_id=account_id,
            operation="purchase_label",
            scope="marketplace:label:purchase",
            principal_scopes=principal_scopes,
            idempotency_key=idem,
            adapter_call=lambda adapter, r=request: adapter.purchase_label(r),
            agent_identity=actor,
            resource_type="shipping_task",
            resource_id=task_id,
        )
        if not receipt.ok:
            return {"ok": False, "reason": receipt.reasons}
        cost = Decimal(str(receipt.data.get("cost", "0")))
        if cost > rules.maximum_label_cost:
            with self._session_factory() as session:
                ExceptionTaskRepository(session).create(
                    exception_type="label_cost_exceeded",
                    reason=f"label cost {cost} exceeds ceiling {rules.maximum_label_cost}",
                    severity="high",
                    resource_type="shipping_task",
                    resource_id=task_id,
                )
                session.commit()
            return {"ok": False, "reason": "label cost exceeds ceiling", "cost": str(cost)}
        with self._session_factory() as session:
            ShippingTaskRepository(session).update(
                task_id,
                expected_version=version,
                status=ShippingTaskStatus.LABEL_PURCHASED,
                label_cost=cost,
                label_reference=receipt.data.get("label_reference"),
                tracking_number=receipt.data.get("tracking_number"),
                carrier=receipt.data.get("carrier"),
            )
            session.commit()
        self._audit("label.purchased", "shipping_task", task_id, {"cost": str(cost)})
        return {"ok": True, "cost": str(cost), "tracking": receipt.data.get("tracking_number")}

    async def _shipping_block(self, task_id: UUID, reason: str) -> dict[str, Any]:
        with self._session_factory() as session:
            ExceptionTaskRepository(session).create(
                exception_type="shipping_data_incomplete",
                reason=reason,
                severity="high",
                resource_type="shipping_task",
                resource_id=task_id,
            )
            session.commit()
        return {"ok": False, "reason": reason, "error_category": "blocked"}

    async def update_tracking(
        self,
        order_id: UUID,
        tracking_number: str,
        carrier: str,
        *,
        principal_scopes=None,
        actor="system:shipping",
    ):
        if not tracking_number.strip() or not carrier.strip():
            raise MarketplaceServiceError("tracking number and carrier are required")
        with self._session_factory() as session:
            order = MarketplaceOrderRepository(session).get(order_id)
            if order is None:
                raise RecordNotFoundError(f"order not found: {order_id}")
            account_id, remote_order_id = order.account_id, order.remote_order_id
        idem = hashlib.sha256(
            f"tracking:{account_id}:{remote_order_id}:{tracking_number}:{carrier}".encode()
        ).hexdigest()[:32]
        receipt = await self._gateway.execute(
            account_id=account_id,
            operation="update_tracking",
            scope="marketplace:tracking:update",
            principal_scopes=principal_scopes,
            idempotency_key=idem,
            adapter_call=lambda adapter: adapter.update_tracking(
                remote_order_id, tracking_number, carrier
            ),
            verify_call=lambda adapter: self._verify_tracking(
                adapter, remote_order_id, tracking_number
            ),
            agent_identity=actor,
            resource_type="marketplace_order",
            resource_id=order_id,
            request_summary={"carrier": carrier},
        )
        if receipt.ok:
            self._audit("tracking.updated", "marketplace_order", order_id, {"carrier": carrier})
        return receipt

    @staticmethod
    async def _verify_tracking(adapter, remote_order_id: str, tracking_number: str) -> bool:
        result = await adapter.read_order(remote_order_id)
        return result.ok and result.data.get("tracking_number") == tracking_number

    # ------------------------------------------------------- reconciliation
    def reconcile_order(self, order_id: UUID, *, final: bool = False):
        from goliath.db.models import ReconciliationStatus

        with self._session_factory() as session:
            order = MarketplaceOrderRepository(session).get(order_id)
            if order is None:
                raise RecordNotFoundError(f"order not found: {order_id}")
            item = (
                InventoryRepository(session).get(order.inventory_item_id)
                if order.inventory_item_id
                else None
            )
            item_cost = item.acquisition_cost if item else Decimal(0)
            shipping_task = None
            for task in ShippingTaskRepository(session).list(limit=500):
                if task.order_id == order_id:
                    shipping_task = task
                    break
            shipping_expense = (
                shipping_task.label_cost
                if shipping_task and shipping_task.label_cost
                else Decimal(0)
            )
            gross = order.sale_price
            fees = order.marketplace_fees
            promotion = order.promotion_fees
            tax = order.tax_amount
            shipping_income = order.shipping_charged
            refund = order.refunded_amount
            payment_fee = _money(gross * Decimal("0.029") + Decimal("0.30"))

            net = _money(
                gross + shipping_income - fees - promotion - payment_fee - shipping_expense - refund
            )
            profit = _money(net - item_cost)
            margin = _money(profit / gross) if gross > 0 else Decimal(0)

            discrepancies: list[str] = []
            if fees == 0 and gross > 0:
                discrepancies.append("missing_marketplace_fees")
            if (
                shipping_task is not None
                and shipping_expense == 0
                and shipping_task.label_reference
            ):
                discrepancies.append("unexpected_missing_shipping_cost")
            if refund > gross:
                discrepancies.append("refund_exceeds_sale")

            status = (
                ReconciliationStatus.DISCREPANCY
                if discrepancies
                else ReconciliationStatus.FINAL
                if final
                else ReconciliationStatus.PRELIMINARY
            )
            record = ReconciliationRepository(session).create(
                order_id=order_id,
                status=status,
                gross_sale=gross,
                shipping_income=shipping_income,
                shipping_expense=shipping_expense,
                marketplace_fees=fees,
                payment_fees=payment_fee,
                promotion_fees=promotion,
                tax_amount=tax,
                item_cost=item_cost,
                refund_amount=refund,
                net_proceeds=net,
                realized_profit=profit,
                realized_margin=margin,
                discrepancies=discrepancies,
                breakdown={"payment_fee": str(payment_fee)},
            )
            if discrepancies:
                ExceptionTaskRepository(session).create(
                    exception_type="reconciliation_discrepancy",
                    reason="; ".join(discrepancies),
                    resource_type="marketplace_order",
                    resource_id=order_id,
                )
                AuditEventRepository(session).append(
                    event_type="discrepancy.detected",
                    actor_type="system",
                    actor_id="reconciler",
                    resource_type="marketplace_order",
                    resource_id=order_id,
                    details={"discrepancies": discrepancies},
                )
            AuditEventRepository(session).append(
                event_type="reconciliation.completed",
                actor_type="system",
                actor_id="reconciler",
                resource_type="marketplace_order",
                resource_id=order_id,
                details={"net_proceeds": str(net), "profit": str(profit)},
            )
            session.commit()
            session.refresh(record)
            session.expunge(record)
            return record

    def run_reconciliation(self):
        results = []
        with self._session_factory() as session:
            orders = list(
                MarketplaceOrderRepository(session).list(status=OrderStatus.PAID, limit=200)
            )
            order_ids = [o.id for o in orders]
        for order_id in order_ids:
            record = self.reconcile_order(order_id)
            results.append({"order_id": str(order_id), "status": record.status.value})
        return results

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _idem(operation: str, primary: UUID, secondary: UUID) -> str:
        return hashlib.sha256(f"{operation}:{primary}:{secondary}".encode()).hexdigest()[:32]

    def list_listings(self, *, status=None, limit=100, offset=0):
        with self._session_factory() as session:
            listings = list(
                RemoteListingRepository(session).list(status=status, limit=limit, offset=offset)
            )
            for listing in listings:
                session.expunge(listing)
            return listings

    def list_orders(self, *, status=None, limit=100, offset=0):
        with self._session_factory() as session:
            orders = list(
                MarketplaceOrderRepository(session).list(status=status, limit=limit, offset=offset)
            )
            for order in orders:
                session.expunge(order)
            return orders

    def list_offers(self, *, status=None, limit=100, offset=0):
        with self._session_factory() as session:
            offers = list(
                MarketplaceOfferRepository(session).list(status=status, limit=limit, offset=offset)
            )
            for offer in offers:
                session.expunge(offer)
            return offers

    def list_shipping_tasks(self, *, status=None, limit=100, offset=0):
        with self._session_factory() as session:
            tasks = list(
                ShippingTaskRepository(session).list(status=status, limit=limit, offset=offset)
            )
            for task in tasks:
                session.expunge(task)
            return tasks

    def list_conflicts(self):
        with self._session_factory() as session:
            conflicts = list(SyncConflictRepository(session).list_open())
            for conflict in conflicts:
                session.expunge(conflict)
            return conflicts

    def list_breakers(self):
        with self._session_factory() as session:
            breakers = list(CircuitBreakerRepository(session).list())
            for breaker in breakers:
                session.expunge(breaker)
            return breakers

    def reset_breaker(self, breaker_id: UUID):
        with self._session_factory() as session:
            breaker = CircuitBreakerRepository(session).reset(breaker_id)
            AuditEventRepository(session).append(
                event_type="breaker.reset",
                actor_type="human",
                actor_id="operator",
                resource_type="circuit_breaker",
                resource_id=breaker_id,
                details={},
            )
            session.commit()
            session.refresh(breaker)
            session.expunge(breaker)
            return breaker
