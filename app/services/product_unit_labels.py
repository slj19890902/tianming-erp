"""Employee unit names, separate from frozen physical identities and ledgers.

No function here converts quantities or rewrites historical source facts.
Only explicit product/processing roles determine the count label. Procurement
and externally purchased goods keep their own contractual units.
"""
import json
import re

COUNT_UNITS = {"", "只", "片", "套", "个", "件", "PCS", "pcs", "boxes", "pieces", "sets"}
GLUE = {"粘合", "粘贴", "粘箱", "糊箱", "糊盒"}
STAPLE = {"打钉", "钉箱", "打钉箱", "钉合"}
NO_JOIN = {"无需结合", "无需", "不需结合", "不需要结合"}


def value(row, key, default=None):
    return row.get(key, default) if isinstance(row, dict) else getattr(row, key, default)


def unit_name(unit=None, *, process=None, component=False, composite=False,
              external=False):
    raw = str(unit or "").strip()
    fallback = {"boxes": "只", "pieces": "片", "sets": "套", "sheets": "张"}.get(raw, raw)
    if external or raw not in COUNT_UNITS:
        return fallback, not bool(raw)
    # An assembled subkit has already become a product, even when subsequently
    # consumed as a component in its parent's recipe.
    if composite:
        return "套", False
    if component:
        return "片", False
    tokens = {part.strip() for part in re.split(r"[,，、;；\s]+", str(process or "")) if part.strip()}
    glue, staple, no_join = bool(tokens & GLUE), bool(tokens & STAPLE), bool(tokens & NO_JOIN)
    if bool(glue or staple) == no_join:
        return fallback, True
    return ("片" if no_join else "只"), False


def product_unit_info(product):
    label, review = unit_name(value(product, "unit"), process=value(product, "production_process"),
        component=bool(value(product, "is_internal_component", False)),
        composite=bool(value(product, "is_composite", False)) or value(product, "box_style") == "BOM组合",
        external=value(product, "supply_mode") == "external_purchase")
    return {"unit_label": label, "unit_needs_review": review}


def product_unit_label(product):
    return product_unit_info(product)["unit_label"]


def basis_unit_label(basis, *, component=False):
    if isinstance(basis, str):
        basis = json.loads(basis)
    physical = basis.get("quantity_basis") or {}
    if physical.get("ledger") == "physical" and physical.get("physical_unit"):
        # Physical-unit conversions have their own immutable contract; a
        # customer-count name must never replace the actual ledger unit.
        return unit_name(physical["physical_unit"], external=True)[0]
    body = basis.get("inventory_stage") == "body"
    return unit_name(basis.get("unit"), process=basis.get("production_process"), component=component or body,
        composite=not body and (bool(basis.get("assembly")) or basis.get("box_style") == "BOM组合"))[0]


def order_unit_label(item, product=None):
    """Keep existing frozen sales units; current master cannot rename history."""
    frozen = str(value(item, "sales_unit_snapshot") or "").strip()
    if frozen:
        return frozen
    if value(item, "combination_role") == "set_parent":
        return "套"
    if value(item, "combination_role") == "priced_component":
        return unit_name(value(product, "unit"), component=True,
                         external=value(item, "supply_mode_snapshot") == "external_purchase")[0]
    return product_unit_label(product) if product is not None else ""
