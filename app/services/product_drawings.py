from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from PIL import Image, UnidentifiedImageError

from app.services.secure_uploads import (
    ValidatedUpload,
    remove_stored_reference,
    store_private_upload,
)

PDF_CONTENT_TYPE = "application/pdf"


class DrawingValidationError(ValueError):
    pass


@dataclass(frozen=True)
class SavedDrawing:
    image_path: str
    thumbnail_path: str


def save_product_drawing_files(
    *,
    product_id: int,
    upload: ValidatedUpload,
) -> SavedDrawing:
    del product_id  # Random server-side names deliberately contain no business identifier.
    if upload.content_type == PDF_CONTENT_TYPE:
        saved = store_private_upload(upload, category="drawings")
        return SavedDrawing(
            image_path=saved.reference,
            thumbnail_path=saved.reference,
        )
    try:
        image = Image.open(BytesIO(upload.content))
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

    high = image.copy()
    high.thumbnail((2000, 2000), Image.Resampling.LANCZOS)
    thumb = image.copy()
    thumb.thumbnail((480, 480), Image.Resampling.LANCZOS)
    high_buffer = BytesIO()
    thumb_buffer = BytesIO()
    high.save(high_buffer, "WEBP", quality=82, method=6)
    thumb.save(thumb_buffer, "WEBP", quality=76, method=6)
    saved_high = store_private_upload(
        upload,
        category="drawings",
        content=high_buffer.getvalue(),
        extension=".webp",
        content_type="image/webp",
    )
    try:
        saved_thumb = store_private_upload(
            upload,
            category="drawing_thumbnails",
            content=thumb_buffer.getvalue(),
            extension=".webp",
            content_type="image/webp",
        )
    except Exception:
        remove_stored_reference(saved_high.reference)
        raise
    return SavedDrawing(
        image_path=saved_high.reference,
        thumbnail_path=saved_thumb.reference,
    )


def remove_drawing_files(image_path: str, thumbnail_path: str) -> list[str]:
    errors: list[str] = []
    for stored_path in {image_path, thumbnail_path}:
        errors.extend(remove_stored_reference(stored_path))
    return errors
