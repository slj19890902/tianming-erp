"""Read-only mobile engineering previews; never reconstruct or persist drawings."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
import math
from pathlib import Path
import re

from fastapi import HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import has_permission
from app.core.time_contract import utc_naive_to_api
from app.models.customer import Customer
from app.models.drawing_design import DrawingRelease
from app.models.product import Product
from app.models.product_drawing import ProductDrawing
from app.services.product_drawings import PRINT_ARTWORK_PREFIX, engineering_drawing_condition
from app.services.secure_uploads import resolve_stored_reference

HEADERS = {"Cache-Control": "private, no-store, max-age=0", "Pragma": "no-cache",
           "X-Content-Type-Options": "nosniff", "X-Robots-Tag": "noindex, nofollow"}
MAX_BYTES = 25 * 1024 * 1024
MAX_PIXELS = 25_000_000
PREVIEW_EDGE = 640
READ_PERMISSIONS = ("warehouse.view", "orders.view", "incoming.view",
                    "production.printing.view", "production.die_cut.view")


def can_preview_product(user) -> bool:
    # These are the existing mobile lookup/station readers. This separate grant
    # covers engineering sources only, never master downloads or print artwork.
    return any(has_permission(user, permission) for permission in READ_PERMISSIONS)


def failure(status: int, detail: str) -> HTTPException:
    return HTTPException(status, detail, headers=HEADERS)


@dataclass(frozen=True)
class DrawingSource:
    key: str
    reference: str
    name: str = "产品工程图纸"
    thumbnail: str | None = None
    uploaded_at: datetime | None = None

    def public(self, product_id: int) -> dict:
        base = f"/api/mobile/erp/products/{product_id}/drawings/{self.key}"
        item = {"id": self.key, "name": self.name,
                "kind": "pdf" if Path(self.reference).suffix.lower() == ".pdf" else "image",
                "preview_url": base + "/preview", "original_url": base + "/original"}
        if self.uploaded_at is not None:
            item["uploaded_at"] = utc_naive_to_api(self.uploaded_at)
        return item


def _engineering_reference(reference: str | None) -> bool:
    return bool(reference and not reference.startswith(PRINT_ARTWORK_PREFIX)
                and Path(reference).suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg", ".webp"})


def product_drawing_metadata(db: Session, product_ids, *, user, visible_customer_ids) -> dict[int, dict]:
    ids = {int(value) for value in product_ids if value is not None}
    result = {value: {"status": "none", "items": []} for value in ids}
    if not ids:
        return result
    if not can_preview_product(user):
        return {value: {"status": "forbidden", "items": []} for value in ids}
    statement = select(Product).join(Customer).where(Product.id.in_(ids),
        Product.is_active.is_(True), Product.deleted_at.is_(None), Customer.is_active.is_(True))
    if visible_customer_ids is not None:
        statement = statement.where(Product.customer_id.in_(visible_customer_ids))
    products = {p.id: p for p in db.scalars(statement)}
    if not products:
        return result
    # Uploads are separate engineering attachments, not replacements. Return
    # every retained upload, in stable newest-first order, without opening files.
    # Product/customer filtering happens before attachment metadata is read.
    sources: dict[int, list[DrawingSource]] = {}
    attachments = select(ProductDrawing).where(ProductDrawing.product_id.in_(products),
        engineering_drawing_condition()).order_by(ProductDrawing.product_id,
        ProductDrawing.uploaded_at.desc(), ProductDrawing.id.desc())
    for drawing in db.scalars(attachments):
        if _engineering_reference(drawing.image_path):
            sources.setdefault(drawing.product_id, []).append(DrawingSource(
                f"attachment-{drawing.id}", drawing.image_path,
                thumbnail=drawing.thumbnail_path, uploaded_at=drawing.uploaded_at))
    released = select(DrawingRelease.id.label("id"), func.row_number().over(
        partition_by=DrawingRelease.product_id, order_by=DrawingRelease.id.desc()).label("rank")
    ).join(Product, Product.id == DrawingRelease.product_id).where(
        Product.id.in_(products), DrawingRelease.customer_id == Product.customer_id,
        DrawingRelease.product_version == Product.version).subquery()
    for release in db.scalars(select(DrawingRelease).join(released, released.c.id == DrawingRelease.id)
                              .where(released.c.rank == 1)):
        if _engineering_reference(release.pdf_reference):
            sources.setdefault(release.product_id, [DrawingSource(f"release-{release.id}", release.pdf_reference,
                               f"已发布产品图纸 {release.revision}")])
    for product in products.values():
        product_sources = sources.get(product.id)
        if product_sources is None and _engineering_reference(product.die_cut_path):
            product_sources = [DrawingSource("legacy", product.die_cut_path)]
        if product_sources:
            result[product.id] = {"status": "available", "items": [source.public(product.id) for source in product_sources]}
    return result


def attach_product_drawings(db: Session, items: list[dict], *, user, visible_customer_ids) -> list[dict]:
    metadata = product_drawing_metadata(db, (item.get("product_id") for item in items),
                                       user=user, visible_customer_ids=visible_customer_ids)
    for item in items:
        item["drawings"] = metadata.get(item.get("product_id"), {"status": "none", "items": []})
    return items


def require_drawing_source(db: Session, product: Product, key: str) -> DrawingSource:
    if len(key) > 40 or (key.rsplit("-", 1)[-1].isdigit() and int(key.rsplit("-", 1)[-1]) > 2_147_483_647):
        raise failure(404, "产品图纸不存在或当前账号无权查看")
    if key == "legacy":
        source = DrawingSource(key, product.die_cut_path or "")
    elif re.fullmatch(r"attachment-[1-9][0-9]*", key):
        drawing = db.scalar(select(ProductDrawing).where(ProductDrawing.id == int(key.split("-")[1]),
                            ProductDrawing.product_id == product.id, engineering_drawing_condition()))
        source = DrawingSource(key, drawing.image_path, thumbnail=drawing.thumbnail_path) if drawing else None
    elif re.fullmatch(r"release-[1-9][0-9]*", key):
        release = db.scalar(select(DrawingRelease).where(DrawingRelease.id == int(key.split("-")[1]),
            DrawingRelease.product_id == product.id, DrawingRelease.customer_id == product.customer_id,
            DrawingRelease.product_version == product.version))
        source = DrawingSource(key, release.pdf_reference) if release else None
    else:
        source = None
    if source is None or not _engineering_reference(source.reference):
        raise failure(404, "产品图纸不存在或当前账号无权查看")
    return source


def drawing_file(source: DrawingSource) -> Path:
    try:
        path = resolve_stored_reference(source.reference)
        if not path.is_file():
            raise FileNotFoundError
        if path.stat().st_size > MAX_BYTES:
            raise failure(413, "图纸文件过大，请在电脑端查看")
        return path
    except (OSError, ValueError):
        raise failure(404, "产品图纸文件不存在，请联系管理员核对") from None


def _read_drawing_bytes(path: Path) -> bytes:
    try:
        with path.open("rb") as handle:
            content = handle.read(MAX_BYTES + 1)
    except OSError:
        raise failure(404, "产品图纸文件不存在，请联系管理员核对") from None
    if len(content) > MAX_BYTES:
        raise failure(413, "图纸文件过大，请在电脑端查看")
    return content


def _pdf(source: Path | bytes, *, preview: bool):
    content = _read_drawing_bytes(source) if isinstance(source, Path) else source
    try:
        import fitz
    except ImportError:
        raise failure(422, "PDF图纸暂无法读取，请联系管理员") from None
    try:
        if not content[:1024].lstrip().startswith(b"%PDF-"):
            raise ValueError
        with fitz.open(stream=content, filetype="pdf") as document:
            if document.needs_pass or document.page_count < 1:
                raise ValueError
            page = document[0]
            width, height = page.rect.width, page.rect.height
            if (not all(math.isfinite(v) and 0 < v <= 14400 for v in (width, height))
                    or width * height > 100_000_000):
                raise ValueError
            if not preview:
                return "application/pdf"
            scale = min(PREVIEW_EDGE / width, PREVIEW_EDGE / height, 2)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
            return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    except Exception:
        raise failure(422, "PDF图纸损坏、加密或页面过大，无法预览") from None


def _image(source: Path | bytes) -> Image.Image:
    content = _read_drawing_bytes(source) if isinstance(source, Path) else source
    try:
        with Image.open(BytesIO(content)) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"} or image.width * image.height > MAX_PIXELS:
                raise ValueError
            image.load()
            oriented = ImageOps.exif_transpose(image)
            if "A" in oriented.getbands() or "transparency" in oriented.info:
                rgba = oriented.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                return background
            return oriented.convert("RGB")
    except (FileNotFoundError, PermissionError):
        raise failure(404, "产品图纸文件不存在，请联系管理员核对") from None
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise failure(422, "图片图纸损坏或像素过大，无法预览") from None


def original_media_type(content: bytes, suffix: str) -> str:
    if suffix == ".pdf":
        # Never serve HTML/SVG disguised as a PDF. Parse only on explicit
        # preview/download requests, keeping search metadata independent of IO.
        if not content[:1024].lstrip().startswith(b"%PDF-"):
            raise failure(422, "PDF图纸无法识别")
        return _pdf(content, preview=False)
    _image(content)
    return {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}[suffix]


def drawing_original(path: Path) -> tuple[bytes, str]:
    # Validate and send the same bounded snapshot. A FileResponse would defer
    # its stat/open until after validation and could send a replaced file or 500.
    content = _read_drawing_bytes(path)
    return content, original_media_type(content, path.suffix.lower())


def drawing_preview(source: DrawingSource, path: Path) -> bytes:
    if path.suffix.lower() == ".pdf":
        image = _pdf(path, preview=True)
    else:
        image = None
        if source.thumbnail and _engineering_reference(source.thumbnail):
            try:
                candidate = resolve_stored_reference(source.thumbnail)
                if candidate.is_file() and candidate.stat().st_size <= MAX_BYTES and candidate.suffix.lower() != ".pdf":
                    image = _image(candidate)
            except HTTPException as error:
                if error.status_code not in (404, 422):
                    raise
                # A broken/missing derived thumbnail cannot hide a valid
                # engineering original. Authorization/source validation has
                # already finished and is never caught here.
            except (OSError, ValueError):
                pass
        if image is None:
            image = _image(path)
    image.thumbnail((PREVIEW_EDGE, PREVIEW_EDGE), Image.Resampling.LANCZOS)
    buffer = BytesIO()
    image.save(buffer, "WEBP", quality=82)
    return buffer.getvalue()
