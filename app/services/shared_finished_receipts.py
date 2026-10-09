"""Freeze sharing eligibility at NEW ordinary-order insertion, never backfill."""
from sqlalchemy import select
from app.models.product import Product
from app.models.material import Material
from app.models.order import Order
from app.models.multilevel_bom import ProductBomProfile
from app.models.shared_finished_stock import (
    SharedFinishedGroup, SharedFinishedMember, SharedFinishedPolicy, SharedFinishedOrderBasis,
)
from app.services import shared_finished_stock as shared
from app.services.product_specification import normalized_specification_text

# Actual order inputs that can change what is made; no prices or quantities.
PAIRS = {
    "snapshot_material":"material", "flute_type":"flute", "layer_count":"layer_count",
    "snapshot_production_notes":"production_notes",
    "sales_unit_snapshot":"unit",
    **{"snapshot_"+key:key for key in ("crease_type","crease_left_mm","crease_middle_mm","crease_right_mm",
        "base_crease_type","base_crease_left_mm","base_crease_middle_mm","base_crease_right_mm",
        "splice_mode","pieces_per_box","flap_mm")},
}
EXTRAS = ("report_length_mm","report_width_mm","base_report_length_mm","base_report_width_mm")
FIELDS = tuple(PAIRS) + tuple("snapshot_"+key for key in EXTRAS) + (
    "order_id", "product_id", "snapshot_spec", "supply_mode_snapshot", "special_process",
    "combination_role", "is_virtual_composite_parent_snapshot", "combination_group_key",
    "combination_parent_product_id", "sheet_cutting_settings_snapshot", "drawing_file",
    "snapshot_report_notes", "snapshot_base_report_notes", "requisition_remark",
)


def order_identity(item):
    def value(key):
        raw=getattr(item,key,None)
        return raw if key=="sheet_cutting_settings_snapshot" or type(raw) is bool else shared._value(raw)
    return shared._json({key:value(key) for key in FIELDS})


def capture_order(connection, item):
    # Use the owning flush connection, never another Session or transaction.
    member=connection.execute(select(SharedFinishedMember.__table__).where(
        SharedFinishedMember.product_id==item.product_id)).mappings().first()
    if not member:
        return
    group=connection.execute(select(SharedFinishedGroup.__table__).where(
        SharedFinishedGroup.id==member["group_id"])).mappings().one()
    policy=connection.execute(select(SharedFinishedPolicy.auto_enroll).where(
        SharedFinishedPolicy.group_id==member["group_id"])).scalar()
    if not group["enabled"] or not policy:
        return
    if connection.execute(select(Order.customer_id).where(Order.id==item.order_id)).scalar()!=member["customer_id"]:
        return
    row=connection.execute(select(Product.__table__).where(Product.id==item.product_id)).mappings().one()
    if (not row["is_active"] or row["deleted_at"] or row["purged_at"] or row["is_composite"]
            or row["is_virtual_composite_parent"] or row["is_internal_component"]
            or row["supply_mode"]!="corrugated_production" or row["customer_id"]!=member["customer_id"]
            or connection.execute(select(ProductBomProfile.product_id).where(ProductBomProfile.product_id==item.product_id)).first()):
        return
    # Detached objects let the identity encoder use the real selected columns
    # without an ORM read during flush or a mutable lookup at receipt time.
    product=Product(**dict(row))
    if row["material_id"]:
        material=connection.execute(select(Material.__table__).where(Material.id==row["material_id"])).mappings().first()
        product.material=Material(**dict(material)) if material else None
    if shared.member_identity(product)!=member["identity_json"]:
        return
    import json
    basis=json.loads(member["product_basis_json"])
    from app.services.box_type_rules import order_snapshot_box_configuration, BoxTypeRuleError
    try:
        configuration=order_snapshot_box_configuration(product)
    except BoxTypeRuleError:
        return
    basis.update({key:configuration[key] for key in ("splice_mode","pieces_per_box","flap_mm")})
    if normalized_specification_text(item.snapshot_spec)!=basis["spec"]:
        return
    from app.services.product_unit_labels import basis_unit_label
    # A name-only count label uses the SAME number, member identity and ledger.
    # Nothing else in the frozen stock identity or order eligibility is relaxed.
    if item.sales_unit_snapshot not in {basis.get("unit"), basis_unit_label(basis)}:
        return
    if any(shared._value(getattr(item,field,None))!=shared._value(basis.get(key))
           for field,key in PAIRS.items() if key != "unit"):
        return
    if any(shared._value(getattr(item,"snapshot_"+key,None))!=shared._value(getattr(product,key,None)) for key in EXTRAS):
        return
    if (item.supply_mode_snapshot!="corrugated_production" or item.special_process!=configuration["default_cutting_mode"]
            or item.combination_role!="standalone" or item.is_virtual_composite_parent_snapshot
            or item.combination_group_key or item.combination_parent_product_id
            or item.drawing_file or item.snapshot_report_notes or item.snapshot_base_report_notes or item.requisition_remark
            or item.sheet_cutting_settings_snapshot!=product.sheet_cutting_settings):
        return
    connection.execute(SharedFinishedOrderBasis.__table__.insert().values(
        order_item_id=item.id,group_id=group["id"],group_version=group["version"],
        member_identity_json=member["identity_json"],product_basis_json=member["product_basis_json"],
        order_identity_json=order_identity(item)))
