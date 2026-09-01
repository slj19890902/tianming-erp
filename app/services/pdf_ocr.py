"""
Phase 19 / v0.19.0: PDF OCR 服务。

识别策略（按优先级）：
  1. PyMuPDF (fitz) — 将 PDF 每页渲染为图片
  2. OCR 引擎（按可用性选择）：
     a. EasyOCR — 纯 Python，中文支持良好，无需系统二进制
     b. pytesseract + Tesseract — 需要系统安装 Tesseract
  3. 如果没有可用引擎，返回 (None, "ocr_unavailable")，不崩溃

OCR 结果仅用于训练样本库，不直接生成正式订单。
临时图片保存在 data/pdf_training_samples/_tmp/，识别完成后删除。
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)


def analyze_pdf_text_quality(pdf_text: str | None) -> dict[str, int | float | str]:
    """Classify PDF text layers that look long but are not usable text."""
    text = pdf_text or ""
    compact = re.sub(r"\s+", "", text)
    basic_han = len(re.findall(r"[\u4e00-\u9fff]", text))
    extension_a = len(re.findall(r"[\u3400-\u4dbf]", text))
    unexpected_controls = sum(
        ord(char) < 32 and char not in "\n\r\t" for char in text
    )
    han_total = basic_han + extension_a
    extension_a_ratio = extension_a / max(han_total, 1)
    if len(compact) < 50:
        status = "image_only"
    elif (
        extension_a >= 10 and extension_a_ratio >= 0.15
    ) or unexpected_controls >= 2:
        status = "garbled_text_layer"
    else:
        status = "readable_text"
    return {
        "status": status,
        "text_chars": len(compact),
        "basic_han_chars": basic_han,
        "extension_a_chars": extension_a,
        "extension_a_ratio": round(extension_a_ratio, 4),
        "unexpected_control_chars": unexpected_controls,
    }

# ---------------------------------------------------------------------------
# 可用性探测（懒加载，不在模块导入时崩溃）
# ---------------------------------------------------------------------------

def _try_import_fitz():
    try:
        import pymupdf  # type: ignore
        return pymupdf
    except ImportError:
        try:
            import fitz  # type: ignore  # PyMuPDF legacy module name
            return fitz
        except ImportError:
            return None


def _try_import_easyocr():
    try:
        import easyocr  # type: ignore
        return easyocr
    except ImportError:
        return None


def _try_import_pytesseract():
    try:
        import pytesseract  # type: ignore
        # 额外检查：tesseract 二进制是否存在
        import shutil
        if shutil.which("tesseract") is None:
            return None
        return pytesseract
    except ImportError:
        return None


def ocr_available() -> bool:
    """返回 True 如果至少有一个 OCR 引擎可用。"""
    if _try_import_fitz() is None:
        return False
    return _try_import_easyocr() is not None or _try_import_pytesseract() is not None


def ocr_engine_name() -> str:
    """返回当前可用 OCR 引擎名称，供日志和 API 展示。"""
    fitz = _try_import_fitz()
    if fitz is None:
        return "none (pymupdf missing)"
    if _try_import_easyocr() is not None:
        return "easyocr"
    if _try_import_pytesseract() is not None:
        return "tesseract"
    return "none (no ocr engine)"


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------

# EasyOCR reader 单例（避免重复加载模型）
_easyocr_reader = None


def _get_easyocr_reader():
    """懒加载 EasyOCR 中文识别器（首次加载约需 2-10 秒）。"""
    global _easyocr_reader
    if _easyocr_reader is None:
        easyocr_mod = _try_import_easyocr()
        if easyocr_mod is None:
            raise RuntimeError("easyocr 未安装")
        # 中文简体 + 英文，gpu=False 避免需要 CUDA
        _easyocr_reader = easyocr_mod.Reader(["ch_sim", "en"], gpu=False, verbose=False)
    return _easyocr_reader


def _pdf_to_images_bytes(pdf_content: bytes, dpi: int = 200) -> list[bytes]:
    """
    用 PyMuPDF 将 PDF 每页渲染为 PNG 字节串列表。
    dpi=200 保留采购明细中较小的名称、规格和供应商参考号。
    """
    fitz = _try_import_fitz()
    if fitz is None:
        raise RuntimeError("pymupdf 未安装，无法渲染 PDF 页面为图片")

    doc = fitz.open(stream=pdf_content, filetype="pdf")
    images: list[bytes] = []
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    for page in doc:
        pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
        images.append(pix.tobytes("png"))
    doc.close()
    return images


def _ocr_with_easyocr(image_bytes_list: list[bytes]) -> str:
    """用 EasyOCR 对图片列表做 OCR，拼接结果文本。"""
    reader = _get_easyocr_reader()
    page_texts: list[str] = []
    for img_bytes in image_bytes_list:
        import numpy as np  # type: ignore
        from PIL import Image
        from io import BytesIO

        pil_img = Image.open(BytesIO(img_bytes))
        img_array = np.array(pil_img)
        results = reader.readtext(img_array, detail=1, paragraph=False)
        # results: list of ([bbox], text, confidence)
        # 按 Y 坐标（上方框中心）排序，然后按 X 坐标，模拟阅读顺序
        results.sort(key=lambda r: (int(r[0][0][1] / 20), int(r[0][0][0])))
        lines = [r[1] for r in results if r[2] > 0.2]  # confidence > 0.2
        page_texts.append(" ".join(lines))
    return "\n".join(page_texts)


def _ocr_with_tesseract(image_bytes_list: list[bytes]) -> str:
    """用 Tesseract 对图片列表做 OCR（仅当 easyocr 不可用时使用）。"""
    pytesseract = _try_import_pytesseract()
    if pytesseract is None:
        raise RuntimeError("pytesseract 或 Tesseract 二进制未安装")
    from PIL import Image
    from io import BytesIO

    page_texts: list[str] = []
    for img_bytes in image_bytes_list:
        img = Image.open(BytesIO(img_bytes))
        text = pytesseract.image_to_string(img, lang="chi_sim+eng")
        page_texts.append(text)
    return "\n".join(page_texts)


# ---------------------------------------------------------------------------
# 主接口
# ---------------------------------------------------------------------------

def ocr_pdf_bytes(
    pdf_content: bytes,
    *,
    dpi: int = 200,
) -> tuple[str | None, str]:
    """
    对 PDF 字节内容执行 OCR。

    返回值：
        (ocr_text, method)
        ocr_text : OCR 识别到的文本，失败时为 None
        method   : "ocr_easyocr" / "ocr_tesseract" / "ocr_unavailable" / "ocr_failed"
    """
    fitz = _try_import_fitz()
    if fitz is None:
        logger.warning("OCR 不可用：pymupdf 未安装")
        return None, "ocr_unavailable"

    has_easyocr = _try_import_easyocr() is not None
    has_tesseract = _try_import_pytesseract() is not None

    if not has_easyocr and not has_tesseract:
        logger.warning("OCR 不可用：未检测到 easyocr 或 tesseract")
        return None, "ocr_unavailable"

    try:
        images = _pdf_to_images_bytes(pdf_content, dpi=dpi)
        if not images:
            return None, "ocr_failed"

        if has_easyocr:
            text = _ocr_with_easyocr(images)
            method = "ocr_easyocr"
        else:
            text = _ocr_with_tesseract(images)
            method = "ocr_tesseract"

        if not text or not text.strip():
            return None, "ocr_failed"

        return text.strip(), method

    except Exception as exc:
        logger.exception("OCR 执行失败: %s", exc)
        return None, "ocr_failed"


def should_use_ocr(pdf_text: str | None, parse_result: dict | None) -> bool:
    """
    判断是否应该对该 PDF 触发 OCR。

    触发条件（满足任一）：
    1. PDF 文本为空或极少（< 50 字符）→ 图片扫描件
    2. PDF 文本存在但解析失败（parse_result 为 None 或 items 为空）→ 可能乱码
    3. PDF 文本中中文字符比例极低但文本不短 → 字体编码问题（思迈尔等）
    """
    quality = analyze_pdf_text_quality(pdf_text)
    if quality["status"] != "readable_text":
        return True

    if parse_result is None:
        return True

    if parse_result and not parse_result.get("items"):
        return True

    return False
