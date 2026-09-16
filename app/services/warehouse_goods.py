"""Qualification shared by reverse suggestions and actual reservation authorization."""
import json
from sqlalchemy.orm import object_session
from app.models.material import Material
from app.services.paper_color import material_face
from app.models.warehouse_goods import WarehouseGoodsProfile
from app.services.warehouse_inventory import normalize_material_code


def goods_profile(db, lot):
    row = db.get(WarehouseGoodsProfile, lot.id)
    return json.loads(row.data_json) if row else None


def product_face(product):
    if product.material is not None:
        return material_face(object_session(product), product.material)
    if product.surface_paper_type == "white":
        return "white"
    if product.surface_paper_type == "kraft":
        return "kraft"
    return "kraft"


def lot_face(db, lot, profile=None):
    detail = lot.semi_finished_detail
    if detail and detail.layer_count == 1:
        profile = profile if profile is not None else goods_profile(db, lot)
        return (profile or {}).get("face_paper", "unknown")
    material_id = (profile or {}).get("verified_material_id") or (detail.material_id if detail else None)
    material = db.get(Material, material_id) if material_id else None
    return material_face(db, material, material_code=detail.material_code_snapshot if detail else None,
        supplier_name=detail.supplier_name if detail else None)


def qualification_issues(db, lot, product, profile=None, expected_material_code=None):
    """Dimensions/conversions/creases are checked by the existing signature engine."""
    profile = profile if profile is not None else goods_profile(db, lot)
    face = lot_face(db, lot, profile)
    issues = []
    if face != "unknown" and face != product_face(product):
        issues.append("白面纸与瓦楞色不能互用")
    if not profile:
        return issues
    if profile["scope"] == "customers" and product.customer_id not in profile["customer_ids"]:
        issues.append("不在已确认的适用客户范围")
    if profile["product_ids"] and product.id not in profile["product_ids"]:
        issues.append("不在已确认的适用产品范围")
    if profile["processing"] in {"die_cut", "printed", "creased"} and product.id not in profile["product_ids"]:
        issues.append("加工过的片料须逐款确认可用产品")
    if profile["mold_tool_id"] and product.mold_tool_id != profile["mold_tool_id"]:
        issues.append("模具不同或产品未登记模具")
    detail = lot.semi_finished_detail
    material = db.get(Material, profile["verified_material_id"]) if profile["verified_material_id"] else None
    actual_code = material.code if material else profile.get("material_code") or detail.material_code_snapshot
    expected_code = expected_material_code or (product.material.code if product.material else product.default_material_code)
    if not expected_code or not actual_code:
        issues.append("产品或库存材质尚未完善")
    elif normalize_material_code(actual_code) != normalize_material_code(expected_code):
        if detail.sheet_type == "raw_board" and face != "white":
            issues.append("材质不一致，不能抵扣")
        elif face == "white" and product_face(product) == "white":
            pass  # Supplier-maintained W/Y white faces may substitute; preserve actual cost/code.
        elif not (profile.get('allow_material_substitution') and profile.get('usage_confirmed')):
            issues.append("替代材质尚未确认适用，不能按尺寸推断强度合格")
    if product.layer_count and detail.layer_count != product.layer_count:
        issues.append("层数不一致")
    return issues
