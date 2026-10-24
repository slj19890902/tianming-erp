"""Display physical purchase totals without merging their receipt ledgers."""
import hashlib
import json
from sqlalchemy import select, or_
from app.models.procurement_source import ProcurementSourceLink
from app.models.supplier_requisition_order import SupplierRequisitionOrderItem, SupplierRequisitionOrder
from app.models.requisition import RequisitionItem
from app.models.stock_replenishment import StockReplenishmentOrderItem
from app.models.product_bom import RequisitionItemBomSource, SalesOrderItemBomComponent
from app.models.order import OrderItem


def pending_groups(db, routes):
    req_ids = {r['requisition_item_id'] for r in routes if r.get('requisition_item_id')}
    stock_ids = {r['stock_replenishment_item_id'] for r in routes if r.get('stock_replenishment_item_id')}
    supplier_ids = {r['supplier_order_item_id'] for r in routes if r.get('supplier_order_item_id')}
    links = list(db.scalars(select(ProcurementSourceLink).where(or_(
            ProcurementSourceLink.supplier_item_id.in_(supplier_ids),
            ProcurementSourceLink.material_requisition_item_id.in_(req_ids),
            ProcurementSourceLink.stock_replenishment_item_id.in_(stock_ids))))) if req_ids or stock_ids or supplier_ids else []
    by_route = {(f'r{x.material_requisition_item_id}' if x.material_requisition_item_id
                 else f'sr{x.stock_replenishment_item_id}'): x.supplier_item_id for x in links if x.status == 'active'}
    typed = {x.supplier_item_id: json.loads(x.source_snapshot_json) for x in links}
    supplier_ids.update(by_route.values())
    suppliers = {x.id: x for x in db.scalars(select(SupplierRequisitionOrderItem).where(
        SupplierRequisitionOrderItem.id.in_(supplier_ids)))} if supplier_ids else {}
    order_ids = {x.supplier_order_id for x in suppliers.values()}
    headers = {x.id: x for x in db.scalars(select(SupplierRequisitionOrder).where(
        SupplierRequisitionOrder.id.in_(order_ids)))} if order_ids else {}
    source_ids = {x.order_item_id for x in suppliers.values() if x.order_item_id}
    orders = {x.id:x for x in db.scalars(select(OrderItem).where(OrderItem.id.in_(source_ids)))} if source_ids else {}
    reqs = {x.id: x for x in db.scalars(select(RequisitionItem).where(RequisitionItem.id.in_(req_ids)))} if req_ids else {}
    component_specs = {}
    if req_ids:
        for source, frozen in db.execute(select(RequisitionItemBomSource, SalesOrderItemBomComponent)
                .join(SalesOrderItemBomComponent, SalesOrderItemBomComponent.id == RequisitionItemBomSource.sales_order_item_bom_component_id)
                .where(RequisitionItemBomSource.requisition_item_id.in_(req_ids), RequisitionItemBomSource.active_guard == 1)):
            prefix = 'snapshot_component_base_crease_' if source.component_type == 'base' else 'snapshot_component_crease_'
            crease = tuple(getattr(frozen, prefix + field) for field in ('type', 'left_mm', 'middle_mm', 'right_mm'))
            if source.component_type == 'base' and not any(value is not None for value in crease):
                crease = tuple(getattr(frozen, 'snapshot_component_crease_' + field) for field in ('type', 'left_mm', 'middle_mm', 'right_mm'))
            spec = (frozen.snapshot_component_layer_count, frozen.snapshot_component_flute_type,
                    *crease)
            component_specs.setdefault(source.requisition_item_id, set()).add(spec)
    stocks = {x.id: x for x in db.scalars(select(StockReplenishmentOrderItem).where(
        StockReplenishmentOrderItem.id.in_(stock_ids)))} if stock_ids else {}
    groups = {}
    for route in routes:
        supplier = suppliers.get(route.get('supplier_order_item_id') or by_route.get(str(route['item_id'])))
        req = reqs.get(route.get('requisition_item_id'))
        stock = stocks.get(route.get('stock_replenishment_item_id'))
        # Only frozen purchasing facts identify a shared physical paper line.
        # Missing dimensions/identity stays a separate row, never guessed from
        # today's product master or a matching display name.
        if supplier:
            header = headers[supplier.supplier_order_id]
            if supplier.id in typed:
                crease = tuple(typed[supplier.id].get('crease_' + field) for field in ('type', 'left_mm', 'middle_mm', 'right_mm'))
            elif supplier.order_item_id in orders:
                from app.services.incoming_receipts import supplier_order_item_component_type
                prefix = 'snapshot_base_crease_' if supplier_order_item_component_type(supplier) == 'base' else 'snapshot_crease_'
                crease = tuple(getattr(orders[supplier.order_item_id], prefix + field) for field in ('type', 'left_mm', 'middle_mm', 'right_mm'))
            else:
                crease = (header.crease_type, header.crease_left_mm, header.crease_middle_mm, header.crease_right_mm)
            identity = ('supplier', supplier.supplier_order_id, supplier.material_id,
                supplier.material_code_snapshot, supplier.layer_count_snapshot, supplier.flute_type_snapshot,
                supplier.report_length_mm, supplier.report_width_mm, *crease,
                supplier.supplier_edge_type)
            valid = supplier.report_length_mm and supplier.report_width_mm and supplier.material_code_snapshot
        elif req:
            frozen_specs = component_specs.get(req.id)
            spec = next(iter(frozen_specs)) if frozen_specs and len(frozen_specs) == 1 else (None, *(route.get(k) for k in (
                    'flute_type', 'snapshot_crease_type', 'snapshot_crease_left_mm',
                    'snapshot_crease_middle_mm', 'snapshot_crease_right_mm')))
            identity = ('requisition', req.requisition_id, req.material_snapshot, req.cardboard_len,
                req.cardboard_width, *spec)
            valid = req.cardboard_len and req.cardboard_width and req.material_snapshot and (not frozen_specs or len(frozen_specs) == 1)
        elif stock:
            identity = ('stock', stock.replenishment_order_id, stock.material_id, stock.material_code_snapshot,
                stock.layer_count, stock.flute_type, stock.report_length_mm, stock.report_width_mm,
                stock.crease_type, stock.crease_left_mm, stock.crease_middle_mm, stock.crease_right_mm,
                stock.sheet_type)
            valid = stock.report_length_mm and stock.report_width_mm and stock.material_code_snapshot
        else:
            valid = False
        identity = (route.get('customer_id'), *identity) if valid else ('single', str(route['item_id']))
        key = hashlib.sha256(json.dumps(identity, ensure_ascii=False, default=str).encode()).hexdigest()
        groups.setdefault(key, []).append(dict(route, receipt_group_key=key))
    # Receipt submission has a deliberate 200-line upper bound. Oversized
    # physical groups remain inspectable in bounded consecutive portions.
    return [[dict(row, receipt_group_key=f"{key}:{i//200}") for row in members[i:i+200]]
            for key, members in groups.items() for i in range(0, len(members), 200)]


def reported_groups(db, candidates):
    routes = []
    for index, candidate in enumerate(candidates):
        line = candidate['physical_line']
        kind, item = candidate['source_type'], candidate['item_id']
        route = dict(item_id=f'{kind}:{item}', candidate_index=index, customer_id=line.get('customer_id'),
                     flute_type=line.get('flute_type'))
        for field in ('type', 'left_mm', 'middle_mm', 'right_mm'):
            route['snapshot_crease_' + field] = line.get('_crease_' + field)
        target = {'supplier_order':'supplier_order_item_id',
                  'composite_bom_requisition':'requisition_item_id',
                  'legacy_material_requisition':'requisition_item_id',
                  'stock_replenishment':'stock_replenishment_item_id'}.get(kind)
        if target:
            route[target] = item
        routes.append(route)
    result = []
    for group in pending_groups(db, routes):
        by_status = {}
        for route in group:
            candidate = candidates[route['candidate_index']]
            status = candidate['physical_line'].get('_item_status') or candidate['candidate_document'].get('status')
            key = f"{candidate['source_type']}:{candidate['document_id']}:{route['receipt_group_key']}:{status}"
            by_status.setdefault(key, []).append(dict(candidate, purchase_group_key=key))
        result.extend(by_status.values())
    return result
