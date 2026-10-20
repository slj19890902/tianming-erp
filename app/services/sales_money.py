"""Original quote amounts and explicitly separate tax references."""
from decimal import Decimal, localcontext
from app.core.exact_money import money_text, multiply_money, sum_money

LABELS = {'tax_inclusive': '含税', 'tax_exclusive': '未税'}


def price_terms(price, mode, rate):
    label = LABELS.get(mode, '原报价')
    result = {'price_label': label + '单价', 'amount_label': label + '金额'}
    if mode not in LABELS or rate is None:
        return result
    factor = Decimal(1) + Decimal(str(rate))
    if factor <= 0:
        return result
    if mode == 'tax_exclusive':
        value = multiply_money(price, factor)
        reference = dict(tax_mode='tax_inclusive', label='含税单价参考', value=money_text(value), approximate=False)
    else:
        with localcontext() as ctx:
            ctx.prec = 50
            value = Decimal(str(price)) / factor
            displayed = value.quantize(Decimal('0.000001'))
        reference = dict(tax_mode='tax_exclusive', label='未税单价参考',
                         value=money_text(displayed), approximate=displayed * factor != Decimal(str(price)))
    result['tax_reference'] = reference
    return result


def order_totals(items):
    groups = {}
    receivables = []
    for item in items:
        mode = item.price_tax_mode_snapshot
        amount = item.subtotal  # Existing frozen history is never recalculated.
        key = mode if mode in LABELS else 'unknown'
        groups[key] = sum_money((groups.get(key, 0), amount))
        if mode == 'tax_inclusive':
            receivables.append(amount)
        elif mode == 'tax_exclusive' and item.tax_rate_snapshot is not None:
            receivables.append(multiply_money(amount, Decimal(1) + item.tax_rate_snapshot))
        else:
            receivables.append(None)
    known = len(groups) == 1 and 'unknown' not in groups
    return dict(groups=[dict(tax_mode=key, label=LABELS.get(key, '原报价') + '金额', amount=money_text(value))
                        for key, value in groups.items()],
                amount_label=(LABELS[next(iter(groups))] + '金额') if known else '按行报价口径',
                original_amount=money_text(next(iter(groups.values()))) if known else None,
                receivable_amount=money_text(sum_money(receivables)) if all(v is not None for v in receivables) else None)
