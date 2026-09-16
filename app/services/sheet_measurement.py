"""Measured dimensions are discovery evidence, never extra usable material."""
from app.services.box_type_rules import get_box_type_rule
from app.services.warehouse_goods import goods_profile, qualification_issues, lot_face, product_face


def measurement_tolerance(profile):
    # Old stock has unknown measurement provenance: discover, but do not certify.
    return 0 if (profile or {}).get("dimension_source") == "label" else 10


def crease_geometry(detail, target, length, width, tolerance=0):
    source = tuple(getattr(detail, f"crease_{part}_mm", None) for part in ("left", "middle", "right"))
    if any(v is None or v <= 0 for v in (*source, *target, length, width)):
        return False
    return (abs(sum(source)-detail.board_width_mm) <= tolerance
        and sum(target) == width and abs(source[1]-target[1]) <= tolerance
        and source[0]+tolerance >= target[0] and source[2]+tolerance >= target[2]
        and detail.board_length_mm+tolerance >= length)


def crease_match(db, lot, product, expected):
    profile = goods_profile(db, lot) or {}
    d = lot.semi_finished_detail
    if profile.get("processing") not in {None, "creased"}:
        return None
    if profile.get("processing") != "creased" and d.sheet_type != "creased_sheet":
        return None
    rule = get_box_type_rule(product.box_style)
    if not rule or rule.code != "a1_0201" or expected.component_type != "whole":
        return None
    issues = qualification_issues(db, lot, product, expected_material_code=expected.normalized_material_code)
    if any(x != "加工过的片料须逐款确认可用产品" for x in issues):
        return None
    if (not (product.layer_count or (product.material and product.material.layer_count))
            or d.layer_count != (product.layer_count or product.material.layer_count)
            or d.flute_type != expected.flute_type or lot_face(db,lot,profile) != product_face(product)
            or d.pieces_per_box != expected.pieces_per_box or d.stock_yield_per_sheet != 1
            or expected.stock_yield_per_sheet != 1):
        return None
    target = tuple(getattr(product, f"crease_{part}_mm", None) for part in ("left","middle","right"))
    exact = crease_geometry(d, target, expected.board_length_mm, expected.board_width_mm)
    if not exact and not crease_geometry(d,target,expected.board_length_mm,expected.board_width_mm,measurement_tolerance(profile)):
        return None
    return dict(known=exact, automatic=False, score=100 if exact else 90,
        reason="同高压线，可裁两侧/长度" if exact else "压线测量差≤10mm，需复核中段高度及裁切余量",
        target_creases=list(target), measurement_review=not exact)


def measurement_review(db, lot, product, expected):
    """Return an unselectable nearby rectangle; preserve all qualification gates."""
    d=lot.semi_finished_detail
    profile=goods_profile(db,lot)
    if not profile or profile.get("processing") not in {"raw","cut"}:
        return None
    if any(getattr(d, f"crease_{part}_mm", None) for part in ("left","middle","right")):
        return None
    if qualification_issues(db,lot,product,expected_material_code=expected.normalized_material_code):
        return None
    if (not (product.layer_count or (product.material and product.material.layer_count))
            or d.layer_count != (product.layer_count or product.material.layer_count)
            or d.flute_type != expected.flute_type or lot_face(db,lot,profile) != product_face(product)
            or d.component_type != expected.component_type or d.pieces_per_box != expected.pieces_per_box
            or d.stock_yield_per_sheet != 1 or expected.stock_yield_per_sheet != 1):
        return None
    axes=[(d.board_length_mm,expected.board_length_mm),(d.board_width_mm,expected.board_width_mm)]
    tolerance=measurement_tolerance(profile)
    if not tolerance or any(not a or not b or a<=0 or b<=0 for a,b in axes):
        return None
    if not any(a<b for a,b in axes) or not all(abs(a-b)<=tolerance for a,b in axes):
        return None
    diff="、".join(f"{name}差{a-b:+}mm" for name,(a,b) in zip(("长","宽"),axes) if a!=b)
    return dict(known=False,automatic=False,score=90,measurement_review=True,
        reason=f"测量待核：{diff}；复核实际可用尺寸后采用")
