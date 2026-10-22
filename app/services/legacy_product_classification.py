"""Owner-confirmed classification; never invent a purchasable supply contract.

This projection keeps the stored legacy identity available for history/search.
External candidates still have to pass the existing supplier/specification gates
before a product may be saved as external_purchase.
"""
from app.services.box_type_rules import BOX_TYPE_RULES, canonical_box_style
from app.services.supplier_master import SUPPLIER_CATEGORY_LABELS


EXTERNAL_ALIASES = {
    "ZHJ 纸护角": "paper_corner_guard",
    "HRHJ 华融护角": "paper_corner_guard",
    "HP03 恒鹏护角": "paper_corner_guard",
    "EPE epe": "epe_cushion",
    "FWB 蜂窝板": "honeycomb_board",
}
EXPENSE_ALIASES = ("00001 模具费",)
CONFIRMATION = "owner_20260927"


def classification(product) -> dict:
    raw = str(getattr(product, "box_style", None) or "").strip()
    mode = getattr(product, "supply_mode", None)
    # Explicitly saved supply contracts remain authoritative.
    category = (getattr(product, "external_packaging_category_code", None)
                if mode == "external_purchase" else EXTERNAL_ALIASES.get(raw))
    if category:
        pending = mode != "external_purchase"
        return dict(kind="external", box_style="其他", category_code=category,
                    label="其他外购产品｜" + SUPPLIER_CATEGORY_LABELS.get(category, category),
                    legacy_name=raw, needs_supply_completion=pending,
                    source=CONFIRMATION if raw in EXTERNAL_ALIASES else "saved_product")
    if raw in EXPENSE_ALIASES:
        return dict(kind="expense", box_style=None, category_code=None,
                    label="费用项目｜模具费", legacy_name=raw,
                    needs_supply_completion=False, source=CONFIRMATION)
    return dict(kind="box", box_style=canonical_box_style(raw), category_code=None,
                label=canonical_box_style(raw) or "箱型待核对", legacy_name=raw,
                needs_supply_completion=False, source="box_type_registry")


def new_order_issue(product) -> str | None:
    result = classification(product)
    if result["kind"] == "expense":
        return "模具费属于费用项目，请通过费用入口登记"
    if result["needs_supply_completion"]:
        return result["label"] + "：请完善真实规格、采购单位和供应商产品后再下单"
    return None


def new_order_issue_condition():
    """SQL counterpart of the existing save guard, for candidate pagination.

    This only removes rows that ``new_order_issue`` already refuses. Requisition
    readiness is a different task: missing board fields must not automatically
    hide an otherwise valid manually entered or stock-supplied order product.
    """
    from sqlalchemy import and_, func, or_
    from app.models.product import Product

    # Match Python str.strip() used by classification(), including tabs and
    # full-width spaces in legacy names. NULL is an empty legacy name.
    whitespace = " \t\n\r\v\f\x1c\x1d\x1e\x1f\x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
    style = func.trim(func.coalesce(Product.box_style, ""), whitespace)
    mode = func.coalesce(Product.supply_mode, "")
    explicit_external = and_(mode == "external_purchase",
                             func.coalesce(Product.external_packaging_category_code, "") != "")
    return or_(
        and_(style.in_(EXPENSE_ALIASES), ~explicit_external),
        and_(style.in_(tuple(EXTERNAL_ALIASES)), mode != "external_purchase"),
    )


def box_style_search_values(keyword: str) -> list[str]:
    """Expand current and legacy names without changing the stored product."""
    token = "".join(keyword.upper().split())
    matches = set()
    for rule in BOX_TYPE_RULES:
        names = (rule.code, rule.display_name, *rule.aliases)
        if any(token in "".join(name.upper().split()) for name in names):
            matches.update(names)
    for alias, category in EXTERNAL_ALIASES.items():
        if token in "".join((alias + SUPPLIER_CATEGORY_LABELS[category]).upper().split()):
            matches.add(alias)
    return sorted(matches)


def external_category_search_values(keyword: str) -> list[str]:
    token = "".join(keyword.upper().split())
    return sorted({category for alias, category in EXTERNAL_ALIASES.items()
                   if token in "".join((alias + SUPPLIER_CATEGORY_LABELS[category]).upper().split())})
