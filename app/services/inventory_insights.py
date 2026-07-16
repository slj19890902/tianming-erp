from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.warehouse_inventory import InventoryLot
from app.services.inventory_cost_snapshot import (
    estimate_from_snapshot,
    estimate_inventory_lot_cost,
)
from app.services.semi_finished_inventory import (
    semi_finished_lot_assigned_product_ids,
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
    return max((as_of - lot.stock_date).days, 0)


def _age_bucket(days: int) -> tuple[str, str]:
    for key, label, maximum in AGE_BUCKETS:
        if maximum is None or days <= maximum:
            return key, label
    return AGE_BUCKETS[-1][0], AGE_BUCKETS[-1][1]


def _demand_by_product(db: Session, as_of: date) -> dict[int, dict[str, int]]:
    demand: dict[int, dict[str, int]] = defaultdict(
        lambda: {"demand_30": 0, "demand_90": 0, "demand_180": 0, "open_demand": 0}
    )
    rows = db.execute(
        select(
            OrderItem.product_id,
            OrderItem.quantity,
            OrderItem.delivered_quantity,
            OrderItem.is_force_closed,
            Order.order_date,
            Order.status,
        ).join(Order, Order.id == OrderItem.order_id)
    ).all()
    cutoffs = {
        "demand_30": as_of - timedelta(days=30),
        "demand_90": as_of - timedelta(days=90),
        "demand_180": as_of - timedelta(days=180),
    }
    for product_id, quantity, delivered, force_closed, order_date, status in rows:
        item = demand[product_id]
        if status not in NON_DEMAND_ORDER_STATUSES:
            for key, cutoff in cutoffs.items():
                if order_date >= cutoff:
                    item[key] += quantity
        if status not in INACTIVE_ORDER_STATUSES and not force_closed:
            item["open_demand"] += max(quantity - delivered, 0)
    return demand


def build_inventory_insights(
    db: Session,
    *,
    as_of: date | None = None,
) -> dict:
    """Build a read-only inventory view; it never reserves or mutates stock."""

    as_of = as_of or date.today()
    lots = list(
        db.scalars(
            select(InventoryLot)
            .options(
                selectinload(InventoryLot.location),
                selectinload(InventoryLot.finished_detail),
                selectinload(InventoryLot.semi_finished_detail),
            )
            .order_by(InventoryLot.id)
        ).all()
    )
    product_ids = {
        lot.finished_detail.product_id
        for lot in lots
        if lot.finished_detail is not None
    }
    products = {
        row.id: row
        for row in db.scalars(select(Product).where(Product.id.in_(product_ids))).all()
    } if product_ids else {}
    demand = _demand_by_product(db, as_of)

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
        "finished": {"lots": 0, "available": 0, "reserved": 0, "damaged": 0, "scrapped": 0},
        "semi_finished": {"lots": 0, "available": 0, "reserved": 0, "damaged": 0, "scrapped": 0},
    }
    actions: list[dict] = []
    available_lots = 0
    active_location_lots = 0
    cost_ready_lots = 0
    estimated_value = Decimal("0")

    for lot in lots:
        metrics = by_type[lot.inventory_type]
        metrics["lots"] += 1
        metrics["available"] += lot.quantity_available
        metrics["reserved"] += lot.quantity_reserved
        metrics["damaged"] += lot.quantity_damaged
        metrics["scrapped"] += lot.quantity_scrapped

        days = _age_days(lot, as_of)
        bucket_key, _bucket_label = _age_bucket(days)
        bucket = bucket_map[bucket_key]
        bucket["lots"] += 1
        bucket[f"{lot.inventory_type}_available"] += lot.quantity_available

        if lot.quantity_available <= 0:
            continue
        available_lots += 1
        if lot.location is not None and lot.location.is_active:
            active_location_lots += 1

        reasons: list[dict[str, str]] = []
        priority = 4
        detail: dict = {}
        estimated_unit_cost: Decimal | None = None
        estimated_cost_status = "pending"
        demand_metrics = {"demand_30": 0, "demand_90": 0, "demand_180": 0, "open_demand": 0}

        if lot.finished_detail is not None:
            row = lot.finished_detail
            product = products.get(row.product_id)
            demand_metrics = demand.get(row.product_id, demand_metrics)
            detail = {
                "customer_name": row.owner_customer_name_snapshot,
                "inventory_code": row.inventory_code_snapshot,
                "name": row.product_name_snapshot,
            }
            if demand_metrics["open_demand"] > 0:
                reasons.append(
                    {
                        "code": "finished_stock_can_cover_order",
                        "text": f"存在 {demand_metrics['open_demand']} 个未完成订单需求，可优先人工确认使用该成品库存。",
                    }
                )
                priority = min(priority, 1)
            if demand_metrics["demand_180"] <= 0:
                reasons.append(
                    {
                        "code": "no_demand_180",
                        "text": "近 180 天没有订单需求，请核对是否长期积压。",
                    }
                )
                priority = min(priority, 2)
        elif lot.semi_finished_detail is not None:
            row = lot.semi_finished_detail
            assigned_ids = semi_finished_lot_assigned_product_ids(db, lot.id)
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
            }
            demand_metrics["open_demand"] = assigned_open_demand
            if assigned_open_demand > 0:
                reasons.append(
                    {
                        "code": "semi_stock_may_cover_demand",
                        "text": f"已分配成品款号存在 {assigned_open_demand} 个未完成需求，请到合并报料中人工核对抵扣。",
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
                    "detail": detail,
                    "demand": demand_metrics,
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
    missing_cost_lots = max(available_lots - cost_ready_lots, 0)
    return {
        "generated_at": datetime.now(),
        "as_of": as_of,
        "scope_notice": "当前看板只统计已录入 ERP 的库存，不代表现场尚未盘点的库存。",
        "recommendation_notice": "所有少报、先消耗或清理建议仅供人工判断，本接口不会修改库存、订单、预占或报料。",
        "summary": {
            "recorded_lots": len(lots),
            "available_lots": available_lots,
            "finished_available": by_type["finished"]["available"],
            "semi_finished_available": by_type["semi_finished"]["available"],
            "total_reserved": by_type["finished"]["reserved"] + by_type["semi_finished"]["reserved"],
            "total_damaged": by_type["finished"]["damaged"] + by_type["semi_finished"]["damaged"],
            "total_scrapped": by_type["finished"]["scrapped"] + by_type["semi_finished"]["scrapped"],
            "actual_inventory_value": None,
            "estimated_inventory_value": _money(estimated_value) if cost_ready_lots else None,
        },
        "data_quality": {
            "active_location_lots": active_location_lots,
            "cost_ready_lots": cost_ready_lots,
            "missing_cost_lots": missing_cost_lots,
            "cost_coverage_percent": round(cost_ready_lots * 100 / available_lots, 1) if available_lots else None,
            "actual_cost_supported": False,
            "actual_cost_message": "库存批次尚无实际单位成本快照，实际现金占用不可计算。",
        },
        "by_type": by_type,
        "age_buckets": list(bucket_map.values()),
        "action_items": actions[:200],
    }
