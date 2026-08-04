"""Milestone six: marketplace API endpoints, scopes, CLI, and metrics."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from goliath import cli
from goliath.agents.registry import AgentRegistry
from goliath.api import create_app
from goliath.config import (
    MarketplaceAutomationConfig,
    OrchestrationConfig,
    SessionEncryptionConfig,
)
from goliath.db.base import Base
from goliath.db.database import build_session_factory, create_test_engine
from goliath.db.operations import AuthRepository
from goliath.marketplace.broker import SessionBroker
from goliath.marketplace.cipher import SessionCipher
from goliath.marketplace.fake_adapter import FakeMarketplaceAdapter
from goliath.marketplace.service import MarketplaceService
from goliath.orchestration.service import JobOrchestrationService
from goliath.orchestration.uow import SqlAlchemyJobUnitOfWork
from tests.test_orchestration import FakeAdapter

runner = CliRunner()


@pytest.fixture
def stack():
    work = Path(tempfile.mkdtemp())
    sess = Path(tempfile.mkdtemp()) / "sessions"
    engine = create_test_engine()
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    config = OrchestrationConfig(
        workspace_roots=[work],
        api_authentication_required=True,
        metrics_enabled=True,
        marketplace=MarketplaceAutomationConfig(
            session=SessionEncryptionConfig(
                session_root=sess, encryption_key=SessionCipher.generate_key()
            )
        ),
    )
    config.domain.media.media_root = work / "m"
    config.domain.media.quarantine_root = work / "q"
    config.domain.media.temp_upload_root = work / "t"
    with factory() as session:
        _, _, admin = AuthRepository(session).create_key("operator", ["admin"])
        _, _, reader = AuthRepository(session).create_key("reader", ["marketplace:read"])
        session.commit()
    broker = SessionBroker(
        session_factory=factory, config=config,
        adapter_factory=lambda p: FakeMarketplaceAdapter(marketplace=p.marketplace),
    )
    marketplace_service = MarketplaceService(
        session_factory=factory, config=config, broker=broker
    )
    registry = AgentRegistry()
    registry.register("fake", FakeAdapter())
    orchestration = JobOrchestrationService(
        uow_factory=lambda: SqlAlchemyJobUnitOfWork(factory), registry=registry, config=config
    )
    app = create_app(
        service=orchestration, registry=registry, session_factory=factory, config=config,
        marketplace_service=marketplace_service,
    )
    with TestClient(app) as test_client:
        yield test_client, admin, reader, factory, config, marketplace_service
    engine.dispose()


def _h(key):
    return {"Authorization": f"Bearer {key}"}


def test_automation_requires_admin_to_stop(stack) -> None:
    api, admin, reader, *_ = stack
    assert api.post("/automation/stop", json={"reason": "x"}, headers=_h(reader)).status_code == 403
    ok = api.post("/automation/stop", json={"reason": "challenge"}, headers=_h(admin))
    assert ok.status_code == 200
    assert api.get("/automation/status", headers=_h(reader)).json()["emergency_stopped"] is True
    api.post("/automation/start", json={"reason": "resolved"}, headers=_h(admin))


def test_account_lifecycle_and_mode(stack) -> None:
    api, admin, _reader, *_ = stack
    created = api.post(
        "/marketplace-accounts",
        json={"marketplace": "ebay", "label": "ebay-main", "capabilities": ["read_orders"]},
        headers=_h(admin),
    )
    assert created.status_code == 201
    account_id = created.json()["id"]
    listing = api.get("/marketplace-accounts", headers=_h(admin))
    assert listing.status_code == 200
    mode = api.post(
        f"/marketplace-accounts/{account_id}/mode",
        json={"mode": "observe"},
        headers=_h(admin),
    )
    assert mode.json()["mode"] == "observe"


def test_marketplace_read_scope_separation(stack) -> None:
    api, _admin, reader, *_ = stack
    # reader has marketplace:read but not admin: can list, cannot create.
    assert api.get("/marketplace-listings", headers=_h(reader)).status_code == 200
    assert api.post(
        "/marketplace-accounts",
        json={"marketplace": "ebay", "label": "x", "capabilities": []},
        headers=_h(reader),
    ).status_code == 403


def test_circuit_breaker_endpoints(stack) -> None:
    api, admin, *_ = stack
    assert api.get("/circuit-breakers", headers=_h(admin)).status_code == 200


def test_metrics_includes_marketplace_series(stack) -> None:
    api, admin, *_ = stack
    body = api.get("/metrics", headers=_h(admin)).text
    assert "goliath_remote_listings{" in body
    assert "goliath_marketplace_writes" in body
    assert "goliath_open_circuit_breakers" in body


def test_cli_automation_and_account(stack, monkeypatch) -> None:
    _api, _admin, _reader, _factory, _config, marketplace_service = stack
    monkeypatch.setattr(cli, "build_marketplace_service", lambda: marketplace_service)
    status = runner.invoke(cli.app, ["automation", "status", "--json"])
    assert status.exit_code == 0
    stopped = runner.invoke(cli.app, ["automation", "stop", "--reason", "test", "--json"])
    assert stopped.exit_code == 0
    assert json.loads(stopped.output)["active"] is True
    runner.invoke(cli.app, ["automation", "start", "--reason", "done"])
    marketplace_service.create_account(marketplace="ebay", label="ebay-main")
    accounts = runner.invoke(cli.app, ["marketplace", "account-list", "--json"])
    assert accounts.exit_code == 0
    assert json.loads(accounts.output)[0]["marketplace"] == "ebay"


def test_cli_expected_error_no_traceback(stack, monkeypatch) -> None:
    from uuid import uuid4

    _api, _admin, _reader, _factory, _config, marketplace_service = stack
    monkeypatch.setattr(cli, "build_marketplace_service", lambda: marketplace_service)
    result = runner.invoke(cli.app, ["breaker", "reset", str(uuid4())])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
