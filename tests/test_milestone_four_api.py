"""Milestone four: authenticated domain API endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from goliath.agents.registry import AgentRegistry
from goliath.api import create_app
from goliath.config import OrchestrationConfig
from goliath.db.base import Base
from goliath.db.database import build_session_factory, create_test_engine
from goliath.db.operations import AuthRepository
from goliath.orchestration.service import JobOrchestrationService
from goliath.orchestration.uow import SqlAlchemyJobUnitOfWork
from tests.test_orchestration import FakeAdapter


@pytest.fixture
def client(tmp_path: Path):
    engine = create_test_engine()
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    media_root = tmp_path / "media"
    media_root.mkdir()
    with factory() as session:
        _, _, admin = AuthRepository(session).create_key("operator", ["admin"])
        _, _, reader = AuthRepository(session).create_key("reader", ["inventory:read"])
        session.commit()
    registry = AgentRegistry()
    registry.register("fake", FakeAdapter())
    config = OrchestrationConfig(
        workspace_roots=[tmp_path], api_authentication_required=True, metrics_enabled=True
    )
    config.domain.media_storage_root = media_root
    service = JobOrchestrationService(
        uow_factory=lambda: SqlAlchemyJobUnitOfWork(factory), registry=registry, config=config
    )
    app = create_app(service=service, registry=registry, session_factory=factory, config=config)
    yield TestClient(app), admin, reader
    engine.dispose()


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def test_inventory_requires_authentication(client) -> None:
    api, _, _ = client
    assert api.get("/inventory").status_code == 401


def test_inventory_scope_enforced(client) -> None:
    api, _, reader = client
    # reader lacks inventory:write_draft
    resp = api.post(
        "/inventory",
        json={"sku": "S1", "title": "Tee", "acquisition_cost": 5},
        headers=_auth(reader),
    )
    assert resp.status_code == 403


def test_inventory_crud_and_pagination(client) -> None:
    api, admin, _ = client
    created = api.post(
        "/inventory",
        json={"sku": "S1", "title": "Tee", "acquisition_cost": 5, "category": "clothing"},
        headers=_auth(admin),
    )
    assert created.status_code == 201
    item_id = created.json()["id"]
    assert api.get(f"/inventory/{item_id}", headers=_auth(admin)).json()["sku"] == "S1"

    api.post(
        "/inventory",
        json={"sku": "S2", "title": "Jeans", "acquisition_cost": 8},
        headers=_auth(admin),
    )
    page = api.get("/inventory?limit=1&offset=0", headers=_auth(admin))
    assert page.status_code == 200
    assert len(page.json()) == 1
    assert api.get("/inventory?limit=0", headers=_auth(admin)).status_code == 422


def test_inventory_draft_optimistic_version(client) -> None:
    api, admin, _ = client
    item_id = api.post(
        "/inventory",
        json={"sku": "S3", "title": "Tee", "acquisition_cost": 5},
        headers=_auth(admin),
    ).json()["id"]
    ok = api.patch(
        f"/inventory/{item_id}/draft",
        json={"expected_version": 1, "changes": {"brand": "Nike"}},
        headers=_auth(admin),
    )
    assert ok.status_code == 200
    assert ok.json()["version"] == 2
    stale = api.patch(
        f"/inventory/{item_id}/draft",
        json={"expected_version": 1, "changes": {"brand": "Other"}},
        headers=_auth(admin),
    )
    assert stale.status_code == 409


def test_inventory_idempotent_creation(client) -> None:
    api, admin, _ = client
    headers = {**_auth(admin), "Idempotency-Key": "same"}
    payload = {"sku": "S4", "title": "Tee", "acquisition_cost": 5}
    first = api.post("/inventory", json=payload, headers=headers)
    second = api.post("/inventory", json=payload, headers=headers)
    assert first.json()["id"] == second.json()["id"]
    conflict = api.post(
        "/inventory", json={**payload, "title": "Different"}, headers=headers
    )
    assert conflict.status_code == 409


def test_pricing_endpoint(client) -> None:
    api, admin, _ = client
    item_id = api.post(
        "/inventory",
        json={"sku": "S5", "title": "Tee", "acquisition_cost": 5},
        headers=_auth(admin),
    ).json()["id"]
    resp = api.post(
        "/pricing/calculate",
        json={"item_id": item_id, "cost_basis": 10, "comparables": [{"price": 40, "is_sold": True}]},
        headers=_auth(admin),
    )
    assert resp.status_code == 200
    assert "recommended_price" in resp.json()


def test_listing_and_approval_flow(client) -> None:
    api, admin, _ = client
    item_id = api.post(
        "/inventory",
        json={"sku": "S6", "title": "Nike Tee", "acquisition_cost": 5, "category": "clothing"},
        headers=_auth(admin),
    ).json()["id"]
    draft = api.post("/listing-drafts", json={"item_id": item_id}, headers=_auth(admin))
    assert draft.status_code == 201
    draft_id = draft.json()["id"]
    assert api.post(f"/listing-drafts/{draft_id}/validate", headers=_auth(admin)).status_code == 200
    proposal = api.post(
        f"/listing-drafts/{draft_id}/request-approval", headers=_auth(admin)
    )
    assert proposal.status_code == 201
    approval_id = proposal.json()["id"]
    assert api.get("/approvals", headers=_auth(admin)).status_code == 200
    approved = api.post(
        f"/approvals/{approval_id}/approve", json={"reviewer": "human:op"}, headers=_auth(admin)
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "executed"


def test_metrics_includes_domain_series(client) -> None:
    api, admin, _ = client
    api.post(
        "/inventory",
        json={"sku": "S7", "title": "Tee", "acquisition_cost": 5},
        headers=_auth(admin),
    )
    body = api.get("/metrics", headers=_auth(admin)).text
    assert "goliath_inventory_items" in body
    assert "goliath_approval_queue_size" in body
    assert "goliath_proposals" in body
