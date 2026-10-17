"""Balance cent-valued invoice output against frozen reconciliation totals.

Only output amounts may change, by at most one cent from the exact source.
No tax-rate, quantity, price, discount or source financial record is changed.
The tax bureau must still accept its quantity/price/amount tolerance on import.
"""
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")
POLICY = "code-drawing-tax-cent-balance-v5"


class InvoiceRoundingError(ValueError):
    pass


def money(value):
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def balance_invoice_lines(lines, *, target_net, target_tax, target_total):
    target_net, target_tax, target_total = map(Decimal, map(str, (target_net, target_tax, target_total)))
    if any(not x.is_finite() or x < 0 or x != money(x) for x in (target_net, target_tax, target_total)):
        raise InvoiceRoundingError("开票目标金额必须为非负两位金额")
    if target_net + target_tax != target_total:
        raise InvoiceRoundingError("开票目标未税、税额和含税金额不平衡")
    result = [dict(line) for line in lines]
    groups = {}
    for index, line in enumerate(result):
        amount, rate = Decimal(str(line["amount"])), Decimal(str(line["tax_rate"]))
        if not amount.is_finite() or not rate.is_finite() or amount < 0 or not 0 <= rate <= 1:
            raise InvoiceRoundingError("开票明细金额或税率无效")
        if line.get("discount_amount") not in (None, "", 0, Decimal(0)):
            raise InvoiceRoundingError("含折扣明细需单独核对开票金额，不自动分摊尾差")
        if line.get("quantity") is not None and line.get("unit_price") is not None:
            quantity, price = Decimal(str(line["quantity"])), Decimal(str(line["unit_price"]))
            if not quantity.is_finite() or not price.is_finite() or quantity < 0 or price < 0 or abs(quantity * price - amount) > CENT:
                raise InvoiceRoundingError("原始开票金额与数量单价不符，不能用尾差调整掩盖")
        groups.setdefault(rate, []).append((index, amount))
    if not result:
        raise InvoiceRoundingError("开票明细不能为空")
    group_net = sum((money(sum(a for _, a in rows)) for rows in groups.values()), Decimal(0))
    group_tax = sum((money(sum(a for _, a in rows) * rate) for rate, rows in groups.items()), Decimal(0))
    if (group_net, group_tax) != (target_net, target_tax):
        raise InvoiceRoundingError("冻结开票目标与分税率原始明细不一致，请核对来源；不会改写对账金额")
    for rate, rows in groups.items():
        bases = [money(a) for _, a in rows]
        wanted = (int((money(sum(a for _, a in rows)) - sum(bases)) / CENT),
                  int((money(sum(a for _, a in rows) * rate) - sum(money(b * rate) for b in bases)) / CENT))
        if max(map(abs, wanted)) > 20:
            raise InvoiceRoundingError("开票尾差超过自动调整范围，请拆分核对")
        limits = (abs(wanted[0]) + 2, abs(wanted[1]) + 2)
        # Bounded exact search, minimizing changed rounded rows, then raw deviation.
        states = {(0, 0): (0, Decimal(0), ())}
        for (index, raw), base in zip(rows, bases):
            next_states = dict(states)
            for delta in (-1, 1):
                amount = base + delta * CENT
                line = result[index]
                if amount < 0 or abs(amount - raw) > CENT:
                    continue
                if line.get("quantity") is not None and line.get("unit_price") is not None:
                    if abs(amount - Decimal(str(line["quantity"])) * Decimal(str(line["unit_price"]))) > CENT:
                        continue
                tax_delta = int((money(amount * rate) - money(base * rate)) / CENT)
                for (net, tax), (count, error, changes) in states.items():
                    key = (net + delta, tax + tax_delta)
                    if abs(key[0]) > limits[0] or abs(key[1]) > limits[1]:
                        continue
                    candidate = (count + 1, error + abs(amount - raw) - abs(base - raw), changes + ((index, amount),))
                    existing = next_states.get(key)
                    if existing is None or candidate[:2] < existing[:2]:
                        next_states[key] = candidate
            states = next_states
        if wanted not in states:
            raise InvoiceRoundingError("在每行一分钱范围内无法同时对齐未税和税额，已停止导出")
        for (index, _), base in zip(rows, bases):
            result[index]["amount"] = base
        for index, amount in states[wanted][2]:
            result[index]["amount"] = amount
    actual_net = sum((line["amount"] for line in result), Decimal(0))
    actual_tax = sum((money(line["amount"] * Decimal(str(line["tax_rate"]))) for line in result), Decimal(0))
    if (actual_net, actual_tax, actual_net + actual_tax) != (target_net, target_tax, target_total):
        raise InvoiceRoundingError("开票输出总额校验失败")
    changes = [{"line": i + 1, "project_name": line["project_name"],
                "source_amount": str(lines[i]["amount"]), "export_amount": str(line["amount"]),
                "delta": str(line["amount"] - Decimal(str(lines[i]["amount"]))),
                "export_tax": str(money(line["amount"] * Decimal(str(line["tax_rate"]))))}
               for i, line in enumerate(result) if line["amount"] != Decimal(str(lines[i]["amount"]))]
    return result, {"policy": POLICY, "net": str(actual_net), "tax": str(actual_tax),
                    "total": str(actual_net + actual_tax), "adjustments": changes,
                    "tax_system_import_verified": False}
