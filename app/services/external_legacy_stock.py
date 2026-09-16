"""Explicit, audited cutover of received-but-unstocked external goods.

Only the confirmed physical remainder enters inventory. Previously dispatched
quantities remain historical delivery facts, not fictitious stock movements.
"""
import hashlib
import json
from decimal import Decimal
from sqlalchemy import select
from app.models.order import Order, OrderItem
from app.models.delivery import Delivery, DeliveryItem
from app.models.product import Product
from app.models.external_packaging_purchase import (
    ExternalPackagingReceiptItem, ExternalPackagingPurchaseItem,
    ExternalPackagingPurchaseCancellation,
)
from app.models.warehouse_inventory import InventoryLot, InventoryMovement, FinishedGoodsInventoryDetail
from app.services.external_packaging_purchase import ExternalPurchaseContractError
from app.services.external_receipt_state import active_receipt_item


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)


def key(order_item_id):
    return f'external-legacy-stock:{order_item_id}'


def record(db, order_item_id):
    movement = db.scalar(select(InventoryMovement).where(InventoryMovement.idempotency_key == key(order_item_id)))
    if not movement:
        return None
    value = json.loads(movement.remarks)
    if (value.get('schema') != 'external_legacy_stock_v1' or value['order_item_id'] != order_item_id
            or movement.related_order_item_id != order_item_id or value['lot_id'] != movement.inventory_lot_id):
        raise ExternalPurchaseContractError('旧外购库存承接身份不完整')
    return value


def receipt_credit(db, order_item_id, receipt_item_id):
    value = record(db, order_item_id)
    return value['received_quantity'] if value and value['receipt_item_id'] == receipt_item_id else None


def delivered_offset(db, item, delivery_item, target_delivered):
    """Use only the post-cutover portion when allocating/reversing new deliveries."""
    if item.supply_mode_snapshot != 'external_purchase':
        return 0
    value = record(db, item.id)
    if not value:
        return 0
    if (delivery_item.delivery_id in value['historical_delivery_ids']
            or target_delivered < value['historical_delivered']):
        from app.services.warehouse_inventory import WarehouseInventoryError
        raise WarehouseInventoryError('该操作涉及入仓前已送的旧单；请保留旧单，现存余货另建送货单。旧单退货需单独核对实物，未扣减现存库存', 409)
    return value['historical_delivered']


def preview(db, receipt_item_id):
    from app.services.direct_external_finished import eligible
    receipt = db.scalar(select(ExternalPackagingReceiptItem).where(
        ExternalPackagingReceiptItem.id == receipt_item_id, active_receipt_item()))
    purchase = db.get(ExternalPackagingPurchaseItem, receipt.purchase_item_id) if receipt else None
    item = db.get(OrderItem, purchase.sales_order_item_id) if purchase else None
    order = db.get(Order, item.order_id) if item else None
    if (not order or order.status in ('cancelled','dead','voided') or not eligible(db,item)
            or receipt.converted_finished_quantity != 0
            or purchase.sales_order_id != order.id
            or db.scalar(select(ExternalPackagingPurchaseCancellation.id).where(
                ExternalPackagingPurchaseCancellation.purchase_order_id == purchase.purchase_order_id))):
        raise ExternalPurchaseContractError('不是可承接的有效旧外购收料')
    product = db.get(Product,item.product_id)
    if (not product or product.customer_id != order.customer_id or not product.is_active
            or product.deleted_at or product.purged_at or purchase.purchase_unit != product.unit
            or receipt.purchase_unit_snapshot != purchase.purchase_unit):
        raise ExternalPurchaseContractError('产品、客户或实收单位不一致')
    if (not item.external_packaging_order_quantity_basis_snapshot
            or item.external_packaging_order_quantity_basis_snapshot != item.external_packaging_purchase_quantity_basis_snapshot):
        raise ExternalPurchaseContractError('旧采购不是明确的1:1实物换算，请单独核对')
    receipts = list(db.scalars(select(ExternalPackagingReceiptItem).join(ExternalPackagingPurchaseItem,
        ExternalPackagingPurchaseItem.id == ExternalPackagingReceiptItem.purchase_item_id).where(
            ExternalPackagingPurchaseItem.sales_order_item_id == item.id, active_receipt_item())))
    if len(receipts) != 1 or receipts[0].id != receipt.id:
        raise ExternalPurchaseContractError('多次旧收料必须逐批核对，不能合并猜填')
    deliveries = list(db.execute(select(DeliveryItem.id,Delivery.id,DeliveryItem.delivered_quantity).join(
        Delivery, Delivery.id == DeliveryItem.delivery_id).where(DeliveryItem.order_item_id == item.id,
            DeliveryItem.is_current.is_(True),Delivery.status == 'dispatched').order_by(DeliveryItem.id)))
    delivered = sum(row[2] for row in deliveries)
    received = int(receipt.received_quantity)
    if (received != receipt.received_quantity or received > purchase.purchase_quantity
            or delivered != int(item.delivered_quantity or 0) or not 0 <= delivered < received):
        raise ExternalPurchaseContractError('旧实收、已送与余量不一致，请核对实物')
    value = dict(order_item_id=item.id,customer_id=order.customer_id,product_id=item.product_id,
                 receipt_item_id=receipt.id,purchase_item_id=purchase.id,received_quantity=received,
                 historical_delivered=delivered,remaining=received-delivered,
                 historical_deliveries=[list(row) for row in deliveries],
                 historical_delivery_ids=sorted({row[1] for row in deliveries}),
                 unit=product.unit,order_quantity=item.quantity,
                 purchase_snapshot={c.name:getattr(purchase,c.name) for c in purchase.__table__.columns},
                 order_snapshot={c.name:getattr(item,c.name) for c in item.__table__.columns})
    return value, hashlib.sha256(encode(value).encode()).hexdigest()


def reconcile(db, *, receipt_item_id, expected_hash, confirmed_quantity, location_id,
              layout_version, actor, reason):
    from app.services.bom_transactions import atomic_bom
    from app.services.warehouse_inventory import _movement, _balances
    from app.services.audit_log import append_audit_event
    from app.services.direct_external_finished import _post_quantity
    if actor.role not in ('admin','boss') or not actor.is_active:
        raise ExternalPurchaseContractError('仅管理员或老板可核对旧外购余货',status_code=403)
    if not reason.strip() or confirmed_quantity <= 0:
        raise ExternalPurchaseContractError('需要确认实物数量及依据')
    with atomic_bom(db):
        receipt=db.get(ExternalPackagingReceiptItem,receipt_item_id)
        if not receipt:
            raise ExternalPurchaseContractError('实收不存在')
        purchase=db.get(ExternalPackagingPurchaseItem,receipt.purchase_item_id)
        request=dict(receipt_item_id=receipt_item_id,expected_hash=expected_hash,quantity=confirmed_quantity,
                     location_id=location_id,layout_version=layout_version,reason=reason,actor_id=actor.id)
        old=record(db,purchase.sales_order_item_id)
        if old:
            if old['request'] != request:
                raise ExternalPurchaseContractError('该批旧外购余货已承接，不能重复入库或更换确认内容')
            return old
        value, fingerprint=preview(db,receipt_item_id)
        if fingerprint != expected_hash or confirmed_quantity != value['remaining']:
            raise ExternalPurchaseContractError('实收或已送数量已变化，请刷新核对')
        # Any existing stock identity needs reconciliation, not another manual-in.
        if db.scalar(select(InventoryLot.id).join(FinishedGoodsInventoryDetail,
                FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id).where(
                    FinishedGoodsInventoryDetail.product_id == value['product_id']).limit(1)):
            raise ExternalPurchaseContractError('该产品已有库存批次，请先核对，不能重复补入')
        lot=_post_quantity(db,purchase=purchase,receipt=receipt,customer_id=value['customer_id'],
                           operator_id=actor.id,quantity=confirmed_quantity,
                           location_id=location_id,layout_version=layout_version)
        value.update(schema='external_legacy_stock_v1',lot_id=lot.id,request=request)
        _movement(db,lot=lot,movement_type='adjust',quantity=0,before=_balances(lot),
                  operator_id=actor.id,reason='旧外购余货入仓承接（不补扣历史已送）',
                  related_order_item_id=value['order_item_id'],idempotency_key=key(value['order_item_id']),
                  remarks=encode(value))
        append_audit_event(db,event_category='business',result='success',source='web',
                          module_code='warehouse',action_code='external_stock.reconcile',resource='inventory',
                          actor=actor,entity_type='inventory_lot',entity_id=lot.id,details=value)
        db.flush()
        return value
