"""Evidence-only historical sales repair. Caller owns the write transaction.

Never reads current customer price terms or Product master units/prices.
"""
import json
from datetime import date
from decimal import Decimal
from sqlalchemy import select, update
from app.models.delivery import Delivery, DeliveryItem
from app.models.order import OrderItem
from app.models.finance import Statement, StatementItem, ReturnReceiptItem
from app.models.warehouse_inventory import DeliveryInventoryAllocation, InventoryMovement, InventoryLot, UnorderedFinishedDeliveryAllocation
from app.models.audit import OperationLog
from app.services.material_cost_supplement import canonical, fingerprint
from app.services.delivery_snapshots import sales_contract, read_sales_contract


def _unit(db, item, order):
    if item.unit_snapshot:
        return item.unit_snapshot.strip(), {'kind':'delivery_unit_snapshot'}
    if order and order.sales_unit_snapshot:
        return order.sales_unit_snapshot.strip(), {'kind':'order_unit_snapshot','order_item_id':order.id}
    units, evidence, quantity = set(), [], 0
    if item.source_type == 'unordered_finished':
        allocations = db.scalars(select(UnorderedFinishedDeliveryAllocation).where(
            UnorderedFinishedDeliveryAllocation.delivery_item_id==item.id,
            UnorderedFinishedDeliveryAllocation.consumed_quantity>0)).all()
    else:
        allocations = db.scalars(select(DeliveryInventoryAllocation).where(
            DeliveryInventoryAllocation.delivery_item_id==item.id)).all()
    for allocation in allocations:
        movement=db.get(InventoryMovement,allocation.consume_movement_id)
        lot=db.get(InventoryLot,movement.inventory_lot_id) if movement else None
        product_id=item.product_id or (order.product_id if order else None)
        if not lot or not lot.finished_detail or lot.finished_detail.product_id!=product_id or movement.movement_type!='consume':
            return None, {'reason':'出库批次产品身份不完整'}
        consumed = allocation.consumed_quantity if item.source_type=='unordered_finished' else allocation.consumed_stock_quantity
        credited = consumed if item.source_type=='unordered_finished' else allocation.credited_requirement_quantity
        if consumed!=credited or movement.quantity!=consumed or not str(movement.unit or '').strip():
            return None, {'reason':'出库单位涉及数量换算，需原销售单位依据'}
        units.add(movement.unit.strip());quantity+=credited
        evidence.append({'allocation_id':allocation.id,'movement_id':movement.id,'lot_id':lot.id,'quantity':credited,'unit':movement.unit})
    if len(units)==1 and quantity==item.delivered_quantity:
        return next(iter(units)), {'kind':'exact_finished_dispatch_unit','sources':evidence}
    if order:
        from app.services.customer_delivery_margin import _frozen_bom_root_unit
        unit=_frozen_bom_root_unit(db,delivery_item_id=item.id,order_item=order)
        if unit:return unit, {'kind':'frozen_order_bom_unit','order_item_id':order.id}
    return None, {'reason':'缺少与本次送货数量一致的冻结销售单位'}


def preview(db, months, *, confirmed_tax_inclusive=False, approved_units=None):
    months=sorted(set(months))
    if not months or any(m not in ('2026-08','2026-09') for m in months):
        raise ValueError('历史销售修复仅限2026年8月、9月')
    proposals, missing = [], []
    rows=db.execute(select(DeliveryItem,Delivery).join(Delivery,Delivery.id==DeliveryItem.delivery_id).where(
        Delivery.status=='dispatched',DeliveryItem.is_current.is_(True),
        Delivery.delivery_date>=date(2026,8,1),Delivery.delivery_date<date(2026,10,1)).order_by(DeliveryItem.id)).all()
    for item, delivery in rows:
        if delivery.delivery_date.strftime('%Y-%m') not in months:continue
        if item.sales_contract_json and item.unit_snapshot:continue
        order=db.get(OrderItem,item.order_item_id) if item.order_item_id else None
        unit, unit_evidence=_unit(db,item,order)
        approved_unit=(approved_units or {}).get(item.id)
        if approved_unit:
            if unit and unit != approved_unit:
                raise ValueError('人工确认单位与既有冻结来源冲突，须专项更正')
            if not unit:
                unit=approved_unit
                unit_evidence={'kind':'owner_confirmed_historical_unit','delivery_item_id':item.id,
                    'unit':unit,'confirmation':'老板2026-09-22逐类确认旧单销售单位；只补缺失，不覆盖已有单位'}
        existing_contract = read_sales_contract(item) if item.sales_contract_json else None
        statements=db.execute(select(StatementItem,Statement).join(Statement,Statement.id==StatementItem.statement_id)
            .join(ReturnReceiptItem,ReturnReceiptItem.id==StatementItem.return_receipt_item_id)
            .where(ReturnReceiptItem.delivery_item_id==item.id,Statement.confirmation_status=='confirmed',
                   Statement.customer_id==delivery.customer_id)).all()
        contract=None
        if len(statements)==1 and unit:
            row, statement=statements[0]
            try:
                contract=sales_contract(unit=unit,price=row.unit_price_snapshot,tax_mode=row.price_tax_mode_snapshot,
                    tax_rate=row.tax_rate_snapshot,source={'kind':'confirmed_statement_history',
                    'statement_id':statement.id,'statement_item_id':row.id,'unit_evidence':unit_evidence})
            except ValueError:pass
        if not item.sales_contract_json and confirmed_tax_inclusive:
            # User confirmed this precise historical period. Preserve recorded
            # price and quantity; no current master-price lookup or recalculation.
            price = order.unit_price if order else item.unit_price_snapshot
            mode = order.price_tax_mode_snapshot if order else None
            if unit and price is not None and mode in (None,'tax_inclusive'):
                contract = sales_contract(unit=unit,price=price,tax_mode='tax_inclusive',tax_rate=None,
                    source={'kind':'historical_confirmed_tax_terms','confirmation':'老板2026-09-22确认2026年8月及9月旧单全部为含税成交价',
                            'order_item_id':item.order_item_id,'delivery_item_id':item.id,'unit_evidence':unit_evidence})
        changes={}
        if unit and not item.unit_snapshot:changes['unit_snapshot']=unit
        if contract and not item.sales_contract_json:changes['sales_contract_json']=contract
        identity={'delivery_item_id':item.id,'delivery_id':delivery.id,'delivery_version':delivery.version,
                  'order_item_id':item.order_item_id,'product_id':item.product_id,'quantity':item.delivered_quantity,
                  'before_unit':item.unit_snapshot,'before_contract':item.sales_contract_json}
        if changes:proposals.append({'identity':identity,'changes':changes,'unit_evidence':unit_evidence})
        if not unit or (not contract and not existing_contract and (not order or not order.price_tax_mode_snapshot)):
            missing.append({'delivery_item_id':item.id,'delivery_number':delivery.delivery_number,
                'product_code':item.product_code_snapshot or (order.snapshot_product_code if order else None),
                'reason':'；'.join(filter(None,[None if unit else unit_evidence.get('reason'),
                    '缺少已确认的历史销售税口径依据' if not contract and not existing_contract and (not order or not order.price_tax_mode_snapshot) else None]))})
    result={'months':months,'proposals':proposals,'missing':missing,'confirmed_tax_inclusive':confirmed_tax_inclusive,
            'approved_units':{str(k):v for k,v in sorted((approved_units or {}).items())}}
    return dict(result,preview_fingerprint=fingerprint(result))


def adopt(db, *, months, actor, expected_preview, batch_id, reason, confirmed_tax_inclusive=False, approved_units=None):
    if not actor.is_active or actor.role not in ('admin','boss'):
        raise PermissionError('仅管理员或老板可确认历史销售口径')
    if not batch_id.strip() or not reason.strip():raise ValueError('缺少操作批次或依据')
    old=db.scalar(select(OperationLog).where(OperationLog.resource=='historical_sales_contract',OperationLog.batch_id==batch_id))
    if old:
        result=json.loads(old.details)
        if result['preview_fingerprint']!=expected_preview or result['months']!=sorted(set(months)) or result['reason']!=reason or result.get('confirmed_tax_inclusive',False)!=confirmed_tax_inclusive or result.get('approved_units_fingerprint',fingerprint({}))!=fingerprint({str(k):v for k,v in sorted((approved_units or {}).items())}):
            raise ValueError('重复操作批次内容不一致')
        return dict(result,replayed=True)
    plan=preview(db,months,confirmed_tax_inclusive=confirmed_tax_inclusive,approved_units=approved_units)
    if plan['preview_fingerprint']!=expected_preview:raise ValueError('历史依据或送货数据已变化，请重新预览')
    from app.services.audit_log import append_audit_event
    changed_deliveries=set()
    for row in plan['proposals']:
        item=db.get(DeliveryItem,row['identity']['delivery_item_id'])
        if item.delivery_id not in changed_deliveries:
            expected=row['identity']['delivery_version']
            changed=db.execute(update(Delivery).where(Delivery.id==item.delivery_id,Delivery.version==expected).values(version=expected+1))
            if changed.rowcount != 1:
                raise ValueError('送货版本已变化，请重新预览')
            changed_deliveries.add(item.delivery_id)
        for name,value in row['changes'].items():setattr(item,name,value)
        append_audit_event(db,actor=actor,event_category='business',result='success',source='script',
            module_code='finance.cost',action_code='historical_sales_contract_line',resource='historical_sales_contract_line',
            batch_id=batch_id,entity_type='delivery_item',entity_id=item.id,description=reason,details=row)
    result=dict(plan,batch_id=batch_id,reason=reason,replayed=False)
    append_audit_event(db,actor=actor,event_category='business',result='success',source='script',
        module_code='finance.cost',action_code='historical_sales_contract',resource='historical_sales_contract',
        batch_id=batch_id,description=reason,details=dict(preview_fingerprint=expected_preview,months=plan['months'],
            reason=reason,batch_id=batch_id,count=len(plan['proposals']),missing_count=len(plan['missing']),replayed=False,
            confirmed_tax_inclusive=confirmed_tax_inclusive,approved_units_fingerprint=fingerprint(plan['approved_units'])))
    db.flush()
    return result
