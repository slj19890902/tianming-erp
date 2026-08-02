from __future__ import annotations

from dataclasses import dataclass
import json
from math import ceil

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.delivery import DeliveryItem
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.product_bom import SalesOrderItemBomComponent
from app.models.warehouse_inventory import (
    DeliveryInventoryAllocation,
    InventoryLot,
    InventoryMovement,
    InventoryReservation,
    OrderItemSemiRequirement,
    SemiFinishedInventoryDetail,
    SemiFinishedLotAllowedProduct,
    SemiFinishedMatchRule,
    SemiFinishedMatchRuleProduct,
)
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    _balances,
    _movement,
    _number,
    active_finished_reserved_qty,
    component_effective_required_piece_qty,
    component_inventory_coverage,
    consume_finished_reservation,
    inventory_fifo_order_columns,
    normalize_material_code,
    replace_semi_finished_lot_allowed_products,
    reverse_finished_consumption,
    semi_finished_lot_allowed_product_ids,
    utc_now,
)


VALID_COMPONENT_TYPES = {"whole", "cover", "base"}
SIGNATURE_OVERRIDE_WARNING = "SEMI_SIGNATURE_OVERRIDE"
MANUAL_CONFIRM_WARNING = "MANUAL_DEDUCTION_CONFIRM_REQUIRED"
GENERAL_SEMI_FINISHED_STOCK = "GENERAL_SEMI_FINISHED_STOCK"


@dataclass(frozen=True)
class SemiFinishedSignature:
    customer_id: int
    board_length_mm: int
    board_width_mm: int
    normalized_material_code: str
    flute_type: str
    component_type: str
    pieces_per_box: int
    stock_yield_per_sheet: int


@dataclass(frozen=True)
class SemiFinishedCandidate:
    lot: InventoryLot
    source: str
    match_rule_id: int | None
    available_stock_quantity: int
    deductible_requirement_quantity: int
    signature_differences: tuple[str, ...]
    warning_codes: tuple[str, ...]
    warning_messages: tuple[str, ...]


@dataclass(frozen=True)
class SemiFinishedLotVersion:
    lot_id: int
    expected_version: int


@dataclass(frozen=True)
class SemiFinishedMatchConfirmation:
    rule: SemiFinishedMatchRule
    mapping: SemiFinishedMatchRuleProduct
    signature_differences: tuple[str, ...]
    warning_codes: tuple[str, ...]


@dataclass(frozen=True)
class SemiFinishedReservationBatch:
    reservations: tuple[InventoryReservation, ...]
    requested_requirement_quantity: int
    allocated_requirement_quantity: int
    unallocated_requirement_quantity: int


@dataclass(frozen=True)
class SemiFinishedReservationMutation:
    reservation: InventoryReservation
    movement: InventoryMovement
    allocation: DeliveryInventoryAllocation | None = None


def _component(value: str) -> str:
    component = (value or "").strip().lower()
    if component not in VALID_COMPONENT_TYPES:
        raise WarehouseInventoryError("半成品组件仅允许 whole、cover 或 base")
    return component


def _flute(value: str) -> str:
    flute = (value or "").strip().upper()
    if not flute:
        raise WarehouseInventoryError("楞型不能为空")
    return flute


def save_order_item_semi_requirement(
    db: Session,
    *,
    order_item_id: int,
    component_type: str,
    board_length_mm: int,
    board_width_mm: int,
    material_code: str,
    flute_type: str,
    pieces_per_box: int,
    stock_yield_per_sheet: int,
    required_piece_quantity: int | None,
    operator_id: int | None,
    sales_order_item_bom_component_id: int | None = None,
) -> OrderItemSemiRequirement:
    row = db.execute(
        select(OrderItem, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.id == order_item_id)
    ).one_or_none()
    if row is None:
        raise WarehouseInventoryError("订单明细不存在", 404)
    item, order = row
    snapshot = (
        db.get(SalesOrderItemBomComponent, sales_order_item_bom_component_id)
        if sales_order_item_bom_component_id is not None
        else None
    )
    if sales_order_item_bom_component_id is not None and (
        snapshot is None or snapshot.sales_order_item_id != item.id
    ):
        raise WarehouseInventoryError("组件快照不属于当前订单明细", 409)
    product = db.get(Product, snapshot.component_product_id if snapshot else item.product_id)
    if product is None or product.deleted_at is not None:
        raise WarehouseInventoryError("订单产品不存在", 404)
    if product.customer_id != order.customer_id:
        raise WarehouseInventoryError("订单产品与订单客户不一致", 409)
    component = _component(component_type)
    if board_length_mm <= 0 or board_width_mm <= 0:
        raise WarehouseInventoryError("半成品实际长宽必须大于0")
    if pieces_per_box <= 0 or stock_yield_per_sheet <= 0:
        raise WarehouseInventoryError("每箱片数和每库存张产出片数必须大于0")
    required = (
        int(required_piece_quantity)
        if required_piece_quantity is not None
        else int(item.quantity) * pieces_per_box
    )
    if required <= 0:
        raise WarehouseInventoryError("半成品需求片数必须大于0")
    material_snapshot = material_code.strip()
    normalized_material = normalize_material_code(material_snapshot)
    normalized_flute = _flute(flute_type)
    existing_query = select(OrderItemSemiRequirement).where(
        OrderItemSemiRequirement.order_item_id == item.id,
    )
    if snapshot is not None:
        existing_query = existing_query.where(
            OrderItemSemiRequirement.sales_order_item_bom_component_id
            == snapshot.id,
            OrderItemSemiRequirement.component_type == component,
        )
    else:
        existing_query = existing_query.where(
            OrderItemSemiRequirement.sales_order_item_bom_component_id.is_(None),
            OrderItemSemiRequirement.component_type == component,
        )
    existing = db.scalar(existing_query)
    values = {
        "customer_id": order.customer_id,
        "board_length_mm": board_length_mm,
        "board_width_mm": board_width_mm,
        "material_code_snapshot": material_snapshot,
        "normalized_material_code": normalized_material,
        "flute_type": normalized_flute,
        "pieces_per_box": pieces_per_box,
        "stock_yield_per_sheet": stock_yield_per_sheet,
        "required_piece_quantity": required,
        "sales_order_item_bom_component_id": snapshot.id if snapshot else None,
    }
    if existing is not None:
        unchanged = all(getattr(existing, name) == value for name, value in values.items())
        if unchanged:
            return existing
        if db.scalar(
            select(InventoryReservation.id)
            .where(
                InventoryReservation.semi_requirement_id == existing.id,
                InventoryReservation.reservation_type == "semi_order",
            )
            .limit(1)
        ):
            raise WarehouseInventoryError(
                "该半成品需求已有预占记录，不能改写签名快照", 409
            )
        for name, value in values.items():
            setattr(existing, name, value)
        existing.updated_by = operator_id
        existing.updated_at = utc_now()
        db.flush()
        return existing
    requirement = OrderItemSemiRequirement(
        order_item_id=item.id,
        component_type=component,
        created_by=operator_id,
        **values,
    )
    db.add(requirement)
    db.flush()
    return requirement


def requirement_signature(
    requirement: OrderItemSemiRequirement,
) -> SemiFinishedSignature:
    return SemiFinishedSignature(
        customer_id=requirement.customer_id,
        board_length_mm=requirement.board_length_mm,
        board_width_mm=requirement.board_width_mm,
        normalized_material_code=requirement.normalized_material_code,
        flute_type=requirement.flute_type,
        component_type=requirement.component_type,
        pieces_per_box=requirement.pieces_per_box,
        stock_yield_per_sheet=requirement.stock_yield_per_sheet,
    )


def lot_signature(
    detail: SemiFinishedInventoryDetail,
) -> SemiFinishedSignature | None:
    if detail.owner_customer_id is None:
        return None
    return SemiFinishedSignature(
        customer_id=detail.owner_customer_id,
        board_length_mm=detail.board_length_mm,
        board_width_mm=detail.board_width_mm,
        normalized_material_code=detail.normalized_material_code,
        flute_type=detail.flute_type,
        component_type=detail.component_type,
        pieces_per_box=detail.pieces_per_box,
        stock_yield_per_sheet=detail.stock_yield_per_sheet,
    )


def rule_signature(rule: SemiFinishedMatchRule) -> SemiFinishedSignature:
    return SemiFinishedSignature(
        customer_id=rule.customer_id,
        board_length_mm=rule.board_length_mm,
        board_width_mm=rule.board_width_mm,
        normalized_material_code=rule.normalized_material_code,
        flute_type=rule.flute_type,
        component_type=rule.component_type,
        pieces_per_box=rule.pieces_per_box,
        stock_yield_per_sheet=rule.stock_yield_per_sheet,
    )


def _signature_differences(
    requirement: OrderItemSemiRequirement,
    detail: SemiFinishedInventoryDetail,
) -> tuple[str, ...]:
    return _signature_differences_from_signature(
        requirement_signature(requirement), detail
    )


def _signature_differences_from_signature(
    expected: SemiFinishedSignature,
    detail: SemiFinishedInventoryDetail,
) -> tuple[str, ...]:
    pairs = (
        ("customer", expected.customer_id, detail.owner_customer_id),
        ("board_length_mm", expected.board_length_mm, detail.board_length_mm),
        ("board_width_mm", expected.board_width_mm, detail.board_width_mm),
        (
            "material_code",
            expected.normalized_material_code,
            detail.normalized_material_code,
        ),
        ("flute", expected.flute_type, detail.flute_type),
        ("component", expected.component_type, detail.component_type),
        ("pieces_per_box", expected.pieces_per_box, detail.pieces_per_box),
        (
            "stock_yield_per_sheet",
            expected.stock_yield_per_sheet,
            detail.stock_yield_per_sheet,
        ),
    )
    return tuple(name for name, expected, actual in pairs if expected != actual)


def safe_physical_board_facts_match(
    detail: SemiFinishedInventoryDetail,
    *,
    supplier_name: str | None,
    layer_count: int | None,
    crease_type: str | None,
    crease_left_mm: int | None,
    crease_middle_mm: int | None,
    crease_right_mm: int | None,
) -> bool:
    """Fail closed for no-dialog customer board-preparation reservations."""

    normalized_supplier = " ".join((supplier_name or "").strip().casefold().split())
    actual_supplier = " ".join(
        (detail.supplier_name or "").strip().casefold().split()
    )
    expected_layer = int(layer_count or 0)
    if (
        not normalized_supplier
        or normalized_supplier != actual_supplier
        or expected_layer <= 0
        or int(detail.layer_count or 0) != expected_layer
    ):
        return False
    normalized_crease = {
        "净": "净料",
        "毛": "毛片",
    }.get((crease_type or "").strip(), (crease_type or "").strip())
    expected_sheet_type = (
        "creased_sheet"
        if normalized_crease == "压线"
        else "net_sheet"
        if normalized_crease == "净料"
        else "raw_board"
    )
    if (
        detail.sheet_type != expected_sheet_type
        or (detail.crease_type or "").strip() != normalized_crease
    ):
        return False
    expected_segments = (
        (crease_left_mm, crease_middle_mm, crease_right_mm)
        if normalized_crease == "压线"
        else (None, None, None)
    )
    return (
        detail.crease_left_mm,
        detail.crease_middle_mm,
        detail.crease_right_mm,
    ) == expected_segments


def _physical_signature_differences(
    expected: SemiFinishedSignature,
    detail: SemiFinishedInventoryDetail,
) -> tuple[str, ...]:
    return tuple(
        name
        for name in _signature_differences_from_signature(expected, detail)
        if name != "customer"
    )


def _requirement_product_id(db: Session, requirement: OrderItemSemiRequirement) -> int:
    item = db.get(OrderItem, requirement.order_item_id)
    snapshot = (
        db.get(
            SalesOrderItemBomComponent,
            requirement.sales_order_item_bom_component_id,
        )
        if requirement.sales_order_item_bom_component_id is not None
        else None
    )
    if requirement.sales_order_item_bom_component_id is not None and (
        snapshot is None
        or item is None
        or snapshot.sales_order_item_id != item.id
    ):
        raise WarehouseInventoryError("组件快照不属于当前订单明细", 409)
    product = db.get(
        Product,
        snapshot.component_product_id if snapshot is not None else item.product_id,
    ) if item is not None else None
    if product is None or product.deleted_at is not None:
        raise WarehouseInventoryError("订单产品不存在", 404)
    if product.customer_id != requirement.customer_id:
        raise WarehouseInventoryError("订单产品与半成品需求客户不一致", 409)
    return product.id


def _allowed_lot_ids_for_product(db: Session, product_id: int) -> set[int]:
    return set(
        db.scalars(
            select(SemiFinishedLotAllowedProduct.inventory_lot_id).where(
                SemiFinishedLotAllowedProduct.product_id == product_id
            )
        ).all()
    )


def _lot_eligibility_scope(
    lot: InventoryLot,
    *,
    customer_id: int,
    expected: SemiFinishedSignature,
    allowed_lot_ids: set[int],
) -> str | None:
    detail = lot.semi_finished_detail
    if lot.inventory_type != "semi_finished" or detail is None:
        return None
    if detail.component_type != expected.component_type:
        return None
    if detail.owner_customer_id is None:
        return (
            "general"
            if not _physical_signature_differences(expected, detail)
            else None
        )
    if detail.owner_customer_id != customer_id or lot.id not in allowed_lot_ids:
        return None
    return "dedicated"


def ensure_semi_finished_lot_eligibility(
    db: Session,
    *,
    lot: InventoryLot,
    product_id: int,
    customer_id: int,
    expected: SemiFinishedSignature,
) -> str:
    """Recheck the lot-level hard authorization independently of client data."""

    product = db.get(Product, product_id)
    if product is None or product.deleted_at is not None:
        raise WarehouseInventoryError("订单产品不存在", 404)
    if product.customer_id != customer_id or expected.customer_id != customer_id:
        raise WarehouseInventoryError("订单产品、客户与半成品需求不一致", 409)
    detail = lot.semi_finished_detail
    if lot.inventory_type != "semi_finished" or detail is None:
        raise WarehouseInventoryError("所选批次不是半成品库存", 409)
    if detail.component_type != expected.component_type:
        raise WarehouseInventoryError("半成品库存组件与订单需求不一致", 409)
    if detail.owner_customer_id is None:
        differences = _physical_signature_differences(expected, detail)
        if differences:
            raise WarehouseInventoryError(
                "通用半成品库存物理规格与订单需求不一致："
                + "、".join(differences),
                409,
            )
        return "general"
    if detail.owner_customer_id != customer_id:
        raise WarehouseInventoryError("其他客户专用半成品库存不能用于当前订单", 409)
    if lot.id not in _allowed_lot_ids_for_product(db, product_id):
        raise WarehouseInventoryError(
            "专用半成品库存批次未绑定当前成品款号，不能抵扣", 409
        )
    return "dedicated"


def _learned_rules_for_product(
    db: Session,
    *,
    product_id: int,
    customer_id: int,
    component_type: str,
) -> dict[SemiFinishedSignature, SemiFinishedMatchRule]:
    product = db.get(Product, product_id)
    if product is None or product.deleted_at is not None:
        raise WarehouseInventoryError("产品不存在", 404)
    if product.customer_id != customer_id:
        raise WarehouseInventoryError("产品不属于所选客户", 409)
    rows = db.scalars(
        select(SemiFinishedMatchRule)
        .join(
            SemiFinishedMatchRuleProduct,
            SemiFinishedMatchRuleProduct.rule_id == SemiFinishedMatchRule.id,
        )
        .where(
            SemiFinishedMatchRuleProduct.product_id == product_id,
            SemiFinishedMatchRule.active.is_(True),
            SemiFinishedMatchRule.customer_id == customer_id,
            SemiFinishedMatchRule.component_type == component_type,
        )
        .order_by(SemiFinishedMatchRule.id)
    ).all()
    return {rule_signature(row): row for row in rows}


def semi_finished_inventory_candidates(
    db: Session,
    requirement_id: int,
) -> list[SemiFinishedCandidate]:
    requirement = db.get(OrderItemSemiRequirement, requirement_id)
    if requirement is None:
        raise WarehouseInventoryError("半成品需求不存在", 404)
    expected = requirement_signature(requirement)
    return _semi_finished_candidates_for_signature(
        db,
        product_id=_requirement_product_id(db, requirement),
        expected=expected,
    )


def semi_finished_candidates_for_product(
    db: Session,
    *,
    product_id: int,
    customer_id: int,
    board_length_mm: int,
    board_width_mm: int,
    material_code: str,
    flute_type: str,
    component_type: str,
    pieces_per_box: int,
    stock_yield_per_sheet: int,
) -> list[SemiFinishedCandidate]:
    product = db.get(Product, product_id)
    if product is None or product.deleted_at is not None:
        raise WarehouseInventoryError("产品不存在", 404)
    if product.customer_id != customer_id:
        raise WarehouseInventoryError("产品不属于所选客户", 409)
    if board_length_mm <= 0 or board_width_mm <= 0:
        raise WarehouseInventoryError("半成品实际长宽必须大于0")
    if pieces_per_box <= 0 or stock_yield_per_sheet <= 0:
        raise WarehouseInventoryError("每箱片数和每库存张产出片数必须大于0")
    expected = SemiFinishedSignature(
        customer_id=customer_id,
        board_length_mm=board_length_mm,
        board_width_mm=board_width_mm,
        normalized_material_code=normalize_material_code(material_code),
        flute_type=_flute(flute_type),
        component_type=_component(component_type),
        pieces_per_box=pieces_per_box,
        stock_yield_per_sheet=stock_yield_per_sheet,
    )
    return _semi_finished_candidates_for_signature(
        db, product_id=product.id, expected=expected
    )


def browse_semi_finished_inventory_for_product(
    db: Session,
    *,
    product_id: int,
    customer_id: int,
    board_length_mm: int,
    board_width_mm: int,
    material_code: str,
    flute_type: str,
    component_type: str,
    pieces_per_box: int,
    stock_yield_per_sheet: int,
) -> list[SemiFinishedCandidate]:
    product = db.get(Product, product_id)
    if product is None or product.deleted_at is not None:
        raise WarehouseInventoryError("产品不存在", 404)
    if product.customer_id != customer_id:
        raise WarehouseInventoryError("产品不属于所选客户", 409)
    expected = SemiFinishedSignature(
        customer_id=customer_id,
        board_length_mm=board_length_mm,
        board_width_mm=board_width_mm,
        normalized_material_code=normalize_material_code(material_code),
        flute_type=_flute(flute_type),
        component_type=_component(component_type),
        pieces_per_box=pieces_per_box,
        stock_yield_per_sheet=stock_yield_per_sheet,
    )
    rows = db.scalars(
        select(InventoryLot)
        .join(
            SemiFinishedInventoryDetail,
            SemiFinishedInventoryDetail.inventory_lot_id == InventoryLot.id,
        )
        .where(
            InventoryLot.inventory_type == "semi_finished",
            InventoryLot.status == "active",
            InventoryLot.quantity_available > 0,
            or_(
                SemiFinishedInventoryDetail.owner_customer_id.is_(None),
                SemiFinishedInventoryDetail.owner_customer_id == customer_id,
            ),
            SemiFinishedInventoryDetail.component_type == expected.component_type,
        )
        .order_by(*inventory_fifo_order_columns())
    ).all()
    allowed_lot_ids = _allowed_lot_ids_for_product(db, product.id)
    candidates: list[SemiFinishedCandidate] = []
    for lot in rows:
        detail = lot.semi_finished_detail
        if detail is None:
            continue
        scope = _lot_eligibility_scope(
            lot,
            customer_id=customer_id,
            expected=expected,
            allowed_lot_ids=allowed_lot_ids,
        )
        if scope is None:
            continue
        if scope == "general":
            source = "general_signature"
            differences: tuple[str, ...] = ()
            warning_codes = (MANUAL_CONFIRM_WARNING, GENERAL_SEMI_FINISHED_STOCK)
            warning_messages = (
                "每次半成品库存抵扣都必须人工确认。",
                "该批次为通用半成品库存，可跨客户按物理规格抵扣，必须人工确认。",
            )
        else:
            source = "manual"
            differences = _signature_differences_from_signature(expected, detail)
            warning_codes = (MANUAL_CONFIRM_WARNING, SIGNATURE_OVERRIDE_WARNING)
            warning_messages = (
                "每次半成品库存抵扣都必须人工确认。",
                "人工浏览候选必须核对差异并明确 override。",
            )
        candidates.append(
            SemiFinishedCandidate(
                lot=lot,
                source=source,
                match_rule_id=None,
                available_stock_quantity=lot.quantity_available,
                deductible_requirement_quantity=(
                    lot.quantity_available * detail.stock_yield_per_sheet
                ),
                signature_differences=differences,
                warning_codes=warning_codes,
                warning_messages=warning_messages,
            )
        )
    return candidates


def _semi_finished_candidates_for_signature(
    db: Session,
    *,
    product_id: int,
    expected: SemiFinishedSignature,
) -> list[SemiFinishedCandidate]:
    learned = _learned_rules_for_product(
        db,
        product_id=product_id,
        customer_id=expected.customer_id,
        component_type=expected.component_type,
    )
    rows = db.scalars(
        select(InventoryLot)
        .join(
            SemiFinishedInventoryDetail,
            SemiFinishedInventoryDetail.inventory_lot_id == InventoryLot.id,
        )
        .where(
            InventoryLot.inventory_type == "semi_finished",
            InventoryLot.status == "active",
            InventoryLot.quantity_available > 0,
            or_(
                SemiFinishedInventoryDetail.owner_customer_id.is_(None),
                SemiFinishedInventoryDetail.owner_customer_id
                == expected.customer_id,
            ),
            SemiFinishedInventoryDetail.component_type == expected.component_type,
        )
        .order_by(*inventory_fifo_order_columns())
    ).all()
    allowed_lot_ids = _allowed_lot_ids_for_product(db, product_id)
    candidates: list[SemiFinishedCandidate] = []
    for lot in rows:
        detail = lot.semi_finished_detail
        if detail is None:
            continue
        scope = _lot_eligibility_scope(
            lot,
            customer_id=expected.customer_id,
            expected=expected,
            allowed_lot_ids=allowed_lot_ids,
        )
        if scope is None:
            continue
        if scope == "general":
            candidates.append(
                SemiFinishedCandidate(
                    lot=lot,
                    source="general_signature",
                    match_rule_id=None,
                    available_stock_quantity=lot.quantity_available,
                    deductible_requirement_quantity=(
                        lot.quantity_available * detail.stock_yield_per_sheet
                    ),
                    signature_differences=(),
                    warning_codes=(
                        MANUAL_CONFIRM_WARNING,
                        GENERAL_SEMI_FINISHED_STOCK,
                    ),
                    warning_messages=(
                        "每次半成品库存抵扣都必须人工确认。",
                        "该批次为通用半成品库存，可跨客户按物理规格抵扣，必须人工确认。",
                    ),
                )
            )
            continue
        signature = lot_signature(detail)
        if signature is None:
            continue
        learned_rule = learned.get(signature)
        exact_heuristic = signature == expected
        if learned_rule is not None:
            source = "learned"
            match_rule_id = learned_rule.id
        elif exact_heuristic:
            source = "signature"
            match_rule_id = None
        else:
            continue
        differences = _signature_differences_from_signature(expected, detail)
        warning_codes = [MANUAL_CONFIRM_WARNING]
        warning_messages = ["每次半成品库存抵扣都必须人工确认。"]
        if differences:
            warning_codes.append(SIGNATURE_OVERRIDE_WARNING)
            warning_messages.append(
                "该候选来自已学习的人工 override，差异字段："
                + "、".join(differences)
            )
        candidates.append(
            SemiFinishedCandidate(
                lot=lot,
                source=source,
                match_rule_id=match_rule_id,
                available_stock_quantity=lot.quantity_available,
                deductible_requirement_quantity=(
                    lot.quantity_available * detail.stock_yield_per_sheet
                ),
                signature_differences=differences,
                warning_codes=tuple(warning_codes),
                warning_messages=tuple(warning_messages),
            )
        )
    return candidates


def browse_semi_finished_inventory(
    db: Session,
    requirement_id: int,
) -> list[SemiFinishedCandidate]:
    requirement = db.get(OrderItemSemiRequirement, requirement_id)
    if requirement is None:
        raise WarehouseInventoryError("半成品需求不存在", 404)
    rows = db.scalars(
        select(InventoryLot)
        .join(
            SemiFinishedInventoryDetail,
            SemiFinishedInventoryDetail.inventory_lot_id == InventoryLot.id,
        )
        .where(
            InventoryLot.inventory_type == "semi_finished",
            InventoryLot.status == "active",
            InventoryLot.quantity_available > 0,
            or_(
                SemiFinishedInventoryDetail.owner_customer_id.is_(None),
                SemiFinishedInventoryDetail.owner_customer_id
                == requirement.customer_id,
            ),
            SemiFinishedInventoryDetail.component_type == requirement.component_type,
        )
        .order_by(*inventory_fifo_order_columns())
    ).all()
    product_id = _requirement_product_id(db, requirement)
    expected = requirement_signature(requirement)
    allowed_lot_ids = _allowed_lot_ids_for_product(db, product_id)
    candidates: list[SemiFinishedCandidate] = []
    for lot in rows:
        detail = lot.semi_finished_detail
        if detail is None:
            continue
        scope = _lot_eligibility_scope(
            lot,
            customer_id=requirement.customer_id,
            expected=expected,
            allowed_lot_ids=allowed_lot_ids,
        )
        if scope is None:
            continue
        if scope == "general":
            source = "general_signature"
            differences: tuple[str, ...] = ()
            warning_codes = (MANUAL_CONFIRM_WARNING, GENERAL_SEMI_FINISHED_STOCK)
            warning_messages = (
                "每次半成品库存抵扣都必须人工确认。",
                "该批次为通用半成品库存，可跨客户按物理规格抵扣，必须人工确认。",
            )
        else:
            source = "manual"
            differences = _signature_differences(requirement, detail)
            warning_codes = (MANUAL_CONFIRM_WARNING, SIGNATURE_OVERRIDE_WARNING)
            warning_messages = (
                "每次半成品库存抵扣都必须人工确认。",
                "人工浏览候选必须核对差异并明确 override。",
            )
        candidates.append(
            SemiFinishedCandidate(
                lot=lot,
                source=source,
                match_rule_id=None,
                available_stock_quantity=lot.quantity_available,
                deductible_requirement_quantity=(
                    lot.quantity_available * detail.stock_yield_per_sheet
                ),
                signature_differences=differences,
                warning_codes=warning_codes,
                warning_messages=warning_messages,
            )
        )
    return candidates


def _rule_for_signature(
    db: Session,
    signature: SemiFinishedSignature,
) -> SemiFinishedMatchRule | None:
    return db.scalar(
        select(SemiFinishedMatchRule).where(
            SemiFinishedMatchRule.customer_id == signature.customer_id,
            SemiFinishedMatchRule.board_length_mm == signature.board_length_mm,
            SemiFinishedMatchRule.board_width_mm == signature.board_width_mm,
            SemiFinishedMatchRule.normalized_material_code
            == signature.normalized_material_code,
            SemiFinishedMatchRule.flute_type == signature.flute_type,
            SemiFinishedMatchRule.component_type == signature.component_type,
            SemiFinishedMatchRule.pieces_per_box == signature.pieces_per_box,
            SemiFinishedMatchRule.stock_yield_per_sheet
            == signature.stock_yield_per_sheet,
        )
    )


def _ensure_rule_for_signature(
    db: Session,
    signature: SemiFinishedSignature,
    *,
    operator_id: int | None,
) -> SemiFinishedMatchRule:
    now = utc_now()
    rule = _rule_for_signature(db, signature)
    if rule is None:
        try:
            with db.begin_nested():
                rule = SemiFinishedMatchRule(
                    customer_id=signature.customer_id,
                    board_length_mm=signature.board_length_mm,
                    board_width_mm=signature.board_width_mm,
                    normalized_material_code=signature.normalized_material_code,
                    flute_type=signature.flute_type,
                    component_type=signature.component_type,
                    pieces_per_box=signature.pieces_per_box,
                    stock_yield_per_sheet=signature.stock_yield_per_sheet,
                    active=True,
                    created_by=operator_id,
                )
                db.add(rule)
                db.flush()
        except IntegrityError:
            rule = _rule_for_signature(db, signature)
            if rule is None:
                raise WarehouseInventoryError("学习规则并发保存失败，请重试", 409)
    elif not rule.active:
        rule.active = True
        rule.updated_by = operator_id
        rule.updated_at = now
    return rule


def semi_finished_lot_assigned_product_ids(
    db: Session,
    inventory_lot_id: int,
) -> tuple[int, ...]:
    lot = db.get(InventoryLot, inventory_lot_id)
    if lot is None or lot.inventory_type != "semi_finished":
        raise WarehouseInventoryError("半成品库存批次不存在", 404)
    return semi_finished_lot_allowed_product_ids(db, inventory_lot_id)


def replace_semi_finished_lot_product_assignments(
    db: Session,
    *,
    inventory_lot_id: int,
    product_ids: list[int],
    operator_id: int | None,
) -> tuple[SemiFinishedMatchRule | None, tuple[Product, ...]]:
    lot = db.get(InventoryLot, inventory_lot_id)
    if lot is None or lot.inventory_type != "semi_finished":
        raise WarehouseInventoryError("半成品库存批次不存在", 404)
    detail = lot.semi_finished_detail
    if detail is None or detail.owner_customer_id is None:
        raise WarehouseInventoryError("请先为半成品库存指定归属客户", 409)
    unique_ids = tuple(dict.fromkeys(int(value) for value in product_ids))
    products = tuple(
        db.scalars(
            select(Product)
            .where(
                Product.id.in_(unique_ids),
                Product.customer_id == detail.owner_customer_id,
                Product.is_active.is_(True),
                Product.deleted_at.is_(None),
            )
            .order_by(Product.product_code, Product.id)
        ).all()
    ) if unique_ids else ()
    if len(products) != len(unique_ids):
        raise WarehouseInventoryError(
            "半成品库存只能分配给同一客户的有效成品款号", 409
        )
    refreshed = replace_semi_finished_lot_allowed_products(
        db,
        inventory_lot_id=lot.id,
        product_ids=list(unique_ids),
        expected_version=lot.version,
        operator_id=operator_id,
    )
    refreshed_detail = refreshed.semi_finished_detail
    signature = lot_signature(refreshed_detail) if refreshed_detail is not None else None
    return (_rule_for_signature(db, signature) if signature is not None else None), products


def confirm_semi_finished_match(
    db: Session,
    *,
    requirement_id: int,
    inventory_lot_id: int,
    operator_id: int | None,
    override: bool,
    warning_acknowledged_codes: list[str],
) -> SemiFinishedMatchConfirmation:
    requirement = db.get(OrderItemSemiRequirement, requirement_id)
    if requirement is None:
        raise WarehouseInventoryError("半成品需求不存在", 404)
    product_id = _requirement_product_id(db, requirement)
    lot = db.get(InventoryLot, inventory_lot_id)
    if lot is None or lot.semi_finished_detail is None:
        raise WarehouseInventoryError("半成品库存批次不存在", 404)
    if lot.inventory_type != "semi_finished" or lot.status != "active":
        raise WarehouseInventoryError("该半成品库存批次当前不可匹配", 409)
    detail = lot.semi_finished_detail
    scope = ensure_semi_finished_lot_eligibility(
        db,
        lot=lot,
        product_id=product_id,
        customer_id=requirement.customer_id,
        expected=requirement_signature(requirement),
    )
    differences = (
        _physical_signature_differences(requirement_signature(requirement), detail)
        if scope == "general"
        else _signature_differences(requirement, detail)
    )
    if (
        scope == "general"
        and GENERAL_SEMI_FINISHED_STOCK not in warning_acknowledged_codes
    ):
        raise WarehouseInventoryError(
            "通用半成品库存跨客户抵扣必须确认 GENERAL_SEMI_FINISHED_STOCK 警告",
            409,
        )
    if differences:
        if not override:
            raise WarehouseInventoryError(
                "库存签名与需求存在差异，必须明确 override 后确认："
                + "、".join(differences),
                409,
            )
        if SIGNATURE_OVERRIDE_WARNING not in warning_acknowledged_codes:
            raise WarehouseInventoryError(
                "人工 override 必须确认半成品签名差异警告", 409
            )
    signature = (
        requirement_signature(requirement)
        if scope == "general"
        else lot_signature(detail)
    )
    if signature is None:
        raise WarehouseInventoryError("库存缺少客户签名，不能保存学习规则", 409)
    now = utc_now()
    rule = _ensure_rule_for_signature(db, signature, operator_id=operator_id)
    mapping = db.scalar(
        select(SemiFinishedMatchRuleProduct).where(
            SemiFinishedMatchRuleProduct.rule_id == rule.id,
            SemiFinishedMatchRuleProduct.product_id == product_id,
        )
    )
    if mapping is None:
        try:
            with db.begin_nested():
                mapping = SemiFinishedMatchRuleProduct(
                    rule_id=rule.id,
                    product_id=product_id,
                    confirmed_by=operator_id,
                    confirmed_at=now,
                )
                db.add(mapping)
                db.flush()
        except IntegrityError:
            mapping = db.scalar(
                select(SemiFinishedMatchRuleProduct).where(
                    SemiFinishedMatchRuleProduct.rule_id == rule.id,
                    SemiFinishedMatchRuleProduct.product_id == product_id,
                )
            )
            if mapping is None:
                raise WarehouseInventoryError("规则产品关联并发保存失败，请重试", 409)
    db.flush()
    warning_codes: list[str] = []
    if scope == "general":
        warning_codes.append(GENERAL_SEMI_FINISHED_STOCK)
    if differences:
        warning_codes.append(SIGNATURE_OVERRIDE_WARNING)
    return SemiFinishedMatchConfirmation(
        rule=rule,
        mapping=mapping,
        signature_differences=differences,
        warning_codes=tuple(warning_codes),
    )


def active_semi_requirement_credited_quantity(
    db: Session,
    requirement_id: int,
) -> int:
    rows = db.scalars(
        select(InventoryReservation).where(
            InventoryReservation.semi_requirement_id == requirement_id,
            InventoryReservation.reservation_type == "semi_order",
            InventoryReservation.status != "cancelled",
        )
    ).all()
    total = 0
    for row in rows:
        credited = int(row.credited_requirement_quantity or 0)
        total += max(
            credited - int(row.released_requirement_quantity or 0), 0
        )
    return total


def active_semi_coverage_by_order_item(
    db: Session,
    order_item_id: int,
) -> dict[str, int]:
    requirements = db.scalars(
        select(OrderItemSemiRequirement).where(
            OrderItemSemiRequirement.order_item_id == order_item_id
        )
    ).all()
    return {
        requirement.component_type: active_semi_requirement_credited_quantity(
            db, requirement.id
        )
        for requirement in requirements
    }


def active_semi_reserved_piece_qty(
    db: Session,
    *,
    order_item_id: int,
    component_type: str,
) -> int:
    requirement = db.scalar(
        select(OrderItemSemiRequirement).where(
            OrderItemSemiRequirement.order_item_id == order_item_id,
            OrderItemSemiRequirement.component_type == _component(component_type),
        )
    )
    if requirement is None:
        return 0
    return active_semi_requirement_credited_quantity(db, requirement.id)


def inventory_fully_covers_order_item(db: Session, order_item_id: int) -> bool:
    item = db.get(OrderItem, order_item_id)
    if item is None:
        raise WarehouseInventoryError("订单明细不存在", 404)
    finished_coverage = active_finished_reserved_qty(db, item.id)
    if finished_coverage >= int(item.quantity or 0):
        return True
    production_boxes = max(int(item.quantity or 0) - finished_coverage, 0)
    requirements = db.scalars(
        select(OrderItemSemiRequirement).where(
            OrderItemSemiRequirement.order_item_id == item.id
        )
    ).all()
    product = db.get(Product, item.product_id)
    box_style = str(product.box_style or "") if product else ""
    expected_components = (
        {"cover", "base"}
        if "天地盖" in box_style or "A3" in box_style.upper()
        else {"whole"}
    )
    by_component = {row.component_type: row for row in requirements}
    if not expected_components.issubset(by_component):
        return False
    for component in expected_components:
        requirement = by_component[component]
        expected_pieces = production_boxes * max(
            int(requirement.pieces_per_box or 1), 1
        )
        if active_semi_requirement_credited_quantity(db, requirement.id) < expected_pieces:
            return False
    return True


def _finished_reservations_for_delivery(
    db: Session,
    order_item_id: int,
    reservation_type: str = "finished_order",
) -> list[InventoryReservation]:
    return db.scalars(
        select(InventoryReservation)
        .join(InventoryLot, InventoryLot.id == InventoryReservation.inventory_lot_id)
        .where(
            InventoryReservation.order_item_id == order_item_id,
            InventoryReservation.reservation_type == reservation_type,
            InventoryReservation.status != "cancelled",
            InventoryReservation.reserved_stock_quantity
            > InventoryReservation.consumed_stock_quantity
            + InventoryReservation.released_stock_quantity,
        )
        .order_by(
            *inventory_fifo_order_columns(),
            InventoryReservation.id,
        )
    ).all()


def _semi_reservations_for_delivery(
    db: Session,
    requirement_id: int,
) -> list[InventoryReservation]:
    return db.scalars(
        select(InventoryReservation)
        .join(InventoryLot, InventoryLot.id == InventoryReservation.inventory_lot_id)
        .where(
            InventoryReservation.semi_requirement_id == requirement_id,
            InventoryReservation.reservation_type == "semi_order",
            InventoryReservation.status != "cancelled",
            InventoryReservation.reserved_stock_quantity
            > InventoryReservation.consumed_stock_quantity
            + InventoryReservation.released_stock_quantity,
        )
        .order_by(
            *inventory_fifo_order_columns(),
            InventoryReservation.id,
        )
    ).all()


def consume_delivery_item_inventory(
    db: Session,
    *,
    delivery_item_id: int,
    delivered_quantity_after_dispatch: int,
    operator_id: int | None,
    operation_key: str,
) -> None:
    delivery_item = db.get(DeliveryItem, delivery_item_id)
    if delivery_item is None:
        raise WarehouseInventoryError("送货明细不存在", 404)
    item = db.get(OrderItem, delivery_item.order_item_id)
    if item is None:
        raise WarehouseInventoryError("送货明细关联订单不存在", 409)
    target_delivered = max(int(delivered_quantity_after_dispatch), 0)
    ordered_quantity = max(int(item.quantity or 0), 0)
    target_order_delivery = min(target_delivered, ordered_quantity)
    finished_coverage = active_finished_reserved_qty(db, item.id)
    target_finished = min(target_order_delivery, finished_coverage)
    current_finished = int(
        db.scalar(
            select(func.coalesce(func.sum(InventoryReservation.consumed_stock_quantity), 0))
            .where(
                InventoryReservation.order_item_id == item.id,
                InventoryReservation.reservation_type == "finished_order",
                InventoryReservation.status != "cancelled",
            )
        )
        or 0
    )
    remaining_finished = max(target_finished - current_finished, 0)
    for reservation in _finished_reservations_for_delivery(db, item.id):
        if remaining_finished <= 0:
            break
        available = (
            int(reservation.reserved_stock_quantity)
            - int(reservation.consumed_stock_quantity or 0)
            - int(reservation.released_stock_quantity or 0)
        )
        quantity = min(available, remaining_finished)
        if quantity <= 0:
            continue
        lot = db.get(InventoryLot, reservation.inventory_lot_id)
        if lot is None:
            raise WarehouseInventoryError("成品库存批次不存在", 409)
        consume_finished_reservation(
            db,
            reservation_id=reservation.id,
            stock_quantity=quantity,
            expected_version=lot.version,
            operator_id=operator_id,
            idempotency_key=f"{operation_key}-f-{reservation.id}",
            delivery_item_id=delivery_item.id,
        )
        remaining_finished -= quantity
    if remaining_finished > 0:
        raise WarehouseInventoryError("成品库存预占余额不足，无法完成发货", 409)

    target_surplus = max(target_delivered - ordered_quantity, 0)
    current_surplus = int(
        db.scalar(
            select(func.coalesce(func.sum(InventoryReservation.consumed_stock_quantity), 0))
            .where(
                InventoryReservation.order_item_id == item.id,
                InventoryReservation.reservation_type == "finished_surplus_delivery",
                InventoryReservation.status != "cancelled",
            )
        )
        or 0
    )
    remaining_surplus = max(target_surplus - current_surplus, 0)
    if remaining_surplus > 0:
        from app.services.warehouse_inventory import (
            reserve_finished_surplus_for_delivery,
        )

        reserve_finished_surplus_for_delivery(
            db,
            order_item_id=item.id,
            quantity=remaining_surplus,
            operator_id=operator_id,
            operation_key=operation_key,
        )
    for reservation in _finished_reservations_for_delivery(
        db,
        item.id,
        "finished_surplus_delivery",
    ):
        if remaining_surplus <= 0:
            break
        available = (
            int(reservation.reserved_stock_quantity)
            - int(reservation.consumed_stock_quantity or 0)
            - int(reservation.released_stock_quantity or 0)
        )
        quantity = min(available, remaining_surplus)
        if quantity <= 0:
            continue
        lot = db.get(InventoryLot, reservation.inventory_lot_id)
        if lot is None:
            raise WarehouseInventoryError("客户专用余货批次不存在", 409)
        consume_finished_reservation(
            db,
            reservation_id=reservation.id,
            stock_quantity=quantity,
            expected_version=lot.version,
            operator_id=operator_id,
            idempotency_key=f"{operation_key}-o-{reservation.id}",
            delivery_item_id=delivery_item.id,
        )
        remaining_surplus -= quantity
    if remaining_surplus > 0:
        raise WarehouseInventoryError("客户专用余货不足，无法完成超量送货", 409)

    # N029 consumes semi-finished reservations when production is completed.
    # Delivery must still consume finished reservations, but must not consume
    # the same semi-finished reservations a second time.
    from app.services.production_workflow import has_production_completion_facts

    if has_production_completion_facts(db, [item.id]):
        return

    semi_boxes = max(target_order_delivery - finished_coverage, 0)
    requirements = db.scalars(
        select(OrderItemSemiRequirement)
        .where(OrderItemSemiRequirement.order_item_id == item.id)
        .order_by(OrderItemSemiRequirement.component_type, OrderItemSemiRequirement.id)
    ).all()
    for requirement in requirements:
        coverage = active_semi_requirement_credited_quantity(db, requirement.id)
        target_pieces = min(
            semi_boxes * max(int(requirement.pieces_per_box or 1), 1),
            coverage,
        )
        current_pieces = int(
            db.scalar(
                select(
                    func.coalesce(
                        func.sum(InventoryReservation.consumed_requirement_quantity),
                        0,
                    )
                ).where(
                    InventoryReservation.semi_requirement_id == requirement.id,
                    InventoryReservation.reservation_type == "semi_order",
                    InventoryReservation.status != "cancelled",
                )
            )
            or 0
        )
        for reservation in _semi_reservations_for_delivery(db, requirement.id):
            if current_pieces >= target_pieces:
                break
            yield_factor = max(int(reservation.yield_factor or 1), 1)
            available_stock = (
                int(reservation.reserved_stock_quantity)
                - int(reservation.consumed_stock_quantity or 0)
                - int(reservation.released_stock_quantity or 0)
            )
            needed_stock = ceil((target_pieces - current_pieces) / yield_factor)
            stock_quantity = min(available_stock, needed_stock)
            if stock_quantity <= 0:
                continue
            lot = db.get(InventoryLot, reservation.inventory_lot_id)
            if lot is None:
                raise WarehouseInventoryError("半成品库存批次不存在", 409)
            before_credit = int(reservation.consumed_requirement_quantity or 0)
            mutation = consume_semi_finished_reservation(
                db,
                reservation_id=reservation.id,
                stock_quantity=stock_quantity,
                expected_version=lot.version,
                operator_id=operator_id,
                idempotency_key=f"{operation_key}-s-{reservation.id}",
                delivery_item_id=delivery_item.id,
            )
            current_pieces += (
                int(mutation.reservation.consumed_requirement_quantity or 0)
                - before_credit
            )
        if current_pieces < target_pieces:
            raise WarehouseInventoryError(
                f"{requirement.component_type}半成品预占余额不足，无法完成发货",
                409,
            )


def _active_delivery_allocations(
    db: Session,
    *,
    order_item_id: int,
    reservation_type: str,
    current_delivery_item_id: int,
    requirement_id: int | None = None,
) -> list[DeliveryInventoryAllocation]:
    query = (
        select(DeliveryInventoryAllocation)
        .join(
            InventoryReservation,
            InventoryReservation.id == DeliveryInventoryAllocation.reservation_id,
        )
        .where(
            InventoryReservation.order_item_id == order_item_id,
            InventoryReservation.reservation_type == reservation_type,
            DeliveryInventoryAllocation.status != "reversed",
        )
    )
    if requirement_id is not None:
        query = query.where(
            InventoryReservation.semi_requirement_id == requirement_id
        )
    rows = db.scalars(query).all()
    return sorted(
        rows,
        key=lambda row: (
            row.delivery_item_id != current_delivery_item_id,
            -row.id,
        ),
    )


def reverse_delivery_item_inventory(
    db: Session,
    *,
    delivery_item_id: int,
    delivered_quantity_after_cancel: int,
    operator_id: int | None,
    operation_key: str,
) -> None:
    delivery_item = db.get(DeliveryItem, delivery_item_id)
    if delivery_item is None:
        raise WarehouseInventoryError("送货明细不存在", 404)
    item = db.get(OrderItem, delivery_item.order_item_id)
    if item is None:
        raise WarehouseInventoryError("送货明细关联订单不存在", 409)
    target_delivered = max(int(delivered_quantity_after_cancel), 0)
    ordered_quantity = max(int(item.quantity or 0), 0)
    target_order_delivery = min(target_delivered, ordered_quantity)
    finished_coverage = active_finished_reserved_qty(db, item.id)
    target_finished = min(target_order_delivery, finished_coverage)
    current_finished = int(
        db.scalar(
            select(func.coalesce(func.sum(InventoryReservation.consumed_stock_quantity), 0))
            .where(
                InventoryReservation.order_item_id == item.id,
                InventoryReservation.reservation_type == "finished_order",
                InventoryReservation.status != "cancelled",
            )
        )
        or 0
    )
    excess_finished = max(current_finished - target_finished, 0)
    for allocation in _active_delivery_allocations(
        db,
        order_item_id=item.id,
        reservation_type="finished_order",
        current_delivery_item_id=delivery_item.id,
    ):
        if excess_finished <= 0:
            break
        active_stock = (
            int(allocation.consumed_stock_quantity)
            - int(allocation.reversed_stock_quantity or 0)
        )
        quantity = min(active_stock, excess_finished)
        if quantity <= 0:
            continue
        reservation = db.get(InventoryReservation, allocation.reservation_id)
        lot = db.get(InventoryLot, reservation.inventory_lot_id) if reservation else None
        if reservation is None or lot is None:
            raise WarehouseInventoryError("成品送货库存分配关联异常", 409)
        reverse_finished_consumption(
            db,
            reservation_id=reservation.id,
            stock_quantity=quantity,
            expected_version=lot.version,
            operator_id=operator_id,
            idempotency_key=f"{operation_key}-f-{allocation.id}",
            allocation_id=allocation.id,
        )
        excess_finished -= quantity
    if excess_finished > 0:
        raise WarehouseInventoryError("成品送货消耗记录不足，无法取消发货", 409)

    target_surplus = max(target_delivered - ordered_quantity, 0)
    current_surplus = int(
        db.scalar(
            select(func.coalesce(func.sum(InventoryReservation.consumed_stock_quantity), 0))
            .where(
                InventoryReservation.order_item_id == item.id,
                InventoryReservation.reservation_type == "finished_surplus_delivery",
                InventoryReservation.status != "cancelled",
            )
        )
        or 0
    )
    excess_surplus = max(current_surplus - target_surplus, 0)
    for allocation in _active_delivery_allocations(
        db,
        order_item_id=item.id,
        reservation_type="finished_surplus_delivery",
        current_delivery_item_id=delivery_item.id,
    ):
        if excess_surplus <= 0:
            break
        active_stock = (
            int(allocation.consumed_stock_quantity)
            - int(allocation.reversed_stock_quantity or 0)
        )
        quantity = min(active_stock, excess_surplus)
        if quantity <= 0:
            continue
        reservation = db.get(InventoryReservation, allocation.reservation_id)
        lot = db.get(InventoryLot, reservation.inventory_lot_id) if reservation else None
        if reservation is None or lot is None:
            raise WarehouseInventoryError("余货送货库存分配关联异常", 409)
        reverse_finished_consumption(
            db,
            reservation_id=reservation.id,
            stock_quantity=quantity,
            expected_version=lot.version,
            operator_id=operator_id,
            idempotency_key=f"{operation_key}-o-{allocation.id}",
            allocation_id=allocation.id,
        )
        excess_surplus -= quantity
    if excess_surplus > 0:
        raise WarehouseInventoryError("余货送货消耗记录不足，无法取消发货", 409)

    # Production-time semi-finished consumption has no delivery allocation and
    # is a completion fact, so cancelling a delivery must not reverse it.
    from app.services.production_workflow import has_production_completion_facts

    if has_production_completion_facts(db, [item.id]):
        return

    semi_boxes = max(target_order_delivery - finished_coverage, 0)
    requirements = db.scalars(
        select(OrderItemSemiRequirement)
        .where(OrderItemSemiRequirement.order_item_id == item.id)
        .order_by(OrderItemSemiRequirement.component_type, OrderItemSemiRequirement.id)
    ).all()
    for requirement in requirements:
        coverage = active_semi_requirement_credited_quantity(db, requirement.id)
        target_pieces = min(
            semi_boxes * max(int(requirement.pieces_per_box or 1), 1),
            coverage,
        )
        current_pieces = int(
            db.scalar(
                select(
                    func.coalesce(
                        func.sum(InventoryReservation.consumed_requirement_quantity),
                        0,
                    )
                ).where(
                    InventoryReservation.semi_requirement_id == requirement.id,
                    InventoryReservation.reservation_type == "semi_order",
                    InventoryReservation.status != "cancelled",
                )
            )
            or 0
        )
        for allocation in _active_delivery_allocations(
            db,
            order_item_id=item.id,
            reservation_type="semi_order",
            current_delivery_item_id=delivery_item.id,
            requirement_id=requirement.id,
        ):
            allowed_credit = current_pieces - target_pieces
            if allowed_credit <= 0:
                break
            reservation = db.get(InventoryReservation, allocation.reservation_id)
            if reservation is None:
                raise WarehouseInventoryError("半成品送货库存分配关联异常", 409)
            yield_factor = max(int(reservation.yield_factor or 1), 1)
            active_stock = (
                int(allocation.consumed_stock_quantity)
                - int(allocation.reversed_stock_quantity or 0)
            )
            active_credit = (
                int(allocation.credited_requirement_quantity)
                - int(allocation.reversed_requirement_quantity or 0)
            )
            stock_quantity = 0
            reverse_credit = 0
            for candidate_stock in range(active_stock, 0, -1):
                candidate_credit = active_credit - min(
                    active_credit,
                    (active_stock - candidate_stock) * yield_factor,
                )
                if candidate_credit <= allowed_credit:
                    stock_quantity = candidate_stock
                    reverse_credit = candidate_credit
                    break
            if stock_quantity <= 0:
                continue
            lot = db.get(InventoryLot, reservation.inventory_lot_id)
            if lot is None:
                raise WarehouseInventoryError("半成品库存批次不存在", 409)
            reverse_semi_finished_consumption(
                db,
                reservation_id=reservation.id,
                stock_quantity=stock_quantity,
                expected_version=lot.version,
                operator_id=operator_id,
                idempotency_key=f"{operation_key}-s-{allocation.id}",
                allocation_id=allocation.id,
            )
            current_pieces -= reverse_credit


def _existing_reservation_batch(
    db: Session,
    *,
    requirement_id: int,
    idempotency_key: str,
    requested_requirement_quantity: int,
) -> SemiFinishedReservationBatch | None:
    rows = db.scalars(
        select(InventoryReservation)
        .where(InventoryReservation.reservation_group_key == idempotency_key)
        .order_by(InventoryReservation.inventory_lot_id)
    ).all()
    if not rows:
        conflicting = db.scalar(
            select(InventoryReservation).where(
                InventoryReservation.idempotency_key == idempotency_key
            )
        )
        if conflicting is not None:
            raise WarehouseInventoryError("该请求标识已用于其他库存预占", 409)
        return None
    if any(
        row.semi_requirement_id != requirement_id
        or row.reservation_group_requested_quantity
        != requested_requirement_quantity
        for row in rows
    ):
        raise WarehouseInventoryError("该请求标识已用于其他半成品预占", 409)
    allocated = sum(int(row.credited_requirement_quantity or 0) for row in rows)
    return SemiFinishedReservationBatch(
        reservations=tuple(rows),
        requested_requirement_quantity=requested_requirement_quantity,
        allocated_requirement_quantity=allocated,
        unallocated_requirement_quantity=max(
            requested_requirement_quantity - allocated, 0
        ),
    )


def reserve_semi_finished_inventory(
    db: Session,
    *,
    requirement_id: int,
    requested_requirement_quantity: int,
    lots: list[SemiFinishedLotVersion],
    operator_id: int | None,
    idempotency_key: str,
    confirmed: bool,
    override: bool = False,
    warning_acknowledged_codes: list[str] | None = None,
) -> SemiFinishedReservationBatch:
    if not confirmed:
        raise WarehouseInventoryError("半成品库存抵扣必须人工确认", 409)
    if requested_requirement_quantity <= 0:
        raise WarehouseInventoryError("本次抵扣需求片数必须大于0")
    if not idempotency_key or len(idempotency_key) > 80:
        raise WarehouseInventoryError("请求标识长度必须为1到80个字符")
    existing = _existing_reservation_batch(
        db,
        requirement_id=requirement_id,
        idempotency_key=idempotency_key,
        requested_requirement_quantity=requested_requirement_quantity,
    )
    if existing is not None:
        return existing
    if not lots:
        raise WarehouseInventoryError("至少选择一个半成品库存批次")
    expected_versions = {row.lot_id: row.expected_version for row in lots}
    if len(expected_versions) != len(lots):
        raise WarehouseInventoryError("同一库存批次不能重复选择")
    if any(version <= 0 for version in expected_versions.values()):
        raise WarehouseInventoryError("库存版本必须大于0")
    warnings = warning_acknowledged_codes or []
    reservations: list[InventoryReservation] = []
    allocated = 0
    with db.begin_nested():
        requirement = db.get(OrderItemSemiRequirement, requirement_id)
        if requirement is None:
            raise WarehouseInventoryError("半成品需求不存在", 404)
        item = db.get(OrderItem, requirement.order_item_id)
        if item is None:
            raise WarehouseInventoryError("订单明细不存在", 404)
        from app.services.production_workflow import (
            ProductionWorkflowError,
            has_production_completion_facts,
            lock_order_rows_for_production_transition,
        )

        try:
            lock_order_rows_for_production_transition(db, [item.order_id])
        except ProductionWorkflowError as error:
            raise WarehouseInventoryError(str(error), error.status_code) from error
        requirement = db.scalar(
            select(OrderItemSemiRequirement)
            .where(OrderItemSemiRequirement.id == requirement_id)
            .execution_options(populate_existing=True)
        )
        if requirement is None:
            raise WarehouseInventoryError("半成品需求已被删除，请刷新后重试", 409)

        if has_production_completion_facts(db, [requirement.order_item_id]):
            raise WarehouseInventoryError(
                "该订单明细已有生产完工事实，不能新增半成品预占", 409
            )
        if requirement.sales_order_item_bom_component_id is not None:
            snapshot = db.get(
                SalesOrderItemBomComponent,
                requirement.sales_order_item_bom_component_id,
            )
            if snapshot is None or snapshot.sales_order_item_id != item.id:
                raise WarehouseInventoryError(
                    "组件库存预占关联已失效，请刷新订单后重试", 409
                )
            coverage = component_inventory_coverage(
                db,
                snapshot.id,
                component_type=requirement.component_type,
            )
            remaining_requirement = max(
                int(requirement.required_piece_quantity)
                - int(coverage["total_piece_quantity"]),
                0,
            )
        else:
            already_credited = active_semi_requirement_credited_quantity(
                db, requirement.id
            )
            remaining_requirement = max(
                requirement.required_piece_quantity - already_credited, 0
            )
        target = min(requested_requirement_quantity, remaining_requirement)
        if target <= 0:
            raise WarehouseInventoryError("该半成品需求已全部抵扣", 409)
        inventory_lots = db.scalars(
            select(InventoryLot)
            .where(InventoryLot.id.in_(expected_versions))
            .order_by(*inventory_fifo_order_columns())
        ).all()
        if len(inventory_lots) != len(expected_versions):
            raise WarehouseInventoryError("所选半成品库存批次不存在", 404)
        for lot in inventory_lots:
            if lot.version != expected_versions[lot.id]:
                raise WarehouseInventoryError(
                    "库存已被其他人修改，请刷新候选后重试", 409
                )
            if (
                lot.inventory_type != "semi_finished"
                or lot.status != "active"
                or lot.semi_finished_detail is None
            ):
                raise WarehouseInventoryError("所选半成品库存批次当前不可预占", 409)
            if lot.quantity_available <= 0:
                continue
            confirmation = confirm_semi_finished_match(
                db,
                requirement_id=requirement.id,
                inventory_lot_id=lot.id,
                operator_id=operator_id,
                override=override,
                warning_acknowledged_codes=warnings,
            )
            detail = lot.semi_finished_detail
            remaining_target = target - allocated
            capacity = lot.quantity_available * detail.stock_yield_per_sheet
            credited = min(remaining_target, capacity)
            if credited <= 0:
                break
            stock_quantity = ceil(credited / detail.stock_yield_per_sheet)
            before = _balances(lot)
            now = utc_now()
            result = db.execute(
                update(InventoryLot)
                .where(
                    InventoryLot.id == lot.id,
                    InventoryLot.version == expected_versions[lot.id],
                    InventoryLot.inventory_type == "semi_finished",
                    InventoryLot.status == "active",
                    InventoryLot.quantity_available >= stock_quantity,
                )
                .values(
                    quantity_available=InventoryLot.quantity_available
                    - stock_quantity,
                    quantity_reserved=InventoryLot.quantity_reserved + stock_quantity,
                    version=InventoryLot.version + 1,
                    last_movement_at=now,
                )
            )
            if result.rowcount != 1:
                raise WarehouseInventoryError(
                    "库存数量或版本已变化，请刷新候选后重试", 409
                )
            row_key = (
                idempotency_key
                if not reservations
                else f"{idempotency_key}:{lot.id}"
            )
            reservation = InventoryReservation(
                reservation_number=_number("SRS"),
                inventory_lot_id=lot.id,
                reservation_type="semi_order",
                order_id=db.scalar(
                    select(OrderItem.order_id).where(
                        OrderItem.id == requirement.order_item_id
                    )
                ),
                order_item_id=requirement.order_item_id,
                semi_requirement_id=requirement.id,
                sales_order_item_bom_component_id=(
                    requirement.sales_order_item_bom_component_id
                ),
                match_rule_id=confirmation.rule.id,
                reserved_stock_quantity=stock_quantity,
                credited_requirement_quantity=credited,
                yield_factor=detail.stock_yield_per_sheet,
                consumed_stock_quantity=0,
                released_stock_quantity=0,
                consumed_requirement_quantity=0,
                released_requirement_quantity=0,
                status="active",
                warning_codes=json.dumps(
                    list(confirmation.warning_codes), ensure_ascii=False
                ),
                warning_acknowledged_by=(
                    operator_id if confirmation.warning_codes else None
                ),
                reserved_by=operator_id,
                reserved_at=now,
                reservation_group_key=idempotency_key,
                reservation_group_requested_quantity=requested_requirement_quantity,
                idempotency_key=row_key,
            )
            db.add(reservation)
            db.flush()
            db.expire(lot)
            refreshed_lot = db.get(InventoryLot, lot.id)
            assert refreshed_lot is not None
            _movement(
                db,
                lot=refreshed_lot,
                movement_type="reserve",
                quantity=stock_quantity,
                before=before,
                operator_id=operator_id,
                reason="半成品共享库存抵扣订单需求",
                idempotency_key=row_key,
                reservation_id=reservation.id,
                related_order_id=reservation.order_id,
                related_order_item_id=requirement.order_item_id,
            )
            reservations.append(reservation)
            allocated += credited
            if allocated >= target:
                break
        if not reservations:
            raise WarehouseInventoryError("所选库存当前没有可预占数量", 409)
        db.flush()
    from app.services.production_workflow import refresh_existing_production_task

    refresh_existing_production_task(db, requirement.order_item_id)
    return SemiFinishedReservationBatch(
        reservations=tuple(reservations),
        requested_requirement_quantity=requested_requirement_quantity,
        allocated_requirement_quantity=allocated,
        unallocated_requirement_quantity=max(
            requested_requirement_quantity - allocated, 0
        ),
    )


def _reservation_status(reservation: InventoryReservation) -> str:
    consumed = int(reservation.consumed_stock_quantity or 0)
    released = int(reservation.released_stock_quantity or 0)
    total = reservation.reserved_stock_quantity
    remaining = total - consumed - released
    if remaining > 0:
        return "active" if consumed == 0 and released == 0 else "partial"
    if consumed == total:
        return "consumed"
    if released == total:
        return "released"
    return "partial"


def _idempotent_mutation(
    db: Session,
    *,
    idempotency_key: str,
    movement_type: str,
    reservation_id: int,
) -> SemiFinishedReservationMutation | None:
    movement = db.scalar(
        select(InventoryMovement).where(
            InventoryMovement.idempotency_key == idempotency_key
        )
    )
    if movement is None:
        return None
    if movement.movement_type != movement_type or movement.reservation_id != reservation_id:
        raise WarehouseInventoryError("该请求标识已用于其他库存操作", 409)
    reservation = db.get(InventoryReservation, reservation_id)
    if reservation is None:
        raise WarehouseInventoryError("库存预占记录不存在", 404)
    allocation = db.scalar(
        select(DeliveryInventoryAllocation).where(
            DeliveryInventoryAllocation.consume_movement_id
            == (
                movement.reversal_of_movement_id
                if movement_type == "reverse_consume"
                else movement.id
            )
        )
    )
    return SemiFinishedReservationMutation(reservation, movement, allocation)


def release_semi_finished_reservation(
    db: Session,
    *,
    reservation_id: int,
    expected_version: int,
    operator_id: int | None,
    release_reason: str | None,
    idempotency_key: str,
    stock_quantity: int | None = None,
) -> SemiFinishedReservationMutation:
    repeated = _idempotent_mutation(
        db,
        idempotency_key=idempotency_key,
        movement_type="release_reserve",
        reservation_id=reservation_id,
    )
    if repeated is not None:
        return repeated
    reason = (release_reason or "").strip() or "释放半成品库存预占（系统记录）"
    with db.begin_nested():
        reservation = db.get(InventoryReservation, reservation_id)
        if reservation is None:
            raise WarehouseInventoryError("库存预占记录不存在", 404)
        if reservation.reservation_type != "semi_order":
            raise WarehouseInventoryError("该记录不是半成品订单预占")
        from app.services.production_workflow import has_production_completion_facts

        if reservation.order_item_id is not None and has_production_completion_facts(
            db, [reservation.order_item_id]
        ):
            raise WarehouseInventoryError(
                "该订单明细已有生产完工事实，不能释放半成品预占", 409
            )
        remaining = (
            reservation.reserved_stock_quantity
            - reservation.consumed_stock_quantity
            - reservation.released_stock_quantity
        )
        quantity = remaining if stock_quantity is None else stock_quantity
        if quantity <= 0 or quantity > remaining:
            raise WarehouseInventoryError("释放数量不能超过未消耗预占余额", 409)
        current_requirement_credit = max(
            int(reservation.credited_requirement_quantity or 0)
            - int(reservation.consumed_requirement_quantity or 0)
            - int(reservation.released_requirement_quantity or 0),
            0,
        )
        after_remaining_stock = remaining - quantity
        release_requirement_quantity = current_requirement_credit - min(
            current_requirement_credit,
            after_remaining_stock * int(reservation.yield_factor or 1),
        )
        lot = db.get(InventoryLot, reservation.inventory_lot_id)
        if lot is None:
            raise WarehouseInventoryError("关联库存批次不存在", 409)
        if lot.version != expected_version:
            raise WarehouseInventoryError("库存已被其他人修改，请刷新后重试", 409)
        before = _balances(lot)
        now = utc_now()
        result = db.execute(
            update(InventoryLot)
            .where(
                InventoryLot.id == lot.id,
                InventoryLot.version == expected_version,
                InventoryLot.quantity_reserved >= quantity,
            )
            .values(
                quantity_available=InventoryLot.quantity_available + quantity,
                quantity_reserved=InventoryLot.quantity_reserved - quantity,
                version=InventoryLot.version + 1,
                last_movement_at=now,
            )
        )
        if result.rowcount != 1:
            raise WarehouseInventoryError("库存数量或版本已变化，请刷新后重试", 409)
        reservation.released_stock_quantity += quantity
        reservation.released_requirement_quantity += release_requirement_quantity
        reservation.released_by = operator_id
        reservation.released_at = now
        reservation.release_reason = reason
        reservation.status = _reservation_status(reservation)
        db.flush()
        db.expire(lot)
        refreshed_lot = db.get(InventoryLot, lot.id)
        assert refreshed_lot is not None
        movement = _movement(
            db,
            lot=refreshed_lot,
            movement_type="release_reserve",
            quantity=quantity,
            before=before,
            operator_id=operator_id,
            reason=reason,
            idempotency_key=idempotency_key,
            reservation_id=reservation.id,
            related_order_id=reservation.order_id,
            related_order_item_id=reservation.order_item_id,
        )
        db.flush()
    from app.services.production_workflow import refresh_existing_production_task

    refresh_existing_production_task(db, reservation.order_item_id)
    return SemiFinishedReservationMutation(reservation, movement)


def release_active_semi_reservations_for_items(
    db: Session,
    *,
    order_item_ids: list[int],
    operator_id: int | None,
    reason: str,
    idempotency_prefix: str,
) -> list[InventoryReservation]:
    if not order_item_ids:
        return []
    rows = db.scalars(
        select(InventoryReservation)
        .where(
            InventoryReservation.order_item_id.in_(order_item_ids),
            InventoryReservation.reservation_type == "semi_order",
            InventoryReservation.status != "cancelled",
            InventoryReservation.reserved_stock_quantity
            > InventoryReservation.consumed_stock_quantity
            + InventoryReservation.released_stock_quantity,
        )
        .order_by(InventoryReservation.inventory_lot_id, InventoryReservation.id)
    ).all()
    released: list[InventoryReservation] = []
    for row in rows:
        lot = db.get(InventoryLot, row.inventory_lot_id)
        if lot is None:
            raise WarehouseInventoryError("关联半成品库存批次不存在", 409)
        result = release_semi_finished_reservation(
            db,
            reservation_id=row.id,
            expected_version=lot.version,
            operator_id=operator_id,
            release_reason=reason,
            idempotency_key=f"{idempotency_prefix}-{row.id}",
        )
        released.append(result.reservation)
    return released


def consume_semi_finished_reservation(
    db: Session,
    *,
    reservation_id: int,
    stock_quantity: int,
    expected_version: int,
    operator_id: int | None,
    idempotency_key: str,
    delivery_item_id: int | None = None,
    reason: str = "送货出库消耗半成品预占",
) -> SemiFinishedReservationMutation:
    repeated = _idempotent_mutation(
        db,
        idempotency_key=idempotency_key,
        movement_type="consume",
        reservation_id=reservation_id,
    )
    if repeated is not None:
        return repeated
    if stock_quantity <= 0:
        raise WarehouseInventoryError("消耗数量必须大于0")
    with db.begin_nested():
        reservation = db.get(InventoryReservation, reservation_id)
        if reservation is None:
            raise WarehouseInventoryError("库存预占记录不存在", 404)
        if reservation.reservation_type != "semi_order":
            raise WarehouseInventoryError("该记录不是半成品订单预占")
        remaining = (
            reservation.reserved_stock_quantity
            - reservation.consumed_stock_quantity
            - reservation.released_stock_quantity
        )
        if stock_quantity > remaining:
            raise WarehouseInventoryError("消耗数量不能超过未消耗预占余额", 409)
        current_requirement_credit = max(
            int(reservation.credited_requirement_quantity or 0)
            - int(reservation.consumed_requirement_quantity or 0)
            - int(reservation.released_requirement_quantity or 0),
            0,
        )
        consumed_requirement_quantity = min(
            current_requirement_credit,
            stock_quantity * int(reservation.yield_factor or 1),
        )
        if consumed_requirement_quantity <= 0:
            raise WarehouseInventoryError("该预占没有可消耗的需求片数余额", 409)
        delivery_item = None
        if delivery_item_id is not None:
            delivery_item = db.get(DeliveryItem, delivery_item_id)
            if delivery_item is None:
                raise WarehouseInventoryError("送货明细不存在", 404)
            if delivery_item.order_item_id != reservation.order_item_id:
                raise WarehouseInventoryError("送货明细与库存预占订单不一致", 409)
        lot = db.get(InventoryLot, reservation.inventory_lot_id)
        if lot is None:
            raise WarehouseInventoryError("关联库存批次不存在", 409)
        requirement = db.get(
            OrderItemSemiRequirement, reservation.semi_requirement_id
        )
        order_item = db.get(OrderItem, reservation.order_item_id)
        order = db.get(Order, reservation.order_id)
        detail = lot.semi_finished_detail
        if (
            requirement is None
            or order_item is None
            or order is None
            or detail is None
            or requirement.order_item_id != order_item.id
            or order_item.product_id is None
        ):
            raise WarehouseInventoryError("半成品预占关联数据不完整", 409)
        requirement_product_id = _requirement_product_id(db, requirement)
        ensure_semi_finished_lot_eligibility(
            db,
            lot=lot,
            product_id=requirement_product_id,
            customer_id=order.customer_id,
            expected=requirement_signature(requirement),
        )
        if lot.version != expected_version:
            raise WarehouseInventoryError("库存已被其他人修改，请刷新后重试", 409)
        before = _balances(lot)
        now = utc_now()
        result = db.execute(
            update(InventoryLot)
            .where(
                InventoryLot.id == lot.id,
                InventoryLot.version == expected_version,
                InventoryLot.quantity_reserved >= stock_quantity,
            )
            .values(
                quantity_reserved=InventoryLot.quantity_reserved - stock_quantity,
                quantity_consumed=InventoryLot.quantity_consumed + stock_quantity,
                version=InventoryLot.version + 1,
                last_movement_at=now,
            )
        )
        if result.rowcount != 1:
            raise WarehouseInventoryError("库存数量或版本已变化，请刷新后重试", 409)
        reservation.consumed_stock_quantity += stock_quantity
        reservation.consumed_requirement_quantity += consumed_requirement_quantity
        reservation.consumed_by = operator_id
        reservation.consumed_at = now
        reservation.status = _reservation_status(reservation)
        db.flush()
        db.expire(lot)
        refreshed_lot = db.get(InventoryLot, lot.id)
        assert refreshed_lot is not None
        movement = _movement(
            db,
            lot=refreshed_lot,
            movement_type="consume",
            quantity=stock_quantity,
            before=before,
            operator_id=operator_id,
            reason=reason,
            idempotency_key=idempotency_key,
            reservation_id=reservation.id,
            related_order_id=reservation.order_id,
            related_order_item_id=reservation.order_item_id,
            related_delivery_id=(delivery_item.delivery_id if delivery_item else None),
        )
        db.flush()
        allocation = None
        if delivery_item is not None:
            allocation = DeliveryInventoryAllocation(
                delivery_item_id=delivery_item.id,
                reservation_id=reservation.id,
                consume_movement_id=movement.id,
                consumed_stock_quantity=stock_quantity,
                credited_requirement_quantity=consumed_requirement_quantity,
                reversed_stock_quantity=0,
                reversed_requirement_quantity=0,
                status="active",
                created_by=operator_id,
            )
            db.add(allocation)
            db.flush()
    return SemiFinishedReservationMutation(reservation, movement, allocation)


def reverse_semi_finished_consumption(
    db: Session,
    *,
    reservation_id: int,
    stock_quantity: int,
    expected_version: int,
    operator_id: int | None,
    idempotency_key: str,
    allocation_id: int | None = None,
) -> SemiFinishedReservationMutation:
    repeated = _idempotent_mutation(
        db,
        idempotency_key=idempotency_key,
        movement_type="reverse_consume",
        reservation_id=reservation_id,
    )
    if repeated is not None:
        return repeated
    if stock_quantity <= 0:
        raise WarehouseInventoryError("逆转消耗数量必须大于0")
    with db.begin_nested():
        reservation = db.get(InventoryReservation, reservation_id)
        if reservation is None:
            raise WarehouseInventoryError("库存预占记录不存在", 404)
        if reservation.reservation_type != "semi_order":
            raise WarehouseInventoryError("该记录不是半成品订单预占")
        if stock_quantity > reservation.consumed_stock_quantity:
            raise WarehouseInventoryError("逆转数量不能超过累计已消耗数量", 409)
        allocation = None
        reversal_of_movement_id = None
        related_delivery_id = None
        if allocation_id is not None:
            allocation = db.get(DeliveryInventoryAllocation, allocation_id)
            if allocation is None or allocation.reservation_id != reservation.id:
                raise WarehouseInventoryError("送货库存分配记录不存在", 404)
            allocation_remaining = (
                allocation.consumed_stock_quantity
                - allocation.reversed_stock_quantity
            )
            if stock_quantity > allocation_remaining:
                raise WarehouseInventoryError("逆转数量不能超过该送货分配余额", 409)
            reversal_of_movement_id = allocation.consume_movement_id
            delivery_item = db.get(DeliveryItem, allocation.delivery_item_id)
            related_delivery_id = delivery_item.delivery_id if delivery_item else None
            active_allocation_stock = (
                allocation.consumed_stock_quantity
                - allocation.reversed_stock_quantity
            )
            active_allocation_credit = (
                allocation.credited_requirement_quantity
                - allocation.reversed_requirement_quantity
            )
            after_allocation_stock = active_allocation_stock - stock_quantity
            reverse_requirement_quantity = active_allocation_credit - min(
                active_allocation_credit,
                after_allocation_stock * int(reservation.yield_factor or 1),
            )
        else:
            after_consumed_stock = (
                reservation.consumed_stock_quantity - stock_quantity
            )
            current_consumed_credit = int(
                reservation.consumed_requirement_quantity or 0
            )
            reverse_requirement_quantity = current_consumed_credit - min(
                current_consumed_credit,
                after_consumed_stock * int(reservation.yield_factor or 1),
            )
        if reverse_requirement_quantity > reservation.consumed_requirement_quantity:
            raise WarehouseInventoryError("预占累计消耗需求片数异常，请联系管理员", 409)
        lot = db.get(InventoryLot, reservation.inventory_lot_id)
        if lot is None:
            raise WarehouseInventoryError("关联库存批次不存在", 409)
        if lot.version != expected_version:
            raise WarehouseInventoryError("库存已被其他人修改，请刷新后重试", 409)
        if lot.quantity_consumed < stock_quantity:
            raise WarehouseInventoryError("库存累计消耗余额异常，请联系管理员", 409)
        before = _balances(lot)
        now = utc_now()
        result = db.execute(
            update(InventoryLot)
            .where(
                InventoryLot.id == lot.id,
                InventoryLot.version == expected_version,
                InventoryLot.quantity_consumed >= stock_quantity,
            )
            .values(
                quantity_reserved=InventoryLot.quantity_reserved + stock_quantity,
                quantity_consumed=InventoryLot.quantity_consumed - stock_quantity,
                version=InventoryLot.version + 1,
                last_movement_at=now,
            )
        )
        if result.rowcount != 1:
            raise WarehouseInventoryError("库存数量或版本已变化，请刷新后重试", 409)
        reservation.consumed_stock_quantity -= stock_quantity
        reservation.consumed_requirement_quantity -= reverse_requirement_quantity
        if reservation.consumed_stock_quantity == 0:
            reservation.consumed_at = None
            reservation.consumed_by = None
        reservation.status = _reservation_status(reservation)
        if allocation is not None:
            allocation.reversed_stock_quantity += stock_quantity
            allocation.reversed_requirement_quantity += reverse_requirement_quantity
            allocation.reversed_by = operator_id
            allocation.reversed_at = now
            allocation.status = (
                "reversed"
                if allocation.reversed_stock_quantity
                == allocation.consumed_stock_quantity
                else "partial"
            )
        db.flush()
        db.expire(lot)
        refreshed_lot = db.get(InventoryLot, lot.id)
        assert refreshed_lot is not None
        movement = _movement(
            db,
            lot=refreshed_lot,
            movement_type="reverse_consume",
            quantity=stock_quantity,
            before=before,
            operator_id=operator_id,
            reason="撤销送货出库半成品消耗",
            idempotency_key=idempotency_key,
            reservation_id=reservation.id,
            related_order_id=reservation.order_id,
            related_order_item_id=reservation.order_item_id,
            related_delivery_id=related_delivery_id,
            reversal_of_movement_id=reversal_of_movement_id,
        )
        db.flush()
    return SemiFinishedReservationMutation(reservation, movement, allocation)
