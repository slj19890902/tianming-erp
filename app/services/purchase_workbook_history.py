"""Source-backed purchase traces; these never create procurement or stock facts."""
from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy import and_, or_, select

from app.models.audit import OperationLog
from app.models.product import Product
from app.services.audit_log import append_audit_event, serialize_audit_details

ACTION = "import_purchase_workbook_trace"
TYPE = "purchase_workbook_trace_v1"
FIELDS = {
    "source_sha256", "source_name", "source_lineage", "sheet", "row", "product_id", "customer_id",
    "source_date_label", "report_dimensions_text", "crease_text", "material_text",
    "supplier_names", "purchase_sheet_qty", "related_product_ids", "identity_basis",
}


def _text(value, maximum):
    text = str(value or "").strip()
    if len(text) > maximum:
        raise ValueError("历史采购来源字段过长")
    return text


def normalize_trace(value):
    if set(value) - FIELDS:
        raise ValueError("历史采购来源包含未允许的字段")
    sha = _text(value.get("source_sha256"), 64)
    if not re.fullmatch(r"[a-f0-9]{64}", sha):
        raise ValueError("缺少完整来源文件摘要")
    result = {
        "source_sha256": sha,
        "source_name": _text(value.get("source_name"), 150),
        "source_lineage": _text(value.get("source_lineage"), 100),
        "sheet": _text(value.get("sheet"), 80),
        "row": int(value["row"]),
        "product_id": int(value["product_id"]),
        "customer_id": int(value["customer_id"]),
        "source_date_label": _text(value.get("source_date_label"), 100),
        "report_dimensions_text": _text(value.get("report_dimensions_text"), 250),
        "crease_text": _text(value.get("crease_text"), 250),
        "material_text": _text(value.get("material_text"), 250),
        "supplier_names": sorted({_text(x, 200) for x in value.get("supplier_names", []) if x}),
        "related_product_ids": sorted({int(x) for x in value.get("related_product_ids", [])}),
        "identity_basis": _text(value.get("identity_basis"), 100),
        "purchase_sheet_qty": None,
    }
    if min(result["row"], result["product_id"], result["customer_id"]) <= 0 or result["row"] > 1_048_576:
        raise ValueError("来源行号或产品身份无效")
    if not result["source_name"] or not result["source_lineage"] or not result["sheet"] or not result["identity_basis"]:
        raise ValueError("来源文件、工作表及匹配依据必须明确")
    if result["product_id"] not in result["related_product_ids"]:
        raise ValueError("本产品不在原表行的已核定产品范围内")
    if any(x <= 0 for x in result["related_product_ids"]):
        raise ValueError("原表关联产品身份无效")
    if len(result["related_product_ids"]) > 30 or len(result["supplier_names"]) > 10:
        raise ValueError("本行关联范围过大，应先人工核对")
    quantity = value.get("purchase_sheet_qty")
    if quantity is not None:
        try:
            quantity = Decimal(str(quantity))
        except InvalidOperation as error:
            raise ValueError("原表采购张数无效") from error
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError("原表采购张数无效")
        if quantity > Decimal("999999999999999999") or quantity.as_tuple().exponent < -6:
            raise ValueError("原表采购张数超出可保存范围")
        result["purchase_sheet_qty"] = format(quantity.normalize(), "f")
    return result


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def trace_key(value):
    lineage = hashlib.sha256(value["source_lineage"].encode()).hexdigest()[:24]
    sheet = hashlib.sha256(value["sheet"].encode()).hexdigest()[:16]
    return f"purchase-trace:{value['product_id']}:{lineage}:{sheet}:{value['row']:08d}"


def record_trace(db, value, *, actor, batch_id):
    """Caller holds a write transaction/maintenance lock and commits the batch."""
    if actor is None or not actor.is_active or actor.role != "admin":
        raise HTTPException(403, "仅管理员可导入已核定的历史采购来源")
    value = normalize_trace(value)
    product = db.get(Product, value["product_id"])
    if product is None or product.customer_id != value["customer_id"] or product.deleted_at is not None or not product.is_active:
        raise ValueError("已核定的产品身份或客户发生变化")
    related = db.scalars(select(Product).where(Product.id.in_(value["related_product_ids"]))).all()
    if len(related) != len(value["related_product_ids"]) or any(
        p.customer_id != product.customer_id or p.deleted_at is not None or not p.is_active
        for p in related
    ):
        raise ValueError("原表混合客户或关联产品身份尚未核定")
    key = trace_key(value)
    # Saving/copying an unchanged XLSX can change its ZIP hash or filename.
    # Those provenance fields must not create a second business trace.
    semantic = {k: v for k, v in value.items() if k not in {"source_sha256", "source_name"}}
    payload = {"type": TYPE, "trace": value, "trace_hash": hashlib.sha256(_canonical(semantic).encode()).hexdigest()}
    existing = db.scalar(select(OperationLog).where(OperationLog.object_ref == key, OperationLog.action_code == ACTION))
    if existing is not None:
        previous = json.loads(existing.details or "{}")
        if previous.get("trace_hash") != payload["trace_hash"]:
            raise ValueError("同一原表行的导入内容已变化，应先核对更正")
        return {"id": existing.id, "created": False}
    serialized = serialize_audit_details(payload)
    if json.loads(serialized) != payload:
        raise ValueError("来源记录超出审计保存范围，应先缩小字段")
    audit = append_audit_event(
        db, event_category="business", result="success", source="import",
        module_code="requisition", action_code=ACTION, resource="purchase_workbook_trace",
        actor=actor, entity_type="product", entity_id=product.id,
        customer_id=product.customer_id, object_ref=key, batch_id=batch_id,
        description="导入历史采购台账参考；不生成采购、收料、订单或库存",
        details=payload,
    )
    return {"id": audit.id, "created": True}


def _public_text(value, *, can_view_costs):
    # Raw workbook cells may contain a price annotation. The cost permission
    # applies to annotations as well as the ordinary material-price response.
    if not can_view_costs and re.search(r"[￥¥]|(?:单价|金额|成本|元[/／㎡张])", value):
        return "原表含价格备注（需成本查看权限）"
    return value


def history_page(db, product, *, can_view_costs, before_id=None, limit=50):
    limit = min(max(int(limit), 1), 100)
    prefix = f"purchase-trace:{product.id}:"
    query = select(OperationLog).where(
        OperationLog.object_ref >= prefix, OperationLog.object_ref < prefix + "\uffff",
        OperationLog.action_code == ACTION, OperationLog.entity_type == "product",
        OperationLog.entity_id == product.id, OperationLog.customer_id_snapshot == product.customer_id,
    )
    if before_id is not None:
        anchor = db.scalar(query.where(OperationLog.id == int(before_id)))
        if anchor is None:
            raise HTTPException(400, "历史采购分页位置已失效，请刷新")
        query = query.where(or_(
            OperationLog.object_ref < anchor.object_ref,
            and_(OperationLog.object_ref == anchor.object_ref, OperationLog.id < anchor.id),
        ))
    records = db.scalars(query.order_by(OperationLog.object_ref.desc(), OperationLog.id.desc()).limit(limit + 1)).all()
    has_more = len(records) > limit
    selected = records[:limit]
    rows = []
    for record in selected:
        payload = json.loads(record.details or "{}")
        value = payload.get("trace", {})
        if payload.get("type") != TYPE or value.get("product_id") != product.id or value.get("customer_id") != product.customer_id:
            raise ValueError("历史采购来源记录身份不一致")
        rows.append({
            "source_type": "purchase_workbook", "source_label": "历史采购台账",
            "source_confidence": "external_workbook", "is_formal_requisition": False,
            "document_id": record.id, "document_item_id": record.id,
            "document_no": f"{value['source_name']} / {value['sheet']} / 第{value['row']}行",
            "document_date": None, "source_date_label": _public_text(value["source_date_label"], can_view_costs=can_view_costs),
            "source_row": value["row"], "material_code": _public_text(value["material_text"], can_view_costs=can_view_costs),
            "supplier_name": " / ".join(value["supplier_names"]) or "原表未注明供应商",
            "report_dimensions_text": _public_text(value["report_dimensions_text"], can_view_costs=can_view_costs),
            "crease_text": _public_text(value["crease_text"], can_view_costs=can_view_costs),
            "source_purchase_sheet_qty": value["purchase_sheet_qty"],
            "shared_source_row": len(value["related_product_ids"]) > 1,
            "document_status": "原表采购参考；实收与用途分别核对",
        })
    return {"items": rows, "has_more": has_more, "next_before_id": selected[-1].id if has_more and selected else None}
