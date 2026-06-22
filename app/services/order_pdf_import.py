from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from io import BytesIO

from pypdf import PdfReader
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.product import Product


ORDER_NO_RE = re.compile(r"\b((?:THPO|PO)\d{8,})\b", re.IGNORECASE)
DATE_RE = re.compile(r"\b(20\d{2})[-/.](\d{2})[-/.](\d{2})\b")
ROW_START_RE = re.compile(r"^\d+\s+\S+")
ITEM_RE = re.compile(
    r"^(?P<line_no>\d+)\s+"
    r"(?P<product_code>\S+)\s+"
    r"(?P<body>.+?)\s+"
    r"(?P<unit>\S+)\s+"
    r"(?P<quantity>\d+(?:\.\d+)?)\s+"
    r"(?P<unit_price>\d+(?:\.\d+)?)\s+"
    r"(?P<amount>[\d,]+(?:\.\d+)?)\s+"
    r"(?P<delivery_date>20\d{2}[./]\d{2}[./]\d{2})$"
)


def extract_text_from_pdf_bytes(content: bytes) -> str:
    reader = PdfReader(BytesIO(content))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _normalize_date(raw: str | None) -> str | None:
    if not raw:
        return None
    match = DATE_RE.search(raw)
    if not match:
        return None
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"


def _decimal_to_str(raw: str, *, places: str) -> str:
    value = Decimal(raw.replace(",", ""))
    return format(value.quantize(Decimal(places)), "f")


def _clean_line(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\u3000", " ")).strip()


def _company_name_key(value: str | None) -> str:
    text = _clean_line(value or "")
    text = re.sub(r"[（）()\-—_·,，.。/\\]", "", text)
    for suffix in ("股份有限公司", "有限责任公司", "有限公司"):
        text = text.replace(suffix, "")
    return text


def _extract_customer_name(lines: list[str], customer_po: str) -> str | None:
    try:
        po_index = next(i for i, line in enumerate(lines) if customer_po in line)
    except StopIteration:
        return None
    for index in range(po_index - 1, -1, -1):
        line = lines[index]
        if any(token in line for token in ("采购订单", "订单号", "供应商全称", "人民币元")):
            continue
        if "苏州天明包装有限公司" in line:
            continue
        if re.fullmatch(r"\d{4}", line) or DATE_RE.search(line):
            continue
        if len(line) >= 4:
            return line
    return None


def _split_records(lines: list[str]) -> list[list[str]]:
    records: list[list[str]] = []
    current: list[str] = []
    in_table = False
    for raw in lines:
        line = _clean_line(raw)
        if not line:
            continue
        if "行号" in line and "料品编码" in line and "交货日期" in line:
            in_table = True
            continue
        if not in_table:
            continue
        if line.startswith("合计"):
            if current:
                records.append(current)
                current = []
            break
        if ROW_START_RE.match(line):
            if current:
                records.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        records.append(current)
    return records


def _parse_record(record_lines: list[str]) -> dict | None:
    joined = " ".join(_clean_line(line) for line in record_lines)
    match = ITEM_RE.match(joined)
    if not match:
        return None
    first_line = _clean_line(record_lines[0])
    first_line_without_prefix = re.sub(
        r"^\d+\s+\S+\s+",
        "",
        first_line,
        count=1,
    )
    body = _clean_line(match.group("body"))
    return {
        "line_no": int(match.group("line_no")),
        "product_code": match.group("product_code"),
        "product_name": first_line_without_prefix or body,
        "specification": body,
        "unit": match.group("unit"),
        "quantity": int(Decimal(match.group("quantity"))),
        "unit_price": _decimal_to_str(match.group("unit_price"), places="0.0000"),
        "amount": _decimal_to_str(match.group("amount"), places="0.00"),
        "delivery_date": _normalize_date(match.group("delivery_date")),
        "raw_lines": record_lines,
        "matched_product_id": None,
        "matched_product_name": None,
        "matched_product_code": None,
    }


def parse_purchase_order_text(text: str, *, source_name: str | None = None) -> dict:
    lines = [_clean_line(line) for line in text.splitlines() if _clean_line(line)]
    if not lines:
        raise ValueError("PDF 中未提取到可识别文字")
    order_match = ORDER_NO_RE.search(text)
    if not order_match:
        raise ValueError("未识别到采购订单号")
    customer_po = order_match.group(1).upper()
    customer_name = _extract_customer_name(lines, customer_po)
    order_date = None
    for line in lines:
        normalized = _normalize_date(line)
        if normalized and normalized != customer_po:
            order_date = normalized
            break
    records = _split_records(lines)
    items = [item for record in records if (item := _parse_record(record))]
    if not items:
        raise ValueError("未识别到订单明细")
    delivery_dates = [item["delivery_date"] for item in items if item["delivery_date"]]
    return {
        "source_name": source_name or "uploaded.pdf",
        "source_type": "purchase_order_pdf",
        "customer_name": customer_name,
        "customer_po": customer_po,
        "order_date": order_date,
        "delivery_date": delivery_dates[0] if delivery_dates else None,
        "item_count": len(items),
        "items": items,
        "warnings": [],
    }


def match_import_draft(db: Session, draft: dict) -> dict:
    matched_customer_id = None
    customer_name = draft.get("customer_name")
    if customer_name:
        customer = db.scalar(select(Customer).where(Customer.name == customer_name))
        if customer is None:
            customer = db.scalar(
                select(Customer).where(Customer.name.ilike(f"%{customer_name.strip()}%"))
            )
        if customer is None:
            target_key = _company_name_key(customer_name)
            for candidate in db.scalars(select(Customer).order_by(Customer.id)).all():
                candidate_key = _company_name_key(candidate.name)
                if candidate_key and (
                    candidate_key == target_key
                    or candidate_key in target_key
                    or target_key in candidate_key
                ):
                    customer = candidate
                    break
        if customer is not None:
            matched_customer_id = customer.id

    matched_items = []
    unmatched_codes: list[str] = []
    for item in draft.get("items", []):
        matched = dict(item)
        if matched_customer_id is not None:
            product = db.scalar(
                select(Product).where(
                    Product.customer_id == matched_customer_id,
                    or_(
                        Product.product_code == item["product_code"],
                        Product.customer_material_code == item["product_code"],
                    ),
                )
            )
            if product is not None:
                matched["matched_product_id"] = product.id
                matched["matched_product_name"] = product.product_name
                matched["matched_product_code"] = product.product_code
            else:
                unmatched_codes.append(item["product_code"])
        matched_items.append(matched)

    warnings: list[str] = []
    if matched_customer_id is None:
        warnings.append("未在系统中自动匹配到客户，请先确认客户。")
    if unmatched_codes:
        warnings.append(
            "以下存货编码未自动匹配到产品：" + "、".join(sorted(set(unmatched_codes)))
        )
    return {
        **draft,
        "matched_customer_id": matched_customer_id,
        "items": matched_items,
        "warnings": [*draft.get("warnings", []), *warnings],
    }
