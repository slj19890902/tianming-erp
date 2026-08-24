from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


# 2026-08-22 经老板明确确认的现行采购口径：全部材质报价均为人民币、
# 含 13% 税。这里只为新建主档和受控历史补齐提供显式事实；读取历史行时
# 仍会校验数据库字段，绝不在实收请求里临时猜值。
CONFIRMED_PURCHASE_CURRENCY = "CNY"
CONFIRMED_PURCHASE_TAX_INCLUDED = True
CONFIRMED_PURCHASE_TAX_RATE = Decimal("0.13")


_PRICE_UNIT_ALIASES = {
    "per_sheet": "per_sheet",
    "sheet": "per_sheet",
    "元/张": "per_sheet",
    "元/片": "per_sheet",
    "per_square_meter": "per_square_meter",
    "sqm": "per_square_meter",
    "m2": "per_square_meter",
    "元/㎡": "per_square_meter",
    "元/平方米": "per_square_meter",
    "元/m²": "per_square_meter",
}


def normalize_purchase_price_unit(value: object) -> str | None:
    normalized = str(value or "").strip().lower().replace(" ", "")
    return _PRICE_UNIT_ALIASES.get(normalized)


def confirmed_purchase_contract_defaults() -> dict[str, Any]:
    return {
        "purchase_currency": CONFIRMED_PURCHASE_CURRENCY,
        "purchase_tax_included": CONFIRMED_PURCHASE_TAX_INCLUDED,
        "purchase_tax_rate": CONFIRMED_PURCHASE_TAX_RATE,
    }


def purchase_price_contract_issues(
    *,
    quote_price: object,
    price_unit: object,
    purchase_currency: object,
    purchase_tax_included: object,
    purchase_tax_rate: object,
) -> list[str]:
    issues: list[str] = []
    try:
        price = Decimal(str(quote_price)) if quote_price is not None else None
    except (InvalidOperation, TypeError, ValueError):
        price = None
    if price is None or price <= 0:
        issues.append("缺少有效采购报价")
    if normalize_purchase_price_unit(price_unit) is None:
        issues.append("采购计价单位不是元/张、元/片或元/㎡")

    currency = str(purchase_currency or "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        issues.append("缺少有效采购币种")
    if not isinstance(purchase_tax_included, bool):
        issues.append("缺少是否含税")

    try:
        tax_rate = (
            Decimal(str(purchase_tax_rate))
            if purchase_tax_rate is not None
            else None
        )
    except (InvalidOperation, TypeError, ValueError):
        tax_rate = None
    if tax_rate is None:
        issues.append("缺少采购税率")
    elif tax_rate < 0 or tax_rate > 1:
        issues.append("采购税率必须在 0 到 1 之间")
    return issues
