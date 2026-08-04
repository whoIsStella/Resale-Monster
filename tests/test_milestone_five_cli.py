"""Milestone five: media, comparables, review, and dashboard CLI commands."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from typer.testing import CliRunner

from goliath import cli
from goliath.config import OrchestrationConfig
from goliath.db.base import Base
from goliath.db.database import build_session_factory, create_test_engine
from goliath.domain import imaging
from goliath.domain.comparables import ComparableImportService
from goliath.domain.media_service import ImageProcessingService, MediaIngestionService
from goliath.domain.review_service import ReviewService
from goliath.domain.service import DomainService

runner = CliRunner()


def _setup(tmp_path: Path):
    engine = create_test_engine()
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    config = OrchestrationConfig(workspace_roots=[tmp_path])
    config.domain.media.media_root = tmp_path / "media"
    config.domain.media.quarantine_root = tmp_path / "quarantine"
    config.domain.media.temp_upload_root = tmp_path / "tmp"
    return engine, factory, config


def _patch(monkeypatch, factory, config):
    domain = DomainService(session_factory=factory, config=config)
    media = MediaIngestionService(session_factory=factory, config=config)
    processing = ImageProcessingService(session_factory=factory, config=config)
    review = ReviewService(session_factory=factory, config=config)
    importer = ComparableImportService(session_factory=factory, config=config)
    monkeypatch.setattr(cli, "build_domain_service", lambda: domain)
    monkeypatch.setattr(cli, "build_media_service", lambda: media)
    monkeypatch.setattr(cli, "build_image_processing_service", lambda: processing)
    monkeypatch.setattr(cli, "build_review_service", lambda: review)
    monkeypatch.setattr(cli, "build_comparable_import_service", lambda: importer)
    return domain


def test_media_ingest_process_and_list(tmp_path: Path, monkeypatch) -> None:
    engine, factory, config = _setup(tmp_path)
    domain = _patch(monkeypatch, factory, config)
    item = domain.create_inventory(
        actor="human:op", sku="M1", title="Tee", condition="good", acquisition_cost=Decimal(5)
    )
    image_path = tmp_path / "photo.png"
    image_path.write_bytes(imaging.make_fixture_image(width=300, height=200))

    ingested = runner.invoke(
        cli.app, ["media", "ingest", str(item.id), str(image_path), "--json"]
    )
    assert ingested.exit_code == 0, ingested.output
    assert json.loads(ingested.output)["status"] == "validated"

    processed = runner.invoke(cli.app, ["media", "process", "--json"])
    assert processed.exit_code == 0

    listed = runner.invoke(cli.app, ["media", "list", str(item.id), "--json"])
    assert listed.exit_code == 0
    assert json.loads(listed.output)[0]["status"] in {"processed", "validated"}
    engine.dispose()


def test_comparables_import_dry_run_and_review(tmp_path: Path, monkeypatch) -> None:
    engine, factory, config = _setup(tmp_path)
    domain = _patch(monkeypatch, factory, config)
    item = domain.create_inventory(
        actor="human:op", sku="C1", title="Tee", condition="good", acquisition_cost=Decimal(5)
    )
    csv_path = tmp_path / "comps.csv"
    csv_path.write_text(
        "marketplace,listing_title,source_identity,is_sold,sold_price,currency\n"
        "ebay,Nike Tee,L1,true,40,USD\n"
    )
    dry = runner.invoke(
        cli.app, ["comparables", "import", str(item.id), str(csv_path), "--dry-run", "--json"]
    )
    assert dry.exit_code == 0
    assert json.loads(dry.output)["status"] == "dry_run"

    real = runner.invoke(cli.app, ["comparables", "import", str(item.id), str(csv_path), "--json"])
    assert json.loads(real.output)["imported"] == 1

    review_list = runner.invoke(cli.app, ["comparables", "review-list", "--json"])
    comparable_id = json.loads(review_list.output)[0]["id"]
    accepted = runner.invoke(cli.app, ["comparables", "accept", comparable_id, "--json"])
    assert json.loads(accepted.output)["review_status"] == "accepted"
    engine.dispose()


def test_review_and_dashboard_cli(tmp_path: Path, monkeypatch) -> None:
    engine, factory, config = _setup(tmp_path)
    domain = _patch(monkeypatch, factory, config)
    item = domain.create_inventory(
        actor="human:op",
        sku="R1",
        title="Tee",
        condition="good",
        acquisition_cost=Decimal(5),
        category="clothing",
    )
    domain.evaluate_completeness(item.id)  # creates a review task
    listing = runner.invoke(cli.app, ["review", "list", "--json"])
    assert listing.exit_code == 0
    task_id = json.loads(listing.output)[0]["id"]
    claimed = runner.invoke(cli.app, ["review", "claim", task_id, "--json"])
    assert json.loads(claimed.output)["status"] == "claimed"
    completed = runner.invoke(cli.app, ["review", "complete", task_id, "--json"])
    assert json.loads(completed.output)["status"] == "completed"

    summary = runner.invoke(cli.app, ["dashboard", "summary", "--json"])
    assert summary.exit_code == 0
    assert "inventory_by_status" in json.loads(summary.output)
    engine.dispose()


def test_cli_expected_error_has_no_traceback(tmp_path: Path, monkeypatch) -> None:
    engine, factory, config = _setup(tmp_path)
    _patch(monkeypatch, factory, config)
    result = runner.invoke(cli.app, ["review", "claim", str(uuid4())])
    assert result.exit_code == 1
    assert "error:" in result.output
    assert "Traceback" not in result.output
    engine.dispose()
