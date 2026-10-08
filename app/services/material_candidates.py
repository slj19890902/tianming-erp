"""Dimension suggestions are not production approval or inventory reservations."""
import json
from decimal import Decimal, ROUND_DOWN
from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.orm import joinedload
from app.models.product import Product
from app.models.material_candidate import MaterialCandidateSelection
from app.models.customer import Customer
from app.models.material import Material
from app.services.warehouse_goods import goods_profile, qualification_issues, lot_face, product_face
from app.services.box_type_rules import get_box_type_rule
from app.services.box_type_rules import BOX_TYPE_RULES
from app.services.processed_sheet_matching import processed_match
from app.services.sheet_cut_plan import rectangular_cut_plan
from app.services.requisition_quantities import cutting_factor
from app.services.sheet_cutting_settings import theoretical_product_yield
from types import SimpleNamespace


def matching_dimensions(product, detail):
    """Only unit-declared mm fields; a finished box footprint is not a flat blank."""
    if detail.sheet_type == "raw_board":
        if detail.component_type == "base":
            values = product.base_report_length_mm, product.base_report_width_mm
        else:
            values = product.report_length_mm, product.report_width_mm
        basis = "报料尺寸 mm"
    else:
        rule = get_box_type_rule(product.box_style)
        if detail.component_type != "base" and rule and rule.code in {"liner", "divider", "die_cut_partition"}:
            values = product.length_mm, product.width_mm
            basis = "净片尺寸 mm"
        else:
            values = ((product.base_report_length_mm, product.base_report_width_mm) if detail.component_type == "base"
                else (product.report_length_mm, product.report_width_mm))
            basis = "后续加工展开尺寸 mm"
    if any(value is None or value <= 0 for value in values):
        return None
    return Decimal(values[0]), Decimal(values[1]), basis


def candidate_items(db, lot, visible_customer_ids=None):
    detail = lot.semi_finished_detail
    if lot.inventory_type != "semi_finished" or detail is None:
        raise HTTPException(422, "仅原材料或半成品片料支持匹配")
    length, width = detail.board_length_mm, detail.board_width_mm
    if not length or not width or length <= 0 or width <= 0:
        raise HTTPException(422, "片料缺少有效的毫米长宽，请先补齐库存尺寸")
    if detail.sheet_type == "raw_board":
        columns = ((Product.base_report_length_mm, Product.base_report_width_mm) if detail.component_type == "base"
            else (Product.report_length_mm, Product.report_width_mm))
    else:
        columns = Product.length_mm, Product.width_mm
    profile = goods_profile(db, lot)
    query = select(Product).join(Customer, Customer.id == Product.customer_id).outerjoin(Material, Material.id == Product.material_id).options(joinedload(Product.customer), joinedload(Product.material)).where(
        Product.is_active.is_(True), Product.is_composite.is_(False),
        Product.deleted_at.is_(None), Customer.status == "active",
        Product.supply_mode == "corrugated_production",
        Product.flute_type == detail.flute_type, func.coalesce(Product.layer_count, Material.layer_count) == detail.layer_count)
    if visible_customer_ids is not None:
        query = query.where(Product.customer_id.in_(visible_customer_ids))
    items = []
    for product in db.scalars(query):
        rule = get_box_type_rule(product.box_style)
        expected = SimpleNamespace(customer_id=product.customer_id,
            board_length_mm=product.base_report_length_mm if detail.component_type == "base" else product.report_length_mm,
            board_width_mm=product.base_report_width_mm if detail.component_type == "base" else product.report_width_mm,
            normalized_material_code=(product.material.code if product.material else product.default_material_code) or "",
            flute_type=product.flute_type, component_type=detail.component_type,
            pieces_per_box=product.pieces_per_box or 1, stock_yield_per_sheet=theoretical_product_yield(product, detail.component_type))
        processed = processed_match(db, lot, product, expected) if expected.board_length_mm and expected.board_width_mm else None
        if processed:
            items.append(dict(product_id=product.id, customer_id=product.customer_id,
                customer_name=product.customer.chinese_short_name or product.customer.name,
                inventory_code=product.product_code, product_name=product.product_name,
                box_style=rule.display_name if rule else product.box_style or "未设置箱型",
                is_liner=bool(rule and rule.code == "liner"), match_kind="measurement_review" if processed.get("measurement_review") else "confirmed_use" if processed["known"] else "needs_review",
                match_reason=processed["reason"], length_mm=float(length), width_mm=float(width), flute_type=product.flute_type,
                dimension_basis="库存净片；按用途匹配", near_dimension_match=processed["known"],
                score=processed["score"], color_compatible=True, selectable=processed["known"],
                warnings=[f"报料 {expected.board_length_mm}×{expected.board_width_mm}mm；库存净片 {length}×{width}mm",
                    f"实际材质 {detail.material_code_snapshot}；产品材质 {expected.normalized_material_code}"] + ([] if processed["known"] else [processed["reason"]]),
                exact_dimension_match=False, requires_production_review=True, face_paper=product_face(product)))
            continue
        if detail.sheet_type == "creased_sheet" or (profile and profile.get("processing") in {"die_cut", "printed", "creased"}):
            continue  # Never fall back to rectangle matching for a shaped blank.
        dimensions = matching_dimensions(product, detail)
        if dimensions is None:
            continue
        pl, pw, basis = dimensions
        if pl > length or pw > width:
            continue  # Preserve flute direction; never rotate to force a match.
        cut_plan = rectangular_cut_plan(db, lot, product, expected) if expected.board_length_mm and expected.board_width_mm else None
        score = cut_plan["utilization"] if cut_plan else float((Decimal(pl * pw) * 100 / Decimal(length * width)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))
        cross_customer = detail.owner_customer_id is not None and product.customer_id != detail.owner_customer_id
        warnings = ["跨客户：使用前需人工确认"] if cross_customer else []
        if not detail.material_id or detail.material_id != product.material_id:
            warnings.append("材质需核对")
        if detail.sheet_type != "raw_board":
            warnings.append("净片/压线需核对")
        if pl != length or pw != width:
            warnings.append("需裁切，楞向不旋转")
        issues = qualification_issues(db, lot, product, profile)
        warnings.extend(issues)
        face = lot_face(db, lot, profile)
        color_compatible = face == "unknown" or face == product_face(product)
        warnings.append("开数及每箱片数需核对")
        items.append(dict(product_id=product.id, customer_id=product.customer_id,
            customer_name=product.customer.chinese_short_name or product.customer.name,
            inventory_code=product.product_code, product_name=product.product_name,
            box_style=rule.display_name if rule else product.box_style or "未设置箱型",
            is_liner=bool(rule and rule.code == "liner"), match_kind="cuttable" if cut_plan else "dimensions",
            match_reason=f"需裁切 · 一张出{cut_plan['yield_factor']}片" if cut_plan else "尺寸一致" if pl == length and pw == width else "尺寸候选，待核用途",
            cut_plan=cut_plan,
            length_mm=float(pl), width_mm=float(pw), flute_type=product.flute_type,
            dimension_basis=basis,
            near_dimension_match=pl >= Decimal(length) * Decimal("0.9") and pw >= Decimal(width) * Decimal("0.9"),
            score=score, color_compatible=color_compatible, selectable=bool(cut_plan or score > 70) and color_compatible and not issues, warnings=list(dict.fromkeys(warnings)),
            exact_dimension_match=pl == length and pw == width,
            requires_production_review=True,
            face_paper=product_face(product)))
    return sorted(items, key=lambda item: (not item["selectable"], item["is_liner"], item["match_kind"] != "confirmed_use", not item["exact_dimension_match"], -item["score"], item["customer_name"], item["inventory_code"], item["product_id"]))


def candidate_response(db, lot, visible_customer_ids=None):
    items = candidate_items(db, lot, visible_customer_ids)
    latest = db.scalar(select(MaterialCandidateSelection).where(MaterialCandidateSelection.lot_id == lot.id)
        .order_by(MaterialCandidateSelection.id.desc()).limit(1))
    saved = json.loads(latest.candidates_json) if latest else []
    if visible_customer_ids is not None:
        saved = [item for item in saved if item["customer_id"] in visible_customer_ids]
    detail = lot.semi_finished_detail
    return dict(lot_id=lot.id, version=lot.version, items=items, saved=saved,
        box_styles=[rule.display_name for rule in BOX_TYPE_RULES],
        editable=lot.status == "active" and lot.quantity_available > 0,
        source=f"{detail.board_length_mm}×{detail.board_width_mm} mm · {detail.flute_type}楞",
        dimension_notice="按工艺及毫米尺寸匹配；卷尺10mm内差异只作待核候选，不增加可用尺寸或产能。",
        saved_at=latest.created_at.isoformat() if latest else None)
