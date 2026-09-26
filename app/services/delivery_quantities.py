"""Frozen customer/physical quantities; inventory remains a physical ledger.

No SKU special cases and no historical inference from today's product master.
The existing product purchase ratio is the source for direct-purchase goods.
"""
from __future__ import annotations

import json
from fractions import Fraction


class QuantityContractError(ValueError):
    pass


def _integer(value, label, *, zero=False):
    if isinstance(value, bool):
        raise QuantityContractError(f'{label}必须为整数')
    try:
        number = Fraction(str(value))
    except (ValueError, TypeError, ZeroDivisionError) as error:
        raise QuantityContractError(f'{label}无效') from error
    if number.denominator != 1 or number < (0 if zero else 1) or number > 2147483647:
        raise QuantityContractError(f'{label}必须为有效整数，不允许四舍五入')
    return int(number)


def _ratio(customer_basis, physical_basis):
    try:
        customer = Fraction(str(customer_basis))
        physical = Fraction(str(physical_basis))
        if customer <= 0 or physical <= 0:
            raise ValueError()
        ratio = physical / customer
        if max(ratio.numerator, ratio.denominator) > 2147483647:
            raise ValueError()
        return ratio
    except (ValueError, TypeError, ZeroDivisionError) as error:
        raise QuantityContractError('客户数量与实物数量换算比例无效') from error


def product_basis(product):
    customer_unit = str(product.unit or '只').strip()
    ratio, physical_unit = Fraction(1), customer_unit
    if (getattr(product, 'supply_mode', None) == 'external_purchase'
            and not getattr(product, 'is_composite', False)
            and getattr(product, 'external_packaging_category_code', None) != 'coated_board'):
        a = product.external_packaging_default_order_quantity_basis
        b = product.external_packaging_default_purchase_quantity_basis
        if a is not None or b is not None:
            ratio = _ratio(a, b)
        physical_unit = str(product.external_packaging_purchase_unit or '').strip()
        if not physical_unit:
            raise QuantityContractError('外购产品缺少实物采购单位')
    return dict(schema=1, customer_id=int(product.customer_id), product_id=int(product.id),
        customer_unit=customer_unit, physical_unit=physical_unit,
        customer_basis=ratio.denominator, physical_basis=ratio.numerator,
        source='product', source_version=int(product.version or 1))


def order_basis(item, customer_id):
    """Only an order's frozen direct-purchase rule may define its quantities."""
    ratio = _ratio(item.external_packaging_order_quantity_basis_snapshot,
                   item.external_packaging_purchase_quantity_basis_snapshot)
    customer_unit = str(item.sales_unit_snapshot or '').strip()
    physical_unit = str(item.external_packaging_purchase_unit_snapshot or '').strip()
    if not customer_unit or not physical_unit:
        raise QuantityContractError('外购订单缺少冻结客户单位或实物单位')
    return dict(schema=1, customer_id=int(customer_id), product_id=int(item.product_id),
        customer_unit=customer_unit, physical_unit=physical_unit,
        customer_basis=ratio.denominator, physical_basis=ratio.numerator,
        source='order', source_version=int(getattr(item, 'version', 1) or 1), order_item_id=item.id)


def reservation_customer_quantity(reservation, physical_quantity):
    credited = reservation.credited_requirement_quantity
    credited = reservation.reserved_stock_quantity if credited is None else credited
    return Fraction(physical_quantity) * Fraction(credited, reservation.reserved_stock_quantity) / requirement_denominator(reservation)


def reservation_physical_quantity(reservation, customer_quantity):
    credited = reservation.credited_requirement_quantity
    credited = reservation.reserved_stock_quantity if credited is None else credited
    return _integer(Fraction(customer_quantity) * requirement_denominator(reservation)
        * Fraction(reservation.reserved_stock_quantity, credited), '预占实物数量', zero=True)


def requirement_denominator(record):
    return _integer(getattr(record, 'requirement_quantity_denominator', None) or 1, '需求数量分母')


def requirement_amount(record, field):
    return Fraction(int(getattr(record, field) or 0), requirement_denominator(record))


def requirement_projection(quantity):
    """Keep exact credit alongside the numeric display value at JSON boundaries."""
    value = Fraction(quantity)
    return {
        'quantity_to_pick_requirement': int(value) if value.denominator == 1 else float(value),
        'requirement_numerator': value.numerator,
        'requirement_denominator': value.denominator,
    }


def covered_requirement_display(record):
    value = max(requirement_amount(record, 'credited_requirement_quantity')
        - requirement_amount(record, 'released_requirement_quantity'), 0)
    return int(value) if value.denominator == 1 else float(value)


def finished_pick_plan(reservations, target_customer_quantity, *, reservation_type='finished_order'):
    """Allocate customer credit FIFO while retaining each lot's physical count."""
    rows = [row for row in reservations if row.reservation_type == reservation_type]
    remaining = max(Fraction(target_customer_quantity) - sum(
        (requirement_amount(row, 'consumed_requirement_quantity') for row in rows),
        Fraction(0)), 0)
    planned = {}
    for row in rows:
        available = max(requirement_amount(row, 'credited_requirement_quantity')
            - requirement_amount(row, 'consumed_requirement_quantity')
            - requirement_amount(row, 'released_requirement_quantity'), 0)
        credit = min(available, remaining)
        planned[row.id] = (reservation_physical_quantity(row, credit), credit)
        remaining -= credit
    return planned


def reservation_requirement_numerator(reservation, physical_quantity):
    return _integer(reservation_customer_quantity(reservation, physical_quantity)
        * requirement_denominator(reservation), '需求数量分子', zero=True)


def allocation_physical_quantity(allocation, customer_quantity):
    return _integer(Fraction(customer_quantity) * requirement_denominator(allocation)
        * Fraction(allocation.consumed_stock_quantity, allocation.credited_requirement_quantity),
        '原分配实物数量', zero=True)


def physical_for(basis, customer_quantity):
    quantity = _integer(customer_quantity, '客户数量', zero=True)
    return _integer(Fraction(quantity) * _ratio(basis['customer_basis'], basis['physical_basis']),
                    '仓库实物数量', zero=True)


def physical_for_fractional_credit(basis, credit):
    """Internal whole-plan allocations may carry exact fractional customer credit."""
    if not isinstance(credit, Fraction) or credit <= 0:
        raise QuantityContractError('批次客户信用必须为正的精确分数')
    return _integer(credit * _ratio(basis['customer_basis'], basis['physical_basis']), '批次实物数量')


def customer_for(basis, physical_quantity):
    quantity = _integer(physical_quantity, '仓库实物数量', zero=True)
    return _integer(Fraction(quantity) / _ratio(basis['customer_basis'], basis['physical_basis']),
                    '客户数量', zero=True)


def finished_reservation_plan(basis, remaining_customer_quantity, entries):
    """Combine selected physical pieces before rounding to complete customer units.

    Entries are (lot_id, available_physical, requested_customer). Returned per-lot
    customer credit may be fractional; the complete plan always covers whole units.
    """
    ratio = _ratio(basis['customer_basis'], basis['physical_basis'])
    capacities, used = [], {}
    for lot_id, available, requested in entries:
        free = max(_integer(available, '可用实物数量', zero=True) - used.get(lot_id, 0), 0)
        take = min(free, physical_for(basis, requested))
        capacities.append(take)
        used[lot_id] = used.get(lot_id, 0) + take
    customer_limit = _integer(remaining_customer_quantity, '客户剩余需求', zero=True)
    complete = min(available_customer_quantity(basis, sum(capacities)), customer_limit)
    complete -= complete % ratio.denominator
    remaining = physical_for(basis, complete)
    result = []
    for capacity in capacities:
        take = min(capacity, remaining)
        result.append((take, Fraction(take) / ratio))
        remaining -= take
    return result


def available_customer_quantity(basis, physical_quantity):
    quantity = _integer(physical_quantity, '可用实物数量', zero=True)
    ratio = _ratio(basis['customer_basis'], basis['physical_basis'])
    # Both sides of a shipment must be integral. Leave partial groups in stock.
    return (quantity // ratio.numerator) * ratio.denominator


def encode(basis, customer_quantity):
    customer_quantity = _integer(customer_quantity, '客户送货数量')
    value = {**basis, 'customer_quantity': customer_quantity,
             'physical_quantity': physical_for(basis, customer_quantity)}
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def decode(raw):
    if raw is None:
        return None
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get('schema') != 1:
            raise ValueError()
        for field in ('customer_id', 'product_id', 'customer_basis', 'physical_basis'):
            _integer(value[field], field)
        if any(not isinstance(value[k], str) or not value[k].strip()
               for k in ('customer_unit', 'physical_unit')):
            raise ValueError()
        _integer(value['customer_quantity'], '客户送货数量')
        if physical_for(value, value['customer_quantity']) != value['physical_quantity']:
            raise ValueError()
        _integer(value['physical_quantity'], '仓库实物数量')
        return value
    except (ValueError, KeyError, TypeError) as error:
        raise QuantityContractError('送货数量快照损坏，请核对原单据，未使用当前常用箱覆盖') from error


def capture(product, customer_quantity, previous=None):
    basis = decode(previous) if previous is not None else product_basis(product)
    if basis['product_id'] != product.id or basis['customer_id'] != product.customer_id:
        raise QuantityContractError('数量快照与客户、产品身份不一致')
    return encode(basis, customer_quantity)


def for_item(item):
    value = decode(getattr(item, 'quantity_contract_json', None))
    if value is not None and value['customer_quantity'] != item.delivered_quantity:
        raise QuantityContractError('送货数量与已保存换算快照不一致')
    return value


def item_physical_quantity(item, customer_quantity=None):
    value = for_item(item)
    quantity = item.delivered_quantity if customer_quantity is None else customer_quantity
    # Missing historical snapshots retain the old accounting basis. Historical
    # corrections must attach explicit audited facts rather than infer a ratio.
    return physical_for(value, quantity) if value else _integer(quantity, '送货数量', zero=True)


def physical_unit(item):
    value = for_item(item)
    return value['physical_unit'] if value else (item.unit_snapshot or '只')


def physical_stock_basis(raw_identity, basis):
    value = json.loads(raw_identity) if raw_identity else {}
    value['quantity_basis'] = dict(schema=1, customer_id=basis['customer_id'],
        product_id=basis['product_id'], physical_unit=basis['physical_unit'], ledger='physical')
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def require_physical_stock(lot, basis, *, require_marker=False):
    if basis['customer_basis'] == basis['physical_basis'] and not require_marker:
        return
    try:
        detail = lot.finished_detail
        value = json.loads(detail.physical_basis_json or '{}').get('quantity_basis', {})
        valid = (value.get('schema') == 1 and value.get('ledger') == 'physical'
            and value.get('customer_id') == basis['customer_id']
            and value.get('product_id') == basis['product_id']
            and value.get('physical_unit') == basis['physical_unit'])
    except (AttributeError, ValueError, TypeError):
        valid = False
    if not valid:
        raise QuantityContractError(f'批次 {lot.lot_number} 的历史库存单位尚未核实，不能按新比例扣库')
