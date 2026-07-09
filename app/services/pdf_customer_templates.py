from __future__ import annotations

import json
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.pdf_training import PdfOrderCustomerTemplate


GAOTAI_TEMPLATE_RULE = {
    "customer_name": "苏州高泰电子技术股份有限公司",
    "customer_type": "gaotai",
    "aliases": ["高泰电子", "苏州高泰"],
    "keywords": ["采购合同", "苏州高泰电子技术股份有限公司"],
    "order_no_labels": ["合同号/P O", "合同号", "PO"],
    "delivery_date_labels": ["交货期", "交期"],
    "item_code_rules": [
        {
            "name": "gaotai_3d_code",
            "pattern": r"^30(\d{5})$",
            "replace": r"3D\1",
            "reason": "高泰存货编码应为 3D + 5位数字，OCR 易把 D 识别成 0",
        }
    ],
    "item_columns": {
        "product_code": ["产品编号", "产品编码", "存货编码"],
        "product_name": ["名称", "产品名称"],
        "spec": ["规格"],
        "quantity": ["数量"],
        "unit": ["单位"],
        "unit_price": ["单价", "单价(含税)"],
        "amount": ["金额"],
        "delivery_date": ["交货期"],
    },
    "_source": "builtin",
}


SINGLETON_TEMPLATE_RULE = {
    "customer_name": "辛格顿（常州）新材料科技有限公司",
    "customer_type": "singleton",
    "aliases": ["辛格顿", "辛格顿（常州）"],
    "keywords": ["采购合同", "辛格顿（常州）新材料科技有限公司"],
    "order_no_labels": ["合同号/P O", "合同号", "PO"],
    "delivery_date_labels": ["交货期", "交期"],
    "item_code_rules": [
        {
            "name": "singleton_3d_code",
            "pattern": r"^30(\d{5})$",
            "replace": r"3D\1",
            "reason": "辛格顿存货编码应为 3D + 5位数字，OCR 易把 D 识别成 0",
        }
    ],
    "item_columns": dict(GAOTAI_TEMPLATE_RULE["item_columns"]),
    "_source": "builtin",
}


def _json_dict(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_active_pdf_template_rules(db: Session) -> list[dict]:
    rows = db.execute(
        select(PdfOrderCustomerTemplate, Customer.name)
        .outerjoin(Customer, PdfOrderCustomerTemplate.customer_id == Customer.id)
        .where(PdfOrderCustomerTemplate.is_active.is_(True))
        .order_by(PdfOrderCustomerTemplate.created_at.desc())
    ).all()

    rules: list[dict] = []
    has_gaotai = False
    has_singleton = False
    for template, customer_name in rows:
        payload = _json_dict(template.column_map_json)
        merged = {
            "template_id": template.id,
            "template_name": template.template_name,
            "customer_id": template.customer_id,
            "customer_name": payload.get("customer_name") or customer_name,
            "customer_name_pattern": template.customer_name_pattern,
            "order_no_pattern": template.order_no_pattern,
            "date_pattern": template.date_pattern,
            "item_row_pattern": template.item_row_pattern,
            "aliases": payload.get("aliases") or [],
            "keywords": payload.get("keywords") or [],
            "item_code_rules": payload.get("item_code_rules") or [],
            "item_columns": payload.get("item_columns") or {},
            "_source": "database",
        }
        rules.append(merged)
        joined_text = " ".join(
            str(part or "")
            for part in (
                merged.get("customer_name"),
                template.template_name,
                template.customer_name_pattern,
            )
        )
        if "高泰" in joined_text:
            has_gaotai = True
        if "辛格顿" in joined_text:
            has_singleton = True

    if not has_gaotai:
        rules.append(dict(GAOTAI_TEMPLATE_RULE))
    if not has_singleton:
        rules.append(dict(SINGLETON_TEMPLATE_RULE))
    return rules
