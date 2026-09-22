"""Deterministic raster bounds; white pixels are never inferred to be paper."""
from decimal import Decimal
from io import BytesIO

from fastapi import HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError


def inspect_artwork(content: bytes) -> dict:
    try:
        with Image.open(BytesIO(content)) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"} or image.width * image.height > 25_000_000:
                raise HTTPException(422, "印刷图片格式不支持或超过2500万像素")
            if getattr(image, "n_frames", 1) != 1:
                raise HTTPException(422, "印刷图片须为单帧静态图片")
            original_format = image.format
            orientation = image.getexif().get(274, 1)
            image.load()
            image = ImageOps.exif_transpose(image)
            alpha = image.convert("RGBA").getchannel("A")
            bounds = alpha.getbbox()
            if bounds is None:
                raise HTTPException(422, "图片全部透明，不能作为印刷内容")
            return {"canvas_pixels": list(image.size), "alpha_bounds_pixels": list(bounds),
                    "format": original_format, "orientation_normalized": orientation not in (None, 1),
                    "has_transparent_margin": bounds != (0, 0, image.width, image.height)}
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise HTTPException(422, "印刷图片无法安全解码") from error


def prepare_artwork(content: bytes, obj: dict) -> tuple[bytes, str, str, dict]:
    info = inspect_artwork(content)
    crop = obj.get("image_bounds", "canvas") == "alpha"
    left, top, right, bottom = info["alpha_bounds_pixels"]
    width, height = (right-left, bottom-top) if crop else info["canvas_pixels"]
    expected = Decimal(str(obj["width_mm"])) * height / width
    if abs(expected - Decimal(str(obj["height_mm"]))) > Decimal("0.01"):
        raise HTTPException(422, "图片宽高与所选范围的比例不一致，请保持比例；不会拉伸Logo")
    extension, mime = {"PNG": (".png", "image/png"), "JPEG": (".jpg", "image/jpeg"),
                       "WEBP": (".webp", "image/webp")}[info["format"]]
    if crop or info["orientation_normalized"]:
        with Image.open(BytesIO(content)) as image:
            output = BytesIO()
            image = ImageOps.exif_transpose(image).convert("RGBA")
            if crop:
                image = image.crop((left, top, right, bottom))
            image.save(output, "PNG")
            content, extension, mime = output.getvalue(), ".png", "image/png"
    # The paper/mobile consumers enforce this same per-image budget. Check
    # after EXIF/cropping too: a small compressed JPEG can expand as PNG.
    if len(content) > 5_000_000:
        raise HTTPException(422, "印刷处理稿超过5MB，请缩小图片像素后重新上传；原件保留")
    return content, extension, mime, info
