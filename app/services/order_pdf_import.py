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
from app.services.pricing import PricingError, calculate_price


ORDER_NO_RE = re.compile(r"\b((?:THPO|PO)[A-Z0-9-]{6,})\b", re.IGNORECASE)
DATE_RE = re.compile(r"\b(20\d{2})[-/.](\d{2})[-/.](\d{2})\b")
ROW_START_RE = re.compile(r"^\d+\s+\S+")
ITEM_RE = re.compile(
    r"^(?P<line_no>\d+)\s+(?P<product_code>\S+)\s+(?P<body>.+?)\s+"
    r"(?P<unit>\S+)\s+(?P<quantity>\d+(?:\.\d+)?)\s+"
    r"(?P<unit_price>\d+(?:\.\d+)?)\s+(?P<amount>[\d,]+(?:\.\d+)?)\s+"
    r"(?P<delivery_date>20\d{2}[./]\d{2}[./]\d{2})$"
)
SPEC_START_RE = re.compile(
    r"(?=(?:\(?\d+(?:\.\d+)?(?:[\"”]|cm|mm|\*)|"
    r"\d+(?:\.\d+)?\s*[×xX*]\s*\d+))",
    re.IGNORECASE,
)
DIMENSION_RE = re.compile(
    r"(?<!\d)(\d+(?:\.\d+)?\s*[×xX*]\s*\d+(?:\.\d+)?"
    r"(?:\s*[×xX*]\s*\d+(?:\.\d+)?)?\s*(?:cm|mm)?)(?!\d)",
    re.IGNORECASE,
)


def file_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def extract_text_from_pdf_bytes(content: bytes) -> str:
    reader = PdfReader(BytesIO(content))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _clean_line(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\u3000", " ")).strip()


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


def _split_records(lines: list[str]) -> list[list[str]]:
    records: list[list[str]] = []
    current: list[str] = []
    in_table = False
    for line in lines:
        if "行号" in line and ("料品编码" in line or "物料编码" in line) and "交货日期" in line:
            in_table = True
            continue
        if not in_table:
            continue
        if line.startswith("合计"):
            if current:
                records.append(current)
            break
        if ROW_START_RE.match(line):
            if current:
                records.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current and current not in records:
        records.append(current)
    return records


def _split_name_and_spec(first_body: str, full_body: str) -> tuple[str, str]:
    match = SPEC_START_RE.search(first_body)
    if match and match.start() > 0:
        name = first_body[: match.start()].strip()
        first_spec = first_body[match.start() :].strip()
        remaining = full_body[len(first_body) :].strip()
        return name, _clean_line(f"{first_spec} {remaining}")
    parts = first_body.split(maxsplit=1)
    name = parts[0] if parts else first_body
    spec = full_body[len(name) :].strip()
    return name, spec


def _extract_spec_dimensions(raw_spec: str) -> str:
    candidates = [match.group(1) for match in DIMENSION_RE.finditer(raw_spec)]
    if not candidates:
        return raw_spec.strip()
    three_dimensional = [
        value for value in candidates if len(re.findall(r"[×xX*]", value)) >= 2
    ]
    selected = three_dimensional[0] if three_dimensional else candidates[0]
    return re.sub(r"\s+", "", selected)


def _parse_record(record_lines: list[str]) -> dict | None:
    joined = " ".join(record_lines)
    match = ITEM_RE.match(joined)
    if not match:
        return None
    first_body = re.sub(r"^\d+\s+\S+\s+", "", record_lines[0], count=1)
    raw_name, raw_spec = _split_name_and_spec(first_body, match.group("body"))
    raw_spec = _extract_spec_dimensions(raw_spec)
    code = match.group("product_code")
    return {
        "line_no": int(match.group("line_no")),
        "raw_product_code": code,
        "raw_product_name": raw_name,
        "raw_spec_model": raw_spec,
        # Compatibility aliases for the existing order form.
        "product_code": code,
        "product_name": raw_name,
        "specification": raw_spec,
        "unit": match.group("unit"),
        "quantity": int(Decimal(match.group("quantity"))),
        "unit_price": _decimal_to_str(match.group("unit_price"), "0.0000"),
        "amount": _decimal_to_str(match.group("amount"), "0.00"),
        "delivery_date": _normalize_date(match.group("delivery_date")),
        "raw_lines": record_lines,
        "matched_product_id": None,
        "matched_material_id": None,
        "match_status": "unmatched",
        "cost_status": "pending",
        "product_candidates": [],
        "material_candidates": [],
    }


def parse_purchase_order_text(text: str, source_name: str | None = None) -> dict:
    lines = [_clean_line(line) for line in text.splitlines() if _clean_line(line)]
    if not lines:
        raise ValueError("文件中未提取到可识别文字")
    order_match = ORDER_NO_RE.search(text)
    if not order_match:
        raise ValueError("未识别到采购订单号")
    customer_po = order_match.group(1).upper()
    items = [item for record in _split_records(lines) if (item := _parse_record(record))]
    if not items:
        raise ValueError("未识别到订单明细")
    dates = [item["delivery_date"] for item in items if item["delivery_date"]]
    order_dates = [_normalize_date(line) for line in lines]
    order_date = next((value for value in order_dates if value), None)
    return {
        "source_name": source_name or "uploaded.pdf",
        "source_type": "purchase_order_pdf",
        "customer_name_raw": _extract_customer_name(lines, customer_po),
        "customer_name": _extract_customer_name(lines, customer_po),
        "customer_po": customer_po,
        "order_date": order_date,
        "delivery_date": dates[0] if dates else None,
        "recognition_status": "recognized",
        "duplicate_status": None,
        "duplicate_reason": None,
        "item_count": len(items),
        "items": items,
        "warnings": [],
    }


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
        "recognized" if match_status == "matched" and all(item.get("matched_product_id") for item in result["items"])
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
