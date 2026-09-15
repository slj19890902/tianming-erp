"""Separate physical accompanying goods from customer-document presentation.

Legacy master matches below are advisory only: they never authorize consumption
or silently convert an old order into a new execution contract.
"""
from sqlalchemy import select
from app.models.multilevel_bom import OrderBomGraph, ProductBomInventoryRelation
from app.models.product_bom import SalesOrderItemBomComponent
from app.models.production import ProductionCompletion
from app.models.product import Product

LEGACY_WARNING = '旧单随货配套尚未冻结并落实预占；请管理员切换BOM版本，本次不补扣历史数量'


def legacy_accompany_preview(db, item, quantity):
    if item is None or db.get(OrderBomGraph, item.id) is not None:
        return []
    from app.services.legacy_accompany import contract
    if contract(db, item.id) is not None:
        return []
    if db.scalar(select(ProductionCompletion.id).where(
            ProductionCompletion.order_item_id == item.id,
            ProductionCompletion.status == 'posted', ProductionCompletion.origin == 'receipt_auto').limit(1)) is None:
        return []
    snapshots = db.scalars(select(SalesOrderItemBomComponent).join(ProductBomInventoryRelation,
        ProductBomInventoryRelation.bom_component_id == SalesOrderItemBomComponent.product_bom_component_id).where(
        SalesOrderItemBomComponent.sales_order_item_id == item.id,
        ProductBomInventoryRelation.relation == 'accompany').order_by(SalesOrderItemBomComponent.display_order))
    return [dict(component_snapshot_id=s.id, component_product_id=s.component_product_id,
        product_code=s.snapshot_component_product_code, product_name=s.snapshot_component_product_name,
        specification=s.snapshot_component_spec,
        unit=('片' if s.snapshot_component_product_name in ('隔板', '衬板')
              else (db.get(Product, s.component_product_id).unit or '件')),
        quantity_per_set=int(s.quantity_per_set),
        planned_delivery_quantity=max(int(quantity), 0)*int(s.quantity_per_set),
        is_graph_root=False, is_required=True, show_on_delivery=False,
        inventory_note=LEGACY_WARNING, relation_basis='legacy_advisory_not_frozen') for s in snapshots]


def physical_label_rows(customer_rows, components, *, order_item_id, delivery_item_id):
    """Components here must already be the physical picking projection, not BOM leaves."""
    result = list(customer_rows)
    present = {r.get('projected_product_id') for r in result}
    for c in components:
        pid = c.get('component_product_id')
        if c.get('is_graph_root') or pid in present or int(c.get('planned_delivery_quantity') or 0) <= 0:
            continue
        result.append(dict(line_type='component', order_item_id=order_item_id,
            component_snapshot_id=c['component_snapshot_id'], projected_product_id=pid,
            source_delivery_item_id=delivery_item_id, product_code=c['product_code'],
            product_name=c['product_name'], specification=c['specification'], unit=c['unit'],
            quantity=c['planned_delivery_quantity'], pricing_included=False,
            independent_return_receipt=False, independent_statement=False))
        present.add(pid)
    return result
