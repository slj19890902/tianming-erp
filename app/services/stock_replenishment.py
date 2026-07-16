from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.product import Product
from app.models.stock_replenishment import (
    InventoryStockPolicy,
    StockReplenishmentOrder,
    StockReplenishmentOrderItem,
)
from app.models.warehouse_inventory import (
    FinishedGoodsInventoryDetail,
    InventoryLot,
    SemiFinishedInventoryDetail,
    WarehouseLocation,
)
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    manual_finished_in,
    manual_semi_finished_in,
    normalize_material_code,
    utc_now,
)


class StockReplenishmentError(ValueError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def current_policy_quantity(db: Session, policy: InventoryStockPolicy) -> int:
    if policy.target_inventory_type == "finished":
        if policy.product_id is None:
            return 0
        value = db.scalar(
            select(func.coalesce(func.sum(InventoryLot.quantity_available), 0))
            .join(
                FinishedGoodsInventoryDetail,
                FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id,
            )
            .join(WarehouseLocation, WarehouseLocation.id == InventoryLot.warehouse_location_id)
            .where(
                InventoryLot.inventory_type == "finished",
                InventoryLot.status == "active",
                FinishedGoodsInventoryDetail.product_id == policy.product_id,
                or_(
                    WarehouseLocation.source_version.is_(None),
                    WarehouseLocation.source_version != "V11",
                ),
            )
        )
        return int(value or 0)

    if policy.target_inventory_type != "semi_finished":
        return 0
    conditions = [
        InventoryLot.inventory_type == "semi_finished",
        InventoryLot.status == "active",
        SemiFinishedInventoryDetail.board_length_mm == policy.report_length_mm,
        SemiFinishedInventoryDetail.board_width_mm == policy.report_width_mm,
        SemiFinishedInventoryDetail.normalized_material_code
        == policy.normalized_material_code,
        SemiFinishedInventoryDetail.flute_type == policy.flute_type,
        SemiFinishedInventoryDetail.component_type == policy.component_type,
        SemiFinishedInventoryDetail.pieces_per_box == policy.pieces_per_box,
        SemiFinishedInventoryDetail.stock_yield_per_sheet
        == policy.stock_yield_per_sheet,
        SemiFinishedInventoryDetail.sheet_type == policy.sheet_type,
    ]
    if policy.customer_id is None:
        conditions.append(SemiFinishedInventoryDetail.owner_customer_id.is_(None))
    else:
        conditions.append(
            SemiFinishedInventoryDetail.owner_customer_id == policy.customer_id
        )
    value = db.scalar(
        select(func.coalesce(func.sum(InventoryLot.quantity_available), 0))
        .join(
            SemiFinishedInventoryDetail,
            SemiFinishedInventoryDetail.inventory_lot_id == InventoryLot.id,
        )
        .join(WarehouseLocation, WarehouseLocation.id == InventoryLot.warehouse_location_id)
        .where(
            *conditions,
            or_(
                WarehouseLocation.source_version.is_(None),
                WarehouseLocation.source_version != "V11",
            ),
        )
    )
    return int(value or 0)


def stock_policy_dict(db: Session, policy: InventoryStockPolicy) -> dict:
    available = current_policy_quantity(db, policy)
    target = int(policy.target_quantity or 0)
    warning = int(policy.warning_quantity or 0)
    location = policy.default_location
    return {
        "id": policy.id,
        "policy_name": policy.policy_name,
        "target_inventory_type": policy.target_inventory_type,
        "product_id": policy.product_id,
        "customer_id": policy.customer_id,
        "customer_name": policy.customer.name if policy.customer else None,
        "product_code": policy.product.product_code if policy.product else None,
        "product_name": policy.product.product_name if policy.product else None,
        "material_code": policy.material_code_snapshot,
        "layer_count": policy.layer_count,
        "flute_type": policy.flute_type,
        "report_length_mm": policy.report_length_mm,
        "report_width_mm": policy.report_width_mm,
        "sheet_type": policy.sheet_type,
        "component_type": policy.component_type,
        "pieces_per_box": policy.pieces_per_box,
        "stock_yield_per_sheet": policy.stock_yield_per_sheet,
        "warning_quantity": warning,
        "target_quantity": target,
        "available_quantity": available,
        "warning_triggered": bool(policy.active and available <= warning),
        "suggested_replenishment_quantity": max(target - available, 0),
        "default_location": (
            {
                "id": location.id,
                "location_code": location.location_code,
                "location_name": location.location_name,
                "warehouse_type": location.warehouse_type,
            }
            if location
            else None
        ),
        "supplier_name": policy.supplier_name,
        "remark": policy.remark,
        "active": policy.active,
        "updated_at": policy.updated_at,
    }


def validate_stock_policy(db: Session, policy: InventoryStockPolicy) -> None:
    if policy.target_quantity <= 0 or policy.warning_quantity < 0:
        raise StockReplenishmentError("预警数量不能小于0，目标数量必须大于0。")
    if policy.target_quantity < policy.warning_quantity:
        raise StockReplenishmentError("目标库存不能小于预警库存。")
    if policy.target_inventory_type == "finished":
        product = db.get(Product, policy.product_id) if policy.product_id else None
        if product is None or product.deleted_at is not None:
            raise StockReplenishmentError("成品库存预警必须选择有效产品。")
        policy.customer_id = product.customer_id
    elif policy.target_inventory_type == "semi_finished":
        required = (
            policy.material_code_snapshot,
            policy.normalized_material_code,
            policy.layer_count,
            policy.flute_type,
            policy.report_length_mm,
            policy.report_width_mm,
        )
        if not all(value not in (None, "") for value in required):
            raise StockReplenishmentError(
                "半成品库存预警必须填写材质、层数、楞型和报料长宽。"
            )
        valid_flutes = {3: {"A", "B", "E"}, 5: {"AB", "BE"}}
        if policy.layer_count not in valid_flutes or policy.flute_type not in valid_flutes[policy.layer_count]:
            raise StockReplenishmentError("三层只允许A/B/E楞，五层只允许AB/BE楞。")
    else:
        raise StockReplenishmentError("库存预警类型无效。")

    if policy.default_location_id:
        location = db.get(WarehouseLocation, policy.default_location_id)
        if location is None or not location.is_active:
            raise StockReplenishmentError("默认库位不存在或已停用。")
        if location.source_version == "V11":
            raise StockReplenishmentError(
                "V11 三楼 Phase A 货位不能用于正式库存预警策略。", 409
            )
        allowed = {
            "finished": {"finished", "shared"},
            "semi_finished": {"semi_finished", "shared"},
        }[policy.target_inventory_type]
        if location.warehouse_type not in allowed:
            raise StockReplenishmentError("默认库位类型与目标库存类型不一致。")


def next_replenishment_order_number() -> str:
    return f"SR-{datetime.now():%Y%m%d}-{uuid4().hex[:8].upper()}"


def replenishment_item_dict(item: StockReplenishmentOrderItem) -> dict:
    location = item.location
    lot = item.inventory_lot
    return {
        "id": item.id,
        "stock_policy_id": item.stock_policy_id,
        "target_inventory_type": item.target_inventory_type,
        "product_id": item.product_id,
        "customer_id": item.customer_id,
        "product_code": item.product_code_snapshot,
        "product_name": item.product_name_snapshot,
        "material_id": item.material_id,
        "material_code": item.material_code_snapshot,
        "layer_count": item.layer_count,
        "flute_type": item.flute_type,
        "report_length_mm": item.report_length_mm,
        "report_width_mm": item.report_width_mm,
        "crease_type": item.crease_type,
        "crease_left_mm": item.crease_left_mm,
        "crease_middle_mm": item.crease_middle_mm,
        "crease_right_mm": item.crease_right_mm,
        "sheet_type": item.sheet_type,
        "component_type": item.component_type,
        "pieces_per_box": item.pieces_per_box,
        "stock_yield_per_sheet": item.stock_yield_per_sheet,
        "quantity": item.quantity,
        "stocked_quantity": item.stocked_quantity,
        "location": (
            {
                "id": location.id,
                "location_code": location.location_code,
                "location_name": location.location_name,
            }
            if location
            else None
        ),
        "inventory_lot": (
            {
                "id": lot.id,
                "lot_number": lot.lot_number,
                "quantity_available": lot.quantity_available,
            }
            if lot
            else None
        ),
        "historical_source": (
            {
                "workbook": item.historical_workbook,
                "sheet": item.historical_sheet,
                "row": item.historical_row,
                "search_text": item.historical_search_text,
            }
            if item.historical_row
            else None
        ),
        "remark": item.remark,
        "stocked_at": item.stocked_at,
    }


def replenishment_order_dict(order: StockReplenishmentOrder) -> dict:
    return {
        "id": order.id,
        "order_number": order.order_number,
        "supplier_name": order.supplier_name,
        "customer_id": order.customer_id,
        "customer_name": order.customer.name if order.customer else None,
        "source_type": order.source_type,
        "status": order.status,
        "remark": order.remark,
        "created_at": order.created_at,
        "confirmed_at": order.confirmed_at,
        "stocked_at": order.stocked_at,
        "total_quantity": sum(item.quantity for item in order.items),
        "stocked_quantity": sum(item.stocked_quantity for item in order.items),
        "items": [replenishment_item_dict(item) for item in order.items],
    }


def stock_replenishment_order(
    db: Session,
    *,
    order: StockReplenishmentOrder,
    operator_id: int | None,
) -> StockReplenishmentOrder:
    if order.status == "voided":
        raise StockReplenishmentError("已作废补库单不能入库。", 409)
    if order.status == "stocked":
        return order

    now = utc_now()
    for item in order.items:
        if item.stocked_quantity >= item.quantity:
            continue
        if item.location_id is None:
            raise StockReplenishmentError(
                f"补库明细“{item.product_name_snapshot}”未选择入库库位。"
            )
        quantity = item.quantity - item.stocked_quantity
        common = {
            "location_id": item.location_id,
            "quantity": quantity,
            "stock_date": date.today(),
            "source_type": "replenishment",
            "remarks": f"补库单 {order.order_number}；{item.remark or ''}".strip("；"),
            "operator_id": operator_id,
            "idempotency_key": f"stock-replenishment-item-{item.id}",
            "source_ref_type": "stock_replenishment_item",
            "source_ref_id": item.id,
        }
        try:
            if item.target_inventory_type == "finished":
                product = db.get(Product, item.product_id) if item.product_id else None
                if product is None:
                    raise StockReplenishmentError("成品补库明细缺少有效产品。")
                lot = manual_finished_in(
                    db,
                    customer_id=item.customer_id or product.customer_id,
                    product_id=product.id,
                    **common,
                )
            elif item.target_inventory_type == "semi_finished":
                required = (
                    item.material_code_snapshot,
                    item.layer_count,
                    item.flute_type,
                    item.report_length_mm,
                    item.report_width_mm,
                )
                if not all(value not in (None, "") for value in required):
                    raise StockReplenishmentError(
                        "半成品补库明细缺少材质、层数、楞型或报料长宽。"
                    )
                lot = manual_semi_finished_in(
                    db,
                    material_code=item.material_code_snapshot or "",
                    layer_count=int(item.layer_count or 0),
                    flute_type=item.flute_type or "",
                    board_length_mm=int(item.report_length_mm or 0),
                    board_width_mm=int(item.report_width_mm or 0),
                    sheet_type=item.sheet_type,
                    component_type=item.component_type,
                    pieces_per_box=item.pieces_per_box,
                    stock_yield_per_sheet=item.stock_yield_per_sheet,
                    supplier_name=order.supplier_name,
                    customer_id=item.customer_id,
                    material_id=item.material_id,
                    crease_type=item.crease_type,
                    crease_left_mm=item.crease_left_mm,
                    crease_middle_mm=item.crease_middle_mm,
                    crease_right_mm=item.crease_right_mm,
                    cutting_note=item.remark,
                    **common,
                )
            else:
                raise StockReplenishmentError("补库目标类型无效。")
        except WarehouseInventoryError as error:
            raise StockReplenishmentError(str(error), error.status_code) from error

        item.inventory_lot_id = lot.id
        item.stocked_quantity = item.quantity
        item.stocked_at = now

    remaining = [item for item in order.items if item.stocked_quantity < item.quantity]
    order.status = "partially_stocked" if remaining else "stocked"
    order.stocked_by = operator_id
    order.stocked_at = now if not remaining else None
    if order.confirmed_at is None:
        order.confirmed_at = now
        order.confirmed_by = operator_id
    db.flush()
    return order
