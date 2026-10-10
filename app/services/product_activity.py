"""Product-scoped purchase, receipt, production and delivery facts; no stock writes."""
from collections import defaultdict
from sqlalchemy import select, or_
from sqlalchemy.orm import joinedload
from app.models.stock_replenishment import StockReplenishmentOrderItem as StockItem, StockReplenishmentOrder as StockOrder
from app.models.incoming_receipt import IncomingReceiptItem as Receipt
from app.models.stock_preparation import StockPreparationJob as Job
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.product_bom import ProductBomComponent
from app.models.warehouse_inventory import InventoryLot, SemiFinishedInventoryDetail
from app.services.product_unit_labels import product_unit_label


def source_lots(db, product):
    """An original receipt FK establishes provenance, not interchangeable usage."""
    stock = select(Receipt.received_inventory_lot_id).join(StockItem,
        StockItem.id == Receipt.stock_replenishment_item_id).join(StockOrder).where(
        or_(StockItem.reference_product_id == product.id, StockItem.product_id == product.id),
        StockItem.customer_id == product.customer_id, StockOrder.status != 'voided', Receipt.status == 'posted')
    orders = select(Receipt.received_inventory_lot_id).join(OrderItem,
        OrderItem.id == Receipt.order_item_id).join(Order).where(
        OrderItem.product_id == product.id, Order.customer_id == product.customer_id,
        Receipt.status == 'posted')
    legacy = select(StockItem.inventory_lot_id).join(StockOrder).where(
        or_(StockItem.reference_product_id == product.id, StockItem.product_id == product.id),
        StockItem.customer_id == product.customer_id, StockOrder.status.notin_(('draft', 'voided')))
    ids = set(db.scalars(stock)) | set(db.scalars(orders)) | set(db.scalars(legacy))
    if not ids - {None}:
        return []
    from app.api.mobile_erp import _lot_load_options
    return list(db.scalars(select(InventoryLot).join(SemiFinishedInventoryDetail).options(*_lot_load_options()).where(
        InventoryLot.id.in_(ids - {None}), InventoryLot.status == 'active',
        InventoryLot.inventory_type == 'semi_finished', SemiFinishedInventoryDetail.owner_customer_id == product.customer_id,
        or_(InventoryLot.quantity_available > 0, InventoryLot.quantity_reserved > 0))))


def related_products(db, product, scope):
    edges = db.scalars(select(ProductBomComponent).where(or_(
        ProductBomComponent.parent_product_id == product.id, ProductBomComponent.component_product_id == product.id)))
    result = []
    for edge in edges:
        parent = edge.component_product_id == product.id
        p = db.get(Product, edge.parent_product_id if parent else edge.component_product_id)
        if not p or p.deleted_at or p.customer_id != product.customer_id or (scope is not None and p.customer_id not in scope):
            continue
        result.append(dict(product_id=p.id, code=p.product_code, name=p.product_name,
            relation='组合产品' if parent else '组成零件', quantity=float(edge.quantity_per_set), unit=product_unit_label(p)))
    from app.models.shared_finished_stock import SharedFinishedMember, SharedFinishedGroup
    member = db.get(SharedFinishedMember, product.id)
    if member and db.get(SharedFinishedGroup, member.group_id).enabled:
        for other in db.scalars(select(SharedFinishedMember).where(SharedFinishedMember.group_id == member.group_id,
                SharedFinishedMember.product_id != product.id)):
            p = db.get(Product, other.product_id)
            if not p or p.deleted_at or (scope is not None and p.customer_id not in scope):
                continue
            result.append(dict(product_id=p.id, code=p.product_code, name=p.product_name,
                relation='已登记共用产品 · 查看各自单据', quantity=None, unit=product_unit_label(p)))
    return result


def activity(db, product, *, permissions, scope, page=1, page_size=30):
    """Each section is independently bounded; next page never hides old deliveries."""
    from app.core.time_contract import utc_naive_to_api
    from app.services.stock_preparation import location_name
    def at(value):
        return utc_naive_to_api(value) if value else None
    result = dict(items=[], deliveries=[], related_products=related_products(db, product, scope),
                  page=page, page_size=page_size, has_more=False, hidden_sections=[])
    items = result['items']; offset = (page - 1) * page_size
    def paged(stmt):
        rows = list(db.execute(stmt.offset(offset).limit(page_size + 1)))
        result['has_more'] |= len(rows) > page_size
        return rows[:page_size]
    def position(lot):
        return location_name(db, lot) if lot and permissions['warehouse'] else '位置未登记' if permissions['warehouse'] else '无库存位置权限'
    def add(kind, identity, status, number, quantity, unit, **more):
        items.append(dict(key=f'{kind}:{identity}', source=kind, status=status, document=number,
                          quantity=quantity, unit=unit, **more))
    if permissions['requisition'] or permissions['incoming'] or permissions['production']:
        rows = paged(select(StockItem).join(StockOrder).options(joinedload(StockItem.order)).where(
            StockItem.customer_id == product.customer_id,
            or_(StockItem.reference_product_id == product.id, StockItem.product_id == product.id)).order_by(StockItem.id.desc()))
        stocks = [r[0] for r in rows]
        receipts = defaultdict(list); jobs = defaultdict(list)
        received = list(db.scalars(select(Receipt).where(Receipt.stock_replenishment_item_id.in_([s.id for s in stocks]))))
        for r in received: receipts[r.stock_replenishment_item_id].append(r)
        for j in db.scalars(select(Job).where(Job.receipt_item_id.in_([r.id for r in received]))): jobs[j.receipt_item_id].append(j)
        for s in stocks:
            actual = sum(r.received_quantity for r in receipts[s.id] if r.status == 'posted') if receipts[s.id] else s.stocked_quantity
            closed = any(r.status == 'posted' and r.resolution_action == 'accept_short' and r.resolution_status == 'resolved' for r in receipts[s.id])
            waiting = 0 if closed else max(s.quantity - actual, 0)
            if permissions['requisition'] and (s.order.status in ('draft', 'voided') or waiting or (not receipts[s.id] and not s.inventory_lot_id)):
                state = '报料草稿' if s.order.status == 'draft' else '已作废' if s.order.status == 'voided' else '待收料'
                add('库存补库',s.id,state,s.order.order_number,waiting if receipts[s.id] else s.quantity,'张',
                    location='尚未到厂' if state == '待收料' else '—', at=at(s.created_at),
                    frozen_spec=f'{s.report_length_mm}×{s.report_width_mm} mm / {s.material_code_snapshot} / {s.flute_type}楞')
            if not (permissions['incoming'] or permissions['production']): continue
            if not receipts[s.id] and s.inventory_lot_id:
                lot = db.get(InventoryLot, s.inventory_lot_id)
                physical = lot.quantity_available + lot.quantity_reserved if lot and lot.status == 'active' else 0
                add('历史补库',s.id,'原料待生产' if lot and lot.inventory_type == 'semi_finished' and physical else '历史入库',
                    s.order.order_number,s.stocked_quantity,'张' if lot and lot.inventory_type == 'semi_finished' else product_unit_label(product),
                    at=at(s.stocked_at),location=position(lot),
                    physical_quantity=physical if permissions['warehouse'] and lot and lot.inventory_type == 'semi_finished' else None,
                    theoretical_output=physical*s.stock_yield_per_sheet//s.pieces_per_box if permissions['warehouse'] and lot and lot.inventory_type == 'semi_finished' else None,
                    factor=s.stock_yield_per_sheet,pieces_per_box=s.pieces_per_box,output_unit=product_unit_label(product))
            for r in receipts[s.id]:
                lot = db.get(InventoryLot, r.received_inventory_lot_id) if r.received_inventory_lot_id else None
                pending = [j for j in jobs[r.id] if j.status == 'pending']
                physical = (lot.quantity_available + lot.quantity_reserved) if lot and lot.status == 'active' else 0
                state = '收料已撤销' if r.status != 'posted' else '来源已作废' if s.order.status == 'voided' else '待生产' if pending or physical else '材料已用完'
                add('补库收料',r.id,state,s.order.order_number,r.received_quantity,'张',at=at(r.created_at),
                    location=position(lot), physical_quantity=physical if permissions['warehouse'] else None,
                    theoretical_output=(physical*s.stock_yield_per_sheet//s.pieces_per_box) if permissions['warehouse'] and r.status == 'posted' else None,
                    output_unit=product_unit_label(product), factor=s.stock_yield_per_sheet, pieces_per_box=s.pieces_per_box,
                    frozen_spec=f'{s.report_length_mm}×{s.report_width_mm} mm / {s.material_code_snapshot} / {s.flute_type}楞',
                    receipt_id=r.id, lot_id=lot.id if lot and permissions['warehouse'] else None)
                if permissions['production']:
                    for j in jobs[r.id]:
                        if j.status != 'completed': continue
                        output = db.get(InventoryLot, j.output_lot_id)
                        add('补库完工',j.id,'已完工入库',s.order.order_number,j.actual_output,product_unit_label(product),
                            at=at(j.created_at),location=position(output), lot_id=j.output_lot_id if permissions['warehouse'] else None)
    else: result['hidden_sections'].append('报料、收料与生产')
    if permissions['orders']:
        from app.models.requisition import Requisition, RequisitionItem
        from app.models.production import ProductionTask
        from app.models.supplier_requisition_order import SupplierRequisitionOrder, SupplierRequisitionOrderItem
        rows = paged(select(OrderItem,Order).join(Order).where(Order.customer_id == product.customer_id,
            OrderItem.product_id == product.id).order_by(OrderItem.id.desc()))
        ids = [r[0].id for r in rows]
        if permissions['requisition']:
            for ri,rq in db.execute(select(RequisitionItem,Requisition).join(Requisition).where(RequisitionItem.order_item_id.in_(ids))):
                add('订单报料',ri.id,str(ri.status),rq.requisition_number,ri.requisition_qty,'张',at=at(ri.created_at),location='报料单')
            for si,so in db.execute(select(SupplierRequisitionOrderItem,SupplierRequisitionOrder).join(SupplierRequisitionOrder).where(SupplierRequisitionOrderItem.order_item_id.in_(ids))):
                add('订单采购',si.id,'已作废' if si.status == 'voided' or so.status == 'voided' else '已采购',so.order_number,si.quantity,'张',
                    at=at(si.created_at),location='采购单',frozen_spec=f'{si.report_length_mm}×{si.report_width_mm} mm')
        if permissions['incoming']:
            for r in db.scalars(select(Receipt).where(Receipt.order_item_id.in_(ids))):
                lot = db.get(InventoryLot,r.received_inventory_lot_id) if r.received_inventory_lot_id else None
                add('订单收料',r.id,'已收料' if r.status == 'posted' else '收料已撤销',f'收料#{r.receipt_id}',r.received_quantity,'张',at=at(r.created_at),location=position(lot))
        if permissions['production']:
            for t in db.scalars(select(ProductionTask).where(ProductionTask.order_item_id.in_(ids))):
                add('订单生产',t.id,{'waiting_material':'待收料','pending':'待生产','completed':'已完工','not_required':'无需生产'}.get(t.status,t.status),
                    f'生产任务#{t.id}',t.planned_quantity,product_unit_label(product),at=at(t.created_at),location='见收料及成品库位')
    else: result['hidden_sections'].append('订单')
    if permissions['deliveries']:
        from app.models.delivery import Delivery, DeliveryItem
        rows = paged(select(DeliveryItem,Delivery).join(Delivery).outerjoin(OrderItem,OrderItem.id == DeliveryItem.order_item_id).where(
            Delivery.customer_id == product.customer_id, DeliveryItem.is_current.is_(True),
            or_(DeliveryItem.product_id == product.id, OrderItem.product_id == product.id)).order_by(Delivery.delivery_date.desc(),DeliveryItem.id.desc()))
        for d,head in rows:
            result['deliveries'].append(dict(id=d.id,document=head.delivery_number,date=str(head.delivery_date),
                status={'pending':'待送货','dispatched':'已送货','voided':'已作废'}.get(head.status,head.status),
                quantity=d.delivered_quantity,unit=d.unit_snapshot,code=d.product_code_snapshot,name=d.product_name_snapshot,
                specification=d.specification_snapshot,customer_po=d.customer_po_snapshot))
    else: result['hidden_sections'].append('送货记录')
    return result
