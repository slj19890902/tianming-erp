"""Direct-purchase goods: receipt -> finished ledger, never production."""
import json
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction

from sqlalchemy import select

from app.models.order import Order, OrderItem
from app.models.multilevel_bom import OrderBomGraph
from app.models.order_external_packaging import SalesOrderItemExternalComponent, SalesOrderItemExternalComponentCandidate
from app.models.external_packaging_purchase import ExternalPackagingReceiptItem, ExternalPackagingPurchaseItem
from app.models.warehouse_inventory import InventoryLot, InventoryReservation
from app.services.external_receipt_state import active_receipt_item
from app.services.external_packaging_purchase import ExternalPurchaseContractError

SOURCE = 'direct_external_receipt'


def eligible(db, item):
    return (item is not None and item.supply_mode_snapshot == 'external_purchase'
        and bool(item.external_packaging_category_code_snapshot)
        and item.external_packaging_category_code_snapshot != 'coated_board'
        and db.get(OrderBomGraph, item.id) is None)


def conversions(db, normalized, *, customer_id):
    result = {}
    for purchase, quantity in normalized:
        item = db.get(OrderItem, purchase.sales_order_item_id) if purchase.sales_order_item_id else None
        if not eligible(db, item):
            continue
        component = db.get(SalesOrderItemExternalComponent, purchase.order_component_id)
        candidate = db.get(SalesOrderItemExternalComponentCandidate, purchase.order_candidate_id)
        order = db.get(Order, item.order_id)
        if (order is None or order.customer_id != customer_id or purchase.sales_order_id != order.id
                or component is None or component.sales_order_item_id != item.id
                or component.source_kind != 'direct_product'
                or component.category_code != purchase.category_code_snapshot
                or component.category_code != item.external_packaging_category_code_snapshot
                or purchase.purchase_unit != item.external_packaging_purchase_unit_snapshot
                or candidate is None or candidate.order_component_id != component.id
                or candidate.external_product_id_snapshot != purchase.external_product_id_snapshot
                or candidate.external_product_version_snapshot != purchase.external_product_version_snapshot
                or candidate.purchase_unit_snapshot != purchase.purchase_unit):
            raise ExternalPurchaseContractError('外购收料产品、客户或单位身份不一致')
        a, b = item.external_packaging_order_quantity_basis_snapshot, item.external_packaging_purchase_quantity_basis_snapshot
        if a is None or b is None or a <= 0 or b <= 0:
            raise ExternalPurchaseContractError('旧外购订单缺少冻结换算比例，请先由管理员核对，未自动补写库存')
        ratio = Fraction(b) / Fraction(a)
        before, output = Fraction(0), 0
        for old in db.scalars(select(ExternalPackagingReceiptItem).where(
                ExternalPackagingReceiptItem.purchase_item_id == purchase.id, active_receipt_item()).order_by(ExternalPackagingReceiptItem.id)):
            after = before + Fraction(old.received_quantity)
            expected = int(after // ratio) - int(before // ratio)
            if old.converted_finished_quantity != expected:
                from app.services.external_legacy_stock import receipt_credit
                if receipt_credit(db, item.id, old.id) != expected:
                    raise ExternalPurchaseContractError('此采购已有未入仓的历史收料，请先核对旧库存，不能重复入库')
            if expected and db.scalar(select(InventoryLot.id).where(
                    InventoryLot.source_ref_type == SOURCE, InventoryLot.source_ref_id == old.id).limit(1)) is None:
                raise ExternalPurchaseContractError('既有收料库存身份缺失，请先核对原入仓记录')
            before, output = after, output + expected
        after = before + Fraction(quantity)
        whole = int(after // ratio)
        remainder = after - whole * ratio
        result[purchase.id] = (whole - output, Decimal(remainder.numerator) / Decimal(remainder.denominator))
    return result


def post(db, *, purchase, receipt, customer_id, operator_id):
    from app.services.external_physical_receipt import received_pieces
    from app.services.delivery_quantities import QuantityContractError
    try:
        return _post_quantity(db, purchase=purchase, receipt=receipt, customer_id=customer_id,
                              operator_id=operator_id, quantity=received_pieces(receipt), physical_entry=True)
    except QuantityContractError as error:
        raise ExternalPurchaseContractError(str(error), status_code=409) from error


def _post_quantity(db, *, purchase, receipt, customer_id, operator_id, quantity,
                   location_id=None, layout_version=None, physical_entry=False):
    from app.core.time_contract import beijing_today, utc_now_naive
    from app.services.production_workflow import _receipt_auto_finished_ground_target, _reserve_component_completion_lot
    from app.services.warehouse_inventory import manual_finished_in
    if quantity <= 0:
        return None
    item = db.get(OrderItem, purchase.sales_order_item_id)
    order = db.get(Order, item.order_id)
    from app.models.product import Product
    from app.services.finished_stock_identity import _document, FIELDS
    product = db.get(Product, item.product_id)
    from app.services.delivery_quantities import order_basis, requirement_amount
    quantity_basis = order_basis(item, customer_id) if physical_entry else None
    unit = quantity_basis['customer_unit'] if quantity_basis else product.unit or '只'
    basis = _document(item.product_id, unit, item.snapshot_spec,
        item.snapshot_material, item.flute_type,
        {**{key:getattr(item, key, None) for key in FIELDS},
         'production_notes':item.snapshot_production_notes, 'mold_tool_id':None, 'die_cut_path':None})
    if location_id is None:
        target = _receipt_auto_finished_ground_target(db, claim=True, customer_id=customer_id, product_id=item.product_id)
    else:
        from types import SimpleNamespace
        from app.models.warehouse_inventory import WarehouseLocation
        target = SimpleNamespace(location=db.get(WarehouseLocation, location_id), layout_version=layout_version)
        if target.location is None:
            raise ExternalPurchaseContractError('正式货位不存在')
    lot = manual_finished_in(db, customer_id=customer_id, product_id=item.product_id,
        location_id=target.location.id, quantity=quantity, stock_date=beijing_today(),
        source_type='purchase_reserve', source_ref_type=SOURCE, source_ref_id=receipt.id,
        remarks='外购包材收料即成品', operator_id=operator_id,
        physical_basis_json=basis,
        idempotency_key=f'direct-external-in:{receipt.id}', expected_layout_version=target.layout_version,
        require_empty_pallet=False, movement_reason='外购包材收料入仓')
    lot.finished_detail.inventory_code_snapshot = item.snapshot_product_code
    lot.finished_detail.product_name_snapshot = item.snapshot_product_name
    ratio = Decimal(item.external_packaging_purchase_quantity_basis_snapshot) / Decimal(item.external_packaging_order_quantity_basis_snapshot)
    if (purchase.purchase_quantity <= 0 or purchase.line_amount < 0
            or purchase.tax_mode not in ('tax_inclusive', 'tax_exclusive')):
        raise ExternalPurchaseContractError('外购冻结成本资料不完整')
    unit_cost = Decimal(purchase.line_amount) / Decimal(purchase.purchase_quantity) * ratio
    lot.estimated_unit_cost_snapshot = unit_cost.quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)
    lot.estimated_square_price_snapshot = lot.estimated_cost_area_m2_snapshot = None
    lot.cost_snapshot_source = SOURCE
    lot.cost_snapshot_at = utc_now_naive()
    lot.cost_snapshot_detail_json = json.dumps(dict(schema=1, actual=True,
        external_receipt_item_id=receipt.id, external_purchase_item_id=purchase.id,
        price_version_id=purchase.price_version_id, currency=purchase.currency,
        tax_mode=purchase.tax_mode, tax_rate=str(purchase.tax_rate),
        purchase_line_amount=str(purchase.line_amount), purchase_quantity=str(purchase.purchase_quantity),
        purchase_per_finished=str(ratio), capitalized_material_cost=str(unit_cost * quantity),
        product_id=item.product_id, customer_id=customer_id,
        stock_unit=unit,
        frozen_specification=item.external_packaging_specification_json_snapshot), ensure_ascii=False)
    if physical_entry:
        from app.services.external_physical_receipt import freeze_purchase_piece_cost
        freeze_purchase_piece_cost(lot, purchase=purchase, receipt=receipt,
            customer_id=customer_id, product_id=item.product_id)
    credited = int(item.delivered_quantity or 0)
    for r in db.scalars(select(InventoryReservation).where(InventoryReservation.order_item_id == item.id,
            InventoryReservation.reservation_type == 'finished_order',
            InventoryReservation.sales_order_item_bom_component_id.is_(None), InventoryReservation.status != 'cancelled')):
        credited += max(0, requirement_amount(r, 'credited_requirement_quantity')
            - requirement_amount(r, 'released_requirement_quantity')
            - requirement_amount(r, 'consumed_requirement_quantity'))
    remaining = max(0, int(item.quantity) - credited)
    reserve = min(quantity, int(remaining * Fraction(quantity_basis['physical_basis'], quantity_basis['customer_basis']))
                  if quantity_basis else int(remaining))
    if reserve:
        _reserve_component_completion_lot(db, completion=receipt, order=order, item=item,
            snapshot_id=None, lot=lot, operator_id=operator_id,
            reserve_quantity=reserve,
            credited_quantity=reserve * quantity_basis['customer_basis'] if quantity_basis else reserve,
            requirement_quantity_denominator=quantity_basis['physical_basis'] if quantity_basis else 1,
            idempotency_key=f'direct-external-reserve:{receipt.id}', reservation_number_prefix='DER',
            movement_reason='外购成品订单预占')
    db.flush()
    return lot


def managed(db, item):
    """Use physical accounting for receipt-backed or explicitly verified adopted stock."""
    if item.supply_mode_snapshot != 'external_purchase':
        return False
    if db.scalar(select(InventoryLot.id).join(ExternalPackagingReceiptItem,
        ExternalPackagingReceiptItem.id == InventoryLot.source_ref_id).join(ExternalPackagingPurchaseItem,
        ExternalPackagingPurchaseItem.id == ExternalPackagingReceiptItem.purchase_item_id
    ).where(InventoryLot.source_ref_type == SOURCE,
        ExternalPackagingPurchaseItem.sales_order_item_id == item.id).limit(1)) is not None:
        return True
    if not eligible(db, item):
        return False
    from app.services.delivery_quantities import order_basis, require_physical_stock, requirement_amount, QuantityContractError
    order = db.get(Order, item.order_id)
    if order is None:
        return False
    from sqlalchemy.orm import joinedload
    reservations = list(db.execute(select(InventoryReservation, InventoryLot)
        .outerjoin(InventoryLot, InventoryLot.id == InventoryReservation.inventory_lot_id)
        .options(joinedload(InventoryLot.finished_detail)).where(
        InventoryReservation.order_item_id == item.id,
        InventoryReservation.reservation_type == 'finished_order',
        InventoryReservation.status != 'cancelled',
        InventoryReservation.reserved_stock_quantity > InventoryReservation.released_stock_quantity)))
    if not reservations:
        return False
    try:
        basis = order_basis(item, order.customer_id)
        for reservation, lot in reservations:
            require_physical_stock(lot, basis, require_marker=True)
            if (lot.inventory_type != 'finished' or lot.finished_detail.product_id != item.product_id
                    or lot.finished_detail.is_general
                    or lot.finished_detail.owner_customer_id != order.customer_id
                    or requirement_amount(reservation, 'credited_requirement_quantity')
                    != Fraction(reservation.reserved_stock_quantity * basis['customer_basis'], basis['physical_basis'])):
                return False
    except QuantityContractError:
        return False
    return True
