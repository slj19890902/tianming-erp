"""Exact FIFO portions of frozen external purchase lines, including loose carry."""
from decimal import Decimal, localcontext, ROUND_HALF_UP
from fractions import Fraction

from sqlalchemy import select

from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem, ExternalPackagingReceiptItem
from app.models.order import Order
from app.services.multilevel_bom_external_identity import read_external_node
from app.services.multilevel_bom_orders import read_compiled_order_bom
from app.services.multilevel_bom_plan import BomPlanError
from app.services.multilevel_bom_purchase_units import cumulative_receipt_conversion


def _rounded(value):
    with localcontext() as ctx:
        ctx.prec = 70
        return (Decimal(value.numerator) / Decimal(value.denominator)).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)


def receipt_output_cost(db, receipt_item_id):
    """Read only; all evidence ends at this receipt, never subsequent receipts.

    The purchase line amount retains its quoted tax basis. Separately charged
    freight/tooling is not silently invented or blended into material cost.
    """
    receipt = db.get(ExternalPackagingReceiptItem, receipt_item_id)
    purchase = db.get(ExternalPackagingPurchaseItem, receipt.purchase_item_id) if receipt else None
    link = read_external_node(db, purchase.order_component_id) if purchase else None
    if link is None or link.order_item_id != purchase.sales_order_item_id:
        raise BomPlanError('外购成本缺少真实采购节点')
    compiled = read_compiled_order_bom(db, link.order_item_id)
    node = next(n for n in compiled.graph.nodes if n.product_id == link.product_id)
    order = db.get(Order, purchase.sales_order_id)
    if (order is None or order.customer_id != compiled.graph.customer_id
            or purchase.purchase_unit != node.purchase_units.purchase_unit
            or purchase.tax_mode not in ('tax_inclusive', 'tax_exclusive')
            or not 0 <= purchase.tax_rate <= 1 or len(purchase.currency) != 3
            or purchase.line_amount < 0 or purchase.purchase_quantity <= 0):
        raise BomPlanError('外购成本冻结价格、客户或单位无效')
    history = list(db.scalars(select(ExternalPackagingReceiptItem).where(
        ExternalPackagingReceiptItem.purchase_item_id == purchase.id,
        ExternalPackagingReceiptItem.id <= receipt.id).order_by(ExternalPackagingReceiptItem.id)))
    total_received, total_output = Decimal(0), 0
    spans = []
    for row in history:
        delta, _ = cumulative_receipt_conversion(node, received_before=total_received, received_now=row.received_quantity)
        if delta != row.converted_finished_quantity or row.purchase_unit_snapshot != purchase.purchase_unit:
            raise BomPlanError('外购收料数量与冻结比例不一致')
        start = Fraction(total_received)
        total_received += row.received_quantity
        spans.append((row.id, start, Fraction(total_received)))
        if row.id == receipt.id:
            previous_output = total_output
        total_output += delta
    if total_received > purchase.purchase_quantity or receipt.converted_finished_quantity <= 0:
        raise BomPlanError('外购成本数量超出采购或没有整件产出')
    ratio = Fraction(node.purchase_units.purchase_basis) / Fraction(node.purchase_units.stock_basis)
    left, right = previous_output * ratio, total_output * ratio
    price = Fraction(purchase.line_amount) / Fraction(purchase.purchase_quantity)
    portions = []
    for rid, start, end in spans:
        a, b = max(left, start), min(right, end)
        if b <= a:
            continue
        amount = _rounded(b * price) - _rounded(a * price)
        portions.append(dict(external_receipt_item_id=rid, purchase_start=str(a), purchase_end=str(b), amount=str(amount)))
    if not portions or sum((Fraction(p['purchase_end']) - Fraction(p['purchase_start']) for p in portions), Fraction(0)) != right-left:
        raise BomPlanError('外购整件成本缺少实际收料来源')
    total = sum((Decimal(p['amount']) for p in portions), Decimal(0))
    return dict(schema=1, actual=True, external_receipt_item_id=receipt.id,
        external_purchase_item_id=purchase.id, price_version_id=purchase.price_version_id,
        bom_snapshot_id=link.bom_snapshot_id, product_id=node.product_id,
        customer_id=compiled.graph.customer_id, quantity=receipt.converted_finished_quantity,
        stock_unit=node.unit, currency=purchase.currency, tax_included=purchase.tax_mode == 'tax_inclusive',
        tax_rate=str(purchase.tax_rate), cost_basis='frozen_purchase_line_amount',
        purchase_line_amount=str(purchase.line_amount), purchase_quantity=str(purchase.purchase_quantity),
        capitalized_material_cost=str(total), sources=portions)


def validated_external_lot_detail(db, lot):
    """Validate transferred descendants against authoritative receipt evidence."""
    import json
    from app.services.bom_subkits import SubkitError
    try:
        detail = receipt_output_cost(db, lot.source_ref_id)
        stored = json.loads(lot.cost_snapshot_detail_json or '{}')
        if (lot.source_ref_type != 'bom_external_receipt' or lot.finished_detail is None
                or lot.finished_detail.product_id != detail['product_id']
                or lot.finished_detail.owner_customer_id != detail['customer_id'] or stored != detail):
            raise BomPlanError('外购库存成本身份不一致')
    except (BomPlanError, ValueError, TypeError) as error:
        raise SubkitError(str(error)) from error
    return detail


def external_lot_cost(db, lot, take):
    from app.services.bom_subkit_costs import cost_slice, lineage_used, estimated_slice
    detail = validated_external_lot_detail(db, lot)
    used = lineage_used(db, lot)
    if used + take > detail['quantity']:
        return estimated_slice(lot, take)
    return cost_slice(Decimal(detail['capitalized_material_cost']), detail['quantity'], used, take), detail
