"""Milestone five: media ingestion, image processing, and imaging primitives."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from goliath.config import OrchestrationConfig
from goliath.db.base import Base
from goliath.db.database import build_session_factory, create_test_engine
from goliath.db.ingestion_repositories import (
    MediaDerivationRepository,
    MediaProcessingJobRepository,
    ReviewTaskRepository,
)
from goliath.db.models import MediaJobStatus, MediaRole, MediaStatus, ReviewTaskType
from goliath.domain import imaging
from goliath.domain.media_service import (
    ImageProcessingService,
    MediaError,
    MediaIngestionService,
    MediaStore,
)
from goliath.domain.service import DomainService


@pytest.fixture
def env(tmp_path: Path):
    engine = create_test_engine()
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    config = OrchestrationConfig(workspace_roots=[tmp_path])
    config.domain.media.media_root = tmp_path / "media"
    config.domain.media.quarantine_root = tmp_path / "quarantine"
    config.domain.media.temp_upload_root = tmp_path / "tmp"
    config.domain.media.min_image_dimension = 50
    config.domain.media.blur_variance_threshold = 1.0
    store = MediaStore(
        media_root=tmp_path / "media",
        quarantine_root=tmp_path / "quarantine",
        temp_root=tmp_path / "tmp",
    )
    domain = DomainService(session_factory=factory, config=config)
    ingest = MediaIngestionService(session_factory=factory, config=config, store=store)
    process = ImageProcessingService(session_factory=factory, config=config, store=store)
    item = domain.create_inventory(
        actor="human:op", sku="M1", title="Tee", condition="good", acquisition_cost=Decimal(5)
    )
    yield {
        "factory": factory,
        "config": config,
        "store": store,
        "ingest": ingest,
        "process": process,
        "item": item,
        "tmp": tmp_path,
    }
    engine.dispose()


# ---------------------------------------------------------------- imaging unit


def test_mime_sniffing_ignores_client_declaration() -> None:
    png = imaging.make_fixture_image(fmt="PNG")
    assert imaging.sniff_media_type(png) == "image/png"
    assert imaging.sniff_media_type(b"totally not an image") is None


def test_sanitize_filename_rejects_traversal() -> None:
    assert imaging.sanitize_filename("../../etc/passwd") == "passwd"
    assert "/" not in imaging.sanitize_filename("a/b/c.jpg")
    assert imaging.sanitize_filename("..") == "upload"


def test_orientation_normalization_and_metadata_stripping() -> None:
    data = imaging.make_fixture_image(width=120, height=80)
    processed = imaging.normalize_and_strip(data, media_type="image/jpeg", quality=85)
    assert processed.media_type == "image/jpeg"
    reloaded = imaging.load_image(processed.data)
    assert not reloaded.getexif()


def test_thumbnail_and_perceptual_hash_are_deterministic() -> None:
    data = imaging.make_fixture_image(width=300, height=200)
    first = imaging.make_thumbnail(data, dimension=64)
    second = imaging.make_thumbnail(data, dimension=64)
    assert first.checksum == second.checksum
    assert first.width <= 64 and first.height <= 64
    assert imaging.perceptual_hash(data) == imaging.perceptual_hash(data)
    assert imaging.hamming_distance(imaging.perceptual_hash(data), imaging.perceptual_hash(data)) == 0


def test_quality_check_flags_small_and_corrupt() -> None:
    small = imaging.make_fixture_image(width=20, height=20)
    report = imaging.assess_quality(
        small, min_dimension=200, max_aspect_ratio=4.0, blur_variance_threshold=1.0
    )
    assert not report.passed
    assert "below_minimum_dimensions" in report.findings
    corrupt = imaging.assess_quality(
        b"broken", min_dimension=10, max_aspect_ratio=4.0, blur_variance_threshold=1.0
    )
    assert corrupt.findings == ["corrupt_file"]


# ----------------------------------------------------------------- ingestion


def test_store_rejects_path_traversal(env) -> None:
    with pytest.raises(MediaError):
        env["store"].write_atomic("../escape.jpg", b"data")


def test_ingest_validates_and_stores_atomically(env) -> None:
    data = imaging.make_fixture_image(width=300, height=200)
    media = env["ingest"].ingest_bytes(
        env["item"].id, data, role=MediaRole.ORIGINAL, original_filename="p.png"
    )
    assert media.status is MediaStatus.VALIDATED
    stored = env["store"].read(media.storage_key)
    assert stored == data


def test_checksum_deduplication(env) -> None:
    data = imaging.make_fixture_image()
    first = env["ingest"].ingest_bytes(env["item"].id, data, role=MediaRole.ORIGINAL)
    second = env["ingest"].ingest_bytes(env["item"].id, data, role=MediaRole.ORIGINAL)
    assert first.id == second.id


def test_invalid_bytes_are_quarantined_and_create_task(env) -> None:
    media = env["ingest"].ingest_bytes(
        env["item"].id,
        b"\x00\x01 not an image",
        role=MediaRole.ORIGINAL,
        original_filename="x.png",
        declared_media_type="image/png",
    )
    assert media.status is MediaStatus.QUARANTINED
    with env["factory"]() as session:
        tasks = ReviewTaskRepository(session).list(task_type=ReviewTaskType.MEDIA_VALIDATION)
    assert len(tasks) == 1


def test_declared_mime_mismatch_is_quarantined(env) -> None:
    png = imaging.make_fixture_image(fmt="PNG")
    media = env["ingest"].ingest_bytes(
        env["item"].id,
        png,
        role=MediaRole.ORIGINAL,
        original_filename="p.png",
        declared_media_type="image/jpeg",
    )
    assert media.status is MediaStatus.QUARANTINED
    assert "declared_mime_mismatch" in media.validation_result["findings"]


def test_disallowed_extension_is_quarantined(env) -> None:
    png = imaging.make_fixture_image(fmt="PNG")
    media = env["ingest"].ingest_bytes(
        env["item"].id, png, role=MediaRole.ORIGINAL, original_filename="p.gif"
    )
    assert media.status is MediaStatus.QUARANTINED


# ------------------------------------------------------------------ processing


def test_processing_produces_derivations_and_marks_processed(env) -> None:
    data = imaging.make_fixture_image(width=400, height=300)
    media = env["ingest"].ingest_bytes(env["item"].id, data, role=MediaRole.ORIGINAL)
    processed = env["process"].run_next()
    assert processed == media.id
    with env["factory"]() as session:
        ops = {d.operation for d in MediaDerivationRepository(session).list_for_media(media.id)}
        job = MediaProcessingJobRepository(session).list()[0]
    assert ops == {"normalized", "thumbnail", "preview"}
    assert job.status is MediaJobStatus.SUCCEEDED


def test_processing_is_idempotent(env) -> None:
    data = imaging.make_fixture_image(width=400, height=300)
    env["ingest"].ingest_bytes(env["item"].id, data, role=MediaRole.ORIGINAL)
    env["process"].run_next()
    # No queued job remains; a second run finds nothing to do.
    assert env["process"].run_next() is None


def test_processing_crash_recovery_via_reap(env) -> None:
    from datetime import UTC, datetime, timedelta

    data = imaging.make_fixture_image(width=400, height=300)
    media = env["ingest"].ingest_bytes(env["item"].id, data, role=MediaRole.ORIGINAL)
    # Simulate a claimed-but-crashed job with an expired lease.
    with env["factory"]() as session:
        repo = MediaProcessingJobRepository(session)
        claimed = repo.claim("worker-x", lease_seconds=60)
        assert claimed is not None
        job, _ = claimed
        job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        job_id = job.id
        session.commit()
    assert media.id is not None
    assert env["process"].reap() == 1
    with env["factory"]() as session:
        job = MediaProcessingJobRepository(session).get(job_id)
    assert job.status is MediaJobStatus.QUEUED


def test_max_file_size_enforced(env) -> None:
    env["config"].domain.media.max_file_bytes = 10
    with pytest.raises(MediaError):
        env["ingest"].ingest_bytes(
            env["item"].id, imaging.make_fixture_image(), role=MediaRole.ORIGINAL
        )
