"""Secure media ingestion and deterministic image-processing services.

Ingestion validates bytes (never trusting client MIME/filenames), deduplicates by
checksum, writes atomically to configured roots, quarantines suspicious files, and
registers metadata. Processing runs through durable, leased jobs that produce
derivations, perceptual hashes, and quality results, with retries and recovery.
No cloud credentials, no shell, no network.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session, sessionmaker

from goliath.config import OrchestrationConfig
from goliath.db.domain_repositories import DuplicateRecordError, InventoryMediaRepository
from goliath.db.ingestion_repositories import (
    ImageQualityRepository,
    MediaDerivationRepository,
    MediaProcessingAttemptRepository,
    MediaProcessingJobRepository,
    PerceptualHashRepository,
    ReviewTaskRepository,
)
from goliath.db.models import (
    MediaRole,
    MediaStatus,
    ReviewTaskType,
    utc_now,
)
from goliath.db.repositories import AuditEventRepository, RecordNotFoundError
from goliath.domain import imaging

_EXT_FOR_MIME = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


class MediaError(RuntimeError):
    pass


class MediaStore:
    """Filesystem-backed byte store confined to configured roots (no traversal)."""

    def __init__(self, *, media_root: Path, quarantine_root: Path, temp_root: Path) -> None:
        # Roots are resolved eagerly but created lazily, so constructing the store
        # (e.g. during app startup) never requires the directories to pre-exist.
        self._media_root = media_root.expanduser().resolve()
        self._quarantine_root = quarantine_root.expanduser().resolve()
        self._temp_root = temp_root.expanduser().resolve()

    def _resolve(self, root: Path, storage_key: str) -> Path:
        candidate = (root / storage_key).resolve()
        if not candidate.is_relative_to(root):
            raise MediaError("storage key escapes its configured root")
        return candidate

    def write_atomic(self, storage_key: str, data: bytes, *, quarantine: bool = False) -> Path:
        root = self._quarantine_root if quarantine else self._media_root
        target = self._resolve(root, storage_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._temp_root.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(dir=self._temp_root)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return target

    def read(self, storage_key: str) -> bytes:
        target = self._resolve(self._media_root, storage_key)
        if not target.exists():
            raise MediaError("stored media file is missing")
        return target.read_bytes()

    def register_path(self, path: Path) -> bytes:
        """Read a pre-existing file that must already live under the media root."""
        resolved = path.expanduser().resolve()
        if not resolved.is_relative_to(self._media_root):
            raise MediaError("pre-existing file is outside the media root")
        if not resolved.is_file():
            raise MediaError("pre-existing file does not exist")
        return resolved.read_bytes()


def _audit(session: Session, event_type: str, *, actor: str, resource_id: UUID, details: dict) -> None:
    actor_type, _, actor_id = actor.partition(":")
    AuditEventRepository(session).append(
        event_type=event_type,
        actor_type=actor_type or "system",
        actor_id=actor_id or actor,
        resource_type="inventory_media",
        resource_id=resource_id,
        details=details,
    )


class MediaIngestionService:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session] | sessionmaker[Session],
        config: OrchestrationConfig,
        store: MediaStore | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._media = config.domain.media
        self._store = store or MediaStore(
            media_root=self._media.media_root,
            quarantine_root=self._media.quarantine_root,
            temp_root=self._media.temp_upload_root,
        )

    @property
    def store(self) -> MediaStore:
        return self._store

    def ingest_bytes(
        self,
        inventory_item_id: UUID,
        data: bytes,
        *,
        role: MediaRole,
        original_filename: str | None = None,
        declared_media_type: str | None = None,
        actor: str = "human:api",
    ):
        """Validate, deduplicate, store atomically, and register uploaded bytes."""
        if len(data) == 0:
            raise MediaError("empty upload")
        if len(data) > self._media.max_file_bytes:
            raise MediaError("upload exceeds maximum file size")

        safe_name = imaging.sanitize_filename(original_filename or "upload")
        checksum = imaging.checksum_bytes(data)
        detected = imaging.sniff_media_type(data)

        # Deduplicate by checksum before any write.
        with self._session_factory() as session:
            existing = InventoryMediaRepository(session).get_by_checksum(
                inventory_item_id, checksum
            )
            if existing is not None:
                _audit(
                    session,
                    "media.deduplicated",
                    actor=actor,
                    resource_id=existing.id,
                    details={"checksum": checksum},
                )
                session.commit()
                session.refresh(existing)
                return existing

        findings = self._validate(data, safe_name, detected, declared_media_type)
        quarantined = bool(findings)
        media_type = detected or "application/octet-stream"
        extension = _EXT_FOR_MIME.get(media_type, ".bin")
        storage_key = imaging.storage_key_for(str(inventory_item_id), checksum, extension)

        width = height = None
        if not quarantined:
            try:
                image = imaging.load_image(data)
                width, height = image.width, image.height
            except imaging.ImageError:
                findings.append("corrupt_file")
                quarantined = True

        # Atomic write into the appropriate root; temp file is always cleaned up.
        self._store.write_atomic(storage_key, data, quarantine=quarantined)

        with self._session_factory() as session:
            try:
                media = InventoryMediaRepository(session).add(
                    inventory_item_id=inventory_item_id,
                    file_identifier=uuid4().hex,
                    media_type=media_type,
                    checksum=checksum,
                    file_size=len(data),
                    role=role,
                    storage_key=storage_key,
                    original_filename=safe_name,
                    image_width=width,
                    image_height=height,
                    status=MediaStatus.QUARANTINED if quarantined else MediaStatus.VALIDATED,
                    detected_media_type=detected,
                    validation_result={"findings": findings, "declared": declared_media_type},
                )
            except DuplicateRecordError:
                session.rollback()
                existing = InventoryMediaRepository(session).get_by_checksum(
                    inventory_item_id, checksum
                )
                return existing
            if not quarantined:
                media.validated_at = utc_now()
                MediaProcessingJobRepository(session).enqueue(
                    media.id, max_attempts=self._media.processing_max_attempts
                )
                _audit(
                    session,
                    "media.validated",
                    actor=actor,
                    resource_id=media.id,
                    details={"media_type": media_type},
                )
            else:
                ReviewTaskRepository(session).create_idempotent(
                    task_type=ReviewTaskType.MEDIA_VALIDATION,
                    resource_type="inventory_media",
                    resource_id=media.id,
                    reason="; ".join(findings) or "quarantined",
                    priority=200,
                )
                _audit(
                    session,
                    "media.quarantined",
                    actor=actor,
                    resource_id=media.id,
                    details={"findings": findings},
                )
            _audit(
                session,
                "media.ingested",
                actor=actor,
                resource_id=media.id,
                details={"role": role.value, "quarantined": quarantined},
            )
            session.commit()
            session.refresh(media)
            return media

    def register_existing(
        self, inventory_item_id: UUID, path: Path, *, role: MediaRole, actor: str = "human:cli"
    ):
        data = self._store.register_path(path)
        return self.ingest_bytes(
            inventory_item_id,
            data,
            role=role,
            original_filename=path.name,
            actor=actor,
        )

    def _validate(
        self, data: bytes, safe_name: str, detected: str | None, declared: str | None
    ) -> list[str]:
        findings: list[str] = []
        if detected is None:
            findings.append("unrecognized_media_type")
        elif detected not in self._media.allowed_mime_types:
            findings.append("disallowed_media_type")
        if declared is not None and detected is not None and declared != detected:
            findings.append("declared_mime_mismatch")
        suffix = Path(safe_name).suffix.lower()
        if suffix and suffix not in self._media.allowed_extensions:
            findings.append("disallowed_extension")
        return findings

    def list_quarantined(self):
        with self._session_factory() as session:
            values = list(
                InventoryMediaRepository(session).list_by_status(MediaStatus.QUARANTINED)
            )
            for value in values:
                session.expunge(value)
            return values

    def archive(self, media_id: UUID, *, actor: str = "human:api"):
        with self._session_factory() as session:
            repo = InventoryMediaRepository(session)
            media = repo.get(media_id)
            if media is None:
                raise RecordNotFoundError(f"media not found: {media_id}")
            media = repo.update_status(
                media_id,
                expected_version=media.version,
                status=MediaStatus.ARCHIVED,
                touch="archived",
            )
            _audit(
                session,
                "media.archived",
                actor=actor,
                resource_id=media_id,
                details={},
            )
            session.commit()
            session.refresh(media)
            return media


class ImageProcessingService:
    """Deterministic image processing run through durable, leased jobs."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session] | sessionmaker[Session],
        config: OrchestrationConfig,
        store: MediaStore | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._media = config.domain.media
        self._store = store or MediaStore(
            media_root=self._media.media_root,
            quarantine_root=self._media.quarantine_root,
            temp_root=self._media.temp_upload_root,
        )

    def enqueue(self, media_id: UUID) -> UUID:
        with self._session_factory() as session:
            job = MediaProcessingJobRepository(session).enqueue(
                media_id, max_attempts=self._media.processing_max_attempts
            )
            session.commit()
            return job.id

    def reap(self) -> int:
        with self._session_factory() as session:
            count = MediaProcessingJobRepository(session).reap_expired()
            session.commit()
            return count

    def run_next(self, *, worker_id: str = "media-worker") -> UUID | None:
        """Claim and process one queued media job; returns the media id processed."""
        with self._session_factory() as session:
            claimed = MediaProcessingJobRepository(session).claim(
                worker_id, lease_seconds=120.0
            )
            if claimed is None:
                session.commit()
                return None
            job, token = claimed
            attempt = MediaProcessingAttemptRepository(session).create(
                job.id, worker_id, job.attempts
            )
            media_id = job.media_id
            attempt_pk = attempt.id
            job_id = job.id
            session.commit()

        try:
            media_id_result = self._process(media_id, worker_id=worker_id)
        except Exception as error:  # noqa: BLE001 - persist failure, retry via job
            with self._session_factory() as session:
                MediaProcessingAttemptRepository(session).complete(
                    attempt_pk, status="failed", error_category="processing_error"
                )
                MediaProcessingJobRepository(session).complete_failure(
                    job_id,
                    worker_id,
                    token,
                    error_category="processing_error",
                    failure_reason=f"{type(error).__name__}: {error}",
                    backoff_seconds=self._media.processing_backoff_seconds,
                )
                repo = InventoryMediaRepository(session)
                media = repo.get(media_id)
                if media is not None and media.status is MediaStatus.PROCESSING:
                    repo.update_status(
                        media_id,
                        expected_version=media.version,
                        status=MediaStatus.FAILED,
                        error_category="processing_error",
                        processing_error=str(error)[:500],
                    )
                session.commit()
            return media_id

        with self._session_factory() as session:
            MediaProcessingAttemptRepository(session).complete(attempt_pk, status="succeeded")
            MediaProcessingJobRepository(session).complete_success(job_id, worker_id, token)
            session.commit()
        return media_id_result

    def _process(self, media_id: UUID, *, worker_id: str) -> UUID:
        with self._session_factory() as session:
            repo = InventoryMediaRepository(session)
            media = repo.get(media_id)
            if media is None:
                raise MediaError(f"media not found: {media_id}")
            if media.status is MediaStatus.PROCESSED:
                return media_id  # idempotent: already processed
            storage_key = media.storage_key
            checksum = media.checksum
            item_id = media.inventory_item_id
            repo.update_status(
                media_id,
                expected_version=media.version,
                status=MediaStatus.PROCESSING,
                touch="processing",
            )
            session.commit()

        data = self._store.read(storage_key)
        derivations = self._build_derivations(data, checksum)
        phash = imaging.perceptual_hash(data)
        quality = imaging.assess_quality(
            data,
            min_dimension=self._media.min_image_dimension,
            max_aspect_ratio=self._media.max_aspect_ratio,
            blur_variance_threshold=self._media.blur_variance_threshold,
        )

        with self._session_factory() as session:
            derivation_repo = MediaDerivationRepository(session)
            for operation, processed in derivations:
                if derivation_repo.exists(media_id, operation):
                    continue  # idempotent: skip already-produced derivation
                self._store.write_atomic(processed["storage_key"], processed["data"])
                derivation_repo.add(
                    parent_media_id=media_id,
                    operation=operation,
                    storage_key=processed["storage_key"],
                    media_type=processed["media_type"],
                    checksum=processed["checksum"],
                    file_size=processed["file_size"],
                    width=processed["width"],
                    height=processed["height"],
                )

            phash_repo = PerceptualHashRepository(session)
            if not phash_repo.find_duplicates(
                algorithm="ahash", hash_hex=phash, exclude_media_id=media_id
            ):
                phash_repo.add(media_id=media_id, algorithm="ahash", hash_hex=phash)
            else:
                phash_repo.add(media_id=media_id, algorithm="ahash", hash_hex=phash)

            ImageQualityRepository(session).add(
                media_id=media_id,
                passed=quality.passed,
                findings=quality.findings,
                width=quality.width,
                height=quality.height,
                blur_variance=quality.blur_variance,
                aspect_ratio=quality.aspect_ratio,
                color_mode=quality.color_mode,
            )
            if not quality.passed:
                ReviewTaskRepository(session).create_idempotent(
                    task_type=ReviewTaskType.IMAGE_QUALITY,
                    resource_type="inventory_media",
                    resource_id=media_id,
                    reason="; ".join(quality.findings),
                    priority=150,
                )

            repo = InventoryMediaRepository(session)
            media = repo.get(media_id)
            repo.update_status(
                media_id,
                expected_version=media.version,
                status=MediaStatus.PROCESSED,
                touch="processed",
            )
            _audit(
                session,
                "media.processed",
                actor=f"worker:{worker_id}",
                resource_id=media_id,
                details={"quality_passed": quality.passed, "item": str(item_id)},
            )
            session.commit()
        return media_id

    def _build_derivations(self, data: bytes, checksum: str) -> list[tuple[str, dict[str, Any]]]:
        results: list[tuple[str, dict[str, Any]]] = []
        normalized = imaging.normalize_and_strip(
            data, media_type="image/jpeg", quality=self._media.jpeg_quality
        )
        thumbnail = imaging.make_thumbnail(data, dimension=self._media.thumbnail_dimension)
        preview = imaging.make_preview(
            data,
            dimension=self._media.preview_dimension,
            pad_square=self._media.pad_square_canvas,
        )
        for operation, processed in (
            ("normalized", normalized),
            ("thumbnail", thumbnail),
            ("preview", preview),
        ):
            results.append(
                (
                    operation,
                    {
                        "data": processed.data,
                        "media_type": processed.media_type,
                        "checksum": processed.checksum,
                        "file_size": processed.file_size,
                        "width": processed.width,
                        "height": processed.height,
                        "storage_key": imaging.derivation_key_for(checksum, operation, ".jpg"),
                    },
                )
            )
        return results
