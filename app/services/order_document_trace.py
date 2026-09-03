from __future__ import annotations

from datetime import date, datetime
import logging
import re
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.time_contract import utc_naive_to_api
from app.models.composite_purchase_group import (
    CompositePhysicalGroupReceipt,
    CompositePhysicalGroupReceiptSourceAllocation,
    CompositePhysicalPurchaseGroup,
    CompositePhysicalPurchaseGroupSource,
)
from app.models.delivery import Delivery, DeliveryItem
from app.models.finance import (
    Invoice,
    ReturnReceipt,
    ReturnReceiptItem,
    SettlementRecord,
    Statement,
    StatementItem,
)
from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.purchase_receipt import IncomingReceiptPurposeAllocation
from app.models.production import (
    ProductionCompletion,
    ProductionStockTransfer,
    ProductionTask,
)
from app.models.requisition import Requisition, RequisitionItem
from app.models.supplier_requisition_order import (
    SupplierRequisitionOrder,
    SupplierRequisitionOrderItem,
)
from app.models.warehouse_inventory import (
    FinishedGoodsInventoryDetail,
    InventoryLot,
    InventoryLotTransfer,
    InventoryMovement,
    InventoryPallet,
    InventoryPalletItem,
    InventoryReservation,
    WarehouseArea,
    WarehouseLocation,
)
from app.services.location_candidates import (
    load_warehouse_location_projection_contexts,
    warehouse_location_projection,
)
from app.services.warehouse_location_address import employee_location_name
from app.services.order_business_status import BUSINESS_STATUS_LABELS
from app.services.product_specification import resolved_product_specification

logger = logging.getLogger(__name__)


WAREHOUSE_MAP_FLOORS = {"1F", "3F", "4F"}


STAGE_LABELS = {
    "order": "下单",
    "requisition": "报料",
    "incoming": "来料",
    "production": "生产",
    "inventory": "库存",
    "delivery": "送货",
    "return_receipt": "回单",
    "statement": "对账",
    "invoice": "开票",
    "settlement": "收款",
}

STATUS_LABELS = {
    "pending": "待处理",
    "confirmed": "已确认",
    "posted": "已生效",
    "reversed": "已撤销",
    "partial": "部分执行",
    "released": "已释放",
    "consumed": "已用完",
    "cancelled": "已取消",
    "dispatched": "已送货",
    "voided": "已作废",
    "unsettled": "未结清",
    "settled": "已结清",
    "completed": "已完成",
    "ready": "待生产",
    "closed": "已关闭",
    "frozen": "已冻结",
}

MOVEMENT_LABELS = {
    "manual_in": "手工入库",
    "adjust": "库存调整",
    "freeze": "冻结",
    "unfreeze": "解冻",
    "damage": "报损",
    "scrap": "报废",
    "transfer_to_general": "转通用库存",
    "reserve": "预占库存",
    "release_reserve": "释放预占",
    "consume": "出库扣减",
    "reverse_consume": "撤销出库",
}

CONTEXT_STATUS_LABELS = {
    ("order", "sales_order", "pending_production"): "待生产",
    ("order", "sales_order", "waiting_material"): "待收料",
    ("order", "sales_order", "production"): "生产中",
    ("order", "sales_order", "delivered"): "已送完",
    ("production", "production_task", "waiting_material"): "待收料",
    ("production", "production_task", "pending"): "待生产",
    ("production", "production_task", "completed"): "已完成",
    ("production", "production_task", "not_required"): "无需生产",
    ("inventory", "inventory_movement", "reserve"): "预占库存",
    ("inventory", "inventory_movement", "release_reserve"): "释放预占",
    ("inventory", "inventory_reservation", "active"): "预占中",
    ("inventory", "inventory_lot", "active"): "正常在库",
    ("inventory", "inventory_lot", "frozen"): "已冻结",
}


TRACE_TARGETS = {
    "sales_order": ("orders", "订单管理", "trace", "订单追溯"),
    "material_requisition": ("requisition", "报料管理", "submitted", "已报料"),
    "supplier_requisition_order": ("requisition", "报料管理", "submitted", "已报料"),
    "supplier_requisition_void": ("requisition", "报料管理", "submitted", "已报料历史"),
    "incoming_receipt": ("incoming", "仓库来料入库", "history", "入库历史"),
    "incoming_receipt_reversal": ("incoming", "仓库来料入库", "history", "入库历史"),
    "composite_physical_group_receipt": (
        "incoming",
        "仓库来料入库",
        "history",
        "组合组件入库",
    ),
    "composite_physical_group_receipt_reversal": (
        "incoming",
        "仓库来料入库",
        "history",
        "组合组件入库历史",
    ),
    "production_task": ("production", "生产确认", "pending", "待生产"),
    "production_completion": ("production", "生产确认", "history", "完工历史"),
    "production_completion_reversal": ("production", "生产确认", "history", "完工历史"),
    "production_stock_transfer": ("production", "生产确认", "history", "完工入库历史"),
    "production_stock_transfer_reversal": ("production", "生产确认", "history", "完工入库历史"),
    "inventory_reservation": ("warehouse", "仓库库存管理", "reservations", "库存预占"),
    "inventory_reservation_release": ("warehouse", "仓库库存管理", "reservations", "库存预占历史"),
    "inventory_reservation_consume": ("warehouse", "仓库库存管理", "movements", "库存流水"),
    "inventory_movement": ("warehouse", "仓库库存管理", "movements", "库存流水"),
    "inventory_lot": ("warehouse", "仓库库存管理", "inventory", "库存批次"),
    "delivery_draft": ("deliveries", "送货与回单", "deliveries", "送货单"),
    "delivery_dispatch": ("deliveries", "送货与回单", "deliveries", "送货单"),
    "delivery_void": ("deliveries", "送货与回单", "deliveries", "送货单历史"),
    "return_receipt": ("deliveries", "送货与回单", "returns", "回单记录"),
    "statement": ("finance", "对账开票收款", "statements", "对账单"),
    "invoice": ("finance", "对账开票收款", "invoices", "开票记录"),
    "settlement": ("finance", "对账开票收款", "settlements", "收款记录"),
}


def trace_event_target(
    *,
    stage: str,
    source_type: str,
    source_id: int,
) -> dict[str, Any]:
    module, module_label, section, section_label = TRACE_TARGETS.get(
        source_type,
        ("orders", "订单管理", "trace", STAGE_LABELS.get(stage, "阶段详情")),
    )
    return {
        "module": module,
        "module_label": module_label,
        "section": section,
        "section_label": section_label,
        "source_type": source_type,
        "source_id": source_id,
    }


def _api_datetime(value: datetime | None) -> str | None:
    return utc_naive_to_api(value) if value is not None else None


def _api_date(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _quantity(value: Any) -> int | float | None:
    if value is None:
        return None
    number = float(value)
    return int(number) if number.is_integer() else number


def _inventory_lot_descendant_ids(
    db: Session,
    seed_ids: set[int],
) -> set[int]:
    """Return every location-transfer descendant of exact source lots."""

    related_ids = {int(value) for value in seed_ids if int(value) > 0}
    frontier = set(related_ids)
    while frontier:
        target_ids = {
            int(value)
            for value in db.scalars(
                select(InventoryLotTransfer.target_lot_id).where(
                    InventoryLotTransfer.source_lot_id.in_(frontier)
                )
            ).all()
            if value is not None
        }
        frontier = target_ids - related_ids
        related_ids.update(frontier)
    return related_ids


def _status_label(
    status: str | None,
    *,
    stage: str,
    source_type: str,
) -> str:
    if not status:
        return "未记录"
    contextual = CONTEXT_STATUS_LABELS.get((stage, source_type, status))
    if contextual:
        return contextual
    if stage == "inventory" and source_type == "inventory_movement":
        movement_label = MOVEMENT_LABELS.get(status)
        if movement_label:
            return movement_label
    if stage == "order" and source_type == "sales_order":
        business_label = BUSINESS_STATUS_LABELS.get(status)
        if business_label:
            return business_label
    generic = STATUS_LABELS.get(status)
    if generic:
        return generic
    if re.search(r"[\u3400-\u9fff]", status):
        return status
    logger.warning(
        "order trace encountered an unmapped status",
        extra={
            "trace_stage": stage,
            "trace_source_type": source_type,
            "trace_status": status,
        },
    )
    return "状态待确认"


def build_order_item_document_trace(
    db: Session,
    *,
    order: Order,
    item: OrderItem,
    customer_name: str,
    display_order_number: str,
    permissions: set[str],
) -> dict[str, Any]:
    """Build a read-only trace from exact foreign-key facts at OrderItem grain."""

    product = db.get(Product, item.product_id) if item.product_id is not None else None
    events: list[dict[str, Any]] = []
    lot_ids: set[int] = set()
    location_contexts: dict[int, dict[str, Any]] = {}
    sequence = 0

    def projected_location_name(location: WarehouseLocation | None) -> str | None:
        if location is None:
            return None
        context = location_contexts.get(int(location.id), {})
        return employee_location_name(
            location,
            area=context.get("area"),
            floor=context.get("floor"),
            area_sequence=context.get("area_sequence"),
        )

    def add_event(
        *,
        stage: str,
        source_type: str,
        source_id: int,
        document_number: str,
        status: str | None,
        occurred_at: datetime | None = None,
        business_date: date | None = None,
        quantity: Any = None,
        unit: str | None = None,
        location_code: str | None = None,
        location_name: str | None = None,
        pallet_code: str | None = None,
        lot_number: str | None = None,
        is_effective: bool = True,
        is_reversal: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        nonlocal sequence
        sequence += 1
        sort_value = (
            occurred_at.isoformat()
            if occurred_at is not None
            else f"{business_date.isoformat()}T00:00:00"
            if business_date is not None
            else "9999-12-31T23:59:59"
        )
        events.append(
            {
                "key": f"{source_type}:{source_id}:{sequence}",
                "stage": stage,
                "stage_label": STAGE_LABELS[stage],
                "source_type": source_type,
                "source_id": source_id,
                "target": trace_event_target(
                    stage=stage,
                    source_type=source_type,
                    source_id=source_id,
                ),
                "document_number": document_number,
                "status": status,
                "status_label": _status_label(
                    status,
                    stage=stage,
                    source_type=source_type,
                ),
                "occurred_at": _api_datetime(occurred_at),
                "business_date": _api_date(business_date),
                "quantity": _quantity(quantity),
                "unit": unit,
                "location_code": location_code,
                "location_name": location_name,
                "pallet_code": pallet_code,
                "lot_number": lot_number,
                "is_effective": is_effective,
                "is_reversal": is_reversal,
                "details": details or {},
                "_sort": (sort_value, sequence),
            }
        )

    add_event(
        stage="order",
        source_type="sales_order",
        source_id=order.id,
        document_number=display_order_number,
        status=order.status,
        occurred_at=order.created_at,
        business_date=order.order_date,
        quantity=item.quantity,
        unit="只",
        details={"customer_po": order.customer_po},
    )

    if "requisition.view" in permissions:
        physical_group_source_rows = db.execute(
            select(
                CompositePhysicalPurchaseGroupSource,
                CompositePhysicalPurchaseGroup,
            )
            .join(
                CompositePhysicalPurchaseGroup,
                CompositePhysicalPurchaseGroup.id
                == CompositePhysicalPurchaseGroupSource.composite_physical_purchase_group_id,
            )
            .where(CompositePhysicalPurchaseGroupSource.order_item_id == item.id)
            .order_by(
                CompositePhysicalPurchaseGroup.created_at,
                CompositePhysicalPurchaseGroupSource.source_sequence,
            )
        ).all()
        physical_group_source_by_requisition_item_id = {
            int(source.requisition_item_id): (source, group)
            for source, group in physical_group_source_rows
        }
        requisition_rows = db.execute(
            select(RequisitionItem, Requisition)
            .join(Requisition, Requisition.id == RequisitionItem.requisition_id)
            .where(RequisitionItem.order_item_id == item.id)
            .order_by(Requisition.created_at, RequisitionItem.id)
        ).all()
        for requisition_item, requisition in requisition_rows:
            physical_source_and_group = (
                physical_group_source_by_requisition_item_id.get(
                    int(requisition_item.id)
                )
            )
            physical_source = (
                physical_source_and_group[0]
                if physical_source_and_group is not None
                else None
            )
            physical_group = (
                physical_source_and_group[1]
                if physical_source_and_group is not None
                else None
            )
            effective = str(requisition_item.status or "").strip().lower() not in {
                "已取消",
                "已作废",
                "voided",
                "cancelled",
            }
            add_event(
                stage="requisition",
                source_type="material_requisition",
                source_id=requisition_item.id,
                document_number=requisition.requisition_number,
                status=requisition_item.status or requisition.status,
                occurred_at=requisition.created_at,
                business_date=requisition.requisition_date,
                quantity=requisition_item.requisition_qty,
                unit="张",
                is_effective=effective,
                details={
                    "supplier_name": requisition.supplier_name,
                    "order_item_id": item.id,
                    **(
                        {
                            "composite_physical_purchase_group_id": (
                                physical_group.id
                            ),
                            "physical_group_key": physical_group.group_key,
                            "group_purchase_sheet_quantity": (
                                physical_group.purchase_sheet_quantity
                            ),
                            "group_order_purpose_sheet_quantity": (
                                physical_group.order_purpose_sheet_quantity
                            ),
                            "group_reserve_sheet_quantity": (
                                physical_group.reserve_sheet_quantity
                            ),
                            "source_sequence": physical_source.source_sequence,
                            "source_count": physical_group.source_count,
                            "source_component_type": (
                                physical_source.component_type_snapshot
                            ),
                            "source_net_required_piece_quantity": (
                                physical_source.net_required_piece_quantity
                            ),
                            "source_allocated_order_purpose_sheet_quantity": (
                                physical_source.allocated_order_purpose_sheet_quantity
                            ),
                        }
                        if physical_source is not None and physical_group is not None
                        else {}
                    ),
                },
            )

        direct_supplier_rows = db.execute(
            select(SupplierRequisitionOrderItem, SupplierRequisitionOrder)
            .join(
                SupplierRequisitionOrder,
                SupplierRequisitionOrder.id
                == SupplierRequisitionOrderItem.supplier_order_id,
            )
            .where(SupplierRequisitionOrderItem.order_item_id == item.id)
            .order_by(
                SupplierRequisitionOrder.created_at,
                SupplierRequisitionOrderItem.id,
            )
        ).all()
        supplier_trace_rows: dict[
            int,
            tuple[
                SupplierRequisitionOrderItem,
                SupplierRequisitionOrder,
                CompositePhysicalPurchaseGroup | None,
                list[CompositePhysicalPurchaseGroupSource],
            ],
        ] = {
            int(supplier_item.id): (
                supplier_item,
                supplier_order,
                None,
                [],
            )
            for supplier_item, supplier_order in direct_supplier_rows
        }
        group_supplier_rows = db.execute(
            select(
                SupplierRequisitionOrderItem,
                SupplierRequisitionOrder,
                CompositePhysicalPurchaseGroup,
                CompositePhysicalPurchaseGroupSource,
            )
            .join(
                CompositePhysicalPurchaseGroup,
                CompositePhysicalPurchaseGroup.supplier_requisition_order_item_id
                == SupplierRequisitionOrderItem.id,
            )
            .join(
                CompositePhysicalPurchaseGroupSource,
                CompositePhysicalPurchaseGroupSource.composite_physical_purchase_group_id
                == CompositePhysicalPurchaseGroup.id,
            )
            .join(
                SupplierRequisitionOrder,
                SupplierRequisitionOrder.id
                == SupplierRequisitionOrderItem.supplier_order_id,
            )
            .where(CompositePhysicalPurchaseGroupSource.order_item_id == item.id)
            .order_by(
                SupplierRequisitionOrder.created_at,
                SupplierRequisitionOrderItem.id,
                CompositePhysicalPurchaseGroupSource.source_sequence,
            )
        ).all()
        for supplier_item, supplier_order, physical_group, physical_source in (
            group_supplier_rows
        ):
            existing = supplier_trace_rows.get(int(supplier_item.id))
            sources = list(existing[3]) if existing is not None else []
            sources.append(physical_source)
            supplier_trace_rows[int(supplier_item.id)] = (
                supplier_item,
                supplier_order,
                physical_group,
                sources,
            )
        for supplier_item, supplier_order, physical_group, physical_sources in sorted(
            supplier_trace_rows.values(),
            key=lambda row: (row[1].created_at, row[0].id),
        ):
            effective = (
                supplier_order.status != "voided"
                and supplier_item.status == "active"
            )
            add_event(
                stage="requisition",
                source_type="supplier_requisition_order",
                source_id=supplier_item.id,
                document_number=supplier_order.order_number,
                status=(
                    supplier_item.status
                    if supplier_order.status == "confirmed"
                    else supplier_order.status
                ),
                occurred_at=supplier_order.created_at,
                quantity=(
                    physical_group.purchase_sheet_quantity
                    if physical_group is not None
                    else supplier_item.requisition_qty
                ),
                unit="张",
                is_effective=effective,
                details={
                    "supplier_name": supplier_order.supplier_name,
                    "supplier_order_id": supplier_order.id,
                    "order_item_id": item.id,
                    **(
                        {
                            "composite_physical_purchase_group_id": (
                                physical_group.id
                            ),
                            "group_order_purpose_sheet_quantity": (
                                physical_group.order_purpose_sheet_quantity
                            ),
                            "group_reserve_sheet_quantity": (
                                physical_group.reserve_sheet_quantity
                            ),
                            "order_source_component_piece_quantity": sum(
                                int(source.net_required_piece_quantity)
                                for source in physical_sources
                            ),
                            "order_source_ids": [
                                int(source.id) for source in physical_sources
                            ],
                        }
                        if physical_group is not None
                        else {}
                    ),
                },
            )
            item_voided_at = supplier_item.voided_at or supplier_order.voided_at
            if item_voided_at is not None:
                add_event(
                    stage="requisition",
                    source_type="supplier_requisition_void",
                    source_id=supplier_item.id,
                    document_number=supplier_order.order_number,
                    status="voided",
                    occurred_at=item_voided_at,
                    quantity=supplier_item.requisition_qty,
                    unit="张",
                    is_effective=False,
                    is_reversal=True,
                )

    if "incoming.view" in permissions:
        incoming_rows = db.execute(
            select(IncomingReceiptItem, IncomingReceipt)
            .join(IncomingReceipt, IncomingReceipt.id == IncomingReceiptItem.receipt_id)
            .where(IncomingReceiptItem.order_item_id == item.id)
            .order_by(IncomingReceipt.received_at, IncomingReceiptItem.id)
        ).all()
        receipt_item_ids = [receipt_item.id for receipt_item, _receipt in incoming_rows]
        purpose_rows = (
            db.scalars(
                select(IncomingReceiptPurposeAllocation).where(
                    IncomingReceiptPurposeAllocation.incoming_receipt_item_id.in_(
                        receipt_item_ids
                    )
                )
            ).all()
            if receipt_item_ids
            else []
        )
        purpose_by_receipt_item = {
            row.incoming_receipt_item_id: row for row in purpose_rows
        }
        for receipt_item, receipt in incoming_rows:
            purpose = purpose_by_receipt_item.get(receipt_item.id)
            if receipt_item.surplus_inventory_lot_id is not None:
                lot_ids.add(receipt_item.surplus_inventory_lot_id)
            if purpose is not None:
                if purpose.finished_inventory_lot_id is not None:
                    lot_ids.add(purpose.finished_inventory_lot_id)
                if purpose.semi_finished_inventory_lot_id is not None:
                    lot_ids.add(purpose.semi_finished_inventory_lot_id)
            effective = receipt.status == "posted" and receipt_item.status == "posted"
            purpose_details: dict[str, Any] = {}
            if purpose is not None:
                purpose_details = {
                    "purpose_status": purpose.purpose_contract_status_snapshot,
                    "order_purpose_sheet_qty": (
                        purpose.receipt_order_purpose_sheet_qty
                    ),
                    "reserve_purpose_sheet_qty": (
                        purpose.receipt_reserve_purpose_sheet_qty
                    ),
                    "cumulative_order_purpose_sheet_qty": (
                        purpose.cumulative_order_purpose_sheet_qty_after
                    ),
                    "cumulative_reserve_purpose_sheet_qty": (
                        purpose.cumulative_reserve_purpose_sheet_qty_after
                    ),
                    "finished_output_qty_delta": purpose.finished_output_qty_delta,
                    "finished_output_qty_cumulative": purpose.finished_output_qty_after,
                    "finished_inventory_lot_id": purpose.finished_inventory_lot_id,
                    "reserve_inventory_lot_id": (
                        purpose.semi_finished_inventory_lot_id
                    ),
                }
                if "cost.view" in permissions:
                    purpose_details.update(
                        {
                            "order_purpose_cost": purpose.order_purpose_cost,
                            "reserve_purpose_cost": purpose.reserve_purpose_cost,
                            "total_cost": purpose.total_cost,
                        }
                    )
            add_event(
                stage="incoming",
                source_type="incoming_receipt",
                source_id=receipt_item.id,
                document_number=receipt.receipt_number,
                status=receipt_item.status,
                occurred_at=receipt.received_at,
                quantity=receipt_item.received_quantity,
                unit="张",
                is_effective=effective,
                details={
                    "planned_quantity": receipt_item.planned_quantity,
                    "variance_quantity": receipt_item.variance_quantity,
                    "variance_type": receipt_item.variance_type,
                    **purpose_details,
                },
            )
            reversed_at = receipt_item.reversed_at or receipt.reversed_at
            if reversed_at is not None:
                add_event(
                    stage="incoming",
                    source_type="incoming_receipt_reversal",
                    source_id=receipt_item.id,
                    document_number=receipt.receipt_number,
                    status="reversed",
                    occurred_at=reversed_at,
                    quantity=receipt_item.received_quantity,
                    unit="张",
                    is_effective=False,
                    is_reversal=True,
                )

        order_group_sources = list(
            db.scalars(
                select(CompositePhysicalPurchaseGroupSource)
                .where(CompositePhysicalPurchaseGroupSource.order_item_id == item.id)
                .order_by(
                    CompositePhysicalPurchaseGroupSource.composite_physical_purchase_group_id,
                    CompositePhysicalPurchaseGroupSource.source_sequence,
                )
            ).all()
        )
        sources_by_group_id: dict[
            int, list[CompositePhysicalPurchaseGroupSource]
        ] = {}
        for source in order_group_sources:
            sources_by_group_id.setdefault(
                int(source.composite_physical_purchase_group_id), []
            ).append(source)
        group_ids = sorted(sources_by_group_id)
        group_receipt_rows = (
            db.execute(
                select(
                    CompositePhysicalGroupReceipt,
                    IncomingReceiptItem,
                    IncomingReceipt,
                )
                .join(
                    IncomingReceiptItem,
                    IncomingReceiptItem.id
                    == CompositePhysicalGroupReceipt.incoming_receipt_item_id,
                )
                .join(
                    IncomingReceipt,
                    IncomingReceipt.id == IncomingReceiptItem.receipt_id,
                )
                .where(
                    CompositePhysicalGroupReceipt.composite_physical_purchase_group_id.in_(
                        group_ids
                    )
                )
                .order_by(
                    IncomingReceipt.received_at,
                    CompositePhysicalGroupReceipt.id,
                )
            ).all()
            if group_ids
            else []
        )
        group_receipt_ids = [
            int(group_receipt.id)
            for group_receipt, _receipt_item, _receipt in group_receipt_rows
        ]
        group_source_allocations = (
            list(
                db.scalars(
                    select(CompositePhysicalGroupReceiptSourceAllocation)
                    .where(
                        CompositePhysicalGroupReceiptSourceAllocation.composite_physical_group_receipt_id.in_(
                            group_receipt_ids
                        ),
                        CompositePhysicalGroupReceiptSourceAllocation.order_item_id
                        == item.id,
                    )
                    .order_by(
                        CompositePhysicalGroupReceiptSourceAllocation.composite_physical_group_receipt_id,
                        CompositePhysicalGroupReceiptSourceAllocation.allocation_sequence,
                    )
                ).all()
            )
            if group_receipt_ids
            else []
        )
        group_allocations_by_receipt_id: dict[
            int, list[CompositePhysicalGroupReceiptSourceAllocation]
        ] = {}
        for allocation in group_source_allocations:
            group_allocations_by_receipt_id.setdefault(
                int(allocation.composite_physical_group_receipt_id), []
            ).append(allocation)
        for group_receipt, receipt_item, receipt in group_receipt_rows:
            group_id = int(group_receipt.composite_physical_purchase_group_id)
            relevant_sources = sources_by_group_id[group_id]
            allocations = group_allocations_by_receipt_id.get(
                int(group_receipt.id), []
            )
            allocated_by_source_id = {
                int(allocation.composite_physical_purchase_group_source_id): int(
                    allocation.allocated_reserved_component_piece_quantity
                )
                for allocation in allocations
            }
            if group_receipt.component_inventory_lot_id is not None:
                lot_ids.add(int(group_receipt.component_inventory_lot_id))
            if group_receipt.reserve_inventory_lot_id is not None:
                lot_ids.add(int(group_receipt.reserve_inventory_lot_id))
            source_details = [
                {
                    "group_source_id": int(source.id),
                    "bom_snapshot_id": int(
                        source.sales_order_item_bom_component_id
                    ),
                    "component_type": source.component_type_snapshot,
                    "net_required_piece_quantity": int(
                        source.net_required_piece_quantity
                    ),
                    "allocated_component_piece_quantity": int(
                        allocated_by_source_id.get(int(source.id), 0)
                    ),
                }
                for source in relevant_sources
            ]
            details: dict[str, Any] = {
                "composite_physical_purchase_group_id": group_id,
                "receipt_sequence": group_receipt.receipt_sequence,
                "order_purpose_sheet_quantity": (
                    group_receipt.order_purpose_received_sheet_quantity
                ),
                "reserve_sheet_quantity": (
                    group_receipt.reserve_received_sheet_quantity
                ),
                "component_output_piece_quantity": (
                    group_receipt.component_output_piece_quantity
                ),
                "order_allocated_component_piece_quantity": sum(
                    int(row["allocated_component_piece_quantity"])
                    for row in source_details
                ),
                "component_inventory_lot_id": (
                    group_receipt.component_inventory_lot_id
                ),
                "reserve_inventory_lot_id": group_receipt.reserve_inventory_lot_id,
                "sources": source_details,
            }
            if "cost.view" in permissions:
                details.update(
                    {
                        "purchase_receipt_fact_id": (
                            group_receipt.purchase_receipt_fact_id
                        ),
                        "order_purpose_cost": (
                            group_receipt.order_purpose_material_cost
                        ),
                        "reserve_purpose_cost": (
                            group_receipt.reserve_material_cost
                        ),
                        "total_cost": group_receipt.total_material_cost,
                    }
                )
            effective = (
                receipt.status == "posted"
                and receipt_item.status == "posted"
                and group_receipt.status == "posted"
            )
            add_event(
                stage="incoming",
                source_type="composite_physical_group_receipt",
                source_id=group_receipt.id,
                document_number=receipt.receipt_number,
                status=group_receipt.status,
                occurred_at=receipt.received_at,
                quantity=group_receipt.received_sheet_quantity,
                unit="张",
                is_effective=effective,
                details=details,
            )
            reversal = group_receipt.reversal
            reversed_at = (
                reversal.reversed_at
                if reversal is not None
                else receipt_item.reversed_at or receipt.reversed_at
            )
            if reversed_at is not None:
                add_event(
                    stage="incoming",
                    source_type="composite_physical_group_receipt_reversal",
                    source_id=group_receipt.id,
                    document_number=receipt.receipt_number,
                    status="reversed",
                    occurred_at=reversed_at,
                    quantity=group_receipt.received_sheet_quantity,
                    unit="张",
                    is_effective=False,
                    is_reversal=True,
                    details={
                        "composite_physical_purchase_group_id": group_id,
                        "reversed_component_piece_quantity": (
                            reversal.reversed_component_output_piece_quantity
                            if reversal is not None
                            else group_receipt.component_output_piece_quantity
                        ),
                    },
                )

    production_tasks = db.scalars(
        select(ProductionTask)
        .where(
            ProductionTask.order_item_id == item.id,
        )
        .order_by(ProductionTask.created_at, ProductionTask.id)
    ).all()
    for task in production_tasks:
        add_event(
            stage="production",
            source_type="production_task",
            source_id=task.id,
            document_number=f"生产任务 #{task.id}",
            status=task.status,
            occurred_at=task.ready_at or task.created_at,
            quantity=task.planned_quantity,
            unit=("片" if task.task_role == "component_internal" else "只"),
            details={
                "readiness_basis": task.readiness_basis,
                "task_role": task.task_role,
                "sales_order_item_bom_component_id": (
                    task.sales_order_item_bom_component_id
                ),
            },
        )

    completion_rows = db.scalars(
        select(ProductionCompletion)
        .where(ProductionCompletion.order_item_id == item.id)
        .order_by(ProductionCompletion.completed_at, ProductionCompletion.id)
    ).all()
    completion_ids = [row.id for row in completion_rows]
    completion_location_ids = {
        row.warehouse_location_id
        for row in completion_rows
        if row.warehouse_location_id is not None
    }
    location_rows = (
        db.scalars(
            select(WarehouseLocation)
            .options(
                selectinload(WarehouseLocation.address_area).selectinload(
                    WarehouseArea.floor
                )
            )
            .where(WarehouseLocation.id.in_(completion_location_ids))
        ).all()
        if completion_location_ids
        else []
    )
    locations = {row.id: row for row in location_rows}
    location_contexts.update(
        load_warehouse_location_projection_contexts(db, location_rows)
    )
    for completion in completion_rows:
        if completion.inventory_lot_id is not None:
            lot_ids.add(completion.inventory_lot_id)
        location = locations.get(completion.warehouse_location_id)
        effective = completion.status == "posted"
        add_event(
            stage="production",
            source_type="production_completion",
            source_id=completion.id,
            document_number=f"完工记录 #{completion.id}",
            status=completion.status,
            occurred_at=completion.completed_at,
            quantity=completion.actual_output_quantity or completion.quantity,
            unit="只",
            location_code=(
                location.location_code
                if location is not None and "warehouse.view" in permissions
                else None
            ),
            location_name=(
                projected_location_name(location)
                if location is not None and "warehouse.view" in permissions
                else None
            ),
            is_effective=effective,
            details={
                "completion_type": completion.completion_type,
                "direct_delivery_quantity": completion.direct_delivery_quantity,
                "stock_quantity": completion.stock_quantity,
            },
        )
        if completion.reversed_at is not None:
            add_event(
                stage="production",
                source_type="production_completion_reversal",
                source_id=completion.id,
                document_number=f"完工记录 #{completion.id}",
                status="reversed",
                occurred_at=completion.reversed_at,
                quantity=completion.actual_output_quantity or completion.quantity,
                unit="只",
                is_effective=False,
                is_reversal=True,
            )

    transfer_rows = (
        db.scalars(
            select(ProductionStockTransfer)
            .where(ProductionStockTransfer.completion_id.in_(completion_ids))
            .order_by(
                ProductionStockTransfer.transferred_at,
                ProductionStockTransfer.id,
            )
        ).all()
        if completion_ids and "warehouse.view" in permissions
        else []
    )
    transfer_location_ids = {
        row.warehouse_location_id
        for row in transfer_rows
        if row.warehouse_location_id is not None
    }
    if transfer_location_ids:
        transfer_locations = db.scalars(
            select(WarehouseLocation)
            .options(
                selectinload(WarehouseLocation.address_area).selectinload(
                    WarehouseArea.floor
                )
            )
            .where(WarehouseLocation.id.in_(transfer_location_ids))
        ).all()
        for location in transfer_locations:
            locations[location.id] = location
        location_contexts.update(
            load_warehouse_location_projection_contexts(db, transfer_locations)
        )
    for transfer in transfer_rows:
        if transfer.inventory_lot_id is not None:
            lot_ids.add(transfer.inventory_lot_id)
        location = locations.get(transfer.warehouse_location_id)
        effective = transfer.status == "posted"
        add_event(
            stage="inventory",
            source_type="production_stock_transfer",
            source_id=transfer.id,
            document_number=f"完工入库 #{transfer.id}",
            status=transfer.status,
            occurred_at=transfer.transferred_at,
            location_code=location.location_code if location else None,
            location_name=projected_location_name(location),
            is_effective=effective,
        )
        if transfer.reversed_at is not None:
            add_event(
                stage="inventory",
                source_type="production_stock_transfer_reversal",
                source_id=transfer.id,
                document_number=f"完工入库 #{transfer.id}",
                status="reversed",
                occurred_at=transfer.reversed_at,
                is_effective=False,
                is_reversal=True,
            )

    if "warehouse.view" in permissions:
        reservations = db.scalars(
            select(InventoryReservation)
            .where(InventoryReservation.order_item_id == item.id)
            .order_by(InventoryReservation.created_at, InventoryReservation.id)
        ).all()
        for reservation in reservations:
            lot_ids.add(reservation.inventory_lot_id)
            add_event(
                stage="inventory",
                source_type="inventory_reservation",
                source_id=reservation.id,
                document_number=reservation.reservation_number,
                status=reservation.status,
                occurred_at=reservation.reserved_at or reservation.created_at,
                quantity=reservation.reserved_stock_quantity,
                unit="只" if reservation.reservation_type == "finished" else "张",
                is_effective=reservation.status in {"active", "partial"},
                details={"reservation_type": reservation.reservation_type},
            )
            if reservation.released_at is not None:
                add_event(
                    stage="inventory",
                    source_type="inventory_reservation_release",
                    source_id=reservation.id,
                    document_number=reservation.reservation_number,
                    status="released",
                    occurred_at=reservation.released_at,
                    quantity=reservation.released_stock_quantity,
                    unit="只" if reservation.reservation_type == "finished" else "张",
                    is_effective=False,
                    is_reversal=True,
                )
            if reservation.consumed_at is not None:
                add_event(
                    stage="inventory",
                    source_type="inventory_reservation_consume",
                    source_id=reservation.id,
                    document_number=reservation.reservation_number,
                    status="consumed",
                    occurred_at=reservation.consumed_at,
                    quantity=reservation.consumed_stock_quantity,
                    unit="只" if reservation.reservation_type == "finished" else "张",
                )

        movements = db.scalars(
            select(InventoryMovement)
            .where(InventoryMovement.related_order_item_id == item.id)
            .order_by(InventoryMovement.created_at, InventoryMovement.id)
        ).all()
        for movement in movements:
            lot_ids.add(movement.inventory_lot_id)
            add_event(
                stage="inventory",
                source_type="inventory_movement",
                source_id=movement.id,
                document_number=movement.movement_number,
                status=movement.movement_type,
                occurred_at=movement.created_at,
                quantity=movement.quantity,
                unit=movement.unit,
                is_effective=movement.movement_type
                not in {"release_reserve", "reverse_consume"},
                is_reversal=movement.movement_type
                in {"release_reserve", "reverse_consume"},
                details={
                    "movement_label": MOVEMENT_LABELS.get(
                        movement.movement_type, "库存动作待确认"
                    )
                },
            )

    delivery_item_ids: list[int] = []
    if "deliveries.view" in permissions:
        delivery_rows = db.execute(
            select(DeliveryItem, Delivery)
            .join(Delivery, Delivery.id == DeliveryItem.delivery_id)
            .where(
                DeliveryItem.order_item_id == item.id,
                DeliveryItem.is_current.is_(True),
            )
            .order_by(Delivery.created_at, DeliveryItem.id)
        ).all()
        delivery_item_ids = [delivery_item.id for delivery_item, _ in delivery_rows]
        for delivery_item, delivery in delivery_rows:
            if delivery.status == "pending" and delivery.ever_dispatched_at is None:
                add_event(
                    stage="delivery",
                    source_type="delivery_draft",
                    source_id=delivery_item.id,
                    document_number=delivery.delivery_number,
                    status="pending",
                    occurred_at=delivery.created_at,
                    business_date=delivery.delivery_date,
                    quantity=delivery_item.delivered_quantity,
                    unit="只",
                    is_effective=False,
                )
            dispatched_at = delivery.dispatched_at or delivery.ever_dispatched_at
            if dispatched_at is not None:
                add_event(
                    stage="delivery",
                    source_type="delivery_dispatch",
                    source_id=delivery_item.id,
                    document_number=delivery.delivery_number,
                    status="dispatched",
                    occurred_at=dispatched_at,
                    business_date=delivery.delivery_date,
                    quantity=delivery_item.delivered_quantity,
                    unit="只",
                    is_effective=delivery.status == "dispatched",
                )
            if delivery.voided_at is not None:
                add_event(
                    stage="delivery",
                    source_type="delivery_void",
                    source_id=delivery_item.id,
                    document_number=delivery.delivery_number,
                    status="voided",
                    occurred_at=delivery.voided_at,
                    quantity=delivery_item.delivered_quantity,
                    unit="只",
                    is_effective=False,
                    is_reversal=True,
                )

    return_item_ids: list[int] = []
    statement_ids: set[int] = set()
    if "deliveries.view" in permissions and delivery_item_ids:
        return_rows = db.execute(
            select(ReturnReceiptItem, ReturnReceipt)
            .join(
                ReturnReceipt,
                ReturnReceipt.id == ReturnReceiptItem.return_receipt_id,
            )
            .where(ReturnReceiptItem.delivery_item_id.in_(delivery_item_ids))
            .order_by(ReturnReceipt.created_at, ReturnReceiptItem.id)
        ).all()
        return_item_ids = [return_item.id for return_item, _ in return_rows]
        for return_item, receipt in return_rows:
            add_event(
                stage="return_receipt",
                source_type="return_receipt",
                source_id=return_item.id,
                document_number=f"回单 #{receipt.id}",
                status=receipt.status,
                occurred_at=receipt.created_at,
                business_date=receipt.actual_received_date,
                quantity=return_item.actual_received_quantity,
                unit="只",
                is_effective=receipt.status == "confirmed",
            )

    if "finance.view" in permissions and return_item_ids:
        statement_rows = db.execute(
            select(StatementItem, Statement)
            .join(Statement, Statement.id == StatementItem.statement_id)
            .where(StatementItem.return_receipt_item_id.in_(return_item_ids))
            .order_by(Statement.created_at, StatementItem.id)
        ).all()
        for statement_item, statement in statement_rows:
            statement_ids.add(statement.id)
            add_event(
                stage="statement",
                source_type="statement",
                source_id=statement_item.id,
                document_number=statement.statement_number,
                status=statement.status,
                occurred_at=statement.created_at,
                quantity=statement_item.actual_received_quantity,
                unit="只",
                details={"statement_month": statement.statement_month},
            )

        if statement_ids:
            invoices = db.scalars(
                select(Invoice)
                .where(Invoice.statement_id.in_(statement_ids))
                .order_by(Invoice.created_at, Invoice.id)
            ).all()
            for invoice in invoices:
                add_event(
                    stage="invoice",
                    source_type="invoice",
                    source_id=invoice.id,
                    document_number=invoice.invoice_number,
                    status="confirmed",
                    occurred_at=invoice.created_at,
                    business_date=invoice.invoice_date,
                )

            settlements = db.scalars(
                select(SettlementRecord)
                .where(SettlementRecord.statement_id.in_(statement_ids))
                .order_by(SettlementRecord.created_at, SettlementRecord.id)
            ).all()
            for settlement in settlements:
                add_event(
                    stage="settlement",
                    source_type="settlement",
                    source_id=settlement.id,
                    document_number=f"收款记录 #{settlement.id}",
                    status="settled",
                    occurred_at=settlement.created_at,
                    business_date=settlement.settlement_date,
                )

    if "warehouse.view" in permissions and completion_ids:
        production_lot_seed_ids = {
            int(row.inventory_lot_id)
            for row in completion_rows
            if row.inventory_lot_id is not None
        }
        production_lot_seed_ids.update(
            int(row.inventory_lot_id)
            for row in transfer_rows
            if row.inventory_lot_id is not None
        )
        production_lot_seed_ids.update(
            int(value)
            for value in db.scalars(
                select(InventoryLot.id).where(
                    InventoryLot.inventory_type == "finished",
                    InventoryLot.source_ref_type == "production_completion",
                    InventoryLot.source_ref_id.in_(completion_ids),
                )
            ).all()
        )
        production_related_lot_ids = _inventory_lot_descendant_ids(
            db,
            production_lot_seed_ids,
        )
        customer_owned_production_lot_ids = set(
            db.scalars(
                select(FinishedGoodsInventoryDetail.inventory_lot_id).where(
                    FinishedGoodsInventoryDetail.inventory_lot_id.in_(
                        production_related_lot_ids
                    ),
                    FinishedGoodsInventoryDetail.owner_customer_id
                    == order.customer_id,
                )
            ).all()
        )
        # Exact production links still fail closed when an anomalous historical
        # transfer changed the customer owner.  Customer-scoped trace must not
        # expose another customer's physical inventory position.
        lot_ids.difference_update(production_related_lot_ids)
        lot_ids.update(customer_owned_production_lot_ids)

    current_inventory: list[dict[str, Any]] = []
    if "warehouse.view" in permissions and lot_ids:
        lots = db.scalars(
            select(InventoryLot)
            .where(InventoryLot.id.in_(lot_ids))
            .order_by(InventoryLot.id)
        ).all()
        lot_location_ids = {row.warehouse_location_id for row in lots}
        lot_locations = {
            row.id: row
            for row in db.scalars(
                select(WarehouseLocation)
                .options(
                    selectinload(WarehouseLocation.address_area).selectinload(
                        WarehouseArea.floor
                    )
                )
                .where(WarehouseLocation.id.in_(lot_location_ids))
            ).all()
        }
        location_contexts.update(
            load_warehouse_location_projection_contexts(
                db,
                lot_locations.values(),
            )
        )
        pallet_rows = db.execute(
            select(InventoryPalletItem, InventoryPallet)
            .join(InventoryPallet, InventoryPallet.id == InventoryPalletItem.pallet_id)
            .where(
                InventoryPalletItem.inventory_lot_id.in_(lot_ids),
                InventoryPallet.is_current.is_(True),
            )
            .order_by(InventoryPalletItem.id.desc())
        ).all()
        pallet_by_lot: dict[int, InventoryPallet] = {}
        for pallet_item, pallet in pallet_rows:
            if pallet_item.inventory_lot_id is not None:
                pallet_by_lot.setdefault(pallet_item.inventory_lot_id, pallet)
        for lot in lots:
            location = lot_locations.get(lot.warehouse_location_id)
            pallet = pallet_by_lot.get(lot.id)
            location_context = (
                location_contexts.get(int(location.id), {})
                if location is not None
                else {}
            )
            location_projection = (
                warehouse_location_projection(location, **location_context)
                if location is not None
                else None
            )
            floor = location_context.get("floor")
            floor_code = str(getattr(floor, "floor_code", "") or "").strip().upper()
            area_code = (
                str(location.area_code or "").strip().upper()
                if location is not None
                else ""
            )
            physical_quantity = max(
                int(lot.quantity_available or 0)
                + int(lot.quantity_reserved or 0)
                + int(lot.quantity_damaged or 0),
                0,
            )
            location_issue: str | None = None
            map_deep_link: str | None = None
            if lot.inventory_type == "finished":
                if physical_quantity <= 0:
                    location_issue = "该成品批次已清零，无当前地图位置"
                elif lot.status not in {"active", "frozen"}:
                    location_issue = "成品批次仍有数量但状态不可用，请核对仓库"
                elif location is None:
                    location_issue = "成品批次缺少有效库位，请核对仓库"
                elif not bool(location.is_active):
                    location_issue = "成品批次所在库位已停用，请核对仓库"
                elif pallet is None:
                    location_issue = "当前成品未关联实体栈板，请核对仓库"
                elif (
                    not bool(pallet.is_current)
                    or str(pallet.status or "").strip().lower() != "active"
                    or pallet.location_id is None
                    or int(pallet.location_id) != int(lot.warehouse_location_id)
                ):
                    location_issue = "成品批次与实体栈板库位不一致，请核对仓库"
                elif (
                    location_projection is None
                    or location_projection.get("position_status") != "mapped"
                ):
                    location_issue = str(
                        (location_projection or {}).get("map_issue")
                        or "该库位尚未发布到当前实测地图"
                    )
                elif floor_code not in WAREHOUSE_MAP_FLOORS:
                    location_issue = "当前实测地图尚未支持该楼层"
                elif not area_code:
                    location_issue = "该库位尚未登记实际区域"
                else:
                    map_deep_link = "/warehouse.html?" + urlencode(
                        {
                            "floor": floor_code,
                            "view": "2d",
                            "mode": "lookup",
                            "readonly": "1",
                            "source": "order_trace",
                            "area_code": area_code,
                            "location_id": int(location.id),
                            "lot_id": int(lot.id),
                        }
                    )
            current_inventory.append(
                {
                    "lot_id": lot.id,
                    "lot_number": lot.lot_number,
                    "inventory_type": lot.inventory_type,
                    "status": lot.status,
                    "status_label": _status_label(
                        lot.status,
                        stage="inventory",
                        source_type="inventory_lot",
                    ),
                    "quantity_available": lot.quantity_available,
                    "quantity_reserved": lot.quantity_reserved,
                    "quantity_consumed": lot.quantity_consumed,
                    "quantity_damaged": lot.quantity_damaged,
                    "physical_quantity": physical_quantity,
                    "unit": lot.unit,
                    "location_id": location.id if location else None,
                    "location_code": location.location_code if location else None,
                    "location_name": projected_location_name(location),
                    "floor_code": floor_code or None,
                    "area_code": area_code or None,
                    "map_position_status": (
                        location_projection.get("position_status")
                        if location_projection is not None
                        else None
                    ),
                    "map_deep_link": map_deep_link,
                    "location_issue": location_issue,
                    "pallet_code": pallet.pallet_code if pallet else None,
                    "last_movement_at": _api_datetime(lot.last_movement_at),
                }
            )

    all_stages = {
        "requisition": "requisition.view",
        "incoming": "incoming.view",
        "inventory": "warehouse.view",
        "delivery": "deliveries.view",
        "return_receipt": "deliveries.view",
        "statement": "finance.view",
        "invoice": "finance.view",
        "settlement": "finance.view",
    }
    restricted_stages = [
        {"stage": stage, "stage_label": STAGE_LABELS[stage]}
        for stage, permission in all_stages.items()
        if permission not in permissions
    ]

    events.sort(key=lambda row: row.pop("_sort"))
    current_event = next(
        (row for row in reversed(events) if row["is_effective"]),
        events[-1] if events else None,
    )
    return {
        "order": {
            "id": order.id,
            "order_number": display_order_number,
            "customer_name": customer_name,
            "customer_po": order.customer_po,
            "order_date": _api_date(order.order_date),
            "created_at": _api_datetime(order.created_at),
        },
        "item": {
            "id": item.id,
            "item_sequence": item.item_sequence,
            "item_order_number": item.item_order_number,
            "product_code": item.snapshot_product_code,
            "product_name": item.snapshot_product_name,
            "specification": resolved_product_specification(item.snapshot_spec, product),
            "quantity": item.quantity,
            "delivered_quantity": item.delivered_quantity,
        },
        "restricted_stages": restricted_stages,
        "current_inventory": current_inventory,
        "current_event_key": current_event["key"] if current_event else None,
        "events": events,
    }
