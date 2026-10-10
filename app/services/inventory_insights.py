from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.time_contract import (
    beijing_date_bounds_utc_naive,
    beijing_today,
    utc_naive_to_api,
    utc_naive_to_beijing_date,
    utc_now_naive,
)
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.warehouse_inventory import (
    FinishedGoodsInventoryDetail,
    InventoryLot,
    InventoryMovement,
    InventoryReservation,
    SemiFinishedInventoryDetail,
    SemiFinishedLotAllowedProduct,
)
from app.services.inventory_cost_snapshot import (
    estimate_from_snapshot,
    estimate_inventory_lot_cost,
)
INACTIVE_ORDER_STATUSES = {"completed", "archived", "closed", "dead", "cancelled"}
NON_DEMAND_ORDER_STATUSES = {"dead", "cancelled"}
AGE_BUCKETS = (
    ("0_90", "0～90 天", 90),
    ("91_180", "91～180 天", 180),
    ("181_365", "181～365 天", 365),
    ("366_548", "366～548 天", 548),
    ("549_730", "549～730 天", 730),
    ("731_plus", "超过 730 天", None),
)


def _money(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _age_days(lot: InventoryLot, as_of: date) -> int:
    """Keep inventory age tied to the original stock date, never movements."""
    return max((as_of - lot.stock_date).days, 0)


def _movement_stagnant_days(
    last_movement_at: datetime | None,
    as_of: date,
) -> int | None:
    if last_movement_at is None:
        return None
    return max((as_of - last_movement_at.date()).days, 0)


def _coverage_percent(covered: int, demand: int) -> float | None:
    if demand <= 0:
        return None
    return round(covered * 100 / demand, 1)


def _movement_history_quality(
    db: Session,
    lot_ids: list[int],
    *,
    as_of: date,
) -> dict[str, object]:
    """Summarize recorded movement depth without inventing missing history."""

    if not lot_ids:
        return {
            "movement_count": 0,
            "history_start_at": None,
            "history_end_at": None,
            "history_days": 0,
            "active_movement_days": 0,
            "consumption_movement_count": 0,
            "recent_90_day_movement_count": 0,
            "consumption_history_days": 0,
            "consumption_month_count": 0,
        }
    recent_cutoff = beijing_date_bounds_utc_naive(
        as_of - timedelta(days=89)
    )[0]
    as_of_exclusive = beijing_date_bounds_utc_naive(as_of)[1]
    base_scope = (
        InventoryMovement.inventory_lot_id.in_(lot_ids),
        InventoryMovement.created_at < as_of_exclusive,
    )
    movement_count, history_start, history_end, active_movement_days = db.execute(
        select(
            func.count(InventoryMovement.id),
            func.min(InventoryMovement.created_at),
            func.max(InventoryMovement.created_at),
            func.count(func.distinct(func.date(InventoryMovement.created_at))),
        ).where(*base_scope)
    ).one()
    demand_consumption_scope = (
        *base_scope,
        InventoryMovement.movement_type == "consume",
        or_(
            InventoryMovement.related_order_item_id.is_not(None),
            InventoryMovement.related_delivery_id.is_not(None),
        ),
    )
    consumption_count, consumption_start, consumption_end = db.execute(
        select(
            func.count(InventoryMovement.id),
            func.min(InventoryMovement.created_at),
            func.max(InventoryMovement.created_at),
        ).where(*demand_consumption_scope)
    ).one()
    recent_consumption_count = db.scalar(
        select(func.count(InventoryMovement.id)).where(
            *demand_consumption_scope,
            InventoryMovement.created_at >= recent_cutoff,
        )
    )
    consumption_dates = db.scalars(
        select(InventoryMovement.created_at).where(*demand_consumption_scope)
    ).all()
    consumption_months = {
        (
            utc_naive_to_beijing_date(value).year,
            utc_naive_to_beijing_date(value).month,
        )
        for value in consumption_dates
        if value is not None
    }
    history_days = 0
    if history_start is not None and history_end is not None:
        history_days = max(
            (
                utc_naive_to_beijing_date(history_end)
                - utc_naive_to_beijing_date(history_start)
            ).days
            + 1,
            1,
        )
    consumption_history_days = 0
    if consumption_start is not None and consumption_end is not None:
        consumption_history_days = max(
            (
                utc_naive_to_beijing_date(consumption_end)
                - utc_naive_to_beijing_date(consumption_start)
            ).days
            + 1,
            1,
        )
    return {
        "movement_count": int(movement_count or 0),
        "history_start_at": (
            utc_naive_to_api(history_start) if history_start is not None else None
        ),
        "history_end_at": (
            utc_naive_to_api(history_end) if history_end is not None else None
        ),
        "history_days": history_days,
        "active_movement_days": int(active_movement_days or 0),
        "consumption_movement_count": int(consumption_count or 0),
        "recent_90_day_movement_count": int(recent_consumption_count or 0),
        "consumption_history_days": consumption_history_days,
        "consumption_month_count": len(consumption_months),
    }


def _age_bucket(days: int) -> tuple[str, str]:
    for key, label, maximum in AGE_BUCKETS:
        if maximum is None or days <= maximum:
            return key, label
    return AGE_BUCKETS[-1][0], AGE_BUCKETS[-1][1]


def _demand_by_product(
    db: Session,
    as_of: date,
    customer_ids: set[int] | None = None,
) -> dict[int, dict[str, int]]:
    demand: dict[int, dict[str, int]] = defaultdict(
        lambda: {
            "demand_30": 0,
            "demand_90": 0,
            "demand_180": 0,
            "open_demand": 0,
            "active_reserved_demand": 0,
            "net_unreserved_demand": 0,
        }
    )
    query = select(
            OrderItem.id,
            OrderItem.product_id,
            OrderItem.quantity,
            OrderItem.delivered_quantity,
            OrderItem.is_force_closed,
            Order.order_date,
            Order.status,
        ).join(Order, Order.id == OrderItem.order_id)
    if customer_ids is not None:
        query = query.where(Order.customer_id.in_(customer_ids))
    rows = db.execute(query).all()
    cutoffs = {
        "demand_30": as_of - timedelta(days=30),
        "demand_90": as_of - timedelta(days=90),
        "demand_180": as_of - timedelta(days=180),
    }
    open_demand_by_item: dict[int, int] = {}
    product_id_by_item: dict[int, int] = {}
    for item_id, product_id, quantity, delivered, force_closed, order_date, status in rows:
        item = demand[product_id]
        if status not in NON_DEMAND_ORDER_STATUSES:
            for key, cutoff in cutoffs.items():
                if order_date >= cutoff:
                    item[key] += quantity
        if status not in INACTIVE_ORDER_STATUSES and not force_closed:
            remaining = max(quantity - delivered, 0)
            item["open_demand"] += remaining
            if remaining > 0:
                open_demand_by_item[item_id] = remaining
                product_id_by_item[item_id] = product_id

    if open_demand_by_item:
        reserved_by_item = _unconsumed_finished_reservations_by_item_ids(
            db,
            list(open_demand_by_item),
        )
        for item_id, gross_open in open_demand_by_item.items():
            reserved = min(reserved_by_item.get(item_id, 0), gross_open)
            demand[product_id_by_item[item_id]]["active_reserved_demand"] += reserved

    for metrics in demand.values():
        metrics["net_unreserved_demand"] = max(
            metrics["open_demand"] - metrics["active_reserved_demand"],
            0,
        )
    return demand


def _assigned_product_ids_by_lot(
    db: Session,
    lots: list[InventoryLot],
    customer_ids: set[int] | None,
) -> dict[int, set[int]]:
    """Bulk-load semi-finished bindings, validating ownership when scoped."""

    lot_ids = [
        lot.id for lot in lots if lot.semi_finished_detail is not None
    ]
    result = {lot_id: set() for lot_id in lot_ids}
    if not lot_ids:
        return result
    query = (
        select(
            SemiFinishedLotAllowedProduct.inventory_lot_id,
            SemiFinishedLotAllowedProduct.product_id,
        )
        .join(
            Product,
            Product.id == SemiFinishedLotAllowedProduct.product_id,
        )
        .where(SemiFinishedLotAllowedProduct.inventory_lot_id.in_(lot_ids))
    )
    if customer_ids is not None:
        query = query.join(
            SemiFinishedInventoryDetail,
            SemiFinishedInventoryDetail.inventory_lot_id
            == SemiFinishedLotAllowedProduct.inventory_lot_id,
        ).where(
            SemiFinishedInventoryDetail.owner_customer_id.in_(customer_ids),
            Product.customer_id
            == SemiFinishedInventoryDetail.owner_customer_id,
        )
    for lot_id, product_id in db.execute(query):
        result[lot_id].add(product_id)
    return result


def _unconsumed_finished_reservations_by_item_ids(
    db: Session,
    order_item_ids: list[int],
) -> dict[int, int]:
    """Return only stock still reserved and not yet consumed or released.

    The shared warehouse helper intentionally represents the total requirement
    already credited by inventory, including stock later consumed for delivery.
    Inventory insight coverage uses a different denominator: delivered quantity
    has already reduced open demand, so consumed reservations must not be counted
    a second time here.
    """

    if not order_item_ids:
        return {}
    rows = db.scalars(
        select(InventoryReservation).where(
            InventoryReservation.order_item_id.in_(order_item_ids),
            InventoryReservation.reservation_type == "finished_order",
            InventoryReservation.status.in_(("active", "partial")),
        )
    ).all()
    result: dict[int, int] = {}
    for row in rows:
        if row.order_item_id is None:
            continue
        remaining = max(
            int(row.credited_requirement_quantity or 0)
            - int(row.consumed_requirement_quantity or 0)
            - int(row.released_requirement_quantity or 0),
            0,
        )
        result[row.order_item_id] = result.get(row.order_item_id, 0) + remaining
    return result


def build_inventory_insights(
    db: Session,
    *,
    as_of: date | None = None,
    customer_ids: set[int] | None = None,
) -> dict:
    """Build a read-only inventory view; it never reserves or mutates stock."""

    as_of = as_of or beijing_today()
    lot_query = select(InventoryLot).options(
        selectinload(InventoryLot.location),
        selectinload(InventoryLot.finished_detail),
        selectinload(InventoryLot.semi_finished_detail),
    )
    if customer_ids is not None:
        lot_query = lot_query.where(
            or_(
                InventoryLot.id.in_(
                    select(FinishedGoodsInventoryDetail.inventory_lot_id)
                    .join(
                        Product,
                        Product.id == FinishedGoodsInventoryDetail.product_id,
                    )
                    .where(
                        FinishedGoodsInventoryDetail.owner_customer_id.in_(
                            customer_ids
                        ),
                        Product.customer_id
                        == FinishedGoodsInventoryDetail.owner_customer_id,
                    )
                ),
                InventoryLot.id.in_(
                    select(SemiFinishedInventoryDetail.inventory_lot_id).where(
                        SemiFinishedInventoryDetail.owner_customer_id.in_(
                            customer_ids
                        )
                    )
                ),
            )
        )
    lots = list(db.scalars(lot_query.order_by(InventoryLot.id)).all())
    movement_quality = _movement_history_quality(
        db,
        [lot.id for lot in lots],
        as_of=as_of,
    )
    assigned_product_ids = _assigned_product_ids_by_lot(
        db, lots, customer_ids
    )
    product_ids = {
        lot.finished_detail.product_id
        for lot in lots
        if lot.finished_detail is not None
    }
    for assigned_ids in assigned_product_ids.values():
        product_ids.update(assigned_ids)
    products = {
        row.id: row
        for row in db.scalars(select(Product).where(Product.id.in_(product_ids))).all()
    } if product_ids else {}
    demand = _demand_by_product(db, as_of, customer_ids)
    finished_available_by_product: dict[int, int] = defaultdict(int)
    finished_primary_lot_by_product: dict[int, InventoryLot] = {}
    for lot in lots:
        if (
            lot.status == "active"
            and lot.quantity_available > 0
            and lot.finished_detail is not None
        ):
            product_id = lot.finished_detail.product_id
            finished_available_by_product[product_id] += lot.quantity_available
            current = finished_primary_lot_by_product.get(product_id)
            if current is None or (lot.stock_date, lot.id) < (
                current.stock_date,
                current.id,
            ):
                finished_primary_lot_by_product[product_id] = lot

    bucket_map = {
        key: {
            "key": key,
            "label": label,
            "lots": 0,
            "finished_available": 0,
            "semi_finished_available": 0,
        }
        for key, label, _maximum in AGE_BUCKETS
    }
    by_type = {
        "finished": {"lots": 0, "available": 0, "frozen": 0, "reserved": 0, "damaged": 0, "scrapped": 0},
        "semi_finished": {"lots": 0, "available": 0, "frozen": 0, "reserved": 0, "damaged": 0, "scrapped": 0},
    }
    actions: list[dict] = []
    available_lots = 0
    usable_lots = 0
    active_location_lots = 0
    cost_ready_lots = 0
    estimate_source_lots = defaultdict(int)
    estimated_value = Decimal("0")

    for lot in lots:
        metrics = by_type[lot.inventory_type]
        metrics["lots"] += 1
        if lot.status == "active":
            metrics["available"] += lot.quantity_available
        elif lot.status == "frozen":
            metrics["frozen"] += lot.quantity_available
        metrics["reserved"] += lot.quantity_reserved
        metrics["damaged"] += lot.quantity_damaged
        metrics["scrapped"] += lot.quantity_scrapped

        days = _age_days(lot, as_of)
        bucket_key, _bucket_label = _age_bucket(days)
        bucket = bucket_map[bucket_key]
        bucket["lots"] += 1
        if lot.status == "active":
            bucket[f"{lot.inventory_type}_available"] += lot.quantity_available

        if lot.quantity_available <= 0:
            continue
        available_lots += 1
        if lot.status == "active":
            usable_lots += 1
        if lot.location is not None and lot.location.is_active:
            active_location_lots += 1

        reasons: list[dict[str, str]] = []
        priority = 4
        detail: dict = {}
        estimated_unit_cost: Decimal | None = None
        estimated_cost_status = "pending"
        demand_metrics = {
            "demand_30": 0,
            "demand_90": 0,
            "demand_180": 0,
            "open_demand": 0,
            "active_reserved_demand": 0,
            "net_unreserved_demand": 0,
        }
        coverage = {
            "covered_demand_quantity": None,
            "uncovered_demand_quantity": None,
            "coverage_percent": None,
            "coverage_basis": "not_applicable",
            "coverage_is_primary": None,
            "coverage_primary_lot_number": None,
            "active_reserved_demand_quantity": None,
            "net_unreserved_demand_quantity": None,
            "free_available_quantity": None,
            "free_covered_demand_quantity": None,
        }

        if lot.status != "active":
            reasons.append(
                {
                    "code": "inventory_lot_not_active",
                    "text": "该库存批次当前已冻结或关闭，不参与订单覆盖和抵扣建议。",
                }
            )
            priority = min(priority, 0)

        if lot.finished_detail is not None:
            row = lot.finished_detail
            product = products.get(row.product_id)
            demand_metrics = demand.get(row.product_id, demand_metrics)
            detail = {
                "customer_name": row.owner_customer_name_snapshot,
                "inventory_code": row.inventory_code_snapshot,
                "name": row.product_name_snapshot,
            }
            total_available = finished_available_by_product[row.product_id]
            gross_open_demand = demand_metrics["open_demand"]
            reserved_demand = min(
                demand_metrics["active_reserved_demand"],
                gross_open_demand,
            )
            net_unreserved_demand = max(gross_open_demand - reserved_demand, 0)
            free_covered_demand = min(total_available, net_unreserved_demand)
            total_covered_demand = min(
                reserved_demand + free_covered_demand,
                gross_open_demand,
            )
            primary_lot = finished_primary_lot_by_product.get(row.product_id)
            is_primary = primary_lot is not None and primary_lot.id == lot.id
            if is_primary:
                coverage = {
                    "covered_demand_quantity": total_covered_demand,
                    "uncovered_demand_quantity": max(
                        gross_open_demand - total_covered_demand,
                        0,
                    ),
                    "coverage_percent": _coverage_percent(
                        total_covered_demand,
                        gross_open_demand,
                    ),
                    "coverage_basis": "finished_free_stock_plus_active_reservations",
                    "coverage_is_primary": True,
                    "coverage_primary_lot_number": primary_lot.lot_number,
                    "active_reserved_demand_quantity": reserved_demand,
                    "net_unreserved_demand_quantity": net_unreserved_demand,
                    "free_available_quantity": total_available,
                    "free_covered_demand_quantity": free_covered_demand,
                }
                if gross_open_demand > 0:
                    reasons.append(
                        {
                            "code": "finished_stock_can_cover_order",
                            "text": (
                                f"存在 {gross_open_demand} 个未完成订单需求；其中 "
                                f"{reserved_demand} 个已有库存预占，尚未预占 "
                                f"{net_unreserved_demand} 个，当前可用库存可再覆盖 "
                                f"{free_covered_demand} 个。"
                            ),
                        }
                    )
                    priority = min(priority, 1)
                if total_available > net_unreserved_demand:
                    reasons.append(
                        {
                            "code": "finished_stock_exceeds_open_demand",
                            "text": (
                                f"扣除已有预占后，当前可用库存仍比尚未预占需求多 "
                                f"{total_available - net_unreserved_demand} 个；仅供人工核对积压风险。"
                            ),
                        }
                    )
                    priority = min(priority, 2)
                if demand_metrics["demand_180"] <= 0:
                    reasons.append(
                        {
                            "code": "no_demand_180",
                            "text": "近 180 天没有订单需求，请核对是否长期积压。",
                        }
                    )
                    priority = min(priority, 2)
            elif primary_lot is not None:
                coverage = {
                    "covered_demand_quantity": None,
                    "uncovered_demand_quantity": None,
                    "coverage_percent": None,
                    "coverage_basis": "finished_product_coverage_secondary_lot",
                    "coverage_is_primary": False,
                    "coverage_primary_lot_number": primary_lot.lot_number,
                    "active_reserved_demand_quantity": None,
                    "net_unreserved_demand_quantity": None,
                    "free_available_quantity": None,
                    "free_covered_demand_quantity": None,
                }
            else:
                coverage = {
                    "covered_demand_quantity": 0,
                    "uncovered_demand_quantity": gross_open_demand,
                    "coverage_percent": 0.0 if gross_open_demand > 0 else None,
                    "coverage_basis": "finished_inventory_inactive",
                    "coverage_is_primary": False,
                    "coverage_primary_lot_number": None,
                    "active_reserved_demand_quantity": reserved_demand,
                    "net_unreserved_demand_quantity": net_unreserved_demand,
                    "free_available_quantity": 0,
                    "free_covered_demand_quantity": 0,
                }
        elif lot.semi_finished_detail is not None:
            row = lot.semi_finished_detail
            assigned_ids = assigned_product_ids.get(lot.id, set())
            is_general = row.owner_customer_id is None
            assigned_open_demand = sum(
                demand.get(product_id, {}).get("open_demand", 0)
                for product_id in assigned_ids
            )
            detail = {
                "customer_name": row.owner_customer_name_snapshot,
                "inventory_code": row.material_code_snapshot,
                "name": f"{row.board_length_mm} × {row.board_width_mm} mm / {row.flute_type}",
                "assigned_product_count": len(assigned_ids),
                "binding_scope": "general" if is_general else "dedicated",
                "deduction_eligibility": (
                    "physical_signature_required"
                    if is_general
                    else (
                        "allowed_products_required"
                        if assigned_ids
                        else "ineligible_no_product_binding"
                    )
                ),
                "assigned_products": [
                    {
                        "product_id": product_id,
                        "inventory_code": products[product_id].product_code if product_id in products else None,
                        "name": products[product_id].product_name if product_id in products else None,
                    }
                    for product_id in assigned_ids
                ],
                "candidate_relationship_read_only": True,
            }
            demand_metrics["open_demand"] = assigned_open_demand
            coverage = {
                "covered_demand_quantity": None,
                "uncovered_demand_quantity": None,
                "coverage_percent": None,
                "coverage_basis": "semi_finished_candidate_relationship_read_only",
            }
            if lot.status == "active" and assigned_open_demand > 0:
                reasons.append(
                    {
                        "code": "semi_stock_may_cover_demand",
                        "text": f"已分配成品款号存在 {assigned_open_demand} 个未完成需求；候选关系只读展示，请到合并报料中人工核对抵扣。",
                    }
                )
                priority = min(priority, 1)
            if not is_general and not assigned_ids:
                reasons.append(
                    {
                        "code": "semi_product_assignment_missing",
                        "text": "专用半成品批次未绑定任何成品款号，当前明确不可抵扣。",
                    }
                )
                priority = min(priority, 3)

        cost_estimate = estimate_from_snapshot(lot)
        if cost_estimate is not None:
            estimated_unit_cost = cost_estimate.unit_cost
            estimated_cost_status = "estimated_snapshot"
        else:
            cost_estimate = estimate_inventory_lot_cost(db, lot)
            if cost_estimate is not None:
                estimated_unit_cost = cost_estimate.unit_cost
                estimated_cost_status = "estimated_current_material_quote"
            elif lot.finished_detail is not None:
                product = products.get(lot.finished_detail.product_id)
                if product is not None and product.cost_unit_price is not None:
                    candidate = Decimal(product.cost_unit_price)
                    if candidate > 0:
                        estimated_unit_cost = candidate
                        estimated_cost_status = "estimated_product_cost"

        if days > 730:
            reasons.append({"code": "age_cleanup", "text": "库龄超过 2 年，列入清理候选。"})
            priority = min(priority, 0)
        elif days > 548:
            reasons.append({"code": "age_handling", "text": "库龄超过 1.5 年，需要制定处理方案。"})
            priority = min(priority, 1)
        elif days > 365:
            reasons.append({"code": "age_attention", "text": "库龄超过 1 年，需要重点关注。"})
            priority = min(priority, 2)
        elif days > 180:
            reasons.append({"code": "age_slow", "text": "库龄超过 180 天，属于慢动库存。"})
            priority = min(priority, 3)

        if lot.location is None or not lot.location.is_active:
            reasons.append({"code": "location_unavailable", "text": "库位缺失或已停用，需要现场核对。"})
            priority = min(priority, 0)
        if estimated_unit_cost is None:
            reasons.append({"code": "cost_pending", "text": "库存成本待补，暂不计算现金占用。"})
        else:
            cost_ready_lots += 1
            estimate_source_lots[estimated_cost_status] += 1
            estimated_value += estimated_unit_cost * lot.quantity_available

        if reasons:
            actions.append(
                {
                    "priority": priority,
                    "lot_id": lot.id,
                    "lot_number": lot.lot_number,
                    "inventory_type": lot.inventory_type,
                    "status": lot.status,
                    "location_code": lot.location.location_code if lot.location else None,
                    "quantity_available": lot.quantity_available,
                    "unit": lot.unit,
                    "age_days": days,
                    "age_basis": "stock_date",
                    "last_movement_at": utc_naive_to_api(lot.last_movement_at)
                    if lot.last_movement_at is not None
                    else None,
                    "movement_stagnant_days": _movement_stagnant_days(
                        lot.last_movement_at, as_of
                    ),
                    "detail": detail,
                    "demand": demand_metrics,
                    **coverage,
                    "cost_status": estimated_cost_status,
                    "estimated_unit_cost": _money(estimated_unit_cost),
                    "estimated_value": _money(
                        estimated_unit_cost * lot.quantity_available
                        if estimated_unit_cost is not None
                        else None
                    ),
                    "reasons": reasons,
                }
            )

    actions.sort(key=lambda row: (row["priority"], -row["age_days"], row["lot_id"]))
    action_item_count = len(actions)
    high_priority_action_item_count = sum(
        1
        for item in actions
        if isinstance(item.get("priority"), (int, float))
        and item["priority"] <= 1
    )
    missing_cost_lots = max(available_lots - cost_ready_lots, 0)
    source_coverage = {
        status: round(estimate_source_lots[status] * 100 / available_lots, 1)
        if available_lots
        else None
        for status in (
            "estimated_snapshot",
            "estimated_current_material_quote",
            "estimated_product_cost",
        )
    }
    location_coverage_percent = (
        round(active_location_lots * 100 / available_lots, 1)
        if available_lots
        else None
    )
    # Existing InventoryLot cost fields are explicitly estimates.  A future
    # confirmed-cost ledger must include source evidence, formula version,
    # confirmer and immutable paperboard/labour/printing/die-cut components.
    actual_cost_supported = False
    actual_cost_coverage_percent = None
    movement_history_days = int(movement_quality["history_days"])
    movement_data_ready = bool(
        int(movement_quality["consumption_history_days"]) >= 90
        and int(movement_quality["consumption_movement_count"]) >= 12
        and int(movement_quality["consumption_month_count"]) >= 3
        and int(movement_quality["recent_90_day_movement_count"]) > 0
    )
    readiness_reasons: list[str] = []
    if available_lots <= 0:
        readiness_reasons.append("尚无可用库存批次，无法形成库存建议样本。")
    elif usable_lots <= 0:
        readiness_reasons.append("现有库存批次均已冻结或关闭，不能参与订单覆盖和抵扣建议。")
    if movement_quality["movement_count"] <= 0:
        readiness_reasons.append("尚无库存流水，无法判断消耗速度和周转规律。")
    elif not movement_data_ready:
        readiness_reasons.append(
            "订单消耗流水尚未同时满足 90 天跨度、至少 12 次、跨 3 个月，且近 90 天仍有真实订单消耗。"
        )
    if not actual_cost_supported:
        readiness_reasons.append(
            "尚无入库时纸板材料成本快照，不能按金额分析库存。"
        )
    if (
        location_coverage_percent is not None
        and location_coverage_percent < 90
    ):
        readiness_reasons.append(
            f"有效库位覆盖为 {location_coverage_percent}%，低于建议的 90%。"
        )
    ai_analysis_ready = bool(
        usable_lots
        and movement_data_ready
        and actual_cost_supported
        and (actual_cost_coverage_percent or 0) >= 80
        and (location_coverage_percent or 0) >= 90
    )
    ai_procurement_ready = bool(
        ai_analysis_ready and movement_history_days >= 180
    )
    if available_lots <= 0:
        readiness_status = "no_inventory_data"
    elif usable_lots <= 0:
        readiness_status = "no_usable_inventory"
    elif not actual_cost_supported:
        readiness_status = "blocked_actual_cost"
    elif not movement_data_ready:
        readiness_status = "collecting_history"
    elif not ai_procurement_ready:
        readiness_status = "pilot_only"
    else:
        readiness_status = "ready"
    return {
        "generated_at": utc_naive_to_api(utc_now_naive()),
        "as_of": as_of,
        "scope_notice": "当前看板只统计已录入 ERP 的库存，不代表现场尚未盘点的库存。",
        "recommendation_notice": "所有少报、先消耗或清理建议仅供人工判断，本接口不会修改库存、订单、预占或报料。",
        "recommendation_readiness": {
            "status": readiness_status,
            "rule_based_ready": usable_lots > 0,
            "movement_data_ready": movement_data_ready,
            "ai_analysis_ready": ai_analysis_ready,
            "ai_procurement_ready": ai_procurement_ready,
            "movement_count": movement_quality["movement_count"],
            "movement_history_start_at": movement_quality["history_start_at"],
            "movement_history_end_at": movement_quality["history_end_at"],
            "movement_history_days": movement_history_days,
            "active_movement_days": movement_quality["active_movement_days"],
            "consumption_movement_count": movement_quality[
                "consumption_movement_count"
            ],
            "recent_90_day_movement_count": movement_quality[
                "recent_90_day_movement_count"
            ],
            "consumption_history_days": movement_quality[
                "consumption_history_days"
            ],
            "consumption_month_count": movement_quality[
                "consumption_month_count"
            ],
            "minimum_consumption_movement_count": 12,
            "minimum_consumption_month_count": 3,
            "minimum_analysis_history_days": 90,
            "minimum_procurement_history_days": 180,
            "active_location_coverage_percent": location_coverage_percent,
            "actual_cost_coverage_percent": actual_cost_coverage_percent,
            "required_actual_cost_coverage_percent": 80.0,
            "reasons": readiness_reasons,
            "safety_notice": "当前仅运行确定性规则，不调用 AI，也不会自动改报料量、库存或订单。",
        },
        "summary": {
            "recorded_lots": len(lots),
            "available_lots": available_lots,
            "usable_lots": usable_lots,
            "finished_available": by_type["finished"]["available"],
            "semi_finished_available": by_type["semi_finished"]["available"],
            "total_reserved": by_type["finished"]["reserved"] + by_type["semi_finished"]["reserved"],
            "total_damaged": by_type["finished"]["damaged"] + by_type["semi_finished"]["damaged"],
            "total_scrapped": by_type["finished"]["scrapped"] + by_type["semi_finished"]["scrapped"],
            "actual_inventory_value": None,
            "confirmed_material_inventory_value": None,
            "estimated_inventory_value": _money(estimated_value) if cost_ready_lots else None,
        },
        "data_quality": {
            "active_location_lots": active_location_lots,
            "cost_ready_lots": cost_ready_lots,
            "missing_cost_lots": missing_cost_lots,
            "cost_coverage_percent": round(cost_ready_lots * 100 / available_lots, 1) if available_lots else None,
            "confirmed_material_cost_lots": 0,
            "actual_material_cost_coverage_percent": actual_cost_coverage_percent,
            "snapshot_estimate_coverage": source_coverage["estimated_snapshot"],
            "material_snapshot_coverage": source_coverage["estimated_snapshot"],
            "current_quote_coverage": source_coverage["estimated_current_material_quote"],
            "product_reference_coverage": source_coverage["estimated_product_cost"],
            "actual_cost_supported": actual_cost_supported,
            "actual_cost_message": (
                "现有入库成本快照、当前报价和产品参考价都只属于估算；"
                "尚未建立带价格凭据、公式版本、确认人以及人工/印刷/模切组成的真实成本台账。"
            ),
        },
        "by_type": by_type,
        "age_buckets": list(bucket_map.values()),
        "action_item_count": action_item_count,
        "high_priority_action_item_count": high_priority_action_item_count,
        "action_items": actions[:200],
    }
