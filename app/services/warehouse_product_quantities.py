"""Read-only whole-product quantities, independent of search pagination/floor."""
import hashlib
import json
from collections import defaultdict

from sqlalchemy import select
from fastapi import HTTPException

from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.warehouse_inventory import InventoryLot, FinishedGoodsInventoryDetail, SemiFinishedInventoryDetail


def product_search_identity(lot):
    from app.services.warehouse_twin_dashboard import _lot_business_fields
    from app.services.warehouse_display_units import lot_display_unit
    fields = _lot_business_fields(lot)
    identity = [fields.get('customer_id'), fields.get('product_id'), lot.inventory_type, lot.unit, lot_display_unit(lot), fields.get('inventory_stage')]
    if not fields.get('product_id'):
        identity += [fields.get(k) for k in ('inventory_code', 'product_name', 'specification')]
    detail = lot.semi_finished_detail
    if detail:
        identity += [getattr(detail, name) for name in ('normalized_material_code', 'layer_count', 'flute_type',
            'board_length_mm', 'board_width_mm', 'component_type', 'pieces_per_box', 'stock_yield_per_sheet',
            'sheet_type', 'crease_type', 'crease_left_mm', 'crease_middle_mm', 'crease_right_mm')]
        identity.append(sorted(r.product_id for r in lot.allowed_products))
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def selected_product_quantities(db, *, seed_lot_id, visible_customer_ids):
    from app.api.warehouse import _lot_query, _visible_lot_condition
    from app.services.warehouse_twin_dashboard import build_inventory_code_search_results, _physical_quantity
    from app.services.location_candidates import load_warehouse_location_projection_contexts
    from app.core.time_contract import beijing_today
    query = _lot_query(require_formal_location=False).where(InventoryLot.status.in_(('active', 'frozen')))
    if visible_customer_ids is not None:
        query = query.where(_visible_lot_condition(visible_customer_ids))
    seed = db.scalar(query.where(InventoryLot.id == seed_lot_id))
    if seed is None:
        raise HTTPException(404, '该产品库存不存在或不在可查看范围内')
    identity = product_search_identity(seed)
    query = query.where(InventoryLot.inventory_type == seed.inventory_type, InventoryLot.unit == seed.unit)
    if seed.finished_detail:
        detail = seed.finished_detail
        query = query.where(InventoryLot.id.in_(select(FinishedGoodsInventoryDetail.inventory_lot_id).where(
            FinishedGoodsInventoryDetail.product_id == detail.product_id,
            FinishedGoodsInventoryDetail.owner_customer_id == detail.owner_customer_id)))
    elif seed.semi_finished_detail:
        detail = seed.semi_finished_detail
        query = query.where(InventoryLot.id.in_(select(SemiFinishedInventoryDetail.inventory_lot_id).where(
            SemiFinishedInventoryDetail.owner_customer_id == detail.owner_customer_id,
            SemiFinishedInventoryDetail.board_length_mm == detail.board_length_mm,
            SemiFinishedInventoryDetail.board_width_mm == detail.board_width_mm)))
    lots = [lot for lot in db.scalars(query.order_by(InventoryLot.id)).unique()
        if _physical_quantity(lot) > 0 and product_search_identity(lot) == identity]
    from app.services.warehouse_intake_age import build_intake_age_projection
    result = build_inventory_code_search_results(lots=lots, keyword='', as_of=beijing_today(),
        intake_projections=build_intake_age_projection(db, lots, visible_customer_ids=visible_customer_ids, as_of=beijing_today()),
        location_projection_contexts=load_warehouse_location_projection_contexts(db,
            [r.location for r in lots if r.location is not None]))
    product_ids = ({seed.finished_detail.product_id} if seed.finished_detail else
        {r.product_id for r in seed.allowed_products}) - {None}
    orders = select(OrderItem, Order, Product).join(Order, Order.id == OrderItem.order_id).join(
        Product, Product.id == OrderItem.product_id).where(OrderItem.product_id.in_(product_ids),
            Order.status.not_in(('cancelled', 'dead', 'closed', 'archived')),
            OrderItem.is_force_closed.is_(False), OrderItem.quantity > OrderItem.delivered_quantity)
    if visible_customer_ids is not None:
        orders = orders.where(Order.customer_id.in_(visible_customer_ids))
    order_rows = []
    totals = defaultdict(lambda: dict(ordered_quantity=0, delivered_quantity=0, remaining_quantity=0))
    for item, order, product in db.execute(orders.order_by(Order.id, OrderItem.id)):
        unit = item.sales_unit_snapshot or product.unit
        row = dict(order_id=order.id, order_item_id=item.id, order_number=order.order_number,
            customer_po=order.customer_po, product_id=product.id, unit=unit,
            ordered_quantity=item.quantity, delivered_quantity=item.delivered_quantity,
            remaining_quantity=item.quantity-item.delivered_quantity)
        order_rows.append(row)
        for name in ('ordered_quantity', 'delivered_quantity', 'remaining_quantity'):
            totals[unit][name] += row[name]
    return dict(**result, product_identity_key=identity, counts_scope='whole_product',
        total_quantity=sum(r['quantity'] for r in result['items']),
        reserved_quantity=sum(r['reserved_quantity'] for r in result['items']),
        orders=order_rows, order_totals=[dict(unit=unit, **values) for unit, values in totals.items()],
        order_scope='同产品有效未结订单；订单数量按销售单位，货位占用量按库存单位，不能相加')
