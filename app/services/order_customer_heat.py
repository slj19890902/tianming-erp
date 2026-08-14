from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import and_, case, func, literal, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.services.order_business_status import (
    BUSINESS_STATUS_ORDER,
    build_order_business_statuses,
)


THRESHOLD_VERSION = "p1-61a-20260814-candidate-v1"
THRESHOLD_STATUS = "prototype_pending_p1_61c_acceptance"
DORMANT_RECENCY_DAYS = 365
NEW_MAX_VALID_ORDERS = 2
NEW_HISTORY_SPAN_DAYS = 30
ESTABLISHED_HEAT_RULES = (
    {
        "level": 5,
        "label": "热5·高活跃",
        "max_recency_days": 1,
        "min_order_days_90": 5,
    },
    {
        "level": 4,
        "label": "热4·活跃",
        "max_recency_days": 2,
        "min_order_days_90": 4,
    },
    {
        "level": 3,
        "label": "热3·稳定",
        "max_recency_days": 7,
        "min_order_days_90": 3,
    },
)

_INVALID_HEAT_ORDER_STATUSES = frozenset(
    {"dead", "cancelled", "canceled", "void", "voided", "draft", "test"}
)
_ONGOING_EXCLUDED_STATUSES = frozenset({"dead", "cancelled", "closed", "archived"})
_STATUS_RANK = {status: index for index, status in enumerate(BUSINESS_STATUS_ORDER)}


def _money_text(value: object | None) -> str | None:
    if value is None:
        return None
    return str(Decimal(str(value)).quantize(Decimal("0.01")))


def threshold_contract() -> dict[str, object]:
    return {
        "version": THRESHOLD_VERSION,
        "status": THRESHOLD_STATUS,
        "dormant_when_recency_days_gt": DORMANT_RECENCY_DAYS,
        "new_when_valid_orders_lte": NEW_MAX_VALID_ORDERS,
        "new_when_history_span_days_lt": NEW_HISTORY_SPAN_DAYS,
        "established_rules": [dict(rule) for rule in ESTABLISHED_HEAT_RULES],
        "established_fallback": {"level": 2, "label": "热2·低频"},
        "amount_usage": "authorized_tiebreak_only",
    }


def _heat_classification(
    *,
    valid_order_count_all: int,
    history_span_days: int | None,
    recency_days: int | None,
    valid_order_days_90: int,
) -> tuple[str, int | None, str, str]:
    if recency_days is None:
        return "dormant", 1, "热1·沉睡", "当前没有有效订单历史"
    if recency_days > DORMANT_RECENCY_DAYS:
        return "dormant", 1, "热1·沉睡", f"最近有效订单距今 {recency_days} 天"
    if (
        valid_order_count_all <= NEW_MAX_VALID_ORDERS
        or (history_span_days or 0) < NEW_HISTORY_SPAN_DAYS
    ):
        return "new", None, "新客户", "有效主单不足3张或有效历史不足30天"
    for rule in ESTABLISHED_HEAT_RULES:
        if (
            recency_days <= int(rule["max_recency_days"])
            and valid_order_days_90 >= int(rule["min_order_days_90"])
        ):
            return (
                "established",
                int(rule["level"]),
                str(rule["label"]),
                f"最近{rule['max_recency_days']}天内下单且近90天下单不少于{rule['min_order_days_90']}天",
            )
    return "established", 2, "热2·低频", "一年内有有效订单但近期频率低于高活跃候选线"


def _ongoing_customer_summaries(
    db: Session,
    customer_ids: list[int],
    *,
    include_finance: bool,
) -> dict[int, dict[str, object | None]]:
    if not customer_ids:
        return {}
    orders = list(
        db.scalars(
            select(Order)
            .options(selectinload(Order.items))
            .where(
                Order.customer_id.in_(customer_ids),
                ~Order.order_number.like("RUIDA-%"),
                Order.status.notin_(_ONGOING_EXCLUDED_STATUSES),
            )
        ).all()
    )
    projections = build_order_business_statuses(
        db,
        orders,
        include_finance=include_finance,
    )
    result: dict[int, dict[str, object | None]] = {
        customer_id: {
            "ongoing_order_count": 0,
            "earliest_blocking_stage": None,
            "earliest_blocking_stage_label": None,
        }
        for customer_id in customer_ids
    }
    for order in orders:
        projection = projections.get(int(order.id), {})
        business_status = str(projection.get("business_status") or order.status)
        if business_status == "completed":
            continue
        row = result[int(order.customer_id)]
        row["ongoing_order_count"] = int(row["ongoing_order_count"] or 0) + 1
        current = row["earliest_blocking_stage"]
        if current is None or _STATUS_RANK.get(business_status, 10_000) < _STATUS_RANK.get(
            str(current), 10_000
        ):
            row["earliest_blocking_stage"] = business_status
            row["earliest_blocking_stage_label"] = projection.get(
                "business_status_label"
            )
    return result


def list_customer_heat(
    db: Session,
    *,
    as_of: date,
    visible_customer_ids: set[int] | None,
    include_amounts: bool,
    include_finance_status: bool,
    keyword: str | None,
    sort_by: Literal["heat", "recency", "frequency", "annual_amount"],
    sort_direction: Literal["asc", "desc"],
    page: int,
    page_size: int,
) -> dict[str, object]:
    """Return one permission-scoped, server-paged customer heat projection.

    ``visible_customer_ids=None`` means unrestricted access.  A concrete set,
    including an empty one, is applied to the customer CTE before any order is
    joined, so an out-of-scope customer never enters aggregation or sorting.
    Amount columns are not joined or calculated unless ``include_amounts`` is
    already true at the API permission boundary.
    """

    if visible_customer_ids is not None and not visible_customer_ids:
        return {
            "total": 0,
            "page": page,
            "page_size": page_size,
            "items": [],
        }

    customer_conditions = [
        Customer.status == "active",
        Customer.is_active.is_(True),
    ]
    if visible_customer_ids is not None:
        customer_conditions.append(Customer.id.in_(visible_customer_ids))
    if keyword and keyword.strip():
        pattern = f"%{keyword.strip()}%"
        customer_conditions.append(
            or_(Customer.name.ilike(pattern), Customer.customer_code.ilike(pattern))
        )

    visible_customers = (
        select(
            Customer.id.label("customer_id"),
            Customer.customer_number.label("customer_number"),
            Customer.customer_code.label("customer_code"),
            Customer.name.label("customer_name"),
        )
        .where(*customer_conditions)
        .cte("visible_customers")
    )
    normalized_status = func.lower(func.trim(func.coalesce(Order.status, "")))
    valid_order_rows = (
        select(
            Order.id.label("order_id"),
            Order.customer_id.label("customer_id"),
            Order.order_date.label("order_date"),
        )
        .join(
            visible_customers,
            visible_customers.c.customer_id == Order.customer_id,
        )
        .where(
            Order.order_number.is_not(None),
            func.trim(Order.order_number) != "",
            Order.order_date <= as_of,
            normalized_status.notin_(_INVALID_HEAT_ORDER_STATUSES),
        )
        .cte("valid_order_rows")
    )

    if include_amounts:
        master_orders = (
            select(
                valid_order_rows.c.order_id,
                valid_order_rows.c.customer_id,
                valid_order_rows.c.order_date,
                func.coalesce(func.sum(OrderItem.subtotal), 0).label("known_amount"),
                case(
                    (func.count(OrderItem.id) == 0, 0),
                    else_=func.min(
                        case((OrderItem.unit_price > 0, 1), else_=0)
                    ),
                ).label("amount_complete"),
            )
            .outerjoin(OrderItem, OrderItem.order_id == valid_order_rows.c.order_id)
            .group_by(
                valid_order_rows.c.order_id,
                valid_order_rows.c.customer_id,
                valid_order_rows.c.order_date,
            )
            .cte("valid_master_orders")
        )
    else:
        master_orders = valid_order_rows

    start_90 = as_of - timedelta(days=89)
    start_365 = as_of - timedelta(days=364)
    rollup_columns = [
        visible_customers.c.customer_id,
        visible_customers.c.customer_number,
        visible_customers.c.customer_code,
        visible_customers.c.customer_name,
        func.count(master_orders.c.order_id).label("valid_order_count_all"),
        func.min(master_orders.c.order_date).label("first_valid_order_date"),
        func.max(master_orders.c.order_date).label("last_valid_order_date"),
        func.count(
            func.distinct(
                case(
                    (master_orders.c.order_date >= start_90, master_orders.c.order_date),
                    else_=None,
                )
            )
        ).label("valid_order_days_90"),
        func.sum(
            case((master_orders.c.order_date >= start_365, 1), else_=0)
        ).label("valid_master_orders_365"),
    ]
    if include_amounts:
        rollup_columns.extend(
            [
                func.sum(
                    case(
                        (master_orders.c.order_date >= start_365, master_orders.c.known_amount),
                        else_=0,
                    )
                ).label("known_annual_amount"),
                func.sum(
                    case(
                        (
                            and_(
                                master_orders.c.order_date >= start_365,
                                master_orders.c.amount_complete == 1,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("complete_amount_orders_365"),
            ]
        )
    rollup = (
        select(*rollup_columns)
        .select_from(
            visible_customers.outerjoin(
                master_orders,
                master_orders.c.customer_id == visible_customers.c.customer_id,
            )
        )
        .group_by(
            visible_customers.c.customer_id,
            visible_customers.c.customer_number,
            visible_customers.c.customer_code,
            visible_customers.c.customer_name,
        )
        .cte("customer_heat_rollup")
    )

    recency_expr = func.julianday(literal(as_of)) - func.julianday(
        rollup.c.last_valid_order_date
    )
    history_span_expr = func.julianday(rollup.c.last_valid_order_date) - func.julianday(
        rollup.c.first_valid_order_date
    )
    dormant_expr = or_(
        rollup.c.last_valid_order_date.is_(None),
        recency_expr > DORMANT_RECENCY_DAYS,
    )
    new_expr = and_(
        ~dormant_expr,
        or_(
            rollup.c.valid_order_count_all <= NEW_MAX_VALID_ORDERS,
            history_span_expr < NEW_HISTORY_SPAN_DAYS,
        ),
    )
    heat_level_expr = case(
        (dormant_expr, 1),
        (new_expr, None),
        *[
            (
                and_(
                    recency_expr <= int(rule["max_recency_days"]),
                    rollup.c.valid_order_days_90 >= int(rule["min_order_days_90"]),
                ),
                int(rule["level"]),
            )
            for rule in ESTABLISHED_HEAT_RULES
        ],
        else_=2,
    )
    heat_sort_expr = case(
        (dormant_expr, 10),
        (new_expr, 15),
        else_=heat_level_expr * 10,
    )
    select_columns = [rollup]
    annual_amount_expr = None
    if include_amounts:
        annual_amount_expr = case(
            (
                and_(
                    rollup.c.valid_master_orders_365 > 0,
                    rollup.c.complete_amount_orders_365
                    == rollup.c.valid_master_orders_365,
                ),
                rollup.c.known_annual_amount,
            ),
            else_=None,
        )
        select_columns.append(annual_amount_expr.label("annual_amount"))

    primary_desc = sort_direction == "desc"
    if sort_by == "recency":
        primary = rollup.c.last_valid_order_date
    elif sort_by == "frequency":
        primary = rollup.c.valid_order_days_90
    elif sort_by == "annual_amount":
        primary = annual_amount_expr
    else:
        primary = heat_sort_expr
    assert primary is not None
    primary_order = primary.desc() if primary_desc else primary.asc()
    secondary_orders = []
    if sort_by == "frequency":
        secondary_orders.append(rollup.c.valid_master_orders_365.desc())
    secondary_orders.extend(
        [
            rollup.c.last_valid_order_date.is_(None),
            rollup.c.last_valid_order_date.desc(),
            rollup.c.valid_order_days_90.desc(),
        ]
    )
    if include_amounts:
        secondary_orders.extend(
            [annual_amount_expr.is_(None), annual_amount_expr.desc()]
        )
    secondary_orders.extend(
        [
            rollup.c.customer_number.is_(None),
            rollup.c.customer_number.asc(),
            rollup.c.customer_code.asc(),
            rollup.c.customer_id.asc(),
        ]
    )

    total = int(db.scalar(select(func.count()).select_from(rollup)) or 0)
    rows = db.execute(
        select(*select_columns)
        .order_by(primary.is_(None), primary_order, *secondary_orders)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).mappings().all()
    customer_ids = [int(row["customer_id"]) for row in rows]
    ongoing = _ongoing_customer_summaries(
        db,
        customer_ids,
        include_finance=include_finance_status,
    )
    items: list[dict[str, object]] = []
    for row in rows:
        first_order_date = row["first_valid_order_date"]
        last_order_date = row["last_valid_order_date"]
        recency_days = (as_of - last_order_date).days if last_order_date else None
        history_span_days = (
            (last_order_date - first_order_date).days
            if first_order_date and last_order_date
            else None
        )
        segment, heat_level, heat_label, heat_reason = _heat_classification(
            valid_order_count_all=int(row["valid_order_count_all"] or 0),
            history_span_days=history_span_days,
            recency_days=recency_days,
            valid_order_days_90=int(row["valid_order_days_90"] or 0),
        )
        customer_id = int(row["customer_id"])
        item: dict[str, object] = {
            "customer_id": customer_id,
            "customer_number": row["customer_number"],
            "customer_code": row["customer_code"],
            "customer_name": row["customer_name"],
            "segment": segment,
            "heat_level": heat_level,
            "heat_label": heat_label,
            "heat_reason": heat_reason,
            "first_valid_order_date": first_order_date,
            "last_valid_order_date": last_order_date,
            "history_span_days": history_span_days,
            "recency_days": recency_days,
            "valid_order_count_all": int(row["valid_order_count_all"] or 0),
            "valid_order_days_90": int(row["valid_order_days_90"] or 0),
            "valid_master_orders_365": int(row["valid_master_orders_365"] or 0),
            **ongoing.get(customer_id, {}),
        }
        if include_amounts:
            order_count_365 = int(row["valid_master_orders_365"] or 0)
            complete_count = int(row["complete_amount_orders_365"] or 0)
            item.update(
                {
                    "annual_amount": _money_text(row["annual_amount"]),
                    "known_annual_amount": _money_text(row["known_annual_amount"]),
                    "annual_amount_complete": bool(
                        order_count_365 > 0 and complete_count == order_count_365
                    ),
                    "annual_amount_completeness_rate": (
                        complete_count / order_count_365
                        if order_count_365 > 0
                        else None
                    ),
                }
            )
        items.append(item)
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": items,
    }
