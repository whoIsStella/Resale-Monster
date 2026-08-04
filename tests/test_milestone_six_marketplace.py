"""Milestone six: autonomous marketplace operations."""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

from goliath.config import (
    MarketplaceAutomationConfig,
    OfferRules,
    OrchestrationConfig,
    PublishingRules,
    RefundRules,
    SessionEncryptionConfig,
)
from goliath.db.base import Base
from goliath.db.database import build_session_factory, create_test_engine
from goliath.db.domain_repositories import ListingDraftRepository
from goliath.db.marketplace_repositories import (
    MarketplaceAccountRepository,
    MarketplaceOfferRepository,
    MessageRepository,
    SessionReferenceRepository,
)
from goliath.db.models import (
    AuditEvent,
    DraftStatus,
    InventoryStatus,
    MarketplaceAccountStatus,
    MediaRole,
    OfferStatus,
    RemoteListingStatus,
    SessionReference,
)
from goliath.domain.pricing import ComparableObservation, PricingInputs
from goliath.domain.service import DomainService
from goliath.marketplace import policy as P
from goliath.marketplace.broker import SessionBroker
from goliath.marketplace.cipher import SessionCipher
from goliath.marketplace.fake_adapter import FakeMarketplaceAdapter
from goliath.marketplace.gateway import MarketplaceGateway
from goliath.marketplace.manual_adapter import ManualMarketplaceAdapter
from goliath.marketplace.service import MarketplaceService

CAPS = [
    "create_listing",
    "read_listings",
    "read_listing",
    "update_listing",
    "end_listing",
    "refresh_listing",
    "promote_listing",
    "read_orders",
    "read_order",
    "accept_offer",
    "decline_offer",
    "counter_offer",
    "read_offers",
    "read_messages",
    "send_message",
    "purchase_label",
]


@pytest.fixture
def env():
    work = Path(tempfile.mkdtemp())
    sess = Path(tempfile.mkdtemp()) / "sessions"
    engine = create_test_engine()
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    config = OrchestrationConfig(
        workspace_roots=[work],
        marketplace=MarketplaceAutomationConfig(
            session=SessionEncryptionConfig(
                session_root=sess, encryption_key=SessionCipher.generate_key()
            ),
            refunds=RefundRules(enabled=True),
        ),
    )
    config.domain.media.media_root = work / "m"
    config.domain.media.quarantine_root = work / "q"
    config.domain.media.temp_upload_root = work / "t"
    adapters: dict[str, FakeMarketplaceAdapter] = {}

    def factory_adapter(provisioning):
        return adapters.setdefault(
            provisioning.marketplace,
            FakeMarketplaceAdapter(marketplace=provisioning.marketplace, capabilities=set(CAPS)),
        )

    broker = SessionBroker(session_factory=factory, config=config, adapter_factory=factory_adapter)
    service = MarketplaceService(session_factory=factory, config=config, broker=broker)
    domain = DomainService(session_factory=factory, config=config)
    yield {
        "factory": factory,
        "config": config,
        "service": service,
        "domain": domain,
        "adapters": adapters,
        "broker": broker,
    }
    engine.dispose()


def _ready_item(env, sku="S1"):
    domain = env["domain"]
    item = domain.create_inventory(
        actor="human:op",
        sku=sku,
        title="Nike Tee",
        condition="good",
        acquisition_cost=Decimal(10),
        category="clothing",
        status="ready_for_listing",
    )
    draft = domain.create_master_draft(item.id, actor="agent:codex")
    with env["factory"]() as session:
        ListingDraftRepository(session).set_status(
            draft.id, status=DraftStatus.APPROVED, created_by="human:op"
        )
        session.commit()
    domain.create_pricing_recommendation(
        item.id,
        actor="human:op",
        inputs=PricingInputs(
            cost_basis=Decimal(10),
            comparables=[ComparableObservation(price=Decimal(40), is_sold=True)],
        ),
    )
    domain.add_media(
        item.id,
        actor="human:op",
        media_type="image/jpeg",
        checksum="c1",
        file_size=100,
        role=MediaRole.ORIGINAL,
        storage_path="a.jpg",
        file_identifier="f1",
    )
    return item


def _account(env, marketplace, label, mode="autonomous_normal"):
    service = env["service"]
    account = service.create_account(marketplace=marketplace, label=label, capabilities=CAPS)
    service.authenticate_account(account.id, b"{}")
    service.set_mode(account.id, mode=mode)
    with env["factory"]() as session:
        MarketplaceAccountRepository(session).set_status(
            account.id, status=MarketplaceAccountStatus.HEALTHY
        )
        session.commit()
    return account


# ----------------------------------------------------------------- adapters


def test_fake_adapter_declares_capabilities() -> None:
    adapter = FakeMarketplaceAdapter(capabilities={"create_listing", "read_listing"})
    assert adapter.capabilities() == {"create_listing", "read_listing"}


def test_adapter_protocol_exposes_every_typed_operation() -> None:
    from goliath.marketplace.adapter import MARKETPLACE_OPERATIONS, MarketplaceAdapter

    assert set(MARKETPLACE_OPERATIONS) <= set(MarketplaceAdapter.__dict__)


def test_manual_adapter_is_read_and_export_only() -> None:
    adapter = ManualMarketplaceAdapter()
    assert "create_listing" in adapter.capabilities()
    assert not hasattr(adapter, "cookies")


# --------------------------------------------------------------- session broker


def test_session_state_encrypted_and_never_exposed(env) -> None:
    account = _account(env, "ebay", "ebay-main", mode="disabled")
    env["service"].authenticate_account(account.id, b'{"cookies":[{"name":"AUTHSECRET"}]}')
    with env["factory"]() as session:
        reference = session.query(SessionReference).first()
        audits = " ".join(str(e.details) for e in session.query(AuditEvent).all())
    assert "AUTHSECRET" not in reference.ciphertext
    assert "AUTHSECRET" not in audits
    # The adapter the broker returns exposes no cookie/browser access.
    adapter = env["broker"].acquire_adapter(account.id)
    assert not hasattr(adapter, "cookies")
    assert not hasattr(adapter, "storage_state")


def test_session_directory_is_outside_workspace_and_restricted(env) -> None:
    account = _account(env, "ebay", "ebay-main", mode="disabled")
    with env["factory"]() as session:
        reference = SessionReferenceRepository(session).get_active(account.id)
    storage = Path(reference.storage_dir)
    work = env["config"].workspace_roots[0]
    assert not storage.is_relative_to(work)
    assert oct(storage.stat().st_mode)[-3:] == "700"


def test_config_rejects_session_root_inside_workspace() -> None:
    work = Path(tempfile.mkdtemp())
    with pytest.raises(ValueError, match="outside all agent workspaces"):
        OrchestrationConfig(
            workspace_roots=[work],
            marketplace=MarketplaceAutomationConfig(
                session=SessionEncryptionConfig(session_root=work / "sessions")
            ),
        )


def test_config_rejects_unencrypted_sessions() -> None:
    with pytest.raises(ValueError, match="encrypted"):
        SessionEncryptionConfig(require_encryption=False)


def test_marketplace_domain_allowlist_rejects_cross_market_domain() -> None:
    from goliath.config import MarketplaceAccountConfig

    with pytest.raises(ValueError, match="outside the ebay allowlist"):
        MarketplaceAccountConfig(marketplace="ebay", label="main", allowed_domains=["facebook.com"])


# --------------------------------------------------------------- automation mode


async def test_disabled_mode_blocks_writes(env) -> None:
    item = _ready_item(env)
    account = _account(env, "ebay", "ebay-main", mode="disabled")
    result = await env["service"].publish_item(
        item.id, account_ids=[account.id], principal_scopes={"admin"}
    )
    assert result["results"]["ebay-main"] == "blocked"


async def test_shadow_mode_computes_without_writing(env) -> None:
    item = _ready_item(env)
    account = _account(env, "ebay", "ebay-main", mode="shadow")
    await env["service"].publish_item(item.id, account_ids=[account.id], principal_scopes={"admin"})
    # Shadow: the listing was created locally but the remote write was not executed.
    assert env["service"].metrics.shadow_writes >= 1
    listings = env["service"].list_listings()
    assert all(x.remote_listing_id is None for x in listings)


# ------------------------------------------------------------------ publishing


async def test_publish_and_cross_post_with_verification(env) -> None:
    item = _ready_item(env)
    _account(env, "ebay", "ebay-main")
    _account(env, "poshmark", "poshmark-main")
    result = await env["service"].publish_item(item.id, principal_scopes={"admin"})
    assert result["results"] == {"ebay-main": "published", "poshmark-main": "published"}
    listings = env["service"].list_listings()
    assert len(listings) == 2
    assert all(
        x.status is RemoteListingStatus.ACTIVE and x.last_verified_action == "publish"
        for x in listings
    )
    assert env["domain"].get_inventory(item.id).status is InventoryStatus.LISTED


async def test_publish_is_idempotent_and_prevents_duplicates(env) -> None:
    item = _ready_item(env)
    _account(env, "ebay", "ebay-main")
    first = await env["service"].publish_item(item.id, principal_scopes={"admin"})
    second = await env["service"].publish_item(item.id, principal_scopes={"admin"})
    assert first["results"]["ebay-main"] == "published"
    assert second["results"]["ebay-main"] == "blocked"  # duplicate active listing
    assert (
        len([x for x in env["service"].list_listings() if x.status is RemoteListingStatus.ACTIVE])
        == 1
    )


async def test_publish_blocks_on_insufficient_profit(env) -> None:
    domain = env["domain"]
    item = domain.create_inventory(
        actor="human:op",
        sku="P0",
        title="Tee",
        condition="good",
        acquisition_cost=Decimal(100),
        category="clothing",
        status="ready_for_listing",
    )
    draft = domain.create_master_draft(item.id, actor="agent:codex")
    with env["factory"]() as session:
        ListingDraftRepository(session).set_status(
            draft.id, status=DraftStatus.APPROVED, created_by="human:op"
        )
        session.commit()
    # Pricing recommendation with negative/low profit.
    domain.create_pricing_recommendation(
        item.id,
        actor="human:op",
        inputs=PricingInputs(
            cost_basis=Decimal(100),
            comparables=[ComparableObservation(price=Decimal(101), is_sold=True)],
        ),
    )
    domain.add_media(
        item.id,
        actor="human:op",
        media_type="image/jpeg",
        checksum="c9",
        file_size=1,
        role=MediaRole.ORIGINAL,
        storage_path="x.jpg",
        file_identifier="f9",
    )
    account = _account(env, "ebay", "ebay-main")
    env["config"].marketplace.publishing = PublishingRules(minimum_expected_profit=Decimal(50))
    result = await env["service"].publish_item(
        item.id, account_ids=[account.id], principal_scopes={"admin"}
    )
    assert result["results"]["ebay-main"] == "blocked"


# ----------------------------------------------------------- refresh/engagement


async def test_refresh_listing_verifies(env) -> None:
    item = _ready_item(env)
    _account(env, "ebay", "ebay-main")
    await env["service"].publish_item(item.id, principal_scopes={"admin"})
    listing = env["service"].list_listings()[0]
    receipt = await env["service"].refresh_listing(listing.id, principal_scopes={"admin"})
    assert receipt.ok


# --------------------------------------------------- sale detection + delisting


async def test_sale_detection_reserves_and_delists_everywhere(env) -> None:
    item = _ready_item(env)
    a1 = _account(env, "ebay", "ebay-main")
    _account(env, "poshmark", "poshmark-main")
    await env["service"].publish_item(item.id, principal_scopes={"admin"})
    active = [x for x in env["service"].list_listings() if x.status is RemoteListingStatus.ACTIVE]
    assert len(active) == 2
    env["adapters"]["ebay"].seed_order(
        "O-1",
        sale_price="40",
        status="paid",
        inventory_item_id=str(item.id),
        remote_listing_id=active[0].remote_listing_id,
        buyer="b1",
        marketplace_fees="4.00",
    )
    sync = await env["service"].sync_orders(a1.id, principal_scopes={"admin"})
    assert sync["orders_detected"] == 1
    # Inventory sold, all remote listings ended.
    assert env["domain"].get_inventory(item.id).status is InventoryStatus.SOLD
    ended = [x for x in env["service"].list_listings() if x.status is RemoteListingStatus.ENDED]
    assert len(ended) == 2


async def test_delisting_failure_opens_exception_and_breaker(env) -> None:
    item = _ready_item(env)
    a1 = _account(env, "ebay", "ebay-main")
    await env["service"].publish_item(item.id, principal_scopes={"admin"})
    active = [x for x in env["service"].list_listings() if x.status is RemoteListingStatus.ACTIVE]
    # Make end_listing fail on the ebay adapter.
    env["adapters"]["ebay"]._fail_operations = {"end_listing"}
    env["adapters"]["ebay"].seed_order(
        "O-2",
        sale_price="40",
        status="paid",
        inventory_item_id=str(item.id),
        remote_listing_id=active[0].remote_listing_id,
        buyer="b2",
    )
    await env["service"].sync_orders(a1.id, principal_scopes={"admin"})
    from goliath.db.marketplace_repositories import ExceptionTaskRepository

    with env["factory"]() as session:
        exceptions = ExceptionTaskRepository(session).list()
    assert any(e.exception_type == "duplicate_sale_risk" for e in exceptions)


# ------------------------------------------------------------------ offers


async def test_offer_accept_and_min_profit(env) -> None:
    item = _ready_item(env)
    account = _account(env, "ebay", "ebay-main")
    with env["factory"]() as session:
        offer = MarketplaceOfferRepository(session).upsert(
            account_id=account.id,
            remote_offer_id="OF-1",
            offer_amount=Decimal(60),
            list_price=Decimal(80),
            inventory_item_id=item.id,
        )
        offer_id = offer.id
        session.commit()
    result = await env["service"].handle_offer(offer_id, principal_scopes={"admin"})
    assert result["decision"] == "accept"
    with env["factory"]() as session:
        offer = MarketplaceOfferRepository(session).get(offer_id)
    assert offer.status is OfferStatus.ACCEPTED


async def test_offer_low_is_countered(env) -> None:
    item = _ready_item(env)
    account = _account(env, "ebay", "ebay-main")
    with env["factory"]() as session:
        offer = MarketplaceOfferRepository(session).upsert(
            account_id=account.id,
            remote_offer_id="OF-2",
            offer_amount=Decimal(15),
            list_price=Decimal(80),
            inventory_item_id=item.id,
        )
        offer_id = offer.id
        session.commit()
    result = await env["service"].handle_offer(offer_id, principal_scopes={"admin"})
    assert result["decision"] == "counter"
    assert Decimal(result["counter_amount"]) > Decimal(15)


async def test_requested_offer_action_cannot_be_substituted(env) -> None:
    item = _ready_item(env)
    account = _account(env, "ebay", "ebay-main")
    with env["factory"]() as session:
        offer = MarketplaceOfferRepository(session).upsert(
            account_id=account.id,
            remote_offer_id="OF-EXACT",
            offer_amount=Decimal(15),
            list_price=Decimal(80),
            inventory_item_id=item.id,
        )
        offer_id = offer.id
        session.commit()
    result = await env["service"].handle_offer(
        offer_id, requested_action="accept", principal_scopes={"admin"}
    )
    assert result["ok"] is False
    assert result["requested_action"] == "accept"
    assert result["executed_action"] is None


async def test_counter_action_requires_policy_counter_amount(env) -> None:
    item = _ready_item(env)
    account = _account(env, "ebay", "ebay-main")
    with env["factory"]() as session:
        offer = MarketplaceOfferRepository(session).upsert(
            account_id=account.id,
            remote_offer_id="OF-COUNTER-EXACT",
            offer_amount=Decimal(15),
            list_price=Decimal(80),
            inventory_item_id=item.id,
        )
        offer_id = offer.id
        session.commit()
    result = await env["service"].handle_offer(
        offer_id,
        requested_action="counter",
        requested_counter_amount=Decimal(16),
        principal_scopes={"admin"},
    )
    assert result["ok"] is False
    assert result["executed_action"] is None


def test_offer_policy_minimum_proceeds_is_authoritative() -> None:
    # A very low offer can never be auto-accepted regardless of flags.
    outcome = P.evaluate_offer(
        P.OfferInputs(offer_amount=Decimal(5), list_price=Decimal(80), cost_basis=Decimal(20)),
        OfferRules(),
    )
    assert outcome.decision != "accept"


# ------------------------------------------------------------------ messaging


async def test_routine_message_answered(env) -> None:
    account = _account(env, "ebay", "ebay-main")
    with env["factory"]() as session:
        thread = MessageRepository(session).upsert_thread(
            account_id=account.id, remote_thread_id="T-1"
        )
        thread_id = thread.id
        session.commit()
    result = await env["service"].respond_message(
        thread_id, "Is this still available?", principal_scopes={"admin"}
    )
    assert result["decision"] == "respond"
    assert result["category"] == "availability"


async def test_sensitive_message_escalates(env) -> None:
    account = _account(env, "ebay", "ebay-main")
    with env["factory"]() as session:
        thread = MessageRepository(session).upsert_thread(
            account_id=account.id, remote_thread_id="T-2"
        )
        thread_id = thread.id
        session.commit()
    result = await env["service"].respond_message(
        thread_id, "This is a counterfeit, I'm calling my lawyer", principal_scopes={"admin"}
    )
    assert result["decision"] == "escalate"


async def test_scheduled_message_sync_responds_once_without_storing_plaintext(env) -> None:
    account = _account(env, "ebay", "message-scheduled")
    adapter = env["broker"].acquire_adapter(account.id)
    secret_body = "Is this still available? private-value-123"
    adapter.seed_message("TH-SCHEDULED", secret_body, buyer="buyer@example.test")
    first = await env["service"].process_routine_messages(account.id, principal_scopes={"admin"})
    second = await env["service"].process_routine_messages(account.id, principal_scopes={"admin"})
    assert first["processed"] == 1
    assert second["processed"] == 0
    assert adapter._sent_messages["TH-SCHEDULED"] == "Yes, this item is still available."
    from goliath.db.models import MessageRecord

    with env["factory"]() as session:
        records = list(session.query(MessageRecord).all())
    assert records
    assert all(secret_body not in record.body_checksum for record in records)


async def test_scheduled_offer_sync_is_idempotent(env) -> None:
    account = _account(env, "ebay", "offer-scheduled")
    env["broker"].acquire_adapter(account.id).seed_offer(
        "OF-SCHEDULED", offer_amount="45", list_price="60"
    )
    first = await env["service"].sync_offers(account.id, principal_scopes={"admin"})
    second = await env["service"].sync_offers(account.id, principal_scopes={"admin"})
    assert first["synced"] == second["synced"] == 1
    assert len(env["service"].list_offers()) == 1


# ------------------------------------------------------------ price automation


async def test_price_markdown_and_cooldown(env) -> None:
    item = _ready_item(env)
    _account(env, "ebay", "ebay-main")
    await env["service"].publish_item(item.id, principal_scopes={"admin"})
    listing = env["service"].list_listings()[0]
    # Force the listing to look stale by backdating published_at.
    from datetime import UTC, datetime, timedelta

    from goliath.db.marketplace_repositories import RemoteListingRepository

    with env["factory"]() as session:
        row = RemoteListingRepository(session).get(listing.id)
        row.published_at = datetime.now(UTC) - timedelta(days=40)
        session.commit()
    result = await env["service"].run_price_automation(listing.id, principal_scopes={"admin"})
    assert result["decision"] == "markdown"
    # A second immediate run is within cooldown (refreshed_at just set).
    second = await env["service"].run_price_automation(listing.id, principal_scopes={"admin"})
    assert second["decision"] == "hold"


# ------------------------------------------------------------------ shipping


async def test_label_purchase_and_ceiling(env) -> None:
    item = _ready_item(env)
    a1 = _account(env, "ebay", "ebay-main")
    await env["service"].publish_item(item.id, principal_scopes={"admin"})
    active = [x for x in env["service"].list_listings() if x.status is RemoteListingStatus.ACTIVE]
    env["adapters"]["ebay"].seed_order(
        "O-3",
        sale_price="40",
        status="paid",
        inventory_item_id=str(item.id),
        remote_listing_id=active[0].remote_listing_id,
        buyer="b3",
    )
    await env["service"].sync_orders(a1.id, principal_scopes={"admin"})
    from goliath.db.marketplace_repositories import ShippingTaskRepository

    with env["factory"]() as session:
        task = ShippingTaskRepository(session).list()[0]
        task.estimated_weight_grams = Decimal(250)
        task.package_dimensions = {"length": 12, "width": 9, "height": 2}
        task_id = task.id
        session.commit()
    result = await env["service"].purchase_label(task_id, principal_scopes={"admin"})
    assert result["ok"] is True
    assert "tracking" in result


async def test_label_ceiling_is_enforced_inside_adapter_before_purchase() -> None:
    from goliath.marketplace.adapter import LabelRequest

    adapter = FakeMarketplaceAdapter()
    result = await adapter.purchase_label(
        LabelRequest(
            remote_order_id="O-1",
            package_profile="box",
            weight_grams=Decimal(100),
            maximum_cost=Decimal("5.00"),
            idempotency_key="label-1",
        )
    )
    assert result.ok is False
    assert adapter._labels == {}


# ------------------------------------------------------------ reconciliation


def test_reconciliation_arithmetic_and_discrepancy(env) -> None:
    from goliath.db.marketplace_repositories import MarketplaceOrderRepository
    from goliath.db.models import OrderStatus

    account = _account(env, "ebay", "ebay-main")
    with env["factory"]() as session:
        order, _ = MarketplaceOrderRepository(session).upsert(
            account_id=account.id,
            remote_order_id="R-1",
            sale_price=Decimal(40),
            status=OrderStatus.PAID,
            payload_checksum="c1",
            marketplace_fees=Decimal(4),
            shipping_charged=Decimal(5),
        )
        order_id = order.id
        session.commit()
    record = env["service"].reconcile_order(order_id)
    # gross 40 + shipping 5 - fees 4 - payment(0.029*40+0.30=1.46) = 39.54, profit = 39.54
    assert record.net_proceeds == Decimal("39.54")
    assert record.realized_profit == Decimal("39.54")
    assert record.discrepancies == []


def test_reconciliation_detects_missing_fees(env) -> None:
    from goliath.db.marketplace_repositories import MarketplaceOrderRepository
    from goliath.db.models import OrderStatus

    account = _account(env, "ebay", "ebay-main")
    with env["factory"]() as session:
        order, _ = MarketplaceOrderRepository(session).upsert(
            account_id=account.id,
            remote_order_id="R-2",
            sale_price=Decimal(40),
            status=OrderStatus.PAID,
            payload_checksum="c2",
            marketplace_fees=Decimal(0),
        )
        order_id = order.id
        session.commit()
    record = env["service"].reconcile_order(order_id)
    assert "missing_marketplace_fees" in record.discrepancies


# ----------------------------------------------------- breakers + emergency stop


async def test_emergency_stop_blocks_writes(env) -> None:
    item = _ready_item(env)
    account = _account(env, "ebay", "ebay-main")
    env["service"].emergency_stop(reason="account challenge")
    result = await env["service"].publish_item(
        item.id, account_ids=[account.id], principal_scopes={"admin"}
    )
    assert result["results"]["ebay-main"] == "blocked"
    env["service"].emergency_start(reason="resolved")
    result2 = await env["service"].publish_item(
        item.id, account_ids=[account.id], principal_scopes={"admin"}
    )
    assert result2["results"]["ebay-main"] == "published"


async def test_circuit_breaker_opens_after_failures(env) -> None:
    item = _ready_item(env)
    account = _account(env, "ebay", "ebay-main")
    env["broker"].acquire_adapter(account.id)  # force lazy adapter creation
    env["adapters"]["ebay"]._fail_operations = {"create_listing"}
    env["config"].marketplace.circuit_breaker.failure_threshold = 2
    for _ in range(3):
        await env["service"].publish_item(
            item.id, account_ids=[account.id], principal_scopes={"admin"}
        )
    breakers = env["service"].list_breakers()
    assert any(b.state.value == "open" for b in breakers)


def test_scope_enforcement_blocks_unauthorized_agent(env) -> None:
    import asyncio

    item = _ready_item(env)
    account = _account(env, "ebay", "ebay-main")
    # An agent lacking marketplace:listing:create cannot publish.
    result = asyncio.run(
        env["service"].publish_item(
            item.id, account_ids=[account.id], principal_scopes={"marketplace:read"}
        )
    )
    assert result["results"]["ebay-main"] in {"failed", "blocked"}


def test_account_scoped_marketplace_authority(env) -> None:
    first = _account(env, "ebay", "ebay-main", mode="observe")
    second = _account(env, "poshmark", "poshmark-main", mode="observe")
    gateway = MarketplaceGateway(
        session_factory=env["factory"], config=env["config"], broker=env["broker"]
    )
    assert gateway._scope_ok("marketplace:read", {f"marketplace:read@{first.id}"}, first.id)
    assert not gateway._scope_ok("marketplace:read", {f"marketplace:read@{first.id}"}, second.id)
