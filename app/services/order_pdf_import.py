from __future__ import annotations

import hashlib
import json
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
    r"\b((?:THPO|PO)[A-Z0-9-]{6,}|P-\d{7}(?:-\d+)?|\d{4}-CG\d{6}-\d{2})\b",
    re.IGNORECASE,
)
DATE_RE = re.compile(r"\b(20\d{2})[-/.](\d{2})[-/.](\d{2})\b")
ROW_START_RE = re.compile(r"^\d+\s+\S+")
TIANHUA_LINE_START_RE = re.compile(r"^\s*(?P<line_no>\d{1,4})\s+(?P<product_code>\d{8})\b")
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
    "gaotai":         "高泰（苏州高泰电子技术股份有限公司）",
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
    # 高泰
    if "高泰" in name or "高泰" in full_text:
        return "gaotai"
    # 天明（天明 PDF 通常是销售方向，不是采购订单）
    if "天明" in name or "天明" in full_text:
        return "tianming"
    return "unknown"


def _is_tianhua_customer(customer_name: str | None, customer_po: str | None = None) -> bool:
    """判断是否是天华系客户（超净或新能源）。"""
    ct = _detect_customer_type(customer_name, customer_po)
    return ct in ("tianhua_chao", "tianhua_energy")


def detect_pdf_customer_by_template(
    text: str,
    template_rules: list[dict] | None = None,
) -> dict | None:
    full_text = (text or "").lower()
    for rule in template_rules or []:
        customer_name = str(rule.get("customer_name") or "").strip()
        identity_candidates = [customer_name]
        identity_candidates.extend(str(value or "").strip() for value in (rule.get("aliases") or []))
        identity_candidates = [value for value in identity_candidates if value]
        keywords = [
            str(value or "").strip()
            for value in (rule.get("keywords") or [])
            if str(value or "").strip()
        ]
        identity_matched = any(candidate.lower() in full_text for candidate in identity_candidates)
        keyword_only_matched = (
            not identity_candidates
            and bool(keywords)
            and all(keyword.lower() in full_text for keyword in keywords)
        )
        if identity_matched or keyword_only_matched:
            return {
                "customer_name": customer_name or None,
                "customer_type": rule.get("customer_type") or "unknown",
                "template_name": rule.get("template_name"),
                "rule": rule,
            }
        pattern = rule.get("customer_name_pattern")
        if pattern:
            try:
                if re.search(pattern, text, re.IGNORECASE):
                    return {
                        "customer_name": customer_name or None,
                        "customer_type": rule.get("customer_type") or "unknown",
                        "template_name": rule.get("template_name"),
                        "rule": rule,
                    }
            except re.error:
                continue
    return None


def normalize_gaotai_product_code(code: str) -> tuple[str, str | None]:
    raw = (code or "").strip()
    match = re.fullmatch(r"30(\d{5})", raw, re.IGNORECASE)
    if not match:
        return raw, None
    normalized = f"3D{match.group(1)}"
    return normalized, f"高泰存货编码由 {raw} 按模板规则修正为 {normalized}"


def apply_customer_template_postprocess(
    result: dict,
    raw_text: str,
    template_rules: list[dict] | None = None,
) -> dict:
    output = json.loads(json.dumps(result, ensure_ascii=False, default=str))
    warnings = list(output.get("warnings") or [])
    if output.get("customer_type") in ("tianhua_chao", "tianhua_energy"):
        output["warnings"] = warnings
        return output
    effective_rules = template_rules or [
        {
            "customer_name": "苏州高泰电子技术股份有限公司",
            "customer_type": "gaotai",
            "aliases": ["高泰电子", "苏州高泰"],
            "keywords": ["采购合同", "苏州高泰电子技术股份有限公司"],
            "item_code_rules": [
                {
                    "name": "gaotai_3d_code",
                    "pattern": r"^30(\d{5})$",
                    "replace": r"3D\1",
                    "reason": "高泰存货编码应为 3D + 5位数字，OCR 易把 D 识别成 0",
                }
            ],
        }
    ]
    matched = detect_pdf_customer_by_template(raw_text, effective_rules)
    if matched:
        if matched.get("customer_name"):
            output["customer_name"] = matched["customer_name"]
            if not output.get("customer_name_raw"):
                output["customer_name_raw"] = matched["customer_name"]
        if matched.get("customer_type") and output.get("customer_type") in (None, "", "unknown"):
            output["customer_type"] = matched["customer_type"]
        output["template_name"] = matched.get("template_name")

    if output.get("customer_type") != "gaotai":
        output["warnings"] = warnings
        return output

    item_code_rules = []
    if matched and isinstance(matched.get("rule"), dict):
        item_code_rules = matched["rule"].get("item_code_rules") or []

    correction_notes: list[str] = []
    for index, item in enumerate(output.get("items") or [], start=1):
        original_code = str(item.get("product_code") or "").strip()
        normalized_code = original_code
        correction_note = None
        if item_code_rules:
            normalized_code, correction_note = normalize_gaotai_product_code(original_code)
        if correction_note and normalized_code and normalized_code != original_code:
            item["raw_product_code"] = item.get("raw_product_code") or original_code
            item["normalized_product_code"] = normalized_code
            item["product_code"] = normalized_code
            correction_notes.append(f"第{index}行存货编码由 {original_code} 按高泰规则修正为 {normalized_code}")

    warnings.extend(correction_notes)
    output["warnings"] = warnings
    if correction_notes:
        output["template_corrections"] = correction_notes
    return output


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


def _extract_gaotai_order_no(text: str) -> str | None:
    patterns = [
        r"合同号[/／]?[PO0Oo]?\s*[:：]\s*([0-9A-Z-]{8,})",
        r"\b(\d{4}-CG\d{6}-\d{2})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    return None


def _decimal_to_str(raw: str, places: str) -> str:
    return format(Decimal(raw.replace(",", "")).quantize(Decimal(places)), "f")


def _quantity_value(raw: str):
    value = Decimal(raw.replace(",", ""))
    return int(value) if value == value.to_integral_value() else float(value)


def _quantity_decimal(value) -> Decimal:
    return Decimal(str(value or 0).replace(",", ""))


def _apply_quantity_review_flags(result: dict) -> dict:
    warnings = list(result.get("warnings") or [])
    requires_review = False
    for item in result.get("items") or []:
        quantity_value = _quantity_decimal(item.get("quantity"))
        if quantity_value != quantity_value.to_integral_value():
            requires_review = True
            item["quantity_review_required"] = True
            item["quantity_warning"] = (
                f"第{item.get('line_no', '?')}行数量 {item.get('quantity')} "
                "不是整数，请按客户原单人工确认后修改。"
            )
            if item["quantity_warning"] not in warnings:
                warnings.append(item["quantity_warning"])
        else:
            item["quantity_review_required"] = False
            item["quantity_warning"] = None
    result["requires_manual_quantity_review"] = requires_review
    result["warnings"] = warnings
    return result


def _company_name_key(value: str | None) -> str:
    text = re.sub(r"[（）()\-—_·,，.。/\\\s]", "", value or "")
    for suffix in ("股份有限公司", "有限责任公司", "有限公司"):
        text = text.replace(suffix, "")
    return text.casefold()


def _full_company_name_key(value: str | None) -> str:
    return re.sub(r"[（）()\-—_·,，.。/\\\s]", "", value or "").casefold()


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
        if TIANHUA_LINE_START_RE.match(line):
            if current:
                records.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current and current not in records:
        records.append(current)
    return records, has_extra_columns, header_found


def _parse_gaotai_items(text: str) -> list[dict]:
    code_matches = list(re.finditer(r"\b3[0D]\d{5}\b", text, re.IGNORECASE))
    items: list[dict] = []
    for index, match in enumerate(code_matches, start=1):
        start = match.start()
        end = code_matches[index].start() if index < len(code_matches) else len(text)
        segment = text[start:end]
        segment = segment.split("合计", 1)[0].strip()
        if not segment:
            continue
        code = match.group(0).upper()
        body = segment[len(match.group(0)):].strip()
        if not body:
            continue
        delivery_matches = list(DATE_RE.finditer(segment))
        delivery_date = None
        body_without_date = body
        if delivery_matches:
            last_date = delivery_matches[-1]
            delivery_date = _normalize_date(last_date.group(0))
            body_without_date = body.replace(last_date.group(0), " ").strip()
        tokens = [token for token in re.split(r"\s+", body_without_date) if token]
        if not tokens:
            continue
        product_name = tokens[0]
        unit = "Pcs" if any(token.lower() == "pcs" for token in tokens) else ""
        quantity = None
        if unit:
            for pos, token in enumerate(tokens):
                if token.lower() == "pcs" and pos > 0 and re.fullmatch(r"\d+(?:\.\d+)?", tokens[pos - 1]):
                    quantity = tokens[pos - 1]
                    break
        if quantity is None:
            for token in tokens[1:]:
                if re.fullmatch(r"\d+(?:\.\d+)?", token):
                    quantity = token
                    break
        spec_tokens: list[str] = []
        quantity_consumed = False
        for token in tokens[1:]:
            if delivery_date and DATE_RE.fullmatch(token):
                continue
            if unit and token.lower() == "pcs":
                break
            if quantity and token == quantity and not quantity_consumed:
                quantity_consumed = True
                continue
            spec_tokens.append(token)
        items.append(
            {
                "line_no": index,
                "product_code": code,
                "product_name": product_name,
                "specification": " ".join(spec_tokens).strip(),
                "raw_spec_model": " ".join(spec_tokens).strip(),
                "quantity": quantity or "",
                "unit": unit or "",
                "unit_price": "",
                "amount": "",
                "delivery_date": delivery_date,
            }
        )
    return items


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
        "quantity": _quantity_value(match.group("quantity")),
        "raw_quantity": match.group("quantity"),
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


def _parse_tianhua_record_tail(record_lines: list[str]) -> dict | None:
    if not record_lines:
        return None
    joined = _join_record_lines(record_lines)
    start = TIANHUA_LINE_START_RE.match(joined)
    if not start:
        return None
    match = re.match(
        r"^(?P<body>.+?)\s+"
        r"(?P<unit>个(?:\s*[（(]\s*无\s*小数\s*[）)])?|Pcs|PCS)\s+"
        r"(?P<quantity>\d[\d,]*(?:\.\d+)?)\s+"
        r"(?P<unit_price>\d+(?:\.\d+)?)\s+"
        r"(?P<amount>[\d,]+(?:\.\d+)?)\s+"
        r"(?P<delivery_date>20\d{2}[./]\d{2}[./]\d{2})$",
        joined[start.end() :].strip(),
        re.IGNORECASE,
    )
    if not match:
        return None
    raw_name, raw_spec = _split_name_and_spec(match.group("body"), match.group("body"))
    code = start.group("product_code")
    return {
        "line_no": int(start.group("line_no")),
        "raw_product_code": code,
        "raw_product_name": raw_name,
        "raw_spec_model": _extract_spec_dimensions(raw_spec),
        "raw_spec_for_enrichment": raw_spec,
        "product_code": code,
        "product_name": raw_name,
        "specification": _extract_spec_dimensions(raw_spec),
        "unit": re.sub(r"\s+", "", match.group("unit")),
        "quantity": _quantity_value(match.group("quantity")),
        "raw_quantity": match.group("quantity"),
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


def _parse_tianhua_record(record: list[str], has_extra_columns: bool = False) -> dict | None:
    return _parse_record(record, has_extra_columns=has_extra_columns) or _parse_tianhua_record_tail(record)


def _tianhua_source_total(text: str) -> tuple[Decimal | None, Decimal | None]:
    matches = list(re.finditer(r"合计\s+([\d,]+(?:\.\d+)?)\s+([\d,]+(?:\.\d+)?)", text))
    return (
        (Decimal(matches[-1].group(1).replace(",", "")), Decimal(matches[-1].group(2).replace(",", "")))
        if matches else (None, None)
    )


def _plain_decimal_string(value: Decimal | None, places: str | None = None) -> str | None:
    if value is None:
        return None
    if places:
        return format(value.quantize(Decimal(places)), "f")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _build_pdf_integrity_check(text: str, records: list[list[str]], items: list[dict]) -> dict:
    source_line_numbers = [
        match.group("line_no")
        for record in records
        if record and (match := TIANHUA_LINE_START_RE.match(record[0]))
    ]
    parsed_line_numbers = [str(item.get("line_no")) for item in items if item.get("line_no") is not None]
    source_total_quantity, source_total_amount = _tianhua_source_total(text)
    parsed_total_quantity = sum(_quantity_decimal(item.get("quantity")) for item in items)
    parsed_total_amount = sum(Decimal(str(item.get("amount") or "0").replace(",", "")) for item in items)
    missing_line_numbers = [
        line_no for line_no in source_line_numbers if line_no not in set(parsed_line_numbers)
    ]
    errors: list[str] = []
    if source_line_numbers and len(source_line_numbers) > len(parsed_line_numbers):
        errors.append(
            f"PDF疑似有{len(source_line_numbers)}行明细，系统只识别出{len(parsed_line_numbers)}行，缺失行号：{'、'.join(missing_line_numbers) or '-'}"
        )
    if missing_line_numbers:
        errors.append(f"PDF明细行号不完整，缺失行号：{'、'.join(missing_line_numbers)}")
    quantity_total_diff = (
        source_total_quantity - parsed_total_quantity
        if source_total_quantity is not None else None
    )
    if quantity_total_diff is not None:
        if abs(quantity_total_diff) > Decimal("0.01"):
            errors.append(
                f"PDF合计数量{_plain_decimal_string(source_total_quantity)}，识别数量{_plain_decimal_string(parsed_total_quantity)}，差异{_plain_decimal_string(quantity_total_diff)}"
            )
    amount_total_diff = (
        source_total_amount - parsed_total_amount
        if source_total_amount is not None else None
    )
    if amount_total_diff is not None:
        if abs(amount_total_diff) > Decimal("0.01"):
            errors.append(
                f"PDF合计金额{source_total_amount.quantize(Decimal('0.00'))}，识别金额{parsed_total_amount.quantize(Decimal('0.00'))}，差异{amount_total_diff.quantize(Decimal('0.00'))}"
            )
    status = "failed" if errors else ("passed" if source_line_numbers else "unknown")
    warnings = [] if source_line_numbers else ["未能可靠识别 PDF 原文明细行号，完整性校验仅供参考。"]
    return {
        "source_detail_count": len(source_line_numbers) if source_line_numbers else None,
        "parsed_detail_count": len(parsed_line_numbers),
        "source_line_numbers": source_line_numbers,
        "parsed_line_numbers": parsed_line_numbers,
        "missing_line_numbers": missing_line_numbers,
        "source_total_quantity": _plain_decimal_string(source_total_quantity),
        "parsed_total_quantity": _plain_decimal_string(parsed_total_quantity),
        "quantity_total_diff": _plain_decimal_string(quantity_total_diff),
        "source_total_amount": _plain_decimal_string(source_total_amount, "0.00"),
        "parsed_total_amount": _plain_decimal_string(parsed_total_amount, "0.00"),
        "amount_total_diff": str(amount_total_diff.quantize(Decimal("0.00"))) if amount_total_diff is not None else None,
        "integrity_status": status,
        "integrity_errors": errors,
        "integrity_warnings": warnings,
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

def parse_purchase_order_text(
    text: str,
    source_name: str | None = None,
    template_rules: list[dict] | None = None,
) -> dict:
    lines = [_clean_line(line) for line in text.splitlines() if _clean_line(line)]
    if not lines:
        # 无可提取文字：通常是图片型/字体未映射，需 OCR
        raise PdfParseError(
            "文件中未提取到可识别文字，可能是图片型 PDF，需要 OCR。",
            "ocr_required" if ocr_available() else "ocr_unavailable",
        )
    order_match = ORDER_NO_RE.search(text)
    template_match = detect_pdf_customer_by_template(text, template_rules)
    if not order_match and template_match and template_match.get("customer_type") == "gaotai":
        gaotai_order_no = _extract_gaotai_order_no(text)
        if gaotai_order_no:
            class _SimpleMatch:
                def __init__(self, value: str) -> None:
                    self._value = value
                def group(self, _index: int) -> str:
                    return self._value
            order_match = _SimpleMatch(gaotai_order_no)
    if not order_match:
        raise PdfParseError("未识别到采购订单号。", "order_no_not_recognized")
    customer_po = order_match.group(1).upper()

    # 提取客户名
    customer_name = _extract_customer_name(lines, customer_po)
    detected_customer_type = _detect_customer_type(customer_name, customer_po, text)
    if detected_customer_type in ("tianhua_chao", "tianhua_energy"):
        template_match = None
        customer_type = detected_customer_type
    else:
        if template_match and template_match.get("customer_name"):
            customer_name = template_match["customer_name"]
        customer_type = (
            template_match.get("customer_type")
            if template_match and template_match.get("customer_type") not in (None, "", "unknown")
            else detected_customer_type
        )
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
    gaotai_items = []
    if customer_type == "gaotai":
        gaotai_items = _parse_gaotai_items(text)
        if gaotai_items:
            header_found = True
    if not header_found:
        raise PdfParseError("未识别到订单明细表头。", "header_not_recognized")
    if not records and not gaotai_items:
        raise PdfParseError("识别到表头但无法切分出订单明细行。", "items_not_split")
    if gaotai_items:
        items_raw = gaotai_items
    elif is_tianhua:
        items_raw = [
            item
            for record in records
            if (item := _parse_tianhua_record(record, has_extra_columns=has_extra_columns))
        ]
    else:
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

    integrity_check = (
        _build_pdf_integrity_check(text, records, items)
        if is_tianhua
        else {
            "integrity_status": "unknown",
            "integrity_errors": [],
            "integrity_warnings": ["当前客户暂未接入完整性校验。"],
        }
    )
    if integrity_check.get("integrity_errors"):
        all_item_warnings.extend(integrity_check["integrity_errors"])

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
        "integrity_check": integrity_check,
        "is_tianhua": is_tianhua,
        "has_extra_columns": has_extra_columns,
    }
    return _apply_quantity_review_flags(
        apply_customer_template_postprocess(result, text, template_rules=template_rules)
    )


# ---------------------------------------------------------------------------
# 订单匹配 / 重匹配
# ---------------------------------------------------------------------------

def _customer_match(db: Session, raw_name: str | None) -> tuple[str, int | None, list[dict]]:
    if not raw_name:
        return "unmatched", None, []
    full_target = _full_company_name_key(raw_name)
    target = _company_name_key(raw_name)
    exact_candidates = []
    normalized_candidates = []
    partial_candidates = []
    for customer in db.scalars(
        select(Customer).where(Customer.is_active.is_(True)).order_by(Customer.id)
    ).all():
        candidate = {"id": customer.id, "name": customer.name}
        full_key = _full_company_name_key(customer.name)
        key = _company_name_key(customer.name)
        if full_key and full_key == full_target:
            exact_candidates.append(candidate)
        elif key and key == target:
            normalized_candidates.append(candidate)
        elif key and target and (key in target or target in key):
            partial_candidates.append(candidate)
    candidates = exact_candidates or normalized_candidates or partial_candidates
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


def _preferred_item_product_code(item: dict) -> str:
    return str(
        item.get("normalized_product_code")
        or item.get("product_code")
        or item.get("raw_product_code")
        or ""
    )


def _score_product(product: Product, item: dict) -> int:
    code = _normalized_text(_preferred_item_product_code(item))
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
        # 材质基础代码（去掉 -B/E 报价后缀），供前端显示 A6D/A 格式
        code_raw = material.code or ""
        item["material_base_code"] = code_raw.split("-")[0] if code_raw else ""
        item["material_weight_structure"] = "/".join(
            str(int(w)) if w == int(w) else str(w)
            for w in _parse_layer_weights(material.basis_weight_description)
        )
    # 生产说明：常用箱 production_process 写入 production_notes（若 PDF 未提供）
    std_production = (product.production_process or "").strip()
    if std_production and not item.get("production_notes"):
        item["production_notes"] = std_production
    if product.print_content:
        item["print_content"] = product.print_content
    # 默认单价（供前端比较，不覆盖 PDF 单价）
    item["product_default_price"] = str(product.sale_unit_price) if product.sale_unit_price is not None else None
    # v0.19.2-B: 报料快照（从常用箱读取）
    item["report_length_mm"] = product.report_length_mm
    item["report_width_mm"] = product.report_width_mm
    item["crease_type"] = product.crease_type
    item["crease_left_mm"] = product.crease_left_mm
    item["crease_middle_mm"] = product.crease_middle_mm
    item["crease_right_mm"] = product.crease_right_mm
    item["report_notes"] = product.report_notes
    item["base_report_length_mm"] = product.base_report_length_mm
    item["base_report_width_mm"] = product.base_report_width_mm
    item["base_crease_type"] = product.base_crease_type
    item["base_crease_left_mm"] = product.base_crease_left_mm
    item["base_crease_middle_mm"] = product.base_crease_middle_mm
    item["base_crease_right_mm"] = product.base_crease_right_mm
    item["base_report_notes"] = product.base_report_notes

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
    from sqlalchemy.orm import selectinload as _sload
    products = (
        db.scalars(
            select(Product)
            .options(
                joinedload(Product.material),
                _sload(Product.drawings),  # type: ignore[attr-defined]
            )
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
        # 默认单价对比（草稿页显示提醒，不自动修改常用箱）
        if selected and selected.sale_unit_price is not None and item.get("unit_price"):
            try:
                pdf_price = float(item["unit_price"])
                product_default = float(selected.sale_unit_price)
                if abs(pdf_price - product_default) > 0.0001:
                    item["price_conflict"] = {
                        "pdf_price": str(item["unit_price"]),
                        "product_default_price": str(selected.sale_unit_price),
                        "product_id": selected.id,
                    }
            except (ValueError, TypeError):
                pass
        # 常用箱图纸 URL（第一张，供草稿页显示）
        if selected is not None and selected.drawings:
            item["product_drawing_file"] = selected.drawings[0].image_path
        matched_items.append(item)
    # 合并相同存货编码（同单价/同交期/同常用箱）
    merged_items = _merge_same_product_code(matched_items)
    return {**draft, "matched_customer_id": customer_id, "items": merged_items}


def _merge_same_product_code(items: list[dict]) -> list[dict]:
    """同一草稿内，满足合并条件的相同存货编码行自动合并数量。"""
    from decimal import Decimal as _D

    def _merge_key(item: dict):
        return (
            _normalized_text(_preferred_item_product_code(item)),
            str(item.get("matched_product_id") or ""),
            str(item.get("unit_price") or ""),
            str(item.get("delivery_date") or ""),
        )

    groups: dict[tuple, list[dict]] = {}
    order_keys: list[tuple] = []
    for item in items:
        k = _merge_key(item)
        code = _normalized_text(_preferred_item_product_code(item))
        if not code:
            # 没有存货编码的行不合并
            groups.setdefault(id(item), []).append(item)  # type: ignore[arg-type]
            order_keys.append(id(item))  # type: ignore[arg-type]
        else:
            if k not in groups:
                groups[k] = []
                order_keys.append(k)
            groups[k].append(item)

    result: list[dict] = []
    for k in order_keys:
        group = groups[k]
        if len(group) == 1:
            result.append(group[0])
            continue
        # 检查是否可以合并
        can_merge = True
        reasons: list[str] = []
        prices = {str(i.get("unit_price") or "") for i in group}
        dates = {str(i.get("delivery_date") or "") for i in group}
        products = {str(i.get("matched_product_id") or "") for i in group}
        if len(prices) > 1:
            can_merge = False; reasons.append("单价不同")
        if len(dates) > 1:
            can_merge = False; reasons.append("交期不同")
        if len(products) > 1:
            can_merge = False; reasons.append("匹配常用箱不同")
        if not can_merge:
            for item in group:
                item["merge_status"] = "conflict"
                item["merge_conflict_reasons"] = reasons
            result.extend(group)
            continue
        # 合并
        base = dict(group[0])
        total_qty = sum(_quantity_decimal(i.get("quantity")) for i in group)
        base["quantity"] = int(total_qty) if total_qty == total_qty.to_integral_value() else float(total_qty)
        base["merge_status"] = "merged"
        base["merged_sources"] = [
            {
                "line_no": i.get("line_no"),
                "page": i.get("page"),
                "quantity": i.get("quantity"),
            }
            for i in group
        ]
        result.append(base)
    return result


def _lines_signature(items: list[dict]) -> str:
    rows = [
        "|".join(
            [
                _normalized_text(_preferred_item_product_code(item)),
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
