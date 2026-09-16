"""Discovery is broader than reservation; dimensions alone never prove a die shape."""
from sqlalchemy import select
from app.models.warehouse_inventory import SemiFinishedLotAllowedProduct
from app.models.mold_tool import MoldTool
from app.services.warehouse_goods import goods_profile, qualification_issues
from app.services.sheet_measurement import crease_match, measurement_review
from app.services.box_type_rules import get_box_type_rule

MISSING_USE = "加工过的片料须逐款确认可用产品"


def processed_match(db, lot, product, expected):
    profile = goods_profile(db, lot)
    detail = lot.semi_finished_detail
    if not detail:
        return None
    if (profile or {}).get("processing") == "creased" or detail.sheet_type == "creased_sheet":
        return crease_match(db, lot, product, expected)
    review = measurement_review(db, lot, product, expected)
    if review:
        return review
    if not profile or not detail or profile.get("processing") not in {"die_cut", "printed", "creased"}:
        return None
    rule = get_box_type_rule(product.box_style)
    if profile.get("processing") == "die_cut" and rule and rule.code == "liner":
        return None
    # Explicit customer/product exclusions, face, material, layer and mold conflicts remain hard.
    issues = qualification_issues(db, lot, product, expected_material_code=expected.normalized_material_code)
    if any(issue != MISSING_USE for issue in issues):
        return None
    if (detail.flute_type != expected.flute_type or detail.component_type != expected.component_type
            or detail.pieces_per_box != expected.pieces_per_box
            or detail.stock_yield_per_sheet != expected.stock_yield_per_sheet):
        return None
    approved = product.id in profile.get("product_ids", []) or db.scalar(select(SemiFinishedLotAllowedProduct.id).where(
        SemiFinishedLotAllowedProduct.inventory_lot_id == lot.id,
        SemiFinishedLotAllowedProduct.product_id == product.id)) is not None
    # Blank die-cut sheets can serve different printed products using the same registered mold.
    mold = db.get(MoldTool, profile["mold_tool_id"]) if profile.get("mold_tool_id") else None
    if mold and profile.get("mold_version") is not None and (not mold.is_active or profile["mold_version"] != mold.version):
        return None
    mold_match = (profile.get("processing") == "die_cut" and mold and mold.is_active
                  and profile["mold_tool_id"] == product.mold_tool_id
                  and profile.get("mold_version") == mold.version
                  and profile.get("blank_unprinted") is True)
    known = bool(approved or mold_match)
    closeness = sum(abs(a-b)/max(b,1) for a,b in [
        (detail.board_length_mm,expected.board_length_mm),
        (detail.board_width_mm,expected.board_width_mm)]) / 2
    # Post-cut/estimated bounds cannot prove incompatibility. Rank, never silently discard.
    same_customer = profile.get("scope") == "customers" and product.customer_id in profile.get("customer_ids", [])
    auto = (known and same_customer and profile.get("material_confidence") == "confirmed"
            and bool(product.layer_count) and profile.get("processing") == "die_cut")
    return {"known": known, "automatic": bool(auto), "score": 100 if known else max(0, 100-round(closeness*100)),
            "reason": "已确认产品适用" if approved else "同模具未印刷片料" if mold_match else "加工后片料，需确认形状与用途"}
