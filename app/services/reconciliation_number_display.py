"""Display every significant decimal without changing frozen sale facts."""
from decimal import Decimal
from app.core.exact_money import decimal_value, money_text, ExcelExactText


def rounded_display_value(value: object) -> Decimal:
    # Compatibility name retained for older callers; this no longer rounds.
    return decimal_value(value or 0)


def format_reconciliation_number(value: object) -> str:
    return money_text(value or 0)


def apply_reconciliation_number_formats(workbook) -> None:
    """Use per-cell formats so integers have no decimal separator or zeros."""
    for sheet in workbook:
        for row in sheet:
            for cell in row:
                if isinstance(cell.value, ExcelExactText):
                    from openpyxl.comments import Comment
                    cell.number_format = '@'
                    cell.comment = Comment('超过Excel可精确计算的15位有效数字，按文本保留完整原值，未舍入。', '天明ERP')
                if isinstance(cell.value, (int, float, Decimal)) and not isinstance(cell.value, bool):
                    text = money_text(cell.value)
                    places = len(text.split('.')[1]) if '.' in text else 0
                    cell.number_format = '0' + ('.' + '0' * places if places else '')
