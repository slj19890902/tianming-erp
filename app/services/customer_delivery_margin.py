"""Read-only delivery-period sales and direct-material margin projection."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryItem
from app.models.order import OrderItem
from app.services.material_cost_lineage import material_cost_coverage_report

MONEY = Decimal("0.01")
RATE = Decimal("0.0001")


def _money(value: Decimal | int | None) -> Decimal:
    return Decimal(str(value or 0)).quantize(MONEY, rounding=ROUND_HALF_UP)


def _amount(value: Decimal | None) -> str:
    return format(_money(value), ".2f")


def _rate(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(Decimal(str(value)).quantize(RATE, rounding=ROUND_HALF_UP), ".4f")


def _empty_metrics() -> dict[str, Any]:
    return {
        "delivery_line_count": 0,
        "quantities": [],
        "unknown_unit_quantity": 0,
        "sales_amount": None,
        "known_sales_amount": "0.00",
        "actual_material_cost": "0.00",
        "supplemental_material_cost": "0.00",
        "material_cost": None,
        "known_material_cost": "0.00",
        "material_margin": None,
        "material_margin_rate": None,
        "covered_sales_amount": "0.00",
        "covered_material_cost": "0.00",
        "covered_material_margin": "0.00",
        "covered_material_margin_rate": None,
        "sales_gap_lines": 0,
        "actual_cost_gap_lines": 0,
        "management_cost_gap_lines": 0,
        "comparable_lines": 0,
        "coverage_rate": "0.0000",
        "status": "empty",
    }


def _next_month(day: date) -> date:
    return date(day.year + (day.month == 12), 1 if day.month == 12 else day.month + 1, 1)


def delivery_cost_line_projections(
    db: Session,
    *,
    date_from: date,
    date_to_exclusive: date,
    visible_customer_ids: set[int] | None = None,
) -> dict[int, dict[str, Any]]:
    """Collect cost details by line while preserving month supplement identity."""

    result: list[dict[str, Any]] = []
    cursor = date_from.replace(day=1)
    while cursor < date_to_exclusive:
        month_end = _next_month(cursor)
        window_end = min(month_end, date_to_exclusive)
        if window_end > date_from:
            month_rows: list[dict[str, Any]] = []
            material_cost_coverage_report(
                db,
                month=cursor.strftime("%Y-%m"),
                visible_customer_ids=visible_customer_ids,
                date_from=max(cursor, date_from),
                date_to_exclusive=window_end,
                _line_collector=month_rows,
            )
            result.extend(month_rows)
        cursor = month_end
    return {int(row["delivery_item_id"]): row for row in result}


# Public descriptive alias for callers and tests.
material_cost_delivery_line_projections = delivery_cost_line_projections


def _sales_projection(
    item: DeliveryItem,
    order_item: OrderItem | None,
    *,
    frozen_unit: str | None = None,
) -> dict[str, Any]:
    quantity = Decimal(int(item.delivered_quantity or 0))
    reasons: set[str] = set()
    amount: Decimal | None = None
    known_amount = Decimal("0")
    if order_item is not None:
        price = order_item.unit_price
        if price is None:
            reasons.add("missing_sales_price")
        else:
            base = quantity * Decimal(str(price))
            if order_item.price_tax_mode_snapshot == "tax_inclusive":
                amount = _money(base)
                known_amount = amount
            elif (
                order_item.price_tax_mode_snapshot == "tax_exclusive"
                and order_item.tax_rate_snapshot is not None
            ):
                amount = _money(base * (Decimal("1") + Decimal(str(order_item.tax_rate_snapshot))))
                known_amount = amount
            else:
                reasons.add("missing_sales_tax_basis")
    else:
        price = item.unit_price_snapshot
        if price is None:
            reasons.add("missing_sales_price")
        else:
            # Existing unordered delivery rows have no tax-basis snapshot.
            reasons.add("missing_sales_tax_basis")
    unit = (str(item.unit_snapshot).strip() if item.unit_snapshot else None) or frozen_unit
    unit_missing = not unit
    if unit_missing:
        reasons.add("missing_sales_unit")
    return {
        "amount": amount,
        "known_amount": known_amount,
        "reasons": reasons,
        "financial_reasons": reasons - {"missing_sales_unit"},
        "quantity": int(item.delivered_quantity or 0),
        "unit": unit,
    }


def _frozen_bom_root_unit(db: Session, *, delivery_item_id: int, order_item: OrderItem) -> str | None:
    """Resolve the root unit from this delivery's frozen BOM history.

    ``delivery_component_lines`` intentionally projects only physical picking
    nodes, so a root that is assembled or split to children can be absent from
    that projection.  Read the historical source contract and then the root
    node itself; never consult the mutable Product master.
    """
    try:
        from app.services.multilevel_bom_delivery_history import historical_delivery_component_demands
        from app.services.multilevel_bom_orders import read_compiled_order_bom, read_order_bom_source_contract

        demands = historical_delivery_component_demands(
            db, delivery_item_id=delivery_item_id, order_item_id=order_item.id
        )
        root = next((d for d in demands if getattr(d, "is_graph_root", False) and getattr(d, "unit", None)), None)
        if root is not None:
            return str(root.unit).strip() or None
        # A child-only delivery still carries a frozen snapshot ID. Re-read
        # that exact source contract to obtain its graph root unit.
        if demands:
            compiled = read_order_bom_source_contract(db, order_item.id, demands[0].snapshot_id)
        else:
            compiled = read_compiled_order_bom(db, order_item.id)
        if compiled is None:
            return None
        root_node = compiled.graph.nodes.get(compiled.graph.root_id)
        unit = getattr(root_node, "unit", None)
        return str(unit).strip() if unit else None
    except Exception:
        return None


def _metrics(lines: list[dict[str, Any]]) -> dict[str, Any]:
    if not lines:
        return _empty_metrics()
    sales_total = Decimal("0")
    known_sales = Decimal("0")
    actual_cost = Decimal("0")
    supplemental_cost = Decimal("0")
    known_cost = Decimal("0")
    covered_sales = Decimal("0")
    covered_cost = Decimal("0")
    covered_lines = 0
    sales_gaps = actual_gaps = management_gaps = 0
    quantities: dict[str, int] = defaultdict(int)
    unknown_quantity = 0
    for line in lines:
        sales = line["sales"]
        known_sales += sales["known_amount"]
        if sales["amount"] is not None:
            sales_total += sales["amount"]
        if sales["financial_reasons"]:
            sales_gaps += 1
        if sales["unit"]:
            quantities[sales["unit"]] += sales["quantity"]
        else:
            unknown_quantity += sales["quantity"]
        cost = line["cost"]
        actual_cost += Decimal(str(cost.get("actual_material_cost", 0)))
        supplemental_cost += Decimal(str(cost.get("supplemental_material_cost", 0)))
        if cost.get("actual_cost_complete") is False:
            actual_gaps += 1
        if cost.get("management_cost_complete") is False:
            management_gaps += 1
        if cost.get("actual_material_cost") is not None or cost.get("supplemental_material_cost") is not None:
            known_cost += Decimal(str(cost.get("management_material_cost", 0)))
        if sales["amount"] is not None and cost.get("management_cost_complete"):
            covered_lines += 1
            covered_sales += sales["amount"]
            covered_cost += Decimal(str(cost.get("management_material_cost", 0)))
    complete_sales = sales_gaps == 0
    complete_cost = management_gaps == 0
    complete = complete_sales and complete_cost
    margin = sales_total - (actual_cost + supplemental_cost) if complete else None
    covered_margin = covered_sales - covered_cost
    metrics = {
        "delivery_line_count": len(lines),
        "quantities": [{"unit": unit, "quantity": quantities[unit]} for unit in sorted(quantities)],
        "unknown_unit_quantity": unknown_quantity,
        "sales_amount": _amount(sales_total) if complete_sales else None,
        "known_sales_amount": _amount(known_sales),
        "actual_material_cost": _amount(actual_cost),
        "supplemental_material_cost": _amount(supplemental_cost),
        "material_cost": _amount(actual_cost + supplemental_cost) if complete_cost else None,
        "known_material_cost": _amount(known_cost),
        "material_margin": _amount(margin) if margin is not None else None,
        "material_margin_rate": _rate(margin / sales_total if margin is not None and sales_total else None),
        "covered_sales_amount": _amount(covered_sales),
        "covered_material_cost": _amount(covered_cost),
        "covered_material_margin": _amount(covered_margin),
        "covered_material_margin_rate": _rate(covered_margin / covered_sales if covered_sales else None),
        "sales_gap_lines": sales_gaps,
        "actual_cost_gap_lines": actual_gaps,
        "management_cost_gap_lines": management_gaps,
        "comparable_lines": covered_lines,
        "coverage_rate": format((Decimal(covered_lines) / Decimal(len(lines))).quantize(RATE, rounding=ROUND_HALF_UP), ".4f"),
        "status": "complete_with_reference" if complete and supplemental_cost else "complete" if complete else "partial",
    }
    return metrics


def build_customer_delivery_margin(
    db: Session,
    *,
    date_from: date,
    date_to: date,
    customer_id: int | None = None,
    visible_customer_ids: set[int] | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    end_exclusive = date_to + timedelta(days=1)
    predicates = [
        Delivery.status == "dispatched",
        DeliveryItem.is_current.is_(True),
        Delivery.delivery_date >= date_from,
        Delivery.delivery_date < end_exclusive,
    ]
    if visible_customer_ids is not None:
        predicates.append(Delivery.customer_id.in_(visible_customer_ids))
    if customer_id is not None:
        predicates.append(Delivery.customer_id == customer_id)
    rows = list(
        db.execute(
            select(DeliveryItem, Delivery, Customer, OrderItem)
            .join(Delivery, Delivery.id == DeliveryItem.delivery_id)
            .join(Customer, Customer.id == Delivery.customer_id)
            .outerjoin(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
            .where(*predicates)
            .order_by(Delivery.delivery_date, Delivery.id, DeliveryItem.id)
        ).all()
    )
    cost_scope = ({customer_id} if customer_id is not None and visible_customer_ids is None else
                  (visible_customer_ids & {customer_id} if customer_id is not None and visible_customer_ids is not None else visible_customer_ids))
    cost_by_item = delivery_cost_line_projections(
        db,
        date_from=date_from,
        date_to_exclusive=end_exclusive,
        visible_customer_ids=cost_scope,
    )
    from app.models.multilevel_bom import OrderBomGraph

    bom_order_ids = (
        set(
            db.scalars(
                select(OrderBomGraph.order_item_id).where(
                    OrderBomGraph.order_item_id.in_(
                        {int(order_item.id) for _, _, _, order_item in rows if order_item is not None}
                    )
                )
            ).all()
        )
        if any(order_item is not None for _, _, _, order_item in rows)
        else set()
    )
    line_rows: list[dict[str, Any]] = []
    for item, delivery, customer, order_item in rows:
        frozen_unit = None
        if (
            order_item is not None
            and not item.unit_snapshot
            and int(order_item.id) in bom_order_ids
        ):
            # For a composite parent delivery, the root demand's unit is the
            # order-specific frozen BOM projection.  It is intentionally not
            # read from the mutable Product master.
            frozen_unit = _frozen_bom_root_unit(
                db, delivery_item_id=int(item.id), order_item=order_item
            )
        line_rows.append(
            {
                "item": item,
                "delivery": delivery,
                "customer": customer,
                "order_item": order_item,
                "sales": _sales_projection(item, order_item, frozen_unit=frozen_unit),
                "cost": cost_by_item.get(
                    int(item.id),
                    {
                        "actual_material_cost": Decimal("0"),
                        "supplemental_material_cost": Decimal("0"),
                        "management_material_cost": Decimal("0"),
                        "actual_cost_complete": False,
                        "management_cost_complete": False,
                        "reason_codes": ["missing_cost_projection"],
                    },
                ),
            }
        )

    def metrics_for(selected: list[dict[str, Any]]) -> dict[str, Any]:
        return _metrics(selected)

    by_customer: dict[int, list[dict[str, Any]]] = defaultdict(list)
    by_day: dict[date, list[dict[str, Any]]] = defaultdict(list)
    reason_counts: Counter[str] = Counter()
    gap_lines: list[dict[str, Any]] = []
    for line in line_rows:
        by_customer[int(line["delivery"].customer_id)].append(line)
        by_day[line["delivery"].delivery_date].append(line)
        reasons = set(line["sales"]["reasons"]) | set(line["cost"].get("reason_codes", []))
        if line["cost"].get("management_cost_complete") is False:
            reasons.add("management_cost_incomplete")
        if reasons:
            reason_counts.update(reasons)
            gap_lines.append({"line": line, "reason_codes": sorted(reasons)})
    customer_items = sorted(
        by_customer.items(),
        key=lambda pair: (str(pair[1][0]["customer"].name or ""), pair[0]),
    )
    offset = (page - 1) * page_size
    paged_customers = customer_items[offset : offset + page_size]
    daily = [
        {"date": (date_from + timedelta(days=i)).isoformat(), "metrics": metrics_for(by_day.get(date_from + timedelta(days=i), []))}
        for i in range((date_to - date_from).days + 1)
    ]
    return {
        "schema_version": 1,
        "as_of": None,
        "filters": {"customer_id": customer_id, "date_from": date_from.isoformat(), "date_to": date_to.isoformat()},
        "basis": {
            "date": "delivery_date", "currency": "CNY", "tax": "tax_inclusive",
            "profit": "delivery_material_margin", "quantity": "sales_line_unit",
            "note": "送货材料毛利；不含工资、能耗、外协和期间费用",
        },
        "summary": metrics_for(line_rows),
        "customers": {
            "items": [
                {"customer_id": cid, "customer_name": lines[0]["customer"].name, "metrics": metrics_for(lines)}
                for cid, lines in paged_customers
            ],
            "page": page, "page_size": page_size, "total": len(customer_items),
        },
        "daily": daily,
        "gaps": {
            "total_lines": len(gap_lines), "reason_counts": dict(sorted(reason_counts.items())),
            "examples": [
                {
                    "customer_id": int(entry["line"]["delivery"].customer_id),
                    "delivery_id": int(entry["line"]["delivery"].id),
                    "delivery_item_id": int(entry["line"]["item"].id),
                    "delivery_number": entry["line"]["delivery"].delivery_number,
                    "reason_codes": entry["reason_codes"],
                }
                for entry in gap_lines[:20]
            ],
            "examples_truncated": len(gap_lines) > 20,
        },
    }
