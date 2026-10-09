"""Customer-scoped shipment and lead-time evidence; never changes a stock policy."""
from collections import defaultdict
from datetime import timedelta
from statistics import median

from sqlalchemy import func, select

from app.core.time_contract import utc_naive_to_beijing_date
from app.models.delivery import Delivery, DeliveryItem
from app.models.finance import ReturnReceipt, ReturnReceiptItem
from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
from app.models.order import Order, OrderItem
from app.models.production import ProductionCompletion
from app.models.requisition import Requisition, RequisitionItem
from app.services.delivery_quantities import for_item, QuantityContractError


def build_stock_advice(db, *, warning, today):
    cid, pid = warning['customer_id'], warning['product_id']
    unit = '套' if warning.get('is_virtual_composite_parent') else warning.get('unit_label')
    start = today - timedelta(days=179)
    lines = db.execute(select(Delivery, DeliveryItem, OrderItem)
        .join(DeliveryItem, DeliveryItem.delivery_id == Delivery.id)
        .outerjoin(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
        .where(Delivery.customer_id == cid, Delivery.status == 'dispatched',
            DeliveryItem.is_current.is_(True), Delivery.delivery_date.between(start, today),
            func.coalesce(DeliveryItem.product_id, OrderItem.product_id) == pid)).all()
    documents, issues = {}, []
    for delivery, line, order_item in lines:
        sample = documents.setdefault(delivery.id, dict(delivery_number=delivery.delivery_number,
            date=delivery.delivery_date.isoformat(), quantity=0))
        try:
            contract = for_item(line)
            if contract:
                if contract['customer_id'] != cid or contract['product_id'] != pid or contract['physical_unit'] != unit:
                    raise QuantityContractError('单位不一致')
                quantity = contract['physical_quantity']
            else:
                if not unit or (line.unit_snapshot or (order_item.sales_unit_snapshot if order_item else None)) != unit:
                    raise QuantityContractError('单位缺失')
                if order_item and order_item.supply_mode_snapshot == 'external_purchase':
                    raise QuantityContractError('外购实物数量未冻结')
                quantity = line.delivered_quantity
            if sample['quantity'] is not None:
                sample['quantity'] += quantity
        except QuantityContractError:
            sample['quantity'] = None
            issues.append('历史送货实物单位待核，暂不建议具体预警数量')
    if lines:
        discrepancy = db.scalar(select(ReturnReceiptItem.id).join(ReturnReceipt,
            ReturnReceipt.id == ReturnReceiptItem.return_receipt_id).join(DeliveryItem,
            DeliveryItem.id == ReturnReceiptItem.delivery_item_id).where(
                ReturnReceipt.status == 'confirmed', DeliveryItem.id.in_([line.id for _, line, _ in lines]),
                ReturnReceiptItem.actual_received_quantity != DeliveryItem.delivered_quantity).limit(1))
        if discrepancy:
            issues.append('送货与签收数量存在差异，需求数量需人工复核')

    # A complete order-source chain is required. Reversed receipts/completions,
    # stock transfers, and customer waiting after completion are never lead time.
    receipts = db.execute(select(IncomingReceiptItem.order_item_id, Requisition.requisition_date,
        IncomingReceipt.received_at, RequisitionItem.id).join(IncomingReceipt,
        IncomingReceipt.id == IncomingReceiptItem.receipt_id).join(RequisitionItem,
        RequisitionItem.id == IncomingReceiptItem.requisition_item_id).join(Requisition,
        Requisition.id == RequisitionItem.requisition_id).join(OrderItem,
        OrderItem.id == IncomingReceiptItem.order_item_id).join(Order, Order.id == OrderItem.order_id)
        .where(Order.customer_id == cid, OrderItem.product_id == pid,
            RequisitionItem.order_item_id == OrderItem.id, RequisitionItem.status == '有效',
            IncomingReceipt.status == 'posted', IncomingReceiptItem.status == 'posted',
            Requisition.requisition_date.between(start, today))).all()
    cycles = defaultdict(list)
    for item_id, reported, received, req_id in receipts:
        received_date = utc_naive_to_beijing_date(received)
        if reported <= received_date <= today:
            cycles[(item_id, req_id)].append((reported, received_date))
    completions = defaultdict(list)
    if cycles:
        for item_id, completed in db.execute(select(ProductionCompletion.order_item_id,
            ProductionCompletion.completed_at).where(ProductionCompletion.order_item_id.in_({k[0] for k in cycles}),
                ProductionCompletion.status == 'posted')):
            completions[item_id].append(utc_naive_to_beijing_date(completed))
    procurement, production, samples = [], [], []
    for (item_id, _), values in cycles.items():
        reported, received = min(v[0] for v in values), max(v[1] for v in values)
        procurement.append((received - reported).days)
        # Multiple source cycles cannot reliably assign one completion to each.
        if sum(k[0] == item_id for k in cycles) != 1:
            continue
        dates = [d for d in completions[item_id] if received <= d <= today]
        if dates:
            completed = max(dates)
            production.append((completed - received).days)
            samples.append(dict(reported=reported.isoformat(), received=received.isoformat(),
                completed=completed.isoformat(), total_days=(completed-reported).days))
    deliveries = sorted(documents.values(), key=lambda r:r['date'], reverse=True)
    recent = [r for r in deliveries if r['date'] >= (today-timedelta(days=89)).isoformat()]
    if len(deliveries) < 5 or len(samples) < 3:
        issues.append('有效送货或报料至完工样本不足，保留现行预警并人工复核')
    # This first delivery exposes auditable evidence only. Automatic statistical
    # thresholds require return reconciliation and approved service-level policy.
    issues.append('建议数量待确认需求波动、最小批量及安全余量；不自动改预警或报料')
    return dict(policy_id=warning['policy_id'], kind='rule_evidence', as_of=today.isoformat(),
        customer_id=cid, product_id=pid, unit=unit, period_start=start.isoformat(),
        shipment_count_90=len(recent), shipment_count_180=len(deliveries),
        shipment_quantity_90=None if any('单位' in s or '签收' in s for s in issues) else sum(r['quantity'] for r in recent),
        procurement_samples=len(procurement), procurement_days=median(procurement) if procurement else None,
        production_samples=len(production), production_days=median(production) if production else None,
        complete_cycles=len(samples), cycles=samples[-5:], shipments=deliveries[:5],
        warning_quantity=warning.get('warning_quantity'), target_quantity=warning.get('target_quantity'),
        suggested_warning_quantity=None, reasons=list(dict.fromkeys(issues)),
        location_advice='可查看实际位置；尚无拣货距离及容量依据，暂不推荐自动移库',
        ai_status='not_invoked')
