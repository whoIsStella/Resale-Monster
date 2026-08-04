from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from goliath.db.database import (
    build_session_factory,
    create_production_engine,
    create_test_engine,
    session_scope,
)
from goliath.db.models import InventoryCondition
from goliath.db.repositories import InventoryRepository


def test_production_engine_requires_postgresql(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOLIATH_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="must be set"):
        create_production_engine()
    with pytest.raises(ValueError, match="PostgreSQL"):
        create_production_engine("sqlite+pysqlite:///:memory:")


def test_test_engine_requires_sqlite() -> None:
    with pytest.raises(ValueError, match="SQLite"):
        create_test_engine("postgresql+psycopg://localhost/example")


def test_session_scope_commits() -> None:
    engine = create_test_engine()
    from goliath.db.base import Base

    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with session_scope(factory) as session:
        item = InventoryRepository(session).create(
            sku="SCOPE-1",
            title="Scoped item",
            condition=InventoryCondition.GOOD,
            acquisition_cost=10,
        )
        item_id = item.id
    with factory() as session:
        assert InventoryRepository(session).get(item_id) is not None
    engine.dispose()


def test_initial_migration_upgrades_and_downgrades_sqlite(tmp_path: Path) -> None:
    database_path = tmp_path / "migration.sqlite3"
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database_path}")

    command.upgrade(config, "head")
    engine = create_test_engine(f"sqlite+pysqlite:///{database_path}")
    assert set(inspect(engine).get_table_names()) == {
        "agent_job_records",
        "alembic_version",
        "approval_requests",
        "audit_events",
        "inventory_items",
        "marketplace_listings",
        "worker_records",
        "job_leases",
        "execution_attempts",
        "schedules",
        "api_principals",
        "api_keys",
        "idempotency_records",
        "inventory_media",
        "measurements",
        "research_records",
        "research_sources",
        "identification_candidates",
        "comparable_sales",
        "pricing_recommendations",
        "master_listing_drafts",
        "listing_draft_versions",
        "marketplace_draft_variants",
        "domain_proposals",
        "proposal_evidence",
        "category_completeness_rules",
        "marketplace_constraint_versions",
        "mcp_service_principals",
        "media_derivations",
        "media_processing_jobs",
        "media_processing_attempts",
        "image_quality_results",
        "perceptual_hashes",
        "comparable_imports",
        "comparable_import_rows",
        "comparable_review_decisions",
        "pricing_source_policies",
        "review_tasks",
        "review_task_events",
        "marketplace_accounts",
        "session_references",
        "remote_listings",
        "marketplace_operation_attempts",
        "marketplace_orders",
        "marketplace_offers",
        "message_threads",
        "message_records",
        "inventory_reservations",
        "shipping_tasks",
        "reconciliation_records",
        "automation_policy_versions",
        "policy_decisions",
        "circuit_breakers",
        "emergency_stop_state",
        "webhook_events",
        "synchronization_conflicts",
        "exception_tasks",
    }
    command.check(config)

    command.downgrade(config, "base")
    assert inspect(engine).get_table_names() == ["alembic_version"]
    engine.dispose()
