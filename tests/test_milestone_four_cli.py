"""Milestone four: resale-domain CLI commands."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from typer.testing import CliRunner

from goliath import cli
from goliath.config import OrchestrationConfig
from goliath.db.base import Base
from goliath.db.database import build_session_factory, create_test_engine
from goliath.domain.service import DomainService

runner = CliRunner()


def _service(tmp_path: Path):
    engine = create_test_engine()
    Base.metadata.create_all(engine)
    media = tmp_path / "media"
    media.mkdir()
    config = OrchestrationConfig(workspace_roots=[tmp_path])
    config.domain.media_storage_root = media
    return DomainService(session_factory=build_session_factory(engine), config=config), engine


def _patch(monkeypatch, service) -> None:
    monkeypatch.setattr(cli, "build_domain_service", lambda: service)


def test_cli_inventory_create_show_and_completeness(tmp_path: Path, monkeypatch) -> None:
    service, engine = _service(tmp_path)
    _patch(monkeypatch, service)
    created = runner.invoke(
        cli.app,
        ["inventory", "create", "--sku", "C1", "--title", "Tee", "--cost", "5", "--category", "clothing", "--json"],
    )
    assert created.exit_code == 0, created.output
    item_id = json.loads(created.output)["id"]

    shown = runner.invoke(cli.app, ["inventory", "show", item_id, "--json"])
    assert shown.exit_code == 0
    assert json.loads(shown.output)["sku"] == "C1"

    completeness = runner.invoke(cli.app, ["inventory", "completeness", item_id, "--json"])
    assert completeness.exit_code == 0
    assert "score" in json.loads(completeness.output)

    listed = runner.invoke(cli.app, ["inventory", "list", "--json"])
    assert listed.exit_code == 0
    assert json.loads(listed.output)[0]["sku"] == "C1"
    engine.dispose()


def test_cli_expected_error_has_no_traceback(tmp_path: Path, monkeypatch) -> None:
    service, engine = _service(tmp_path)
    _patch(monkeypatch, service)
    result = runner.invoke(cli.app, ["inventory", "show", str(uuid4())])
    assert result.exit_code == 1
    assert "error:" in result.output
    assert "Traceback" not in result.output
    engine.dispose()


def test_cli_pricing_listing_and_approval(tmp_path: Path, monkeypatch) -> None:
    service, engine = _service(tmp_path)
    _patch(monkeypatch, service)
    item_id = json.loads(
        runner.invoke(
            cli.app,
            ["inventory", "create", "--sku", "P1", "--title", "Nike Tee", "--cost", "5", "--json"],
        ).output
    )["id"]

    priced = runner.invoke(cli.app, ["pricing", "calculate", item_id, "--cost-basis", "10", "--json"])
    assert priced.exit_code == 0
    assert "recommended_price" in json.loads(priced.output)

    draft = runner.invoke(cli.app, ["listing", "draft-create", item_id, "--json"])
    assert draft.exit_code == 0
    draft_id = json.loads(draft.output)["id"]

    variant = runner.invoke(
        cli.app, ["listing", "variant-create", draft_id, "--marketplace", "ebay", "--json"]
    )
    assert variant.exit_code == 0
    assert json.loads(variant.output)["marketplace"] == "ebay"

    approvals = runner.invoke(cli.app, ["approval", "list", "--json"])
    assert approvals.exit_code == 0
    engine.dispose()


def test_cli_research_workflow(tmp_path: Path, monkeypatch) -> None:
    service, engine = _service(tmp_path)
    _patch(monkeypatch, service)
    item_id = json.loads(
        runner.invoke(
            cli.app,
            ["inventory", "create", "--sku", "R1", "--title", "Tee", "--cost", "5", "--json"],
        ).output
    )["id"]
    research = runner.invoke(
        cli.app, ["research", "create", item_id, "--question", "model?", "--json"]
    )
    assert research.exit_code == 0
    research_id = json.loads(research.output)["id"]
    shown = runner.invoke(cli.app, ["research", "show", research_id, "--json"])
    assert shown.exit_code == 0
    completed = runner.invoke(
        cli.app, ["research", "complete", research_id, "--status", "inconclusive", "--json"]
    )
    assert completed.exit_code == 0
    assert json.loads(completed.output)["status"] == "inconclusive"
    engine.dispose()
