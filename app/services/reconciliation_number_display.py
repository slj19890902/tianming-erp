"""Display precision only; keep the stored reconciliation values untouched."""
from decimal import Decimal, ROUND_HALF_UP

THREE_PLACES = Decimal('0.001')


def rounded_display_value(value: object) -> Decimal:
    number = Decimal(str(value or 0))
    if not number.is_finite():
        raise ValueError('对账数值必须是有限数值')
    return number.quantize(THREE_PLACES, rounding=ROUND_HALF_UP)


def format_reconciliation_number(value: object) -> str:
    rounded = rounded_display_value(value)
    if not rounded:
        return '0'
    if rounded == rounded.to_integral_value():
        return format(rounded.to_integral_value(), 'f')
    return format(rounded, '.3f')


def apply_reconciliation_number_formats(workbook) -> None:
    """Use per-cell formats so integers have no decimal separator or zeros."""
    for sheet in workbook:
        for row in sheet:
            for cell in row:
                if isinstance(cell.value, (int, float, Decimal)) and not isinstance(cell.value, bool):
                    rounded = rounded_display_value(cell.value)
                    cell.number_format = '0' if rounded == rounded.to_integral_value() else '0.000'
