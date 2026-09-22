from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError
from app.models.product_drawing import ProductDrawing

from app.services.secure_uploads import (
    ValidatedUpload,
    remove_stored_reference,
    store_private_upload,
)

PDF_CONTENT_TYPE = "application/pdf"
PRINT_ARTWORK_PREFIX = "private:drawing_originals/"


def drawing_purpose(drawing: ProductDrawing) -> str:
    return "print_artwork" if drawing.image_path.startswith(PRINT_ARTWORK_PREFIX) else "engineering"


def engineering_drawing_condition():
    """The source category is server-assigned; client filenames cannot set it."""
    return ~ProductDrawing.image_path.startswith(PRINT_ARTWORK_PREFIX, autoescape=True)


def default_product_drawing(drawings) -> ProductDrawing | None:
    """Select from the existing newest-first relationship, preserving its order."""
    return next((drawing for drawing in drawings if drawing_purpose(drawing) == "engineering"), None)


class DrawingValidationError(ValueError):
    pass


@dataclass(frozen=True)
class SavedDrawing:
    image_path: str
    thumbnail_path: str


def validate_product_drawing_upload(upload: ValidatedUpload) -> None:
    """Fully decode images during preflight; PDFs were signature-checked upstream."""

    if upload.content_type == PDF_CONTENT_TYPE:
        return
    try:
        image = Image.open(BytesIO(upload.content))
        image.load()
    except (UnidentifiedImageError, OSError) as error:
        raise DrawingValidationError("图纸文件无法识别") from error


def save_product_drawing_files(
    *,
    product_id: int,
    upload: ValidatedUpload,
    preserve_original: bool = False,
) -> SavedDrawing:
    del product_id  # Random server-side names deliberately contain no business identifier.
    if preserve_original and upload.content_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise DrawingValidationError("印刷素材仅支持PNG/JPEG/WebP；结构PDF请使用原图纸附件入口")
    if upload.content_type == PDF_CONTENT_TYPE:
        saved = store_private_upload(upload, category="drawings")
        return SavedDrawing(
            image_path=saved.reference,
            thumbnail_path=saved.reference,
        )
    if preserve_original:
        # V2 artwork keeps the exact upload, including alpha and resolution.
        # Legacy attachments are neither rewritten nor migrated.
        try:
            with Image.open(BytesIO(upload.content)) as source:
                if source.width * source.height > 25_000_000 or len(upload.content) > 5_000_000:
                    raise DrawingValidationError("印刷原件不能超过5MB或2500万像素")
                source.load()
                thumb = ImageOps.exif_transpose(source).convert("RGBA")
                thumb.thumbnail((480, 480), Image.Resampling.LANCZOS)
                buffer = BytesIO()
                thumb.save(buffer, "WEBP", lossless=True)
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
            raise DrawingValidationError("印刷原件无法安全解码") from error
        saved = store_private_upload(upload, category="drawing_originals")
        try:
            thumbnail = store_private_upload(upload, category="drawing_thumbnails",
                content=buffer.getvalue(), extension=".webp", content_type="image/webp")
        except Exception:
            remove_stored_reference(saved.reference)
            raise
        return SavedDrawing(image_path=saved.reference, thumbnail_path=thumbnail.reference)
    validate_product_drawing_upload(upload)
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
