"""A directional, per-allocation cutting contract. Never changes master cutting yield."""
from decimal import Decimal
from app.services.box_type_rules import get_box_type_rule
from app.services.warehouse_goods import goods_profile, qualification_issues, lot_face, product_face
from app.services.sheet_measurement import crease_match


def rectangular_cut_plan(db, lot, product, expected):
    detail = lot.semi_finished_detail
    profile = goods_profile(db, lot)
    if detail and ((profile or {}).get("processing") == "creased" or detail.sheet_type == "creased_sheet"):
        match = crease_match(db,lot,product,expected)
        if not match or not match["known"]:
            return None
        return dict(schema=1,method="crease_preserving_trim",product_id=product.id,lot_id=lot.id,
            source_length_mm=detail.board_length_mm,source_width_mm=detail.board_width_mm,
            target_length_mm=expected.board_length_mm,target_width_mm=expected.board_width_mm,
            source_creases=[detail.crease_left_mm,detail.crease_middle_mm,detail.crease_right_mm],
            target_creases=match["target_creases"],rows=1,columns=1,blank_yield=1,yield_factor=1,
            rotated=False,requires_production=True,material_code=detail.material_code_snapshot,flute_type=detail.flute_type,
            utilization=round(expected.board_length_mm*expected.board_width_mm*100/(detail.board_length_mm*detail.board_width_mm),2))
    # Only explicitly intact, unprinted rectangles. A die-cut bounding box is not a sheet.
    if not detail or not profile or profile.get("processing") not in {"raw", "cut"}:
        return None
    if any(getattr(detail, name, None) for name in ('crease_left_mm', 'crease_middle_mm', 'crease_right_mm')):
        return None  # Registered creases cannot be treated as an arbitrary blank rectangle.
    if qualification_issues(db, lot, product, expected_material_code=expected.normalized_material_code):
        return None
    if (lot_face(db, lot, profile) != product_face(product)
            or detail.flute_type != expected.flute_type
            or detail.component_type != expected.component_type
            or not detail.layer_count or not (product.layer_count or (product.material and product.material.layer_count))
            or detail.layer_count != (product.layer_count or product.material.layer_count)
            or detail.stock_yield_per_sheet != 1):
        return None
    rule = get_box_type_rule(product.box_style)
    # The requirement's frozen report dimensions define one production blank. For
    # plain one-up flat pieces net dimensions must agree; never guess old cm fields.
    length, width = expected.board_length_mm, expected.board_width_mm
    if detail.sheet_type != "raw_board" and rule and rule.code in {"liner", "divider", "die_cut_partition"}:
        if expected.stock_yield_per_sheet != 1 or (product.length_mm, product.width_mm) != (length, width):
            return None
    if any(not value or value <= 0 for value in (length, width, detail.board_length_mm, detail.board_width_mm)):
        return None
    # Net sheet edges need no extra trim by default. Explicitly recorded allowances
    # override zero; missing raw trim is never invented as an actual factory fact.
    try:
        trim = Decimal(str(profile.get("cut_trim_mm", 0)))
        kerf = Decimal(str(profile.get("cut_kerf_mm", 0)))
        if not trim.is_finite() or not kerf.is_finite() or trim < 0 or kerf < 0:
            return None
        usable_l = Decimal(detail.board_length_mm) - 2 * trim
        usable_w = Decimal(detail.board_width_mm) - 2 * trim
        across = int((usable_l + kerf) // (Decimal(length) + kerf))
        down = int((usable_w + kerf) // (Decimal(width) + kerf))
    except (ValueError, ArithmeticError):
        return None
    if across < 1 or down < 1:
        return None
    if (length, width) == (detail.board_length_mm, detail.board_width_mm):
        return None  # Existing exact-match production rules are unchanged.
    output = across * down * expected.stock_yield_per_sheet
    return dict(schema=1, method="fixed_direction_grid", product_id=product.id,
        lot_id=lot.id, source_length_mm=detail.board_length_mm, source_width_mm=detail.board_width_mm,
        target_length_mm=int(length), target_width_mm=int(width), rows=across, columns=down,
        trim_mm=str(trim), kerf_mm=str(kerf), rotated=False,
        blank_yield=expected.stock_yield_per_sheet, yield_factor=output,
        material_code=detail.material_code_snapshot, flute_type=detail.flute_type,
        requires_production=True, allowance_basis="recorded" if "cut_trim_mm" in profile or "cut_kerf_mm" in profile else "zero_allowance_theoretical",
        utilization=round(float(Decimal(length * width * across * down) * 100 / Decimal(detail.board_length_mm * detail.board_width_mm)), 2))
