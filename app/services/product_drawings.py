from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import os
from pathlib import Path
from uuid import uuid4

from PIL import Image, UnidentifiedImageError


ALLOWED_DRAWING_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_DRAWING_BYTES = 20 * 1024 * 1024


class DrawingValidationError(ValueError):
    pass


@dataclass(frozen=True)
class SavedDrawing:
    image_path: str
    thumbnail_path: str


def drawing_dir() -> Path:
    configured = os.getenv("ERP_DRAWING_DIR")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "static" / "uploads" / "drawings"


def _drawing_url(filename: str) -> str:
    return f"/static/uploads/drawings/{filename}"


def save_product_drawing_files(
    *,
    product_id: int,
    content: bytes,
    content_type: str | None,
) -> SavedDrawing:
    if content_type not in ALLOWED_DRAWING_TYPES:
        raise DrawingValidationError("图纸仅支持 JPG、PNG、WEBP")
    if not content or len(content) > MAX_DRAWING_BYTES:
        raise DrawingValidationError("图纸文件不能为空且不能超过 20MB")
    try:
        image = Image.open(BytesIO(content))
        image.load()
    except (UnidentifiedImageError, OSError) as error:
        raise DrawingValidationError("图纸文件无法识别") from error

    if image.mode not in {"RGB", "L"}:
        background = Image.new("RGB", image.size, "white")
        if "A" in image.getbands():
            background.paste(image, mask=image.getchannel("A"))
        else:
            background.paste(image)
        image = background
    else:
        image = image.convert("RGB")

    target = drawing_dir()
    target.mkdir(parents=True, exist_ok=True)
    stem = f"product_{product_id}_{uuid4().hex}"
    high_name = f"{stem}.webp"
    thumb_name = f"{stem}_thumb.webp"
    high = image.copy()
    high.thumbnail((2000, 2000), Image.Resampling.LANCZOS)
    thumb = image.copy()
    thumb.thumbnail((480, 480), Image.Resampling.LANCZOS)
    high.save(target / high_name, "WEBP", quality=82, method=6)
    thumb.save(target / thumb_name, "WEBP", quality=76, method=6)
    return SavedDrawing(
        image_path=_drawing_url(high_name),
        thumbnail_path=_drawing_url(thumb_name),
    )


def remove_drawing_files(image_path: str, thumbnail_path: str) -> list[str]:
    target = drawing_dir().resolve()
    errors: list[str] = []
    for stored_path in {image_path, thumbnail_path}:
        candidate = (target / Path(stored_path).name).resolve()
        if candidate.parent != target:
            errors.append(f"unsafe path: {stored_path}")
            continue
        try:
            candidate.unlink(missing_ok=True)
        except OSError as error:
            errors.append(f"{stored_path}: {error}")
    return errors
