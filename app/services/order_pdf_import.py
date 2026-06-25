from __future__ import annotations

import hashlib
import re
from decimal import Decimal
from io import BytesIO

from pypdf import PdfReader
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from app.models.customer import Customer
from app.models.material import Material
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.api.materials import _parse_layer_weights
from app.services.pricing import PricingError, calculate_price


class PdfParseError(ValueError):
    """PDF 解析失败，携带结构化失败原因码（parse_status）。

    继承 ValueError 以兼容既有 `except ValueError` 调用点。
    """

    def __init__(self, message: str, parse_status: str = "failed") -> None:
        super().__init__(message)
        self.message = message
        self.parse_status = parse_status


# ---------------------------------------------------------------------------
# 正则表达式
# ---------------------------------------------------------------------------

# v0.19.1: 扩展支持思迈尔 P-XXXXXXX(-N) 格式
ORDER_NO_RE = re.compile(
    r"\b((?:THPO|PO)[A-Z0-9-]{6,}|P-\d{7}(?:-\d+)?)\b",
    re.IGNORECASE,
)
DATE_RE = re.compile(r"\b(20\d{2})[-/.](\d{2})[-/.](\d{2})\b")
ROW_START_RE = re.compile(r"^\d+\s+\S+")
ITEM_RE = re.compile(
    r"^(?P<line_no>\d+)\s+(?P<product_code>\S+)\s+(?P<body>.+?)\s+"
    r"(?P<unit>\S+)\s+(?P<quantity>\d+(?:\.\d+)?)\s+"
    r"(?P<unit_price>\d+(?:\.\d+)?)\s+(?P<amount>[\d,]+(?:\.\d+)?)\s+"
    r"(?P<delivery_date>20\d{2}[./]\d{2}[./]\d{2})$"
)
# 带额外列（番号/销售订单号）的 ITEM_RE：行号 番号 料品编码 物料名称 规格 单位 数量 单价 金额 销售订单号 交货日期
ITEM_RE_EXTRA = re.compile(
    r"^(?P<line_no>\d+)\s+(?P<extra1>\S+)\s+(?P<product_code>\S+)\s+(?P<body>.+?)\s+"
    r"(?P<unit>\S+)\s+(?P<quantity>\d+(?:\.\d+)?)\s+"
    r"(?P<unit_price>\d+(?:\.\d+)?)\s+(?P<amount>[\d,]+(?:\.\d+)?)\s+"
    r"(?P<extra2>\S+)\s+"
    r"(?P<delivery_date>20\d{2}[./]\d{2}[./]\d{2})$"
)
# 规格起始匹配
SPEC_START_RE = re.compile(
    r"(?<!\()(?=(?:\d+(?:\.\d+)?(?:cm|mm|\*)|"
    r"\d+(?:\.\d+)?\s*[xX*]\s*\d+))",
    re.IGNORECASE,
)
# v0.19.1 F-3: 修复斜杠规格截断，支持 115*67*2.5/2.8cm
# 第三维度允许 /N.N 形式（双厚度：2.5/2.8cm）
DIMENSION_RE = re.compile(
    r"(?<!\d)(\d+(?:\.\d+)?\s*[×xX*]\s*\d+(?:\.\d+)?"
    r"(?:\s*[×xX*]\s*\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?)?"
    r"\s*(?:cm|mm)?)(?!\d)",
    re.IGNORECASE,
)

# 天华旧材质代码：W535A/AB、T5P/A、A535T/AB；
# v0.19.2: 兼容数字开头代码（如 9CCC9/AB），要求至少含一个字母以排除纯尺寸比值。
OLD_MATERIAL_CODE_RE = re.compile(
    r"\b([0-9A-Z]*[A-Z][0-9A-Z]*/(?:AB|BE|A|B|E))\b",
    re.IGNORECASE,
)
# 天华型号：THH10 / THB10 / THH8 / THB8
TIANHUA_MODEL_RE = re.compile(r"\b(TH[HB]\d+)\b", re.IGNORECASE)
# 天华 extra mark
EXTRA_MARK_RE = re.compile(r"\b([A-Z])\b(?!\d)")
# v0.19.1 F-4: 包装注记（不能混入 size_spec）
PACKAGING_ANNOTATION_RE = re.compile(
    r"\d+\s*[盒本件片袋]\s*/\s*[箱盒件袋]+",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# 客户类型识别
# ---------------------------------------------------------------------------

# 支持的客户类型
CUSTOMER_TYPES = {
    "tianhua_chao":   "天华超净（苏州天华超净有限公司）",
    "tianhua_energy": "天华新能源（苏州天华新能源科技股份有限公司）",
    "simair":         "思迈尔（苏州思迈尔电子设备有限公司）",
    "tianming":       "天明（苏州天明包装有限公司）",
    "unknown":        "未识别客户",
}


def _detect_customer_type(
    customer_name: str | None,
    customer_po: str | None,
    text: str | None = None,
) -> str:
    """识别客户类型，返回 CUSTOMER_TYPES 中的 key。"""
    name = (customer_name or "").lower()
    po = (customer_po or "").upper()
    full_text = (text or "").lower()

    # 天华新能源（比天华超净更具体，先匹配）
    if "新能源" in name or "新能源" in full_text:
        return "tianhua_energy"
    # 天华超净
    if "天华" in name or "canmax" in name or "tianhua" in name or "thpo" in name:
        return "tianhua_chao"
    if po.startswith("THPO") or po.startswith("PO20"):
        return "tianhua_chao"
    # 思迈尔
    if "思迈尔" in name or "simair" in name or "思迈尔" in full_text:
        return "simair"
    if re.match(r"P-\d{7}", po):
        return "simair"
    # 天明（天明 PDF 通常是销售方向，不是采购订单）
    if "天明" in name or "天明" in full_text:
        return "tianming"
    return "unknown"


def _is_tianhua_customer(customer_name: str | None, customer_po: str | None = None) -> bool:
    """判断是否是天华系客户（超净或新能源）。"""
    ct = _detect_customer_type(customer_name, customer_po)
    return ct in ("tianhua_chao", "tianhua_energy")


# ---------------------------------------------------------------------------
# 图片/OCR 检测
# ---------------------------------------------------------------------------

def _detect_pdf_type(content: bytes) -> dict:
    """
    检测 PDF 是否图片型，并判断是否需要 OCR。
    返回包含 is_image_pdf / ocr_required / page_count / image_count 的字典。
    """
    result = {
        "is_image_pdf": False,
        "ocr_required": False,
        "page_count": 0,
        "image_count": 0,
        "text_block_count": 0,
    }
    try:
        reader = PdfReader(BytesIO(content))
        result["page_count"] = len(reader.pages)
        total_text = 0
        total_images = 0
        for page in reader.pages:
            text = page.extract_text() or ""
            total_text += len(text.strip())
            # 统计页面内图像资源数（XObject/Image）
            resources = page.get("/Resources")
            if resources:
                xobjects = resources.get("/XObject", {})
                for key in xobjects:
                    obj = xobjects[key]
                    if hasattr(obj, "get") and obj.get("/Subtype") == "/Image":
                        total_images += 1
        result["text_block_count"] = total_text
        result["image_count"] = total_images
        # 判断：文本极少 + 有图像 → 图片型
        if total_text < 50 and total_images > 0:
            result["is_image_pdf"] = True
            result["ocr_required"] = True
    except Exception:
        pass
    return result


def _chinese_ratio(text: str) -> float:
    """计算文本中中文字符占比。"""
    if not text:
        return 0.0
    chinese = sum(1 for c in text if "一" <= c <= "鿿")
    return chinese / len(text)


def should_use_ocr(text: str, parse_result: dict | None) -> bool:
    """判断是否需要 OCR。"""
    if not text or not text.strip():
        return True
    if _chinese_ratio(text) < 0.01:
        return True
    if parse_result is None:
        return False
    if not parse_result.get("items"):
        return True
    return False


# ---------------------------------------------------------------------------
# OCR 调用
# ---------------------------------------------------------------------------

def _try_ocr(content: bytes) -> tuple[str, str]:
    """尝试对 PDF 进行 OCR。返回 (ocr_text, parse_method)。"""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return "", "ocr_unavailable"
    try:
        import easyocr
    except ImportError:
        return "", "ocr_unavailable"
    try:
        doc = fitz.open(stream=content, filetype="pdf")
        all_text: list[str] = []
        reader = easyocr.Reader(["ch_sim", "en"], gpu=False, verbose=False)
        for page in doc:
            mat = fitz.Matrix(2, 2)
            pix = page.get_pixmap(matrix=mat)
            img_bytes = pix.tobytes("png")
            results = reader.readtext(img_bytes, detail=0, paragraph=True)
            all_text.extend(results)
        return "\n".join(all_text), "ocr_easyocr"
    except Exception:
        return "", "ocr_failed"


def ocr_available() -> bool:
    """检查 OCR 引擎是否可用。"""
    try:
        import fitz  # noqa: F401
        import easyocr  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# 文本提取与工具函数
# ---------------------------------------------------------------------------

def file_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def extract_text_from_pdf_bytes(content: bytes) -> str:
    reader = PdfReader(BytesIO(content))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _clean_line(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("　", " ")).strip()


def _normalize_date(raw: str | None) -> str | None:
    match = DATE_RE.search(raw or "")
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}" if match else None


def _decimal_to_str(raw: str, places: str) -> str:
    return format(Decimal(raw.replace(",", "")).quantize(Decimal(places)), "f")


def _company_name_key(value: str | None) -> str:
    text = re.sub(r"[（）()\-—_·,，.。/\\\s]", "", value or "")
    for suffix in ("股份有限公司", "有限责任公司", "有限公司"):
        text = text.replace(suffix, "")
    return text.casefold()


def _normalized_text(value: str | None) -> str:
    return re.sub(r"[\s（）()\-—_·,，.。/\\\"'×xX*]", "", value or "").casefold()


def _extract_customer_name(lines: list[str], customer_po: str) -> str | None:
    try:
        po_index = next(i for i, line in enumerate(lines) if customer_po in line)
    except StopIteration:
        return None
    for index in range(po_index - 1, -1, -1):
        line = lines[index]
        if any(token in line for token in ("采购订单", "订单号", "供应商全称", "人民币元")):
            continue
        if "苏州天明包装有限公司" in line or DATE_RE.search(line):
            continue
        if len(line) >= 4:
            return line
    return None


def _join_record_lines(lines: list[str]) -> str:
    """合并明细行，修复跨行断号。

    已处理场景：
      '18.' + '5cm'   → '18.5cm'   （行尾数字+点，下行以数字开头）
    Hotfix-2 新增：
      '2.3/2' + '.6cm' → '2.3/2.6cm' （斜杠厚度跨行断开，行尾数字，下行以 .数字 开头）
    """
    parts: list[str] = []
    for line in lines:
        if parts:
            prev = parts[-1]
            # 原有：行尾 "\d." + 下行 "\d..." → 直接拼接
            if re.search(r"\d\.$", prev) and re.match(r"^\d", line):
                parts[-1] = prev + line
                continue
            # Hotfix-2：行尾 "\d" + 下行 "\.数字..." → 直接拼接（斜杠规格跨行）
            if re.search(r"\d$", prev) and re.match(r"^\.\d", line):
                parts[-1] = prev + line
                continue
        parts.append(line)
    return " ".join(parts)


def _split_records(lines: list[str]) -> tuple[list[list[str]], bool, bool]:
    """
    拆分明细行。
    返回 (records, has_extra_columns, header_found)。
    has_extra_columns=True 表示表头有额外列（番号/销售订单号）。
    header_found=True 表示找到了明细表头行。
    """
    records: list[list[str]] = []
    current: list[str] = []
    in_table = False
    has_extra_columns = False
    header_found = False
    for line in lines:
        if "行号" in line and ("料品编码" in line or "物料编码" in line) and "交货日期" in line:
            in_table = True
            header_found = True
            # v0.19.1 F-5: 检测额外列
            if "番号" in line or "销售订单号" in line:
                has_extra_columns = True
            continue
        if not in_table:
            continue
        if line.startswith("合计"):
            # v0.19.2 多页修复：'合计' 是每页页脚（多页 PDF 每页都重复打印同一
            # 合计行），不能据此终止解析，否则第 2~N 页明细会全部丢失。
            # 改为：结束当前记录并退出表格态，等待下一页 '行号...' 表头重新激活。
            if current:
                records.append(current)
                current = []
            in_table = False
            continue
        if ROW_START_RE.match(line):
            if current:
                records.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current and current not in records:
        records.append(current)
    return records, has_extra_columns, header_found


def _find_spec_start_outside_parens(text: str) -> int | None:
    """返回规格起始位置（跳过括号内的内容）。

    v0.19.2-A A-1: 同时识别半角 ()、全角 （）。天华 PDF 物料名称内常带
    全角括号尺寸标注（如 白底黑字内箱（18"*36"）），括号内的数字不得被
    当成规格起点，否则物料名称会被截断、括号内容被错误切入规格型号。
    """
    depth = 0
    for i, c in enumerate(text):
        if c in ("(", "（"):
            depth += 1
        elif c in (")", "）"):
            depth = max(0, depth - 1)
        elif depth == 0 and c.isdigit():
            if re.match(r"\d+(?:\.\d+)?(?:cm|mm|[*xX])", text[i:], re.IGNORECASE):
                return i
    return None


def _split_name_and_spec(first_body: str, full_body: str) -> tuple[str, str]:
    pos = _find_spec_start_outside_parens(first_body)
    if pos is not None and pos > 0:
        name = first_body[:pos].strip()
        first_spec = first_body[pos:].strip()
        remaining = full_body[len(first_body):].strip()
        return name, _clean_line(f"{first_spec} {remaining}")
    # v0.19.2 多页修复：first_body（仅第一物理行）未找到规格起点时，品名可能跨行，
    # 且括号尺寸标注（如 （18”*36”））常单独成行。改为在完整 body 上做括号感知的
    # 规格定位，使 "白底黑字内箱" + "（18”*36”）" 合并为完整品名，规格从 93.5*... 起。
    pos_full = _find_spec_start_outside_parens(full_body)
    if pos_full is not None and pos_full > 0:
        name = re.sub(r"\s+", "", full_body[:pos_full].strip())
        spec = full_body[pos_full:].strip()
        return name, spec
    # 整个 first_body 即为品名（无规格，如垫板）。
    name = first_body.strip()
    spec = full_body[len(first_body):].strip()
    return name, spec


def _extract_spec_dimensions(raw_spec: str) -> str:
    """
    v0.19.1 F-3: 从规格原文中提取尺寸，保留斜杠形式（115*67*2.5/2.8cm 不截断）。
    """
    candidates = [match.group(1) for match in DIMENSION_RE.finditer(raw_spec)]
    if not candidates:
        return raw_spec.strip()
    three_dimensional = [
        value for value in candidates if len(re.findall(r"[×xX*]", value)) >= 2
    ]
    selected = three_dimensional[0] if three_dimensional else candidates[0]
    return re.sub(r"\s+", "", selected)


# ---------------------------------------------------------------------------
# 天华订单专项字段解析
# ---------------------------------------------------------------------------

_FLUTE_SUFFIX_MAP = {
    "AB": ("AB", 5),
    "BE": ("BE", 5),
    "A":  ("A",  3),
    "B":  ("B",  3),
    "E":  ("E",  3),
}


def _parse_old_material_code(code: str | None) -> dict:
    """解析旧材质代码，返回 flute_type / layer_count / surface_paper_type。"""
    if not code:
        return {}
    m = re.search(r"/([A-Z]+)$", code.upper())
    if not m:
        return {}
    suffix = m.group(1)
    if suffix not in _FLUTE_SUFFIX_MAP:
        return {}
    flute_type, layer_count = _FLUTE_SUFFIX_MAP[suffix]
    surface = "white" if re.match(r"^W", code, re.IGNORECASE) else None
    return {
        "flute_type": flute_type,
        "layer_count": layer_count,
        "surface_paper_type": surface,
    }


# 中日文/全角符号检测（用于判定是否为有生产价值的说明）
_CJK_JP_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿　-〿＀-￯]")


def _extract_production_notes(
    spec_raw: str,
    size_spec: str,
    old_mat: str,
    customer_model: str,
) -> str:
    """
    v0.19.2-A: 从规格型号原文中抽出"生产/印刷/打勾/摆放/日文警示"等说明。

    规则：
      1. 移除已分类内容：尺寸、旧材质代码、TH型号、包装注记。
      2. 移除与中日文不相邻的纯 ASCII / 数字 / 符号噪声。
      3. 仅当残留文本包含中文/日文假名/全角符号时才视为有效说明，否则返回空串。
    不丢弃无法归类但有业务价值的中/日文文本。
    """
    if not spec_raw:
        return ""
    notes = spec_raw
    # 移除尺寸（含斜杠厚度）
    for match in DIMENSION_RE.finditer(spec_raw):
        notes = notes.replace(match.group(1), " ")
    if size_spec:
        notes = notes.replace(size_spec, " ")
    if old_mat:
        notes = notes.replace(old_mat, " ")
    if customer_model:
        notes = re.sub(re.escape(customer_model), " ", notes, flags=re.IGNORECASE)
    # 移除包装注记（不展示）
    notes = PACKAGING_ANNOTATION_RE.sub(" ", notes)
    # 移除与中日文不相邻的 ASCII/数字/英文标点噪声
    notes = re.sub(
        r"(?<![぀-ヿ一-鿿])[A-Za-z0-9/().,*×xX\-]+(?![぀-ヿ一-鿿])",
        " ",
        notes,
    )
    # v0.19.2: 残留为纯中/日文警示语，PDF 换行产生的空格需全部去除，
    # 否则 '在白色处 打钩' 无法与期望 '在白色处打钩' 匹配。
    notes = re.sub(r"\s+", "", notes).strip("，,、.。/\\-*×xX")
    if not notes or not _CJK_JP_RE.search(notes):
        return ""
    return notes


def _enrich_tianhua_item(item: dict, spec_raw: str) -> dict:
    """
    从规格型号原文中提取天华专属字段。
    v0.19.1 F-4: 清除包装注记（5盒/箱、3本/盒 等）不混入 size_spec。
    """
    enriched = dict(item)

    enriched["customer_material_code"] = item.get("product_code", "")

    th_match = TIANHUA_MODEL_RE.search(spec_raw)
    enriched["customer_model"] = th_match.group(1).upper() if th_match else ""

    mat_match = OLD_MATERIAL_CODE_RE.search(spec_raw)
    old_mat = mat_match.group(1).upper() if mat_match else ""
    enriched["old_material_code"] = old_mat

    mat_info = _parse_old_material_code(old_mat)
    enriched.update(mat_info)

    # v0.19.2-A A-2/A-3: size_spec 只保留纯尺寸（含 2.5/2.8cm 斜杠厚度，不截断）
    enriched["size_spec"] = _extract_spec_dimensions(spec_raw)
    enriched["specification"] = enriched["size_spec"]
    enriched["raw_spec_model"] = enriched["size_spec"]

    # extra_mark：去掉尺寸/材质/型号后残留的单字母标记（如 R）
    size_text = spec_raw
    if old_mat:
        size_text = size_text.replace(old_mat, " ")
    if enriched["customer_model"]:
        size_text = size_text.replace(enriched["customer_model"], " ")
    for dim in DIMENSION_RE.findall(spec_raw):
        size_text = size_text.replace(dim if isinstance(dim, str) else dim[0], " ")
    extra_candidates = EXTRA_MARK_RE.findall(size_text)
    extra_mark = extra_candidates[-1] if extra_candidates else ""
    enriched["extra_mark"] = extra_mark

    # v0.19.1 F-4 / v0.19.2-A 问题7: 包装注记（5盒/箱 等）仅内部识别，丢弃，不展示
    packaging_notes = PACKAGING_ANNOTATION_RE.findall(spec_raw)
    enriched["packaging_note"] = packaging_notes[0] if packaging_notes else ""

    # v0.19.2-A A-3/问题4: 规格栏里无法归类但有生产价值的中/日文说明 → production_notes
    enriched["production_notes"] = _extract_production_notes(
        spec_raw,
        size_spec=enriched["size_spec"],
        old_mat=old_mat,
        customer_model=enriched["customer_model"],
    )

    pname = item.get("product_name", "")
    model = enriched["customer_model"]
    enriched["display_product_name"] = f"{pname} {model}".strip() if model else pname

    return enriched


def _parse_record(record_lines: list[str], has_extra_columns: bool = False) -> dict | None:
    """
    解析单条明细记录。
    v0.19.1 F-1: 修复多行品名 bug（行号+料品编码单独一行，品名在后续行）。
    v0.19.1 F-2: 修复垫板 size_spec 为空 bug（name_len 计算错误）。
    v0.19.1 F-5: 支持额外列（番号/销售订单号）。
    """
    # 修复跨行断号
    joined = _join_record_lines(record_lines)

    # F-5: 额外列时优先尝试 ITEM_RE_EXTRA
    match = None
    if has_extra_columns:
        match = ITEM_RE_EXTRA.match(joined)
    if match is None:
        match = ITEM_RE.match(joined)
    if not match:
        return None

    # F-1: 检测 record_lines[0] 是否只有"行号 料品编码"（无品名）
    # 如果 regex 把 record_lines[0] 完整吃掉而没有品名文字，则品名在后续行
    first_line = record_lines[0]
    # 去掉"行号 料品编码"前缀后剩余内容
    stripped_first = re.sub(r"^\d+\s+\S+\s*", "", first_line, count=1).strip()

    if stripped_first:
        # 正常情况：第一行含品名（部分）
        first_body = stripped_first
        name_start_lines = record_lines[1:]  # 后续行用于补全品名/规格
    else:
        # F-1 触发：第一行只有"行号 料品编码"，品名从第二行开始
        first_body = ""
        name_start_lines = record_lines[1:]

    body = match.group("body")  # ITEM_RE 解析出的完整 body

    if first_body:
        raw_name, raw_spec = _split_name_and_spec(first_body, body)
    else:
        # F-1: 品名从 body 中解析（body 已经是完整的 product_name + spec 合并串）
        raw_name, raw_spec = _split_name_and_spec(body, body)
    # v0.19.2: 直接使用 _split_name_and_spec 返回的完整规格串做富化；不再用
    # body[len(raw_name):] 切割——品名空格归一化后长度与 body 不再对齐，
    # 旧切法会把品名尾部括号 '）' 误并入规格/生产说明。
    raw_spec_for_enrichment = raw_spec

    raw_spec = _extract_spec_dimensions(raw_spec)
    code = match.group("product_code")

    return {
        "line_no": int(match.group("line_no")),
        "raw_product_code": code,
        "raw_product_name": raw_name,
        "raw_spec_model": raw_spec,
        "raw_spec_for_enrichment": raw_spec_for_enrichment,
        "product_code": code,
        "product_name": raw_name,
        "specification": raw_spec,
        "unit": match.group("unit"),
        "quantity": int(Decimal(match.group("quantity"))),
        "unit_price": _decimal_to_str(match.group("unit_price"), "0.0000"),
        "amount": _decimal_to_str(match.group("amount"), "0.00"),
        "delivery_date": _normalize_date(match.group("delivery_date")),
        "raw_lines": record_lines,
        "production_notes": "",
        "matched_product_id": None,
        "matched_material_id": None,
        "match_status": "unmatched",
        "cost_status": "pending",
        "product_candidates": [],
        "material_candidates": [],
    }


# ---------------------------------------------------------------------------
# 明细质量检查（管理端提示用）
# ---------------------------------------------------------------------------

def _check_item_warnings(item: dict) -> list[str]:
    """
    对单条解析结果检查质量问题，返回警告列表。
    供 PDF 训练详情页面显示给管理员。
    """
    warnings: list[str] = []
    pname = item.get("product_name") or item.get("raw_product_name") or ""
    spec = item.get("size_spec") or item.get("raw_spec_model") or ""
    old_mat = item.get("old_material_code") or ""
    customer_model = item.get("customer_model") or ""
    line_no = item.get("line_no", "?")

    # 物料名称疑似行号
    if pname and re.fullmatch(r"\d{1,3}", pname.strip()):
        warnings.append(f"第{line_no}行：物料名称疑似识别为行号（\"{pname}\"），可能是多行品名 bug。")

    # size_spec 为空但有 old_material_code（垫板等物料）
    if not spec.strip() and old_mat:
        warnings.append(f"第{line_no}行：规格型号为空，但找到材质代码 {old_mat}，可能丢失了尺寸信息。")

    # size_spec 含斜杠数字（可能截断）
    if spec and re.search(r"\d+\.\d+/\d+", spec):
        warnings.append(f"第{line_no}行：规格型号含斜杠（{spec}），请确认是否完整。")

    # size_spec 含包装注记
    if spec and PACKAGING_ANNOTATION_RE.search(spec):
        warnings.append(f"第{line_no}行：规格型号含包装注记（{spec}），疑似未清除污染。")

    # customer_model 缺失但有 old_material_code
    if old_mat and not customer_model:
        warnings.append(f"第{line_no}行：客户型号缺失，但存在材质代码 {old_mat}，解析可能不完整。")

    return warnings


# ---------------------------------------------------------------------------
# PDF 分类识别
# ---------------------------------------------------------------------------

# 支持的 parse_status 值（扩展版）
PARSE_STATUS_LABELS = {
    "recognized":           "已识别",
    "needs_confirmation":   "需人工确认",
    "unsupported_format":   "暂不支持的格式",
    "customer_not_recognized": "客户未识别",
    "order_no_not_recognized": "订单号未识别",
    "header_not_recognized":   "明细表头未识别",
    "items_not_split":         "明细无法切行",
    "missing_customer_template": "已识别客户，但当前版本暂未建立该客户解析模板",
    "ocr_required":         "需要 OCR，当前版本暂未完整接入",
    "ocr_unavailable":      "需要 OCR，但 OCR 引擎未安装",
    "ocr_failed":           "OCR 识别失败",
    "header_not_supported": "表头格式不支持",
    "image_pdf":            "图片型 PDF，需要 OCR",
    "tianming_direction":   "疑似销售方向 / 待人工确认 / 暂不纳入采购订单解析",
    "failed":               "解析失败",
}


def _classify_pdf(
    text: str,
    source_name: str | None,
    customer_type: str,
    pdf_type_info: dict | None = None,
) -> dict | None:
    """
    对无法正常解析的 PDF 做分类，返回分类结果字典（含 parse_status、说明等）。
    如果是可正常解析的天华 PDF，返回 None（不需要特殊分类）。
    """
    name = (source_name or "").lower()
    info = pdf_type_info or {}

    # 天明：疑似销售方向
    if customer_type == "tianming" or "天明" in name:
        return {
            "parse_status": "tianming_direction",
            "customer_type": "tianming",
            "message": "疑似销售方向文件（发往天明的销售订单），待人工确认，暂不纳入采购订单解析。",
            "is_image_pdf": info.get("is_image_pdf", False),
            "ocr_required": info.get("is_image_pdf", False),
            "ocr_available": ocr_available(),
        }

    # 图片型 PDF（文本 < 50 字节）
    if info.get("is_image_pdf"):
        av = ocr_available()
        return {
            "parse_status": "image_pdf",
            "customer_type": customer_type,
            "message": "图片扫描型 PDF，需要 OCR 才能提取文字，当前版本暂未完整接入订单解析流。",
            "is_image_pdf": True,
            "ocr_required": True,
            "ocr_available": av,
            "ocr_status": "available" if av else "unavailable",
        }

    # 思迈尔：已识别客户，无完整模板
    if customer_type == "simair":
        return {
            "parse_status": "missing_customer_template",
            "customer_type": "simair",
            "message": "已识别客户（思迈尔），但当前版本暂未建立该客户解析模板。",
            "is_image_pdf": False,
            "ocr_required": _chinese_ratio(text) < 0.01,
            "ocr_available": ocr_available(),
        }

    # 乱码但非图片（字体不可提取）
    if _chinese_ratio(text) < 0.01 and text.strip():
        av = ocr_available()
        return {
            "parse_status": "ocr_required" if av else "ocr_unavailable",
            "customer_type": customer_type,
            "message": "文本提取结果乱码（字体未映射），需要 OCR。" + (
                "" if av else " OCR 引擎未安装，无法继续。"
            ),
            "is_image_pdf": False,
            "ocr_required": True,
            "ocr_available": av,
        }

    return None  # 无特殊分类，可正常解析


# ---------------------------------------------------------------------------
# 主解析入口
# ---------------------------------------------------------------------------

def parse_purchase_order_text(text: str, source_name: str | None = None) -> dict:
    lines = [_clean_line(line) for line in text.splitlines() if _clean_line(line)]
    if not lines:
        # 无可提取文字：通常是图片型/字体未映射，需 OCR
        raise PdfParseError(
            "文件中未提取到可识别文字，可能是图片型 PDF，需要 OCR。",
            "ocr_required" if ocr_available() else "ocr_unavailable",
        )
    order_match = ORDER_NO_RE.search(text)
    if not order_match:
        raise PdfParseError("未识别到采购订单号。", "order_no_not_recognized")
    customer_po = order_match.group(1).upper()

    # 提取客户名
    customer_name = _extract_customer_name(lines, customer_po)
    customer_type = _detect_customer_type(customer_name, customer_po, text)
    is_tianhua = customer_type in ("tianhua_chao", "tianhua_energy")

    # 思迈尔：识别到但无模板，明确返回状态
    if customer_type == "simair":
        return {
            "source_name": source_name or "uploaded.pdf",
            "source_type": "purchase_order_pdf",
            "customer_name_raw": customer_name,
            "customer_name": customer_name,
            "customer_type": customer_type,
            "customer_po": customer_po,
            "order_date": None,
            "delivery_date": None,
            "recognition_status": "missing_customer_template",
            "parse_status": "missing_customer_template",
            "message": "已识别客户（思迈尔），但当前版本暂未建立该客户解析模板。",
            "duplicate_status": None,
            "duplicate_reason": None,
            "item_count": 0,
            "items": [],
            "warnings": ["已识别客户（思迈尔），但当前版本暂未建立该客户解析模板。"],
            "is_tianhua": False,
        }

    # 天明：疑似销售方向文件，明确返回状态，不纳入采购订单解析
    if customer_type == "tianming":
        return {
            "source_name": source_name or "uploaded.pdf",
            "source_type": "purchase_order_pdf",
            "customer_name_raw": customer_name,
            "customer_name": customer_name,
            "customer_type": customer_type,
            "customer_po": customer_po,
            "order_date": None,
            "delivery_date": None,
            "recognition_status": "tianming_direction",
            "parse_status": "tianming_direction",
            "message": "疑似销售方向文件（发往天明），待人工确认，暂不纳入采购订单解析。",
            "duplicate_status": None,
            "duplicate_reason": None,
            "item_count": 0,
            "items": [],
            "warnings": ["疑似销售方向文件（发往天明），待人工确认。"],
            "is_tianhua": False,
        }

    # 其他未识别客户：明确失败原因，不静默
    if customer_type == "unknown" and not customer_name:
        raise PdfParseError(
            "未识别到客户，请按客户模板维护后再导入。",
            "customer_not_recognized",
        )

    records, has_extra_columns, header_found = _split_records(lines)
    if not header_found:
        raise PdfParseError("未识别到订单明细表头。", "header_not_recognized")
    if not records:
        raise PdfParseError("识别到表头但无法切分出订单明细行。", "items_not_split")
    items_raw = [
        item
        for record in records
        if (item := _parse_record(record, has_extra_columns=has_extra_columns))
    ]
    if not items_raw:
        raise PdfParseError("识别到表头但无法切分出订单明细行。", "items_not_split")

    # 天华专属字段补全
    items: list[dict] = []
    for item in items_raw:
        spec_raw = item.pop("raw_spec_for_enrichment", item.get("raw_spec_model", ""))
        if is_tianhua:
            item = _enrich_tianhua_item(item, spec_raw)
        items.append(item)

    # 每条明细质量检查
    all_item_warnings: list[str] = []
    for item in items:
        all_item_warnings.extend(_check_item_warnings(item))

    dates = [item["delivery_date"] for item in items if item["delivery_date"]]
    order_dates = [_normalize_date(line) for line in lines]
    order_date = next((value for value in order_dates if value), None)

    result = {
        "source_name": source_name or "uploaded.pdf",
        "source_type": "purchase_order_pdf",
        "customer_name_raw": customer_name,
        "customer_name": customer_name,
        "customer_type": customer_type,
        "customer_po": customer_po,
        "order_date": order_date,
        "delivery_date": dates[0] if dates else None,
        "recognition_status": "recognized",
        "duplicate_status": None,
        "duplicate_reason": None,
        "item_count": len(items),
        "items": items,
        "warnings": all_item_warnings,
        "is_tianhua": is_tianhua,
        "has_extra_columns": has_extra_columns,
    }
    return result


# ---------------------------------------------------------------------------
# 订单匹配 / 重匹配
# ---------------------------------------------------------------------------

def _customer_match(db: Session, raw_name: str | None) -> tuple[str, int | None, list[dict]]:
    if not raw_name:
        return "unmatched", None, []
    target = _company_name_key(raw_name)
    candidates = []
    for customer in db.scalars(select(Customer).order_by(Customer.id)).all():
        key = _company_name_key(customer.name)
        if key and (key == target or key in target or target in key):
            candidates.append({"id": customer.id, "name": customer.name})
    if len(candidates) == 1:
        return "matched", candidates[0]["id"], candidates
    if len(candidates) > 1:
        return "multiple_candidates", None, candidates
    return "unmatched", None, []


def _product_candidate(product: Product) -> dict:
    material = product.material
    return {
        "id": product.id,
        "product_code": product.product_code,
        "customer_material_code": product.customer_material_code,
        "product_name": product.product_name,
        "specification": _product_spec(product),
        "material_id": product.material_id,
        "material_code": material.code if material else product.legacy_material_text,
        "sale_unit_price": str(product.sale_unit_price) if product.sale_unit_price is not None else None,
    }


def _product_spec(product: Product) -> str | None:
    values = (product.length_mm, product.width_mm, product.height_mm)
    if any(value is None for value in values):
        return None
    return "×".join(format(value, "f").rstrip("0").rstrip(".") for value in values) + "mm"


def _score_product(product: Product, item: dict) -> int:
    code = _normalized_text(item.get("raw_product_code") or item.get("product_code"))
    name = _normalized_text(item.get("raw_product_name") or item.get("product_name"))
    spec = _normalized_text(item.get("raw_spec_model") or item.get("specification"))
    material = _normalized_text(item.get("raw_material"))
    score = 0
    if code and code == _normalized_text(product.product_code):
        score = max(score, 100)
    if code and code == _normalized_text(product.customer_material_code):
        score = max(score, 95)
    if name and name == _normalized_text(product.product_name):
        score = max(score, 80)
    product_spec = _normalized_text(_product_spec(product))
    if spec and product_spec and (spec in product_spec or product_spec in spec):
        score = max(score, 70)
        product_material = _normalized_text(
            product.material.code if product.material else product.legacy_material_text
        )
        if material and product_material and material == product_material:
            score = max(score, 75)
    return score


def _cost_reference(product: Product, material: Material | None = None) -> dict:
    selected_material = material or product.material
    if product.cost_unit_price is not None:
        return {"cost_status": "calculated", "estimated_cost": str(product.cost_unit_price)}
    if selected_material and selected_material.quote_price is not None:
        try:
            result = calculate_price(
                box_category=product.box_category,
                length_mm=product.length_mm,
                width_mm=product.width_mm,
                height_mm=product.height_mm,
                unfolded_length_mm=product.default_cardboard_length,
                unfolded_width_mm=product.default_cardboard_width,
                board_square_price=selected_material.quote_price,
            )
            return {"cost_status": "calculated", "estimated_cost": str(result.unit_price)}
        except PricingError:
            pass
    return {"cost_status": "pending", "estimated_cost": None}


def _material_label(material: Material | None) -> str:
    """材质展示：代码｜供应商｜克重结构｜平方报价。"""
    if material is None:
        return ""
    code = re.sub(r"^\s*\d+\s+", "", material.code or "").strip()
    weights = _parse_layer_weights(material.basis_weight_description)
    weight_text = "/".join(
        str(int(w)) if w == int(w) else str(w) for w in weights
    ) if weights else "-"
    price = f"¥{material.quote_price}" if material.quote_price is not None else "-"
    return f"{code}｜{material.supplier_name or '-'}｜{weight_text}｜{price}"


def _apply_standard_product(item: dict, product: Product) -> None:
    """v0.19.2 六/七：匹配到常用箱（标准产品资料）后，标准字段以常用箱为准，
    订单事实字段（数量/交期/客户单价/客户单号/料号/TH型号/本次备注）仍以 PDF 为准。

    不反向修改常用箱；仅在草稿上覆盖展示，并生成对比信息供草稿页显示。
    """
    material = product.material
    pdf_name = item.get("raw_product_name") or item.get("product_name") or ""
    pdf_size = item.get("size_spec") or item.get("raw_spec_model") or ""
    pdf_material_code = item.get("old_material_code") or ""
    std_size = _product_spec(product) or ""

    # 标准字段：常用箱优先
    if product.product_name:
        item["product_name"] = product.product_name
    if std_size:
        item["specification"] = std_size
        item["size_spec"] = std_size
    if product.material_id is not None:
        item["matched_material_id"] = product.material_id
    if material is not None:
        item["flute_type"] = material.flute_type or item.get("flute_type")
        item["layer_count"] = material.layer_count or item.get("layer_count")
        item["material_supplier_name"] = material.supplier_name
        item["material_weight_structure"] = "/".join(
            str(int(w)) if w == int(w) else str(w)
            for w in _parse_layer_weights(material.basis_weight_description)
        )
    if product.production_process:
        item["production_process"] = product.production_process
    if product.print_content:
        item["print_content"] = product.print_content

    # 对比信息（草稿页展示「已匹配常用箱 / 使用常用箱资料」）
    item["standard_match"] = {
        "matched": True,
        "product_id": product.id,
        "applied": "standard",
        "pdf_product_name": pdf_name,
        "standard_product_name": product.product_name,
        "pdf_size": pdf_size,
        "standard_size": std_size,
        "size_differs": bool(std_size and pdf_size and _normalized_text(std_size) != _normalized_text(pdf_size)),
        "pdf_material_code": pdf_material_code,
        "standard_material_label": _material_label(material),
        "material_differs": bool(material and pdf_material_code),
    }


def rematch_draft_items(db: Session, draft: dict, customer_id: int | None) -> dict:
    products = (
        db.scalars(
            select(Product)
            .options(joinedload(Product.material))
            .where(
                Product.customer_id == customer_id,
                Product.is_active.is_(True),
                Product.deleted_at.is_(None),
            )
            .order_by(Product.id)
        ).unique().all()
        if customer_id
        else []
    )
    materials = db.scalars(
        select(Material).where(Material.is_active.is_(True)).order_by(Material.code)
    ).all()
    material_candidates = [
        {"id": row.id, "code": re.sub(r"^\s*\d+\s+", "", row.code).strip()}
        for row in materials
    ]
    matched_items = []
    for raw in draft.get("items", []):
        item = dict(raw)
        scored = [(product, _score_product(product, item)) for product in products]
        scored = [(product, score) for product, score in scored if score > 0]
        scored.sort(key=lambda pair: (-pair[1], pair[0].id))
        top_score = scored[0][1] if scored else 0
        top = [product for product, score in scored if score == top_score]
        candidates = [_product_candidate(product) for product, _score in scored[:20]]
        item["product_candidates"] = candidates
        item["matched_product_id"] = top[0].id if len(top) == 1 else None
        item["match_status"] = "matched" if len(top) == 1 else "unmatched"
        selected = top[0] if len(top) == 1 else None
        item["matched_material_id"] = selected.material_id if selected else None
        item["material_candidates"] = material_candidates
        item.update(_cost_reference(selected) if selected else {"cost_status": "pending", "estimated_cost": None})
        # v0.19.2 六/七：唯一命中常用箱 → 标准字段以常用箱为准（不覆盖 PDF 订单事实字段）
        if selected is not None:
            _apply_standard_product(item, selected)
        else:
            item["standard_match"] = {"matched": False}
        # 客户单价仍以 PDF 为准；仅当 PDF 未识别到单价时回退常用箱默认价
        if selected and not item.get("unit_price") and selected.sale_unit_price is not None:
            item["unit_price"] = str(selected.sale_unit_price)
        matched_items.append(item)
    return {**draft, "matched_customer_id": customer_id, "items": matched_items}


def _lines_signature(items: list[dict]) -> str:
    rows = [
        "|".join(
            [
                _normalized_text(item.get("raw_product_code") or item.get("product_code")),
                _normalized_text(item.get("raw_spec_model") or item.get("specification")),
                str(item.get("quantity") or ""),
                str(item.get("unit_price") or ""),
            ]
        )
        for item in items
    ]
    return hashlib.sha256("\n".join(sorted(rows)).encode("utf-8")).hexdigest()


def mark_order_duplicate(db: Session, draft: dict) -> dict:
    customer_id = draft.get("matched_customer_id")
    customer_po = (draft.get("customer_po") or "").strip()
    signature = _lines_signature(draft.get("items", []))
    result = {**draft, "lines_signature": signature}
    if not customer_id or not customer_po:
        return result
    orders = db.scalars(
        select(Order)
        .where(Order.customer_id == customer_id, Order.customer_po == customer_po)
        .options(joinedload(Order.items))
    ).unique().all()
    for order in orders:
        existing = [
            {
                "product_code": item.snapshot_product_code,
                "specification": item.snapshot_spec,
                "quantity": item.quantity,
                "unit_price": str(item.unit_price),
            }
            for item in order.items
        ]
        if _lines_signature(existing) == signature:
            return {
                **result,
                "duplicate_status": "duplicate_skipped",
                "duplicate_reason": "系统中已存在相同客户、客户单号和明细的订单",
                "duplicate_order_id": order.id,
            }
    if orders:
        result["duplicate_status"] = "duplicate_candidate"
        result["duplicate_reason"] = "系统中存在相同客户单号，但订单明细不同，请人工确认"
    return result


def match_import_draft(db: Session, draft: dict, customer_id: int | None = None) -> dict:
    match_status, matched_customer_id, candidates = _customer_match(
        db, draft.get("customer_name_raw") or draft.get("customer_name")
    )
    if customer_id is not None:
        match_status, matched_customer_id = "matched", customer_id
    result = rematch_draft_items(db, draft, matched_customer_id)
    result.update(
        customer_match_status=match_status,
        customer_candidates=candidates,
        matched_customer_id=matched_customer_id,
    )
    warnings = list(draft.get("warnings", []))
    if match_status == "unmatched":
        warnings.append("未匹配到客户，请手动选择客户。")
    elif match_status == "multiple_candidates":
        warnings.append("匹配到多个客户候选，请人工确认。")
    if any(not item.get("matched_product_id") for item in result["items"]):
        warnings.append("部分明细未唯一匹配产品，请逐行选择。")
    result["warnings"] = warnings
    result["recognition_status"] = (
        "recognized"
        if match_status == "matched"
        and all(item.get("matched_product_id") for item in result["items"])
        else "needs_confirmation"
    )
    return mark_order_duplicate(db, result)


def calculate_draft_cost(
    db: Session, product_id: int, material_id: int | None = None
) -> dict:
    product = db.scalar(
        select(Product).options(joinedload(Product.material)).where(Product.id == product_id)
    )
    if product is None:
        raise ValueError("产品不存在")
    material = db.get(Material, material_id) if material_id else None
    result = _cost_reference(product, material)
    sale_price = product.sale_unit_price
    result["customer_unit_price"] = str(sale_price) if sale_price is not None else None
    if sale_price is not None and result["estimated_cost"] is not None:
        result["estimated_gross_profit"] = str(
            (sale_price - Decimal(result["estimated_cost"])).quantize(Decimal("0.0001"))
        )
    else:
        result["estimated_gross_profit"] = None
    return result
