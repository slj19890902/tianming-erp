"""Physical inventory facts sourced from an immutable external receipt."""
import json
from decimal import Decimal, ROUND_HALF_UP

from app.core.time_contract import utc_now_naive
from app.services.delivery_quantities import QuantityContractError, _integer, physical_stock_basis


def received_pieces(receipt):
    return _integer(receipt.received_quantity, '实际收到的实物片数')


def freeze_purchase_piece_cost(lot, *, purchase, receipt, customer_id, product_id):
    """Freeze the purchase's original amount/unit; never price from today's master."""
    if receipt.purchase_item_id != purchase.id:
        raise QuantityContractError('实收入库与采购来源不一致')
    if (purchase.purchase_quantity <= 0 or purchase.total_amount < 0
            or purchase.tax_mode not in ('tax_inclusive', 'tax_exclusive')
            or not str(purchase.purchase_unit or '').strip()):
        raise QuantityContractError('采购实物成本或单位资料不完整')
    quantity = received_pieces(receipt)
    detail = lot.finished_detail
    if (lot.source_ref_id != receipt.id
            or lot.source_ref_type not in {'external_packaging_receipt_item', 'direct_external_receipt'}
            or detail is None or detail.product_id != product_id
            or detail.owner_customer_id != customer_id
            or sum(int(getattr(lot, field) or 0) for field in (
                'quantity_available', 'quantity_reserved', 'quantity_consumed',
                'quantity_damaged', 'quantity_scrapped')) != quantity):
        raise QuantityContractError('入库批次的产品、客户、来源或实物数量与实收不一致')
    # total_amount is the frozen tax-inclusive goods amount, excluding freight.
    unit_cost = Decimal(purchase.total_amount) / Decimal(purchase.purchase_quantity)
    lot.estimated_unit_cost_snapshot = unit_cost.quantize(Decimal('.0001'), rounding=ROUND_HALF_UP)
    lot.estimated_square_price_snapshot = lot.estimated_cost_area_m2_snapshot = None
    lot.cost_snapshot_source = 'purchase_receipt_actual'
    lot.cost_snapshot_at = utc_now_naive()
    lot.cost_snapshot_detail_json = json.dumps(dict(
        schema=1, actual=True, external_receipt_item_id=receipt.id,
        external_purchase_item_id=purchase.id, price_version_id=purchase.price_version_id,
        currency=purchase.currency, tax_mode=purchase.tax_mode, tax_rate=str(purchase.tax_rate),
        purchase_line_amount=str(purchase.line_amount), purchase_total_amount=str(purchase.total_amount),
        purchase_quantity=str(purchase.purchase_quantity), received_physical_quantity=quantity,
        capitalized_material_cost=str(unit_cost * quantity),
        product_id=product_id, customer_id=customer_id,
        product_unit=purchase.purchase_unit, stock_unit=purchase.purchase_unit,
        frozen_specification=purchase.specification_json_snapshot,
        quantity_basis='physical',
    ), ensure_ascii=False)
    lot.finished_detail.physical_basis_json = physical_stock_basis(
        lot.finished_detail.physical_basis_json,
        dict(customer_id=customer_id, product_id=product_id, physical_unit=purchase.purchase_unit))
