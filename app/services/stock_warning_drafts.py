"""Physical replenishment demand on the existing stock-order ledger.

The caller owns authorization and the transaction. Creating a draft never
creates a purchase, sales order, reservation, or inventory movement.
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import select, update

from app.models.stock_replenishment import (
    InventoryStockPolicy, StockReplenishmentOrder, StockReplenishmentOrderItem,
)
from app.services.delivery_quantities import product_basis
from app.services.stock_replenishment import StockReplenishmentError


def physical_demand_contract(item):
    if item.quantity_contract_json is None:
        return None
    try:
        value = json.loads(item.quantity_contract_json)
        if not (isinstance(value, dict) and value['schema'] == 1
                and value['quantity_basis'] == 'physical'
                and value['product_id'] == item.product_id
                and value['customer_id'] == item.customer_id
                and type(value['physical_quantity']) is int
                and value['physical_quantity'] == item.quantity and item.quantity > 0
                and type(value['physical_basis']) is int and value['physical_basis'] > 0
                and type(value['customer_basis']) is int and value['customer_basis'] > 0
                and isinstance(value['physical_unit'], str) and value['physical_unit'].strip()
                and isinstance(value['customer_unit'], str) and value['customer_unit'].strip()):
            raise ValueError('invalid physical demand')
    except (ValueError, TypeError, KeyError) as error:
        raise StockReplenishmentError('补库实物需求快照无效，请重新核对单据', 409) from error
    return value


def pending_physical_warning_quantity(db, *, customer_id, product_id):
    items = db.scalars(select(StockReplenishmentOrderItem)
        .join(StockReplenishmentOrder,
              StockReplenishmentOrder.id == StockReplenishmentOrderItem.replenishment_order_id)
        .where(StockReplenishmentOrder.status == 'draft',
               StockReplenishmentOrder.customer_id == customer_id,
               StockReplenishmentOrderItem.product_id == product_id,
               StockReplenishmentOrderItem.quantity_contract_json.is_not(None)))
    return sum(physical_demand_contract(item)['physical_quantity'] for item in items)


def warning_origin_number(delivery_id, policy_id):
    return 'SW-' + hashlib.sha256(f'delivery-warning:{delivery_id}:{policy_id}'.encode()).hexdigest()[:32]


def paperboard_warning_plan(db, *, policy_id, user):
    """Use the same component, cutting and specification rules as manual reporting."""
    from app.api.requisition import stock_policy_replenishment_draft, StockReplenishmentItemPayload
    draft = stock_policy_replenishment_draft(policy_id, db, user)
    if draft.get('procurement_mode') == 'external_purchase':
        raise StockReplenishmentError('外购需求必须按实物快照生成', 409)
    if not draft.get('draft_ready'):
        raise StockReplenishmentError('报料资料不完整：' + '、'.join(draft.get('missing_fields') or []), 409)
    items = [StockReplenishmentItemPayload(**item) for item in draft['items'] if int(item.get('quantity') or 0) > 0]
    frozen = {'customer_id': draft['customer_id'], 'supplier_name': draft['supplier_name'],
              'items': [item.model_dump(mode='json') for item in items]}
    encoded = json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return frozen, hashlib.sha256(encoded.encode()).hexdigest()


def create_paperboard_warning_draft(db, *, policy, delivery, plan, plan_hash, operator_id):
    from app.api.requisition import _build_replenishment_item, StockReplenishmentItemPayload
    if not plan['items'] or plan['customer_id'] != delivery.customer_id or policy.customer_id != delivery.customer_id:
        raise StockReplenishmentError('报料需求或客户已变化', 409)
    order = StockReplenishmentOrder(order_number=warning_origin_number(delivery.id, policy.id),
        request_hash=plan_hash, customer_id=policy.customer_id, supplier_name=plan['supplier_name'],
        source_type='stock_warning', status='draft', created_by=operator_id,
        items=[_build_replenishment_item(db, StockReplenishmentItemPayload(**item), source_type='stock_warning')
               for item in plan['items']])
    db.add(order)
    db.flush()
    return order, True


def create_external_warning_draft(db, *, policy, delivery, physical_quantity, operator_id):
    """Replay by originating dispatch and policy; never reinterpret a saved draft."""
    if type(physical_quantity) is not int or not 0 < physical_quantity <= 2147483647:
        raise StockReplenishmentError('补库实物数量必须为正整数', 422)
    if delivery.status != 'dispatched' or delivery.is_historical_backfill:
        raise StockReplenishmentError('只有实际发货的送货单可以生成补库需求', 409)
    if policy.customer_id != delivery.customer_id:
        raise StockReplenishmentError('补库客户与送货客户不一致', 403)
    # Serialize concurrent confirmations for a policy, including distinct deliveries.
    locked = db.execute(update(InventoryStockPolicy).where(
        InventoryStockPolicy.id == policy.id,
        InventoryStockPolicy.active.is_(True),
    ).values(active=True)).rowcount
    if locked != 1:
        raise StockReplenishmentError('库存预警策略已停用，请刷新', 409)
    origin = f'delivery-warning:{delivery.id}:{policy.id}'
    number = warning_origin_number(delivery.id, policy.id)
    existing = db.scalar(select(StockReplenishmentOrder).where(
        StockReplenishmentOrder.order_number == number))
    if existing is not None:
        if existing.customer_id != policy.customer_id or len(existing.items) != 1:
            raise StockReplenishmentError('补库来源记录不一致', 409)
        contract = physical_demand_contract(existing.items[0])
        if not contract or contract.get('origin') != origin:
            raise StockReplenishmentError('补库来源快照不一致', 409)
        return existing, False
    product = policy.product
    if (policy.target_inventory_type != 'finished' or product is None
            or product.customer_id != policy.customer_id
            or not product.is_active or product.deleted_at is not None
            or product.supply_mode != 'external_purchase' or product.is_composite
            or product.external_packaging_category_code == 'coated_board'):
        raise StockReplenishmentError('该预警不是可直接外购的成品补库需求', 409)
    contract = dict(product_basis(product), quantity_basis='physical',
        physical_quantity=physical_quantity, origin=origin,
        delivery_id=delivery.id, stock_policy_id=policy.id)
    encoded = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    order = StockReplenishmentOrder(order_number=number,
        request_hash=hashlib.sha256(encoded.encode()).hexdigest(),
        customer_id=policy.customer_id, source_type='stock_warning', status='draft',
        created_by=operator_id,
        items=[StockReplenishmentOrderItem(
            stock_policy_id=policy.id, target_inventory_type='finished',
            procurement_route_snapshot='external_packaging', product_id=product.id,
            reference_product_id=product.id, customer_id=product.customer_id,
            product_code_snapshot=product.product_code, product_name_snapshot=product.product_name,
            quantity=physical_quantity, stocked_quantity=0,
            quantity_contract_json=encoded, sheet_type='raw_board', component_type='whole',
            pieces_per_box=1, stock_yield_per_sheet=1,
        )])
    db.add(order)
    db.flush()
    return order, True


def prepare_warning_purchase(db, *, order):
    from app.services.external_packaging_stock_replenishment import prepare_external_stock_purchase
    if order.status != 'draft' or len(order.items) != 1:
        raise StockReplenishmentError('补库草稿状态已变化，请刷新', 409)
    item = order.items[0]
    contract = physical_demand_contract(item)
    if contract is None or item.procurement_route_snapshot != 'external_packaging':
        raise StockReplenishmentError('该单据不是外购实物需求草稿', 409)
    if (order.customer_id != item.customer_id or order.request_hash != hashlib.sha256(
            item.quantity_contract_json.encode()).hexdigest()):
        raise StockReplenishmentError('补库草稿快照已变化，请重新核对', 409)
    product = item.product
    if (product is None or not product.is_active or product.deleted_at is not None
            or product.supply_mode != 'external_purchase' or product.is_composite
            or product.external_packaging_category_code == 'coated_board'):
        raise StockReplenishmentError('补库产品已停用或不再适用直接外购', 409)
    prepared = prepare_external_stock_purchase(db, product=product,
        finished_quantity=item.quantity, purchase_quantity_override=item.quantity,
        physical_contract=contract)
    frozen = {key: value for key, value in prepared.items()
              if key not in ('product', 'external_product', 'supplier', 'price')}
    frozen.update(contract=contract, supplier_id=prepared['supplier'].id,
        supplier_name=prepared['supplier'].display_name or prepared['supplier'].standard_name,
        supplier_version=prepared['supplier'].version,
        external_product_id=prepared['external_product'].id,
        external_product_version=prepared['external_product'].version,
        price={column.name: getattr(prepared['price'], column.name)
               for column in prepared['price'].__table__.columns})
    quote_hash = hashlib.sha256(json.dumps(frozen, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), default=str).encode()).hexdigest()
    return prepared, quote_hash


def warning_purchase_preview(db, *, order):
    prepared, quote_hash = prepare_warning_purchase(db, order=order)
    return dict(order_id=order.id, order_number=order.order_number,
        expected_request_hash=order.request_hash, expected_quote_hash=quote_hash,
        supplier_name=prepared['supplier'].display_name or prepared['supplier'].standard_name,
        product_name=order.items[0].product_name_snapshot,
        quantity=str(prepared['purchase_quantity']), unit=prepared['purchase_unit'],
        unit_price=str(prepared['unit_price']), currency=prepared['price'].currency,
        tax_mode=prepared['price'].tax_mode, tax_rate=str(prepared['price'].tax_rate),
        total_amount=str(prepared['total_amount']), tax_amount=str(prepared['tax_amount']),
        specification=prepared['specification_summary'])


def confirm_external_warning_draft(db, *, order, operator, expected_quote_hash=None):
    """Purchase the reviewed physical demand and price once."""
    from app.services.external_packaging_stock_replenishment import (
        external_purchase_batch_for_replenishment, _post_external_stock_purchase,
    )
    from app.core.time_contract import utc_now_naive
    if order.status == 'voided':
        raise StockReplenishmentError('已作废的补库草稿不能采购', 409)
    existing = external_purchase_batch_for_replenishment(db, order.id)
    if existing is not None:
        if expected_quote_hash is not None and existing.request_fingerprint != expected_quote_hash:
            raise StockReplenishmentError('已确认采购与本次价格预览不一致，请查看原采购单', 409)
        return order, False
    prepared, quote_hash = prepare_warning_purchase(db, order=order)
    if expected_quote_hash != quote_hash:
        raise StockReplenishmentError('供应商或采购价格已变化，请重新预览后确认', 409)
    item = order.items[0]
    changed = db.execute(update(StockReplenishmentOrder).where(
        StockReplenishmentOrder.id == order.id, StockReplenishmentOrder.status == 'draft',
        StockReplenishmentOrder.request_hash == order.request_hash,
    ).values(status='confirmed', confirmed_by=operator.id, confirmed_at=utc_now_naive())).rowcount
    if changed != 1:
        raise StockReplenishmentError('补库草稿已由其他操作处理，请刷新', 409)
    order.supplier_name = prepared['supplier'].display_name or prepared['supplier'].standard_name
    _post_external_stock_purchase(db, order=order, item=item, prepared=prepared,
        idempotency_key=f'physical-warning-draft:{order.id}', fingerprint=quote_hash, user=operator)
    return order, True
