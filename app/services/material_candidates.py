"""Dimension suggestions are not production approval or inventory reservations."""
import json
from decimal import Decimal, ROUND_DOWN
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from app.models.product import Product
from app.models.material_candidate import MaterialCandidateSelection


def candidate_items(db, lot, visible_customer_ids=None):
    detail = lot.semi_finished_detail
    if lot.inventory_type != "semi_finished" or detail is None:
        raise HTTPException(422, "仅原材料或半成品片料支持匹配")
    length, width = detail.board_length_mm, detail.board_width_mm
    base = detail.component_type == "base"
    length_column = Product.base_report_length_mm if base else Product.report_length_mm
    width_column = Product.base_report_width_mm if base else Product.report_width_mm
    query = select(Product).options(joinedload(Product.customer)).where(
        Product.is_active.is_(True), Product.is_composite.is_(False),
        Product.supply_mode == "corrugated_production",
        Product.flute_type == detail.flute_type, Product.layer_count == detail.layer_count,
        length_column > 0, length_column <= length, width_column > 0, width_column <= width)
    if visible_customer_ids is not None:
        query = query.where(Product.customer_id.in_(visible_customer_ids))
    items = []
    for product in db.scalars(query):
        pl = product.base_report_length_mm if base else product.report_length_mm
        pw = product.base_report_width_mm if base else product.report_width_mm
        score = float((Decimal(pl * pw) * 100 / Decimal(length * width)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))
        cross_customer = detail.owner_customer_id is not None and product.customer_id != detail.owner_customer_id
        warnings = ["跨客户：使用前需人工确认"] if cross_customer else []
        if not detail.material_id or detail.material_id != product.material_id:
            warnings.append("材质需核对")
        if detail.sheet_type != "raw_board":
            warnings.append("净片/压线需核对")
        if pl != length or pw != width:
            warnings.append("需裁切，楞向不旋转")
        warnings.append("开数及每箱片数需核对")
        items.append(dict(product_id=product.id, customer_id=product.customer_id,
            customer_name=product.customer.chinese_short_name or product.customer.name,
            inventory_code=product.product_code, product_name=product.product_name,
            length_mm=pl, width_mm=pw, flute_type=product.flute_type,
            score=score, selectable=score > 70, warnings=warnings))
    return sorted(items, key=lambda item: (-item["score"], item["customer_name"], item["inventory_code"], item["product_id"]))


def candidate_response(db, lot, visible_customer_ids=None):
    items = candidate_items(db, lot, visible_customer_ids)
    latest = db.scalar(select(MaterialCandidateSelection).where(MaterialCandidateSelection.lot_id == lot.id)
        .order_by(MaterialCandidateSelection.id.desc()).limit(1))
    saved = json.loads(latest.candidates_json) if latest else []
    if visible_customer_ids is not None:
        saved = [item for item in saved if item["customer_id"] in visible_customer_ids]
    detail = lot.semi_finished_detail
    return dict(lot_id=lot.id, version=lot.version, items=items, saved=saved,
        editable=lot.status == "active" and lot.quantity_available > 0,
        source=f"{detail.board_length_mm}×{detail.board_width_mm} mm · {detail.flute_type}楞",
        saved_at=latest.created_at.isoformat() if latest else None)
