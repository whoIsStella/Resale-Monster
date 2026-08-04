"""Deterministic image processing and validation.

All operations are pure and deterministic (fixed parameters, no randomness, no
network). Pillow provides the pixel operations. Client-supplied MIME types are
never trusted; media types are sniffed from magic bytes.
"""

from __future__ import annotations

import hashlib
import io
import re
import statistics
from dataclasses import dataclass, field
from decimal import Decimal

from PIL import Image, ImageFilter, ImageOps

# Magic-byte signatures used to sniff the true media type of uploaded bytes.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)

_MIME_TO_PIL_FORMAT = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
}

_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]")


def sniff_media_type(data: bytes) -> str | None:
    """Detect the media type from magic bytes; never trust a client declaration."""
    for signature, mime in _SIGNATURES:
        if data.startswith(signature):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def sanitize_filename(name: str) -> str:
    """Strip directory components and unsafe characters; reject traversal."""
    base = name.replace("\\", "/").split("/")[-1]
    base = _SAFE_FILENAME.sub("_", base).strip("._")
    base = base[:200] or "upload"
    if base in {"", ".", ".."}:
        base = "upload"
    return base


def checksum_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def storage_key_for(inventory_item_id: str, checksum: str, extension: str) -> str:
    """Deterministic, non-user-controlled storage key layout."""
    extension = extension if extension.startswith(".") else f".{extension}"
    return f"{inventory_item_id}/{checksum[:2]}/{checksum}{extension}"


def derivation_key_for(parent_checksum: str, operation: str, extension: str) -> str:
    extension = extension if extension.startswith(".") else f".{extension}"
    return f"derived/{parent_checksum[:2]}/{parent_checksum}/{operation}{extension}"


@dataclass(slots=True)
class ProcessedImage:
    data: bytes
    media_type: str
    width: int
    height: int
    checksum: str

    @property
    def file_size(self) -> int:
        return len(self.data)


@dataclass(slots=True)
class QualityReport:
    passed: bool
    findings: list[str] = field(default_factory=list)
    width: int | None = None
    height: int | None = None
    blur_variance: Decimal | None = None
    aspect_ratio: Decimal | None = None
    color_mode: str | None = None


class ImageError(ValueError):
    """Raised for corrupt or unprocessable images (controlled, no stack trace to callers)."""


def load_image(data: bytes) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
        return image
    except Exception as error:
        raise ImageError(f"corrupt or unreadable image: {type(error).__name__}") from error


def _encode(image: Image.Image, media_type: str, *, quality: int) -> ProcessedImage:
    pil_format = _MIME_TO_PIL_FORMAT.get(media_type, "JPEG")
    buffer = io.BytesIO()
    to_save = image
    if pil_format == "JPEG" and image.mode not in {"RGB", "L"}:
        to_save = image.convert("RGB")
    save_kwargs = {}
    if pil_format in {"JPEG", "WEBP"}:
        save_kwargs["quality"] = quality
    to_save.save(buffer, format=pil_format, **save_kwargs)
    payload = buffer.getvalue()
    return ProcessedImage(
        data=payload,
        media_type=media_type,
        width=to_save.width,
        height=to_save.height,
        checksum=checksum_bytes(payload),
    )


def normalize_and_strip(data: bytes, *, media_type: str, quality: int) -> ProcessedImage:
    """Apply EXIF-orientation normalization and strip all metadata by re-encoding."""
    image = load_image(data)
    # exif_transpose applies orientation then the re-encode drops metadata entirely.
    oriented = ImageOps.exif_transpose(image)
    clean = Image.new(oriented.mode, oriented.size)
    clean.putdata(list(oriented.getdata()))
    return _encode(clean, media_type, quality=quality)


def _resize_preserving_aspect(image: Image.Image, max_dimension: int) -> Image.Image:
    resized = image.copy()
    resized.thumbnail((max_dimension, max_dimension), Image.LANCZOS)
    return resized


def make_thumbnail(
    data: bytes, *, dimension: int, media_type: str = "image/jpeg", quality: int = 80
) -> ProcessedImage:
    image = load_image(data)
    return _encode(_resize_preserving_aspect(image, dimension), media_type, quality=quality)


def make_preview(
    data: bytes,
    *,
    dimension: int,
    media_type: str = "image/jpeg",
    quality: int = 85,
    pad_square: bool = False,
) -> ProcessedImage:
    image = load_image(data)
    resized = _resize_preserving_aspect(image, dimension)
    if pad_square:
        canvas = Image.new("RGB", (dimension, dimension), (255, 255, 255))
        offset = ((dimension - resized.width) // 2, (dimension - resized.height) // 2)
        canvas.paste(resized.convert("RGB"), offset)
        resized = canvas
    return _encode(resized, media_type, quality=quality)


def perceptual_hash(data: bytes) -> str:
    """Deterministic 64-bit average hash rendered as 16 hex characters."""
    image = load_image(data).convert("L").resize((8, 8), Image.LANCZOS)
    pixels = list(image.getdata())
    average = sum(pixels) / len(pixels)
    bits = 0
    for pixel in pixels:
        bits = (bits << 1) | (1 if pixel >= average else 0)
    return f"{bits:016x}"


def hamming_distance(hash_a: str, hash_b: str) -> int:
    return (int(hash_a, 16) ^ int(hash_b, 16)).bit_count()


def assess_quality(
    data: bytes,
    *,
    min_dimension: int,
    max_aspect_ratio: float,
    blur_variance_threshold: float,
) -> QualityReport:
    """Deterministic quality gate; corrupt files fail closed."""
    try:
        image = load_image(data)
    except ImageError:
        return QualityReport(passed=False, findings=["corrupt_file"])

    findings: list[str] = []
    width, height = image.width, image.height
    if min(width, height) < min_dimension:
        findings.append("below_minimum_dimensions")

    aspect = max(width, height) / max(min(width, height), 1)
    if aspect > max_aspect_ratio:
        findings.append("extreme_aspect_ratio")

    if image.mode not in {"RGB", "RGBA", "L", "P"}:
        findings.append("unsupported_color_mode")

    grayscale = image.convert("L")
    edges = grayscale.filter(ImageFilter.FIND_EDGES)
    edge_pixels = list(edges.getdata())
    blur_variance = statistics.pvariance(edge_pixels) if len(edge_pixels) > 1 else 0.0
    if blur_variance < blur_variance_threshold:
        findings.append("excessive_blur")

    # Placeholder exposure checks based on mean luminance.
    mean_luminance = sum(grayscale.getdata()) / max(len(list(grayscale.getdata())), 1)
    if mean_luminance > 245:
        findings.append("overexposed")
    elif mean_luminance < 12:
        findings.append("underexposed")

    return QualityReport(
        passed=not findings,
        findings=findings,
        width=width,
        height=height,
        blur_variance=Decimal(str(round(blur_variance, 3))),
        aspect_ratio=Decimal(str(round(aspect, 3))),
        color_mode=image.mode,
    )


def make_fixture_image(
    *, width: int = 400, height: int = 400, color: tuple[int, int, int] = (120, 90, 60), fmt: str = "PNG"
) -> bytes:
    """Deterministic in-memory image for tests (no fixtures on disk, no network)."""
    image = Image.new("RGB", (width, height), color)
    # Add a simple gradient so quality/perceptual-hash checks have signal.
    pixels = image.load()
    for x in range(width):
        for y in range(0, height, 4):
            shade = (x * 255) // max(width, 1)
            pixels[x, y] = (shade, (shade + 40) % 256, (shade + 80) % 256)
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()
