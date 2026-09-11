"""Frozen external-unit arithmetic. No purchase, inventory or cost writes."""
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction

from app.services.multilevel_bom_plan import BomPlanError
from app.services.order_external_packaging import DISCRETE_PURCHASE_UNITS


def _contract(node):
    if node.source != 'purchased' or node.purchase_units is None:
        raise BomPlanError('订单未冻结外购采购比例，不能从当前常用箱补算')
    return node.purchase_units.validated()


def _quantity(value, unit):
    try:
        if isinstance(value, (float, bool)):
            raise ValueError()
        qty = Decimal(value)
        if not qty.is_finite() or qty < 0 or qty >= Decimal('1e12') or qty.quantize(Decimal('0.000001')) != qty:
            raise ValueError()
        if unit in DISCRETE_PURCHASE_UNITS and qty != qty.to_integral_value():
            raise ValueError()
    except (TypeError, ValueError, InvalidOperation):
        raise BomPlanError('外购累计数量或采购单位精度无效') from None
    return qty


def purchase_quantity_for_stock(node, stock_quantity):
    units = _contract(node)
    if type(stock_quantity) is not int or not 0 <= stock_quantity < 10**10:
        raise BomPlanError('外购库存需求必须为非负整数')
    raw = Fraction(stock_quantity) * Fraction(units.purchase_basis) / Fraction(units.stock_basis)
    precision = Decimal(1) if units.purchase_unit in DISCRETE_PURCHASE_UNITS else Decimal('0.000001')
    steps = raw / Fraction(precision)
    rounded = -(-steps.numerator // steps.denominator)
    return _quantity(Decimal(rounded) * precision, units.purchase_unit)


def cumulative_receipt_conversion(node, *, received_before, received_now):
    """Return this receipt's whole output and cumulative loose purchase units.

    Only differences of cumulative floors may be stocked; flooring each batch
    separately loses valid units at fractional boundaries.
    """
    units = _contract(node)
    before = _quantity(received_before, units.purchase_unit)
    increment = _quantity(received_now, units.purchase_unit)
    after = _quantity(before + increment, units.purchase_unit)
    stock_basis, purchase_basis = Fraction(units.stock_basis), Fraction(units.purchase_basis)
    old_ratio = Fraction(before) * stock_basis / purchase_basis
    new_ratio = Fraction(after) * stock_basis / purchase_basis
    old = old_ratio.numerator // old_ratio.denominator
    new = new_ratio.numerator // new_ratio.denominator
    if new >= 10**10:
        raise BomPlanError('外购换算库存数量超出数据库精度')
    remainder = Fraction(after) - new * purchase_basis / stock_basis
    with localcontext() as ctx:
        ctx.prec = 60
        return new - old, Decimal(remainder.numerator) / Decimal(remainder.denominator)
