from __future__ import annotations

import hashlib
import json
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.delivery import Delivery, DeliveryItem
from app.models.delivery_loading import DeliveryVehicle, ProductLoadingProfile
from app.models.order import OrderItem
from app.models.product import Product

M3_DIVISOR = Decimal("1000000000")
VOLUME_QUANT = Decimal("0.00000001")
RATE_QUANT = Decimal("0.01")


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def dumps_snapshot(value: dict) -> str:
    return json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def loads_snapshot(value: str | None) -> dict | None:
    if not value:
        return None
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def effective_volume_m3(vehicle: DeliveryVehicle) -> Decimal:
    return (
        _decimal(vehicle.cargo_length_mm)
        * _decimal(vehicle.cargo_width_mm)
        * _decimal(vehicle.cargo_height_mm)
        / M3_DIVISOR
        * _decimal(vehicle.safety_load_factor)
    ).quantize(VOLUME_QUANT, rounding=ROUND_HALF_UP)


def vehicle_snapshot(vehicle: DeliveryVehicle) -> dict:
    effective_volume = effective_volume_m3(vehicle)
    return {
        "vehicle_id": vehicle.id,
        "name": vehicle.name,
        "vehicle_type": vehicle.vehicle_type,
        "plate_number": vehicle.plate_number,
        "cargo_length_mm": _decimal(vehicle.cargo_length_mm),
        "cargo_width_mm": _decimal(vehicle.cargo_width_mm),
        "cargo_height_mm": _decimal(vehicle.cargo_height_mm),
        "safety_load_factor": _decimal(vehicle.safety_load_factor),
        "effective_volume_m3": effective_volume,
        "yellow_threshold_pct": _decimal(vehicle.yellow_threshold_pct),
        "red_threshold_pct": _decimal(vehicle.red_threshold_pct),
    }


def _theoretical_volume(product: Product) -> Decimal | None:
    dimensions = (_decimal(product.length_mm), _decimal(product.width_mm), _decimal(product.height_mm))
    if any(value is None or value <= 0 for value in dimensions):
        return None
    return (dimensions[0] * dimensions[1] * dimensions[2] / M3_DIVISOR).quantize(VOLUME_QUANT, rounding=ROUND_HALF_UP)


def profile_snapshot(product: Product, profile: ProductLoadingProfile | None) -> dict:
    mode = profile.mode if profile else "theoretical_box"
    confirmed = bool(profile and profile.confirmed_at)
    unit_volume: Decimal | None = None
    data_pending = False
    source = ""
    if profile is None:
        unit_volume = _theoretical_volume(product)
        data_pending = True
        source = "theoretical_estimate_pending_confirmation" if unit_volume is not None else "missing_profile_and_dimensions"
    elif mode == "theoretical_box":
        unit_volume = _theoretical_volume(product)
        data_pending = not confirmed or unit_volume is None
        if not confirmed:
            source = (
                "theoretical_box_pending_confirmation"
                if unit_volume is not None
                else "theoretical_box_pending_confirmation_missing_dimensions"
            )
        else:
            source = "confirmed_theoretical_box" if unit_volume is not None else "confirmed_profile_missing_dimensions"
    elif mode == "manual_unit":
        unit_volume = _decimal(profile.manual_unit_m3)
        complete = unit_volume is not None and unit_volume > 0
        data_pending = not confirmed or not complete
        source = (
            "manual_unit_pending_confirmation"
            if not confirmed
            else ("manual_unit" if complete else "manual_unit_data_incomplete")
        )
    else:
        dimensions = (_decimal(profile.package_length_mm), _decimal(profile.package_width_mm), _decimal(profile.package_height_mm))
        pieces = profile.package_piece_count
        if pieces and pieces > 0 and all(value is not None and value > 0 for value in dimensions):
            unit_volume = (dimensions[0] * dimensions[1] * dimensions[2] / M3_DIVISOR / Decimal(pieces)).quantize(VOLUME_QUANT, rounding=ROUND_HALF_UP)
        data_pending = not confirmed or unit_volume is None
        source = (
            "package_pending_confirmation"
            if not confirmed
            else ("package" if unit_volume is not None else "package_data_incomplete")
        )
    return {
        "product_id": product.id,
        "mode": mode,
        "unit_volume_m3": unit_volume,
        "data_pending": data_pending,
        "source": source,
        "confirmed": confirmed,
        "confirmed_at": profile.confirmed_at.isoformat() if confirmed else None,
        "source_note": profile.source_note if profile else "理论估算待确认",
        "product_dimensions_mm": {
            "length": _decimal(product.length_mm),
            "width": _decimal(product.width_mm),
            "height": _decimal(product.height_mm),
        },
    }


def calculate_loading(
    db: Session,
    *,
    vehicle: DeliveryVehicle | None,
    lines: list[tuple[int, int]],
) -> dict:
    if vehicle is None:
        summary = {
            "status": "not_evaluated",
            "vehicle": None,
            "estimated_total_volume_m3": Decimal("0"),
            "load_rate_pct": None,
            "data_pending": False,
            "items": [],
        }
        summary["hash"] = hashlib.sha256(dumps_snapshot(summary).encode()).hexdigest()
        return summary
    order_item_ids = [order_item_id for order_item_id, _quantity in lines]
    rows = db.execute(
        select(OrderItem.id, Product)
        .join(Product, Product.id == OrderItem.product_id)
        .where(OrderItem.id.in_(order_item_ids))
    ).all()
    products = {int(order_item_id): product for order_item_id, product in rows}
    product_ids = [product.id for product in products.values()]
    profiles = {
        profile.product_id: profile
        for profile in db.scalars(select(ProductLoadingProfile).where(ProductLoadingProfile.product_id.in_(product_ids))).all()
    } if product_ids else {}
    item_snapshots = []
    total = Decimal("0")
    data_pending = False
    for order_item_id, quantity in lines:
        product = products.get(order_item_id)
        if product is None:
            raise ValueError(f"送货明细 {order_item_id} 缺少产品")
        item = profile_snapshot(product, profiles.get(product.id))
        item["order_item_id"] = order_item_id
        item["delivered_quantity"] = int(quantity)
        unit_volume = item["unit_volume_m3"]
        item["estimated_volume_m3"] = (
            (unit_volume * int(quantity)).quantize(VOLUME_QUANT, rounding=ROUND_HALF_UP)
            if unit_volume is not None else None
        )
        if item["estimated_volume_m3"] is not None:
            total += item["estimated_volume_m3"]
        data_pending = data_pending or bool(item["data_pending"])
        item_snapshots.append(item)
    vehicle_data = vehicle_snapshot(vehicle)
    rate = (total / vehicle_data["effective_volume_m3"] * 100).quantize(RATE_QUANT, rounding=ROUND_HALF_UP)
    if data_pending:
        loading_status = "data_pending"
    elif rate > vehicle_data["red_threshold_pct"]:
        loading_status = "over_capacity"
    elif rate >= vehicle_data["yellow_threshold_pct"]:
        loading_status = "warning"
    else:
        loading_status = "normal"
    summary = {
        "status": loading_status,
        "vehicle": vehicle_data,
        "estimated_total_volume_m3": total.quantize(VOLUME_QUANT, rounding=ROUND_HALF_UP),
        "load_rate_pct": rate,
        "data_pending": data_pending,
        "items": item_snapshots,
    }
    summary["hash"] = hashlib.sha256(dumps_snapshot(summary).encode()).hexdigest()
    return summary


def recalculate_delivery_loading(db: Session, delivery: Delivery, vehicle: DeliveryVehicle | None = None) -> dict:
    if vehicle is None and delivery.vehicle_id:
        vehicle = db.get(DeliveryVehicle, delivery.vehicle_id)
    lines = db.scalars(select(DeliveryItem).where(DeliveryItem.delivery_id == delivery.id).order_by(DeliveryItem.id)).all()
    result = calculate_loading(db, vehicle=vehicle, lines=[(line.order_item_id, line.delivered_quantity) for line in lines])
    delivery.vehicle_capacity_snapshot_json = dumps_snapshot(result["vehicle"]) if result["vehicle"] else None
    delivery.loading_total_snapshot_json = dumps_snapshot(result)
    delivery.estimated_total_volume_m3 = result["estimated_total_volume_m3"]
    delivery.load_rate_pct = result["load_rate_pct"]
    delivery.loading_status = result["status"]
    delivery.loading_calculation_hash = result["hash"]
    delivery.loading_confirmed_by = None
    delivery.loading_confirmed_at = None
    delivery.loading_confirmed_hash = None
    by_order_item = {item["order_item_id"]: item for item in result["items"]}
    for line in lines:
        item = by_order_item.get(line.order_item_id)
        line.estimated_volume_m3 = item["estimated_volume_m3"] if item else None
        line.loading_snapshot_json = dumps_snapshot(item) if item else None
    return result


def stored_delivery_loading(delivery: Delivery) -> dict:
    summary = loads_snapshot(delivery.loading_total_snapshot_json)
    if summary is not None:
        return summary
    return {
        "status": delivery.loading_status or "not_evaluated",
        "estimated_total_volume_m3": delivery.estimated_total_volume_m3,
        "load_rate_pct": delivery.load_rate_pct,
        "hash": delivery.loading_calculation_hash,
        "vehicle": loads_snapshot(delivery.vehicle_capacity_snapshot_json),
    }
