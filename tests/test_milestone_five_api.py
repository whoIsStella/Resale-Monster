"""Milestone five: dashboard, media, comparable, and review API endpoints."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from goliath.agents.registry import AgentRegistry
from goliath.api import create_app
from goliath.config import OrchestrationConfig
from goliath.db.base import Base
from goliath.db.database import build_session_factory, create_test_engine
from goliath.db.operations import AuthRepository
from goliath.domain import imaging
from goliath.domain.service import DomainService
from goliath.orchestration.service import JobOrchestrationService
from goliath.orchestration.uow import SqlAlchemyJobUnitOfWork
from tests.test_orchestration import FakeAdapter


@pytest.fixture
def client(tmp_path: Path):
    engine = create_test_engine()
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    config = OrchestrationConfig(
        workspace_roots=[tmp_path], api_authentication_required=True, metrics_enabled=True
    )
    config.domain.media.media_root = tmp_path / "media"
    config.domain.media.quarantine_root = tmp_path / "quarantine"
    config.domain.media.temp_upload_root = tmp_path / "tmp"
    with factory() as session:
        _, _, admin = AuthRepository(session).create_key("operator", ["admin"])
        _, _, reader = AuthRepository(session).create_key("reader", ["dashboard:read"])
        _, revoked_record, revoked = AuthRepository(session).create_key(
            "revoked", ["dashboard:read"]
        )
        AuthRepository(session).revoke(revoked_record.id)
        _, _, expired = AuthRepository(session).create_key(
            "expired",
            ["dashboard:read"],
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        session.commit()
    registry = AgentRegistry()
    registry.register("fake", FakeAdapter())
    service = JobOrchestrationService(
        uow_factory=lambda: SqlAlchemyJobUnitOfWork(factory), registry=registry, config=config
    )
    domain = DomainService(session_factory=factory, config=config)
    item = domain.create_inventory(
        actor="human:op", sku="A1", title="Tee", condition="good", acquisition_cost=Decimal(5)
    )
    app = create_app(service=service, registry=registry, session_factory=factory, config=config)
    app.state.revoked_key = revoked
    app.state.expired_key = expired
    with TestClient(app) as test_client:
        yield test_client, admin, reader, str(item.id)
    engine.dispose()


def _h(key):
    return {"Authorization": f"Bearer {key}"}


def test_dashboard_requires_scope(client) -> None:
    api, _, reader, _ = client
    assert api.get("/dashboard/summary").status_code == 401
    # reader has dashboard:read but not media:read
    assert api.get("/dashboard/summary", headers=_h(reader)).status_code == 200
    assert api.get("/media/quarantine/list", headers=_h(reader)).status_code == 403


def test_all_protected_routes_execute_authorization_dependencies(client) -> None:
    api, *_ = client
    routes = [
        route
        for route in api.app.routes
        if isinstance(route, APIRoute) and route.path not in {"/health", "/ready"}
    ]

    assert routes
    for route in routes:
        path = re.sub(r"\{[^}]+\}", "00000000-0000-0000-0000-000000000000", route.path)
        method = next(iter(route.methods))
        response = api.request(method, path)
        assert response.status_code == 401, f"{method} {route.path} returned {response.status_code}"


def test_invalid_revoked_and_expired_keys_are_unauthorized(client) -> None:
    api, *_ = client
    for key in ("invalid", api.app.state.revoked_key, api.app.state.expired_key):
        assert api.get("/dashboard/summary", headers=_h(key)).status_code == 401


def test_media_upload_and_process(client) -> None:
    api, admin, _, item_id = client
    img = imaging.make_fixture_image(width=300, height=200)
    resp = api.post(
        f"/media/{item_id}/upload",
        files={"file": ("photo.png", img, "image/png")},
        data={"role": "original"},
        headers=_h(admin),
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "validated"
    listed = api.get(f"/media/{item_id}", headers=_h(admin))
    assert listed.status_code == 200
    media_id = listed.json()[0]["id"]
    processed = api.post(f"/media/{media_id}/process", headers=_h(admin))
    assert processed.status_code == 200
    assert processed.json()["enqueued"] is True


def test_comparable_import_and_review_flow(client) -> None:
    api, admin, _, item_id = client
    csv = (
        "marketplace,listing_title,source_identity,is_sold,sold_price,currency\n"
        "ebay,Nike Tee,L1,true,40,USD\n"
    )
    imported = api.post(
        "/comparables/import",
        json={"item_id": item_id, "source_format": "csv", "content": csv},
        headers=_h(admin),
    )
    assert imported.status_code == 201
    assert imported.json()["imported"] == 1
    review_list = api.get("/comparables/review-list", headers=_h(admin))
    assert review_list.status_code == 200
    comparable_id = review_list.json()[0]["id"]
    accepted = api.post(
        f"/comparables/{comparable_id}/accept", json={"reviewer": "human:op"}, headers=_h(admin)
    )
    assert accepted.status_code == 200
    assert accepted.json()["review_status"] == "accepted"


def test_review_task_endpoints_and_pagination(client) -> None:
    api, admin, _, item_id = client
    # Create a completeness-blocking task by evaluating a clothing item.
    api.patch(
        f"/inventory/{item_id}/draft",
        json={"expected_version": 1, "changes": {"category": "clothing"}},
        headers=_h(admin),
    )
    # trigger completeness via dashboard? use bulk reevaluate is internal; instead import bad comparable
    listing = api.get("/reviews?limit=0", headers=_h(admin))
    assert listing.status_code == 422
    ok = api.get("/reviews", headers=_h(admin))
    assert ok.status_code == 200


def test_dashboard_endpoints(client) -> None:
    api, admin, _, _ = client
    for path in (
        "/dashboard/summary",
        "/dashboard/tasks",
        "/dashboard/inventory-needing-review",
        "/dashboard/comparables-needing-review",
        "/dashboard/media-failures",
        "/dashboard/worker-health",
        "/dashboard/approvals",
    ):
        assert api.get(path, headers=_h(admin)).status_code == 200, path


def test_metrics_includes_media_and_review_series(client) -> None:
    api, admin, _, _ = client
    body = api.get("/metrics", headers=_h(admin)).text
    assert "goliath_media{" in body
    assert "goliath_review_tasks{" in body
    assert "goliath_comparables_awaiting_review" in body


def test_bulk_review_completion(client) -> None:
    api, admin, _, item_id = client
    # import comparable to create a review task
    csv = "marketplace,listing_title,source_identity,is_sold,sold_price,currency\nebay,X,L9,true,10,USD\n"
    api.post(
        "/comparables/import",
        json={"item_id": item_id, "source_format": "csv", "content": csv},
        headers=_h(admin),
    )
    tasks = api.get("/reviews", headers=_h(admin)).json()
    ids = [t["id"] for t in tasks]
    result = api.post(
        "/bulk/reviews/complete",
        json={"ids": ids, "reviewer": "human:op", "outcome": "done"},
        headers=_h(admin),
    )
    assert result.status_code == 200
    assert result.json()["succeeded"] == len(ids)
