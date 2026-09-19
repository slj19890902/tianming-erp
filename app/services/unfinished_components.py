"""Dedicated unfinished A3 parts retain source identity and never imply finished output."""
import json
from sqlalchemy import select
from app.models.warehouse_goods import WarehouseGoodsProfile
from app.models.warehouse_inventory import InventoryLot, InventoryReservation, OrderItemSemiRequirement

PROCESSING = 'dedicated_component'


def component_match(db, lot, product, expected, profile):
    """Exact physical route only; never fall back to a rectangular liner."""
    from app.services.warehouse_goods import qualification_issues
    from app.services.box_type_rules import get_box_type_rule
    detail = lot.semi_finished_detail
    rule = get_box_type_rule(product.box_style) if product else None
    if (not detail or not rule or rule.code != 'a3_set'
            or profile.get('scope') != 'customers'
            or profile.get('customer_ids') != [product.customer_id]
            or profile.get('product_ids') != [product.id]
            or profile.get('component_type') not in {'cover','base'}
            or profile.get('component_type') != expected.component_type
            or profile.get('flute_direction') not in {'length','width'}
            or set(profile.get('completed_processes',[])) != {'creasing','slotting'}
            or profile.get('remaining_processes') != ['nailing']):
        return None
    if qualification_issues(db,lot,product,profile,expected.normalized_material_code):
        return None
    for name in ('component_type','board_length_mm','board_width_mm','flute_type','pieces_per_box','stock_yield_per_sheet'):
        if getattr(detail,name) != getattr(expected,name):
            return None
    if detail.normalized_material_code != expected.normalized_material_code:
        return None
    return dict(known=True,automatic=False,score=100,
        reason='专用盖/底：已压线开槽，仍须打钉；请核对瓦楞方向后采用')


def unfinished_reservations(db, order_item_id, *, bom_snapshot_id=None):
    rows=db.execute(select(InventoryReservation,OrderItemSemiRequirement,WarehouseGoodsProfile)
        .join(OrderItemSemiRequirement,OrderItemSemiRequirement.id==InventoryReservation.semi_requirement_id)
        .join(WarehouseGoodsProfile,WarehouseGoodsProfile.lot_id==InventoryReservation.inventory_lot_id)
        .where((InventoryReservation.order_item_id==order_item_id if isinstance(order_item_id,int) else InventoryReservation.order_item_id.in_(order_item_id)),
            InventoryReservation.reservation_type=='semi_order',InventoryReservation.status!='cancelled',
            InventoryReservation.credited_requirement_quantity > InventoryReservation.consumed_requirement_quantity + InventoryReservation.released_requirement_quantity)).all()
    return [(reservation,requirement,json.loads(profile.data_json)) for reservation,requirement,profile in rows
        if json.loads(profile.data_json).get('processing')==PROCESSING
        and (bom_snapshot_id is None or requirement.sales_order_item_bom_component_id==bom_snapshot_id)]


def receipt_component_identity(db, target, context, direction):
    """Validate the explicit actual-surplus choice against a frozen physical source."""
    from app.models.product import Product
    from app.models.product_bom import RequisitionItemBomSource
    from app.services.box_type_rules import get_box_type_rule
    from app.services.warehouse_inventory import WarehouseInventoryError
    snapshot=context.snapshot
    bom_source=db.get(RequisitionItemBomSource,snapshot.source_bom_requisition_source_id) if snapshot.source_bom_requisition_source_id else None
    bom=bom_source.sales_order_item_bom_component if bom_source else None
    product_id=bom.component_product_id if bom else target.order_item.product_id
    product=db.get(Product,product_id)
    rule=get_box_type_rule(product.box_style) if product else None
    if (not rule or rule.code!='a3_set' or snapshot.component_type not in {'cover','base'}
            or direction not in {'length','width'} or not product.is_active
            or product.customer_id!=snapshot.customer_id):
        raise WarehouseInventoryError('专用未完工部件仅适用于当前客户天地盖的盖/底；请明确瓦楞方向',409)
    from app.api.requisition import _component_crease,_bom_snapshot_crease
    crease=_bom_snapshot_crease(bom,snapshot.component_type) if bom else _component_crease(target.order_item,snapshot.component_type)
    source=context.source
    if hasattr(source,'crease_type'):
        crease=tuple(getattr(source,name,None) for name in ('crease_type','crease_left_mm','crease_middle_mm','crease_right_mm'))
    return dict(product_id=product_id,customer_id=snapshot.customer_id,component_type=snapshot.component_type,
        flute_direction=direction,crease=crease,product_code=(bom.snapshot_component_product_code if bom else target.order_item.snapshot_product_code) or product.product_code,
        product_name=(bom.snapshot_component_product_name if bom else target.order_item.snapshot_product_name) or product.product_name)


def freeze_component_profile(db,lot,identity,receipt_item,snapshot):
    from app.services.paper_color import material_face
    from app.models.material import Material
    detail=lot.semi_finished_detail
    material=db.get(Material,detail.material_id) if detail.material_id else None
    profile=dict(display_name=detail.internal_name,scope='customers',customer_ids=[identity['customer_id']],
        product_ids=[identity['product_id']],processing=PROCESSING,material_confidence='confirmed',
        verified_material_id=detail.material_id,material_code=detail.material_code_snapshot,
        face_paper=material_face(db,material),mold_tool_id=None,mold_version=None,blank_unprinted=True,
        dimension_source='label',usage_confirmed=True,allow_material_substitution=False,
        component_type=identity['component_type'],internal_component_code=f"PC-{identity['customer_id']}-{identity['product_id']}-{identity['component_type']}",
        flute_direction=identity['flute_direction'],completed_processes=['creasing','slotting'],remaining_processes=['nailing'],
        source_receipt_item_id=receipt_item.id,source_purchase_purpose_snapshot_id=snapshot.id,
        source_product_code=identity['product_code'],source_customer_id=identity['customer_id'],note='实收多余专用部件，尚未打钉，不能作为完整成品或通用衬板')
    db.add(WarehouseGoodsProfile(lot_id=lot.id,data_json=json.dumps(profile,ensure_ascii=False)))
    return profile
