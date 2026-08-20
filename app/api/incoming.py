from __future__ import annotations

import base64
import hashlib
import hmac
import json
import socket
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import qrcode
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, aliased, selectinload

from app.api.deps import (
    PermissionChecker,
    RoleChecker,
    customer_scope_ids,
    get_db,
    has_permission,
    has_unrestricted_customer_access,
    require_customer_access,
)
from app.core.time_contract import (
    beijing_naive_to_api,
    utc_naive_to_api,
    utc_now_naive,
)
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.incoming_receipt import IncomingReceiptItem
from app.models.material import Material
from app.models.purchase_receipt import (
    IncomingReceiptBatchFact,
    IncomingReceiptReversalFact,
    IncomingReceiptPurposeAllocation,
    IncomingReceiptPurposeReversal,
    PurchaseReceiptFact,
    PurchaseReceiptMaterialVariance,
    PurchaseReceiptMaterialVarianceApproval,
)
from app.models.order import Order, OrderItem
from app.models.product_drawing import ProductDrawing
from app.models.product import Product
from app.models.product_bom import (
    RequisitionItemBomSource,
    SalesOrderItemBomComponent,
)
from app.models.production import ProductionTask
from app.models.requisition import Requisition, RequisitionItem
from app.models.stock_replenishment import (
    StockReplenishmentOrder,
    StockReplenishmentOrderItem,
)
from app.models.supplier_requisition_order import (
    PurchasePurposeSourceSnapshot,
    SupplierRequisitionOrder,
    SupplierRequisitionOrderItem,
)
from app.models.user import User
from app.models.warehouse_inventory import WarehouseLocation
from app.services.history_orders import (
    build_display_registry,
    build_display_registry_for_order_ids,
    display_order_number,
    is_history_order_number,
)
from app.services.location_candidates import list_operational_locations
from app.services.incoming_receipts import (
    IncomingReceiptError,
    accept_short,
    current_supplier_order_items,
    receipt_history,
    receipt_item_dict,
    receive_one,
    revert_receipt_item,
    source_summary_for_item,
    stock_source_summary,
    supplier_order_item_component_type,
    supplier_order_item_key,
)
from app.services.production_workflow import (
    ProductionWorkflowError,
    cutting_output_factor,
    has_production_completion_facts,
    lock_order_rows_for_production_transition,
    refresh_order_production_status,
    refresh_production_task,
    receipt_auto_finished_location_projection,
)
from app.services.composite_bom_workflow import is_composite_order_item
from app.services.audit_log import append_audit_event
from app.services.requisition_production_print import (
    build_receipt_production_print_package,
)
from app.services.supplier_material_display import clean_supplier_material_code
from app.services.product_specification import resolved_product_specification
from app.services.receipt_purpose_distribution import (
    receipt_purpose_finished_capacity,
    serialize_receipt_purpose_allocation,
    serialize_receipt_purpose_allocations,
    serialize_receipt_purpose_reversal,
)
from app.services.purchase_receipt_facts import (
    canonical_purchase_receipt_hash,
    material_calculation_fingerprint,
)
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    automatic_raw_material_staging_location,
)


router = APIRouter()
can_read = PermissionChecker("incoming.view")
can_operate = PermissionChecker("incoming.execute")
admin_rollback = RoleChecker(["admin"])


def _drawing_suffix(reference: str | None) -> str:
    suffix = Path(reference or "").suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".webp", ".pdf"} else ".bin"


def _order_drawing_url(item_id: int, reference: str | None) -> str | None:
    if not reference:
        return None
    return f"/api/orders/items/{item_id}/drawing/content/file{_drawing_suffix(reference)}"


def _product_drawing_url(drawing: ProductDrawing | None) -> str | None:
    if drawing is None:
        return None
    return (
        f"/api/master/products/drawings/{drawing.id}/content/"
        f"original{_drawing_suffix(drawing.image_path)}"
    )


class _PendingIncomingReadContext:
    """Batch posted-receipt summaries for rows already selected by this page.

    ``_rows`` has already resolved whether a line uses the ordinary order-item,
    requisition-item or stock-replenishment route.  Re-reading that route plus
    the same receipt facts once per row created five repeated queries for each
    formal pending line.  This request-local context keeps the route decision
    intact and only batches the posted receipt aggregation/latest-row lookup.
    """

    def __init__(self, db: Session, rows: list[dict]) -> None:
        self._handled_keys: set[tuple[str, int]] = set()
        self._summaries: dict[tuple[str, int], dict | None] = {}
        planned_by_key: dict[tuple[str, int], int] = {}
        for row in rows:
            key = self._row_source_key(row)
            if key is None:
                continue
            self._handled_keys.add(key)
            kind, _source_id = key
            if kind == "stock":
                planned = int(row.get("quantity") or 0)
            elif kind in {"requisition", "supplier_order_item"}:
                planned = int(row.get("requisition_qty") or 0)
            else:
                planned = int(
                    row.get("requisition_qty") or row.get("quantity") or 0
                )
            planned_by_key[key] = planned

        if not self._handled_keys:
            return

        order_item_ids = {
            source_id
            for kind, source_id in self._handled_keys
            if kind == "order"
        }
        requisition_item_ids = {
            source_id
            for kind, source_id in self._handled_keys
            if kind == "requisition"
        }
        supplier_order_item_ids = {
            source_id
            for kind, source_id in self._handled_keys
            if kind == "supplier_order_item"
        }
        stock_item_ids = {
            source_id
            for kind, source_id in self._handled_keys
            if kind == "stock"
        }
        source_filters = []
        if order_item_ids:
            source_filters.append(
                (
                    IncomingReceiptItem.order_item_id.in_(order_item_ids)
                    & IncomingReceiptItem.requisition_item_id.is_(None)
                    & IncomingReceiptItem.supplier_order_item_id.is_(None)
                )
            )
        if requisition_item_ids:
            source_filters.append(
                IncomingReceiptItem.requisition_item_id.in_(requisition_item_ids)
            )
        if supplier_order_item_ids:
            source_filters.append(
                (
                    IncomingReceiptItem.supplier_order_item_id.in_(
                        supplier_order_item_ids
                    )
                    & IncomingReceiptItem.requisition_item_id.is_(None)
                )
            )
        if stock_item_ids:
            source_filters.append(
                IncomingReceiptItem.stock_replenishment_item_id.in_(stock_item_ids)
            )

        facts_by_key: dict[tuple[str, int], list[IncomingReceiptItem]] = {}
        receipt_items = db.scalars(
            select(IncomingReceiptItem)
            .where(
                IncomingReceiptItem.status == "posted",
                or_(*source_filters),
            )
            .order_by(IncomingReceiptItem.id)
        ).all()
        for receipt_item in receipt_items:
            key: tuple[str, int] | None = None
            if (
                receipt_item.stock_replenishment_item_id is not None
                and ("stock", int(receipt_item.stock_replenishment_item_id))
                in self._handled_keys
            ):
                key = ("stock", int(receipt_item.stock_replenishment_item_id))
            elif (
                receipt_item.supplier_order_item_id is not None
                and receipt_item.requisition_item_id is None
                and (
                    "supplier_order_item",
                    int(receipt_item.supplier_order_item_id),
                )
                in self._handled_keys
            ):
                key = (
                    "supplier_order_item",
                    int(receipt_item.supplier_order_item_id),
                )
            elif (
                receipt_item.requisition_item_id is not None
                and ("requisition", int(receipt_item.requisition_item_id))
                in self._handled_keys
            ):
                key = ("requisition", int(receipt_item.requisition_item_id))
            elif (
                receipt_item.order_item_id is not None
                and receipt_item.requisition_item_id is None
                and receipt_item.supplier_order_item_id is None
                and ("order", int(receipt_item.order_item_id))
                in self._handled_keys
            ):
                key = ("order", int(receipt_item.order_item_id))
            if key is not None:
                facts_by_key.setdefault(key, []).append(receipt_item)

        for key in self._handled_keys:
            kind, _source_id = key
            facts = facts_by_key.get(key, [])
            # The ordinary/requisition helper intentionally returned ``None``
            # until at least one posted fact existed.  Stock summaries always
            # expose the remaining quantity, even before the first receipt.
            if not facts and kind != "stock":
                self._summaries[key] = None
                continue
            latest = facts[-1] if facts else None
            planned = planned_by_key[key]
            received = sum(int(row.received_quantity or 0) for row in facts)
            variance = received - planned
            summary = {
                "planned_quantity": planned,
                "cumulative_received_quantity": received,
                "remaining_quantity": max(planned - received, 0),
                "variance_quantity": variance,
                "variance_type": (
                    "matched"
                    if variance == 0
                    else "short"
                    if variance < 0
                    else "over"
                ),
                "resolution_status": (
                    latest.resolution_status if latest else "not_required"
                ),
                "resolution_action": latest.resolution_action if latest else None,
                "pending_receipt_item_id": (
                    latest.id
                    if latest
                    and latest.resolution_status == "pending"
                    and latest.resolution_action == "await_supplier"
                    else None
                ),
                "latest_receipt_item_id": latest.id if latest else None,
                "latest_receipt_id": latest.receipt_id if latest else None,
            }
            if kind == "stock":
                summary.update(
                    {
                        "received_inventory_lot_id": (
                            latest.received_inventory_lot_id if latest else None
                        ),
                        "surplus_inventory_lot_id": None,
                    }
                )
            else:
                summary["surplus_inventory_lot_id"] = (
                    latest.surplus_inventory_lot_id if latest else None
                )
            self._summaries[key] = summary

    @staticmethod
    def _row_source_key(row: dict) -> tuple[str, int] | None:
        item_id = row.get("item_id")
        if isinstance(item_id, str) and item_id.startswith("sr"):
            source_id = row.get("stock_replenishment_item_id")
            return (
                ("stock", int(source_id))
                if source_id is not None and item_id == f"sr{int(source_id)}"
                else None
            )
        if isinstance(item_id, str) and item_id.startswith("so"):
            source_id = row.get("supplier_order_item_id")
            return (
                ("supplier_order_item", int(source_id))
                if source_id is not None and item_id == f"so{int(source_id)}"
                else None
            )
        if isinstance(item_id, str) and item_id.startswith("r"):
            source_id = row.get("requisition_item_id")
            return (
                ("requisition", int(source_id))
                if source_id is not None and item_id == f"r{int(source_id)}"
                else None
            )
        if isinstance(item_id, int) and not row.get("requisition_item_id"):
            return ("order", int(item_id))
        return None

    def summary_for(self, row: dict) -> tuple[bool, dict | None]:
        key = self._row_source_key(row)
        if key is None or key not in self._handled_keys:
            return False, None
        return True, self._summaries.get(key)


def _decorate_rows_with_receipt_purpose(db: Session, rows: list[dict]) -> None:
    """Attach a server-authoritative P1-81 preview to pending formal sources."""

    finished_projection = receipt_auto_finished_location_projection(db)
    try:
        reserve_location = automatic_raw_material_staging_location(db)
        reserve_projection = {
            "ready": True,
            "location_name": reserve_location.location_name,
            "issue": None,
        }
    except WarehouseInventoryError as error:
        reserve_projection = {
            "ready": False,
            "location_name": None,
            "issue": str(error),
        }

    supplier_ids = {
        int(row["supplier_order_item_id"])
        for row in rows
        if row.get("supplier_order_item_id") is not None
    }
    requisition_ids = {
        int(row["requisition_item_id"])
        for row in rows
        if row.get("requisition_item_id") is not None
    }
    supplier_sources = {
        source.id: source
        for source in db.scalars(
            select(SupplierRequisitionOrderItem).where(
                SupplierRequisitionOrderItem.id.in_(supplier_ids)
            )
        ).all()
    } if supplier_ids else {}
    requisition_sources = {
        source.id: source
        for source in db.scalars(
            select(RequisitionItem).where(RequisitionItem.id.in_(requisition_ids))
        ).all()
    } if requisition_ids else {}
    order_item_ids = {
        int(source.order_item_id)
        for source in [*supplier_sources.values(), *requisition_sources.values()]
        if source.order_item_id is not None
    }

    snapshots: list[PurchasePurposeSourceSnapshot] = []
    if order_item_ids:
        snapshots = list(
            db.scalars(
                select(PurchasePurposeSourceSnapshot)
                .outerjoin(
                    SupplierRequisitionOrderItem,
                    SupplierRequisitionOrderItem.id
                    == PurchasePurposeSourceSnapshot.supplier_requisition_order_item_id,
                )
                .outerjoin(
                    RequisitionItem,
                    RequisitionItem.id
                    == PurchasePurposeSourceSnapshot.material_requisition_item_id,
                )
                .where(
                    or_(
                        SupplierRequisitionOrderItem.order_item_id.in_(order_item_ids),
                        RequisitionItem.order_item_id.in_(order_item_ids),
                    )
                )
                .order_by(PurchasePurposeSourceSnapshot.id)
            ).unique().all()
        )
    snapshots_by_source: dict[tuple[str, int], list[PurchasePurposeSourceSnapshot]] = {}
    snapshots_by_order_item: dict[int, list[PurchasePurposeSourceSnapshot]] = {}
    snapshot_order_item_ids: dict[int, int] = {}
    for snapshot in snapshots:
        if snapshot.supplier_requisition_order_item_id is not None:
            source_key = ("supplier", int(snapshot.supplier_requisition_order_item_id))
            source = supplier_sources.get(source_key[1])
        elif snapshot.material_requisition_item_id is not None:
            source_key = ("requisition", int(snapshot.material_requisition_item_id))
            source = requisition_sources.get(source_key[1])
        else:
            continue
        snapshots_by_source.setdefault(source_key, []).append(snapshot)
        if source is not None and source.order_item_id is not None:
            order_item_id = int(source.order_item_id)
            snapshots_by_order_item.setdefault(order_item_id, []).append(snapshot)
            snapshot_order_item_ids[snapshot.id] = order_item_id

    snapshot_ids = {snapshot.id for snapshot in snapshots}
    facts: list[PurchaseReceiptFact] = []
    fact_filters = []
    if supplier_ids:
        fact_filters.append(
            PurchaseReceiptFact.supplier_requisition_order_item_id.in_(supplier_ids)
        )
    if requisition_ids:
        fact_filters.append(
            PurchaseReceiptFact.material_requisition_item_id.in_(requisition_ids)
        )
    if fact_filters:
        facts = list(
            db.scalars(
                select(PurchaseReceiptFact)
                .where(or_(*fact_filters))
                .order_by(
                    PurchaseReceiptFact.receipt_fact_version.desc(),
                    PurchaseReceiptFact.id.desc(),
                )
            ).all()
        )
    latest_fact_by_snapshot: dict[int, PurchaseReceiptFact] = {}
    for fact in facts:
        latest_fact_by_snapshot.setdefault(
            int(fact.purchase_purpose_source_snapshot_id), fact
        )
    allocations = list(
        db.scalars(
            select(IncomingReceiptPurposeAllocation).where(
                IncomingReceiptPurposeAllocation.purchase_purpose_source_snapshot_id.in_(
                    snapshot_ids
                )
            )
        ).all()
    ) if snapshot_ids else []
    reversed_allocation_ids = {
        int(allocation_id)
        for allocation_id in db.scalars(
            select(
                IncomingReceiptPurposeReversal.incoming_receipt_purpose_allocation_id
            ).where(
                IncomingReceiptPurposeReversal.incoming_receipt_purpose_allocation_id.in_(
                    [allocation.id for allocation in allocations]
                )
            )
        ).all()
    } if allocations else set()
    active_allocations = [
        allocation
        for allocation in allocations
        if allocation.id not in reversed_allocation_ids
    ]
    variances = list(
        db.scalars(
            select(PurchaseReceiptMaterialVariance)
            .where(
                PurchaseReceiptMaterialVariance.purchase_purpose_source_snapshot_id.in_(
                    snapshot_ids
                )
            )
            .order_by(PurchaseReceiptMaterialVariance.id.desc())
        ).all()
    ) if snapshot_ids else []
    latest_variance_by_snapshot: dict[int, PurchaseReceiptMaterialVariance] = {}
    for variance in variances:
        latest_variance_by_snapshot.setdefault(
            int(variance.purchase_purpose_source_snapshot_id), variance
        )
    actual_material_ids = {
        int(fact.actual_material_id) for fact in facts
    } | {
        int(variance.actual_material_id) for variance in variances
    }
    actual_materials = {
        row.id: row
        for row in db.scalars(
            select(Material).where(Material.id.in_(actual_material_ids))
        ).all()
    } if actual_material_ids else {}
    approvals = list(
        db.scalars(
            select(PurchaseReceiptMaterialVarianceApproval).where(
                PurchaseReceiptMaterialVarianceApproval.material_variance_id.in_(
                    [row.id for row in variances]
                )
            )
        ).all()
    ) if variances else []
    approval_by_variance = {int(row.material_variance_id): row for row in approvals}
    allocations_by_snapshot: dict[int, list[IncomingReceiptPurposeAllocation]] = {}
    allocations_by_order_item: dict[int, list[IncomingReceiptPurposeAllocation]] = {}
    for allocation in active_allocations:
        snapshot_id = int(allocation.purchase_purpose_source_snapshot_id or 0)
        allocations_by_snapshot.setdefault(snapshot_id, []).append(allocation)
        order_item_id = snapshot_order_item_ids.get(snapshot_id)
        if order_item_id is not None:
            allocations_by_order_item.setdefault(order_item_id, []).append(allocation)

    for row in rows:
        supplier_id = row.get("supplier_order_item_id")
        requisition_id = row.get("requisition_item_id")
        if supplier_id is not None:
            source_key = ("supplier", int(supplier_id))
            source = supplier_sources.get(int(supplier_id))
        elif requisition_id is not None:
            source_key = ("requisition", int(requisition_id))
            source = requisition_sources.get(int(requisition_id))
        else:
            row.update({"purpose_status": "legacy_unset", "receipt_fact_ready": True})
            continue
        marker = str(getattr(source, "purpose_contract_status", "legacy_unset"))
        source_snapshots = snapshots_by_source.get(source_key, [])
        if marker == "legacy_unset" and not source_snapshots:
            row.update({"purpose_status": "legacy_unset", "receipt_fact_ready": True})
            continue
        row["purpose_status"] = "frozen"
        row["receipt_fact_ready"] = False
        if marker != "frozen" or len(source_snapshots) != 1 or source is None:
            row["purpose_issue"] = "正式采购用途快照缺失、重复或损坏"
            continue
        snapshot = source_snapshots[0]
        order_item_id = int(source.order_item_id)
        prior_source = allocations_by_snapshot.get(snapshot.id, [])
        before_total = sum(int(item.receipt_total_sheet_qty) for item in prior_source)
        before_order = sum(
            int(item.receipt_order_purpose_sheet_qty) for item in prior_source
        )
        before_reserve = sum(
            int(item.receipt_reserve_purpose_sheet_qty) for item in prior_source
        )
        quantity = max(int(row.get("incoming_quantity") or 0), 0)
        after_total = before_total + quantity
        order_plan = int(snapshot.order_purpose_sheet_qty or 0)
        reserve_plan = int(snapshot.reserve_purpose_sheet_qty or 0)
        after_order = after_total if reserve_plan == 0 else min(after_total, order_plan)
        after_reserve = after_total - after_order
        order_delta = max(after_order - before_order, 0)
        reserve_delta = max(after_reserve - before_reserve, 0)
        before_sheets: dict[int, int] = {}
        for allocation in allocations_by_order_item.get(order_item_id, []):
            sid = int(allocation.purchase_purpose_source_snapshot_id or 0)
            before_sheets[sid] = before_sheets.get(sid, 0) + int(
                allocation.receipt_order_purpose_sheet_qty
            )
        after_sheets = dict(before_sheets)
        after_sheets[snapshot.id] = after_sheets.get(snapshot.id, 0) + order_delta
        item_snapshots = snapshots_by_order_item.get(order_item_id, [])
        finished_before = receipt_purpose_finished_capacity(
            db, item_snapshots, before_sheets
        )
        finished_after = receipt_purpose_finished_capacity(
            db, item_snapshots, after_sheets
        )
        fact = latest_fact_by_snapshot.get(int(snapshot.id))
        variance = latest_variance_by_snapshot.get(int(snapshot.id))
        variance_material = (
            actual_materials.get(int(variance.actual_material_id))
            if variance is not None
            else None
        )
        if variance is not None and (
            int(variance.expected_source_version) != int(source.version)
            or int(variance.purpose_snapshot_version) != int(snapshot.snapshot_version)
            or variance.receipt_plan_fingerprint != snapshot.preview_fingerprint
            or variance_material is None
            or not bool(variance_material.is_active)
            or int(variance_material.version) != int(variance.actual_material_version)
            or material_calculation_fingerprint(variance_material)
            != variance.actual_material_fingerprint
        ):
            variance = None
        variance_approval = (
            approval_by_variance.get(int(variance.id)) if variance is not None else None
        )
        actual_material = (
            actual_materials.get(int(fact.actual_material_id))
            if fact is not None
            else None
        )
        fact_ready = bool(
            fact is not None
            and int(fact.expected_source_version) == int(source.version)
            and fact.purchase_purpose_source_snapshot_id == snapshot.id
            and int(fact.purpose_snapshot_version) == int(snapshot.snapshot_version)
            and actual_material is not None
            and bool(actual_material.is_active)
            and int(actual_material.version) == int(fact.actual_material_version)
            and material_calculation_fingerprint(actual_material)
            == fact.actual_material_fingerprint
        )
        row.update(
            {
                "source_key": snapshot.source_key,
                "purchase_purpose_source_snapshot_id": snapshot.id,
                "expected_source_version": int(source.version),
                "expected_receipt_fact_version": (
                    int(fact.receipt_fact_version) if fact_ready else None
                ),
                "latest_receipt_fact_version": (
                    int(fact.receipt_fact_version) if fact is not None else 0
                ),
                "expected_actual_material_version": (
                    int(fact.actual_material_version) if fact_ready else None
                ),
                "actual_material_fingerprint": (
                    fact.actual_material_fingerprint if fact_ready else None
                ),
                "expected_purpose_snapshot_version": int(snapshot.snapshot_version),
                "receipt_plan_fingerprint": (
                    snapshot.preview_fingerprint
                ),
                "receipt_fact_ready": fact_ready,
                "actual_material_id": fact.actual_material_id if fact_ready else None,
                "formal_material_id": (
                    fact.expected_material_id
                    if fact_ready
                    else getattr(source, "material_id", None)
                ),
                "formal_material_code": (
                    fact.expected_material_code_snapshot
                    if fact_ready
                    else str(
                        getattr(source, "material_code_snapshot", None)
                        or getattr(source, "material_snapshot", None)
                        or ""
                    ).strip()
                    or None
                ),
                "material_variance_id": variance.id if variance is not None else None,
                "material_variance_actual_material_id": (
                    variance.actual_material_id if variance is not None else None
                ),
                "material_variance_actual_material_code": (
                    variance.actual_material_code_snapshot
                    if variance is not None
                    else None
                ),
                "material_variance_reason": (
                    variance.reason if variance is not None else None
                ),
                "material_variance_approval_id": (
                    variance_approval.id if variance_approval is not None else None
                ),
                "material_variance_requested_by": (
                    variance.requested_by if variance is not None else None
                ),
                "expected_order_purpose_sheet_qty": order_delta,
                "expected_reserve_purpose_sheet_qty": reserve_delta,
                "expected_finished_output_qty": max(
                    finished_after - finished_before, 0
                ),
                "finished_location_name": (
                    finished_projection.get("location_name")
                    if finished_after > finished_before
                    else None
                ),
                "reserve_location_name": (
                    reserve_projection.get("location_name") if reserve_delta > 0 else None
                ),
                "finished_location_ready": (
                    bool(finished_projection.get("ready"))
                    if finished_after > finished_before
                    else True
                ),
                "finished_location_issue": (
                    finished_projection.get("issue")
                    if finished_after > finished_before
                    else None
                ),
                "finished_capacity_warning": (
                    finished_projection.get("capacity_warning")
                    if finished_after > finished_before
                    else None
                ),
                "reserve_location_ready": (
                    bool(reserve_projection.get("ready")) if reserve_delta > 0 else True
                ),
                "reserve_location_issue": (
                    reserve_projection.get("issue") if reserve_delta > 0 else None
                ),
            }
        )
        if finished_after > finished_before and not finished_projection.get("ready"):
            row["receipt_fact_ready"] = False
            row["purpose_issue"] = str(finished_projection.get("issue") or "成品暂存位置未就绪")
        elif reserve_delta > 0 and not reserve_projection.get("ready"):
            row["receipt_fact_ready"] = False
            row["purpose_issue"] = str(reserve_projection.get("issue") or "片料暂存位置未就绪")
        if not fact_ready:
            row["purpose_issue"] = "请先确认实际材质和正式采购价格"


def _utc_now() -> datetime:
    return utc_now_naive()


def _refresh_production_after_material_change(
    db: Session,
    order_item: OrderItem,
) -> bool:
    """Refresh N029 state and report whether this is a production-managed item."""
    try:
        # Composite BOM parent items do not own a single ordinary task.  Once
        # their material receipt changes, refresh/create every snapshot task
        # independently; ordinary and A3 items retain the existing behavior.
        task = refresh_production_task(
            db,
            order_item.id,
            create_if_missing=is_composite_order_item(db, order_item.id),
        )
    except ProductionWorkflowError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    if task is None:
        return False
    refresh_order_production_status(db, order_item.order_id)
    return True


def _lock_order_for_material_revert(db: Session, order_id: int) -> Order:
    try:
        return lock_order_rows_for_production_transition(db, [order_id])[order_id]
    except ProductionWorkflowError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error


class RevertRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)
    idempotency_key: str | None = Field(default=None, max_length=120)


def _material_revert_reason(value: str | None) -> str:
    return (value or "").strip() or "撤回来料实收（系统记录）"


def _reversal_idempotency_contract(
    db: Session,
    *,
    user: User,
    target_kind: str,
    target_id: int,
    payload: RevertRequest,
) -> tuple[str, str, dict | None]:
    key = (payload.idempotency_key or "").strip() or (
        f"incoming-revert:{target_kind}:{target_id}"
    )
    request_hash = canonical_purchase_receipt_hash(
        {
            "target_kind": target_kind,
            "target_id": int(target_id),
            "reason": _material_revert_reason(payload.reason),
        }
    )
    existing = db.scalar(
        select(IncomingReceiptReversalFact).where(
            or_(
                IncomingReceiptReversalFact.idempotency_key == key,
                (
                    IncomingReceiptReversalFact.target_kind == target_kind
                )
                & (IncomingReceiptReversalFact.target_id == int(target_id)),
            )
        )
    )
    if existing is None:
        return key, request_hash, None
    if (
        existing.idempotency_key != key
        or int(existing.reversed_by) != int(user.id)
        or not hmac.compare_digest(existing.request_hash, request_hash)
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "INCOMING_REVERSAL_IDEMPOTENCY_CONFLICT",
                "message": "该撤销幂等键或业务对象已由不同操作者或载荷使用。",
            },
        )
    return key, request_hash, json.loads(existing.response_json)


def _record_reversal_fact(
    db: Session,
    *,
    user: User,
    target_kind: str,
    target_id: int,
    idempotency_key: str,
    request_hash: str,
    response: dict,
    incoming_receipt_item_id: int | None = None,
    incoming_receipt_purpose_reversal_id: int | None = None,
) -> dict:
    stable_response = jsonable_encoder(response)
    db.add(
        IncomingReceiptReversalFact(
            target_kind=target_kind,
            target_id=int(target_id),
            incoming_receipt_item_id=incoming_receipt_item_id,
            incoming_receipt_purpose_reversal_id=(
                incoming_receipt_purpose_reversal_id
            ),
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            response_json=json.dumps(
                stable_response, ensure_ascii=False, sort_keys=True
            ),
            reversed_by=user.id,
        )
    )
    db.flush()
    return stable_response


class ReceiveRequest(BaseModel):
    model_config = {"extra": "forbid"}
    received_quantity: int | None = None
    resolution_action: str | None = None
    resolution_reason: str | None = None
    surplus_location_id: int | None = Field(default=None, gt=0)
    expected_surplus_layout_version: int | None = Field(default=None, gt=0)
    expected_receipt_fact_version: int | None = Field(default=None, gt=0)
    purchase_purpose_source_snapshot_id: int | None = Field(default=None, gt=0)
    expected_purpose_snapshot_version: int | None = Field(default=None, gt=0)
    receipt_plan_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64
    )
    expected_actual_material_version: int | None = Field(default=None, gt=0)
    actual_material_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64
    )
    idempotency_key: str | None = Field(default=None, max_length=100)

    @field_validator("received_quantity")
    @classmethod
    def validate_received_quantity(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("入库数量必须大于0")
        return value


class BatchReceiveLine(BaseModel):
    model_config = {"extra": "forbid"}
    item_id: int | str
    received_quantity: int
    resolution_action: str | None = None
    resolution_reason: str | None = None
    surplus_location_id: int | None = Field(default=None, gt=0)
    expected_surplus_layout_version: int | None = Field(default=None, gt=0)
    expected_receipt_fact_version: int | None = Field(default=None, gt=0)
    purchase_purpose_source_snapshot_id: int | None = Field(default=None, gt=0)
    expected_purpose_snapshot_version: int | None = Field(default=None, gt=0)
    receipt_plan_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64
    )
    expected_actual_material_version: int | None = Field(default=None, gt=0)
    actual_material_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64
    )
    idempotency_key: str | None = Field(default=None, max_length=100)

    @field_validator("received_quantity")
    @classmethod
    def validate_received_quantity(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("入库数量必须大于0")
        return value


class BatchReceiveRequest(BaseModel):
    items: list[BatchReceiveLine] = Field(min_length=1, max_length=200)
    idempotency_key: str | None = Field(default=None, max_length=80)


class AcceptShortRequest(BaseModel):
    reason: str | None = None


def _component_kind(name: str | None) -> str:
    value = str(name or "")
    if value.endswith("-底"):
        return "base"
    if value.endswith("-盖"):
        return "cover"
    return ""


def _source_component_types(
    db: Session,
    requisition_item_ids: list[int],
) -> dict[int, str]:
    if not requisition_item_ids:
        return {}
    return {
        int(item_id): str(component_type or "whole").strip().lower()
        for item_id, component_type in db.execute(
            select(
                RequisitionItemBomSource.requisition_item_id,
                RequisitionItemBomSource.component_type,
            ).where(
                RequisitionItemBomSource.requisition_item_id.in_(
                    requisition_item_ids
                )
            )
        ).all()
    }


def _requisition_component_kind(
    db: Session,
    requisition_item: RequisitionItem,
) -> str:
    return _source_component_types(db, [requisition_item.id]).get(
        requisition_item.id,
        _component_kind(requisition_item.product_name_snapshot),
    )


def _is_component_key(value: int | str) -> bool:
    return isinstance(value, str) and value.startswith("r") and value[1:].isdigit()


def _is_stock_replenishment_key(value: int | str) -> bool:
    text = str(value)
    return text.startswith("sr") and text[2:].isdigit()


def _is_supplier_order_item_key(value: int | str) -> bool:
    text = str(value)
    return text.startswith("so") and text[2:].isdigit()


def _supplier_order_item_route_id(value: int | str) -> int:
    if not _is_supplier_order_item_key(value):
        raise ValueError("供应商报料明细ID无效")
    return int(str(value)[2:])


def _component_id(value: int | str) -> int:
    if _is_component_key(value):
        return int(str(value)[1:])
    return int(value)


def _apply_component_crease(data: dict, component: str) -> None:
    if component == "base":
        data["snapshot_crease_type"] = data.get("snapshot_base_crease_type")
        data["snapshot_crease_left_mm"] = data.get("snapshot_base_crease_left_mm")
        data["snapshot_crease_middle_mm"] = data.get("snapshot_base_crease_middle_mm")
        data["snapshot_crease_right_mm"] = data.get("snapshot_base_crease_right_mm")


def _supplier_order_item_overlay(
    db: Session,
    supplier_item: SupplierRequisitionOrderItem,
) -> dict:
    """Project one formal supplier line as one physical incoming route."""

    supplier_order = db.get(
        SupplierRequisitionOrder,
        supplier_item.supplier_order_id,
    )
    component = supplier_order_item_component_type(supplier_item)
    length = supplier_item.report_length_mm
    width = supplier_item.report_width_mm
    # Historical supplier-order lines created before per-line dimension
    # snapshots were persisted can have NULL dimensions even though the
    # originating order line still has its immutable report-size snapshot.
    # Keep an explicitly stored supplier value, and fall back independently
    # for each missing edge so one-sided legacy data is still displayed.
    order_item = (
        db.get(OrderItem, supplier_item.order_item_id)
        if supplier_item.order_item_id is not None
        else None
    )
    if order_item is not None:
        length = length or (
            order_item.cardboard_len
            or order_item.snapshot_report_length_mm
        )
        width = width or (
            order_item.cardboard_width
            or order_item.snapshot_report_width_mm
        )
    return {
        "item_id": supplier_order_item_key(supplier_item.id),
        "order_item_id": supplier_item.order_item_id,
        "supplier_order_id": supplier_item.supplier_order_id,
        "supplier_order_item_id": supplier_item.id,
        "source_type": "supplier_order_item",
        "component_type": component,
        "product_id": supplier_item.product_id,
        "product_code": supplier_item.product_code,
        "product_name": supplier_item.product_name,
        "specification": (
            f"{length or '-'}×{width or '-'}"
        ),
        "material": clean_supplier_material_code(
            supplier_item.material_code_snapshot,
            supplier_item.layer_count_snapshot,
        ),
        "flute_type": supplier_item.flute_type_snapshot,
        "requisition_qty": int(supplier_item.requisition_qty or 0),
        "incoming_quantity": int(supplier_item.requisition_qty or 0),
        "cardboard_len": length,
        "cardboard_width": width,
        "snapshot_supplier_name": (
            supplier_item.supplier_name_snapshot
            or (supplier_order.supplier_name if supplier_order else None)
        ),
        "supplier_order_number": (
            supplier_order.order_number if supplier_order else None
        ),
        "special_process": supplier_item.cutting_mode,
    }


def _open_supplier_order_items(
    db: Session,
    supplier_items: list[SupplierRequisitionOrderItem],
) -> list[SupplierRequisitionOrderItem]:
    """Keep only physical supplier lines that still require a receipt fact."""

    if not supplier_items:
        return []
    item_ids = [item.id for item in supplier_items]
    facts_by_item: dict[int, list[IncomingReceiptItem]] = {}
    for fact in db.scalars(
        select(IncomingReceiptItem)
        .where(
            IncomingReceiptItem.supplier_order_item_id.in_(item_ids),
            IncomingReceiptItem.status == "posted",
        )
        .order_by(IncomingReceiptItem.id)
    ).all():
        facts_by_item.setdefault(int(fact.supplier_order_item_id), []).append(fact)
    open_items: list[SupplierRequisitionOrderItem] = []
    for item in supplier_items:
        facts = facts_by_item.get(item.id, [])
        received = sum(int(fact.received_quantity or 0) for fact in facts)
        latest = facts[-1] if facts else None
        if received >= int(item.requisition_qty or 0):
            continue
        if latest is not None and latest.resolution_action == "accept_short":
            continue
        open_items.append(item)
    return open_items


def _component_receive_times(
    db: Session,
    *,
    received_since: datetime,
) -> dict[int, datetime]:
    query = select(OperationLog).where(
        OperationLog.action == "RECEIVE_MATERIAL",
        OperationLog.details.like("%requisition_item_id%"),
    )
    if received_since.year > 2000:
        query = query.where(OperationLog.created_at >= received_since)
    times: dict[int, datetime] = {}
    for log in db.scalars(query.order_by(OperationLog.created_at.desc())).all():
        try:
            details = json.loads(log.details or "{}")
        except json.JSONDecodeError:
            continue
        requisition_item_id = details.get("requisition_item_id")
        if not requisition_item_id:
            continue
        times.setdefault(int(requisition_item_id), log.created_at)
    return times


def _visible_customer_ids(user: User, db: Session) -> set[int] | None:
    if has_unrestricted_customer_access(user, db):
        return None
    return customer_scope_ids(user, db)


def _pending_order_item_query(query):
    """Apply the authoritative pending-incoming eligibility and ordering."""
    has_receivable_requisition_item = (
        select(RequisitionItem.id)
        .where(
            RequisitionItem.order_item_id == OrderItem.id,
            RequisitionItem.status.in_(["有效", "supplier_requisition_created"]),
        )
        .exists()
    )
    return query.where(
        Order.status.notin_(["cancelled", "dead"]),
        OrderItem.material_status == "pending",
        or_(
            OrderItem.requisition_status.in_(["已报料", "供应商已排单"]),
            has_receivable_requisition_item,
        ),
    ).order_by(
        OrderItem.requisition_date.desc(),
        OrderItem.created_at.desc(),
        OrderItem.id.desc(),
    )


def _stock_replenishment_pending_query(query, *, db: Session, user: User):
    """Apply the authoritative stock-replenishment incoming eligibility."""
    query = query.where(
        StockReplenishmentOrder.status.in_(("confirmed", "partially_stocked")),
        StockReplenishmentOrderItem.stocked_quantity
        < StockReplenishmentOrderItem.quantity,
    )
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is not None:
        query = query.where(
            StockReplenishmentOrderItem.customer_id.in_(visible_customer_ids)
        )
    return query.order_by(
        StockReplenishmentOrder.created_at.desc(),
        StockReplenishmentOrderItem.id.desc(),
    )


def _require_order_item_customer_access(
    db: Session,
    *,
    order_item_id: int,
    user: User,
) -> None:
    customer_id = db.scalar(
        select(Order.customer_id)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(OrderItem.id == order_item_id)
    )
    if customer_id is not None:
        require_customer_access(customer_id, user, db)


def _preflight_item_customer_access(
    db: Session,
    *,
    item_id: int | str,
    user: User,
) -> None:
    if _is_stock_replenishment_key(item_id):
        customer_id = db.scalar(
            select(StockReplenishmentOrderItem.customer_id).where(
                StockReplenishmentOrderItem.id == int(str(item_id)[2:])
            )
        )
        if customer_id is not None:
            require_customer_access(customer_id, user, db)
        return
    if _is_supplier_order_item_key(item_id):
        order_item_id = db.scalar(
            select(SupplierRequisitionOrderItem.order_item_id).where(
                SupplierRequisitionOrderItem.id
                == _supplier_order_item_route_id(item_id)
            )
        )
        if order_item_id is not None:
            _require_order_item_customer_access(
                db,
                order_item_id=int(order_item_id),
                user=user,
            )
        return
    if _is_component_key(item_id):
        order_item_id = db.scalar(
            select(RequisitionItem.order_item_id).where(
                RequisitionItem.id == _component_id(item_id)
            )
        )
    else:
        try:
            order_item_id = int(item_id)
        except (TypeError, ValueError):
            return
    if order_item_id is not None:
        _require_order_item_customer_access(
            db,
            order_item_id=order_item_id,
            user=user,
        )


def _stock_replenishment_pending_rows(
    db: Session,
    *,
    user: User,
    stock_replenishment_item_ids: set[int] | None = None,
) -> list[dict]:
    if stock_replenishment_item_ids is not None and not stock_replenishment_item_ids:
        return []
    query = (
        select(StockReplenishmentOrderItem, StockReplenishmentOrder)
        .join(
            StockReplenishmentOrder,
            StockReplenishmentOrder.id
            == StockReplenishmentOrderItem.replenishment_order_id,
        )
        .options(
            selectinload(StockReplenishmentOrderItem.customer),
            selectinload(StockReplenishmentOrderItem.product),
        )
    )
    if stock_replenishment_item_ids is not None:
        query = query.where(
            StockReplenishmentOrderItem.id.in_(stock_replenishment_item_ids)
        )
    query = _stock_replenishment_pending_query(query, db=db, user=user)
    rows: list[dict] = []
    for item, order in db.execute(query):
        customer_name = item.customer.name if item.customer else ""
        rows.append(
            {
                "item_id": f"sr{item.id}",
                "stock_replenishment_order_id": order.id,
                "stock_replenishment_item_id": item.id,
                "source_type": "stock_replenishment",
                "product_id": item.product_id,
                "order_id": None,
                "customer_id": item.customer_id,
                "order_number": order.order_number,
                "display_order_number": order.order_number,
                "customer_po": None,
                "created_at": order.created_at,
                "customer_name": customer_name,
                "product_name": item.product_name_snapshot,
                "product_code": item.product_code_snapshot,
                "specification": (
                    f"{item.report_length_mm or '-'}×{item.report_width_mm or '-'}"
                ),
                "material": item.material_code_snapshot,
                "material_code": item.material_code_snapshot,
                "flute_type": item.flute_type,
                "material_display": " / ".join(
                    value
                    for value in (
                        (item.material_code_snapshot or "").strip(),
                        (item.flute_type or "").strip(),
                    )
                    if value
                ),
                "quantity": item.quantity,
                "delivery_date": None,
                "order_status": order.status,
                "material_status": "pending",
                "requisition_status": "已报料",
                "requisition_qty": item.quantity,
                "incoming_quantity": item.quantity,
                "requisition_date": order.confirmed_at or order.created_at,
                "requisition_spec": None,
                "cardboard_len": item.report_length_mm,
                "cardboard_width": item.report_width_mm,
                "snapshot_crease_type": item.crease_type,
                "snapshot_crease_left_mm": item.crease_left_mm,
                "snapshot_crease_middle_mm": item.crease_middle_mm,
                "snapshot_crease_right_mm": item.crease_right_mm,
                "snapshot_base_crease_type": None,
                "snapshot_base_crease_left_mm": None,
                "snapshot_base_crease_middle_mm": None,
                "snapshot_base_crease_right_mm": None,
                "snapshot_supplier_name": order.supplier_name,
                "requisition_remark": item.remark or order.remark,
                "special_process": None,
                "supplier_delivery_time": None,
                "supplier_order_number": order.order_number,
                "material_received_at": None,
                "material_received_by": None,
                "received_by_name": None,
                "component_type": item.component_type,
                "drawing_path": None,
                "drawing_is_pdf": False,
                "can_revert_receipt": False,
            }
        )
    return rows


def _confirmed_supplier_order_item_ids(
    db: Session,
    *,
    order_item_ids: list[int],
) -> set[int]:
    if not order_item_ids:
        return set()
    return set(
        db.scalars(
            select(OrderItem.id)
            .join(
                SupplierRequisitionOrder,
                (SupplierRequisitionOrder.order_number == OrderItem.supplier_order_number)
                & (SupplierRequisitionOrder.status == "confirmed"),
            )
            .join(
                SupplierRequisitionOrderItem,
                (SupplierRequisitionOrderItem.supplier_order_id == SupplierRequisitionOrder.id)
                & (SupplierRequisitionOrderItem.order_item_id == OrderItem.id),
            )
            .where(
                OrderItem.id.in_(order_item_ids),
                SupplierRequisitionOrderItem.status == "active",
            )
            .distinct()
        ).all()
    )


def _active_requisition_components(
    db: Session,
    *,
    order_item_ids: list[int],
    include_received: bool = False,
) -> list[RequisitionItem]:
    """Return only the requisition lines that the incoming workflow may use.

    Supplier-created requisition rows do not point directly at a supplier order.
    The order item's current supplier-order number is therefore the authority for
    selecting the latest matching requisition group.  Legacy effective rows keep
    their original behavior for order items without such a current supplier order.
    """
    if not order_item_ids:
        return []

    supplier_order_item_ids = _confirmed_supplier_order_item_ids(
        db,
        order_item_ids=order_item_ids,
    )
    legacy_order_item_ids = set(order_item_ids) - supplier_order_item_ids
    components: list[RequisitionItem] = []

    legacy_statuses = ["有效"]
    if include_received:
        legacy_statuses.append("已入库")
    if legacy_order_item_ids:
        components.extend(
            db.scalars(
                select(RequisitionItem)
                .where(
                    RequisitionItem.order_item_id.in_(legacy_order_item_ids),
                    RequisitionItem.status.in_(legacy_statuses),
                )
                .order_by(RequisitionItem.order_item_id, RequisitionItem.id)
            ).all()
        )

    supplier_statuses = ["supplier_requisition_created"]
    if include_received:
        supplier_statuses.append("已入库")
    if supplier_order_item_ids:
        latest_groups = (
            select(
                RequisitionItem.order_item_id.label("order_item_id"),
                func.max(RequisitionItem.requisition_id).label("requisition_id"),
            )
            .join(Requisition, Requisition.id == RequisitionItem.requisition_id)
            .where(
                RequisitionItem.order_item_id.in_(supplier_order_item_ids),
                Requisition.status == "supplier_requisition_created",
            )
            .group_by(RequisitionItem.order_item_id)
            .subquery()
        )
        components.extend(
            db.scalars(
                select(RequisitionItem)
                .join(
                    latest_groups,
                    (latest_groups.c.order_item_id == RequisitionItem.order_item_id)
                    & (latest_groups.c.requisition_id == RequisitionItem.requisition_id),
                )
                .where(RequisitionItem.status.in_(supplier_statuses))
                .order_by(RequisitionItem.order_item_id, RequisitionItem.id)
            ).all()
        )
    return components


def _is_a3_snapshot(snapshot: SalesOrderItemBomComponent) -> bool:
    box_style = (snapshot.snapshot_component_box_style or "").strip().upper()
    return bool(box_style) and ("天地盖" in box_style or "A3" in box_style)


def _is_surround_snapshot(snapshot: SalesOrderItemBomComponent) -> bool:
    box_style = (snapshot.snapshot_component_box_style or "").strip()
    product_name = (snapshot.snapshot_component_product_name or "").strip()
    return box_style in {"围板", "围套"} or "围板" in product_name


def _is_set_only_a3_surround_bom(
    snapshots: list[SalesOrderItemBomComponent],
) -> bool:
    if len(snapshots) != 2:
        return False
    return (
        all(int(snapshot.quantity_per_set) == 1 for snapshot in snapshots)
        and sum(1 for snapshot in snapshots if _is_a3_snapshot(snapshot)) == 1
        and sum(
            1 for snapshot in snapshots if _is_surround_snapshot(snapshot)
        )
        == 1
    )


def _all_expected_bom_material_received(
    db: Session,
    *,
    order_item_id: int,
) -> bool:
    snapshots = db.scalars(
        select(SalesOrderItemBomComponent)
        .where(
            SalesOrderItemBomComponent.sales_order_item_id == order_item_id
        )
        .order_by(
            SalesOrderItemBomComponent.display_order,
            SalesOrderItemBomComponent.id,
        )
    ).all()
    if not snapshots:
        return True

    received_sources = db.execute(
        select(
            RequisitionItemBomSource.sales_order_item_bom_component_id,
            RequisitionItemBomSource.component_type,
        )
        .join(
            RequisitionItem,
            RequisitionItem.id
            == RequisitionItemBomSource.requisition_item_id,
        )
        .where(
            RequisitionItem.order_item_id == order_item_id,
            RequisitionItem.status == "已入库",
        )
    ).all()
    received_by_snapshot: dict[int, set[str]] = {}
    for snapshot_id, component_type in received_sources:
        received_by_snapshot.setdefault(int(snapshot_id), set()).add(
            str(component_type or "whole").strip().lower()
        )
    for snapshot in snapshots:
        received_types = received_by_snapshot.get(snapshot.id, set())
        required_types = (
            {"cover", "base"} if _is_a3_snapshot(snapshot) else {"whole"}
        )
        if "whole" in received_types:
            continue
        if not required_types.issubset(received_types):
            return False

    if _is_set_only_a3_surround_bom(snapshots):
        return True
    parent_received = db.scalar(
        select(RequisitionItem.id)
        .where(
            RequisitionItem.order_item_id == order_item_id,
            RequisitionItem.status == "已入库",
            ~select(RequisitionItemBomSource.id)
            .where(
                RequisitionItemBomSource.requisition_item_id
                == RequisitionItem.id
            )
            .exists(),
        )
        .limit(1)
    )
    return parent_received is not None


_DASHBOARD_PENDING_INCOMING_KEYS = (
    "item_id",
    "order_item_id",
    "requisition_item_id",
    "supplier_order_id",
    "supplier_order_item_id",
    "stock_replenishment_item_id",
    "customer_id",
    "customer_name",
    "order_number",
    "product_code",
    "delivery_date",
    "created_at",
)


def _pending_incoming_route_rows(db: Session, user: User) -> list[dict]:
    """Return final pending route identities plus lightweight search facts.

    The route eligibility is shared by the dashboard, paged incoming list and
    the mobile dimension search.  It deliberately does not enter drawing,
    receipt-summary, material-master or location decoration.
    """
    query = (
        select(
            OrderItem.id.label("item_id"),
            OrderItem.id.label("order_item_id"),
            Order.id.label("order_id"),
            Customer.id.label("customer_id"),
            Customer.name.label("customer_name"),
            Customer.customer_code.label("customer_code"),
            Order.order_number,
            Order.customer_po,
            Order.created_at,
            Order.delivery_date,
            func.coalesce(
                OrderItem.snapshot_product_code,
                Product.product_code,
            ).label("product_code"),
            OrderItem.snapshot_product_name.label("product_name"),
            OrderItem.snapshot_spec.label("specification"),
            Product.length_mm.label("product_length_mm"),
            Product.width_mm.label("product_width_mm"),
            Product.height_mm.label("product_height_mm"),
            OrderItem.snapshot_material.label("material"),
            OrderItem.flute_type,
            OrderItem.cardboard_len,
            OrderItem.cardboard_width,
            OrderItem.snapshot_crease_type,
            OrderItem.snapshot_crease_left_mm,
            OrderItem.snapshot_crease_middle_mm,
            OrderItem.snapshot_crease_right_mm,
            OrderItem.snapshot_supplier_name,
            OrderItem.requisition_qty,
            OrderItem.quantity,
        )
        .join(Order, Order.id == OrderItem.order_id)
        .join(Product, Product.id == OrderItem.product_id)
        .join(Customer, Customer.id == Order.customer_id)
    )
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is not None:
        query = query.where(Order.customer_id.in_(visible_customer_ids))
    query = _pending_order_item_query(query)

    base_rows = []
    for row in db.execute(query):
        data = dict(row._mapping)
        data["specification"] = resolved_product_specification(
            data.get("specification"),
            length_mm=data.pop("product_length_mm", None),
            width_mm=data.pop("product_width_mm", None),
            height_mm=data.pop("product_height_mm", None),
        )
        base_rows.append(data)
    order_item_ids = [row["item_id"] for row in base_rows]
    order_items_with_requisitions: set[int] = set()
    order_items_with_current_supplier_orders: set[int] = set()
    active_by_order_item: dict[int, list[RequisitionItem]] = {}
    supplier_by_order_item: dict[int, list[SupplierRequisitionOrderItem]] = {}
    if order_item_ids:
        order_items_with_requisitions = set(
            db.scalars(
                select(RequisitionItem.order_item_id).where(
                    RequisitionItem.order_item_id.in_(order_item_ids)
                )
            ).all()
        )
        for requisition_item in _active_requisition_components(
            db,
            order_item_ids=order_item_ids,
        ):
            active_by_order_item.setdefault(
                requisition_item.order_item_id, []
            ).append(requisition_item)
        current_supplier_items = current_supplier_order_items(db, order_item_ids)
        order_items_with_current_supplier_orders = {
            int(supplier_item.order_item_id)
            for supplier_item in current_supplier_items
            if supplier_item.order_item_id is not None
        }
        for supplier_item in _open_supplier_order_items(
            db,
            current_supplier_items,
        ):
            if supplier_item.order_item_id is not None:
                # Historical cancelled/closed requisition rows must not hide a
                # newer confirmed supplier requisition.  Only a currently
                # receivable requisition route may take precedence over the
                # supplier-order physical line.
                if int(supplier_item.order_item_id) in active_by_order_item:
                    continue
                supplier_by_order_item.setdefault(
                    int(supplier_item.order_item_id), []
                ).append(supplier_item)

    rows: list[dict] = []
    for data in base_rows:
        requisition_rows = active_by_order_item.get(data["item_id"], [])
        if requisition_rows:
            for requisition_item in requisition_rows:
                component_data = dict(data)
                component_data["order_item_id"] = data["item_id"]
                component_data["requisition_item_id"] = requisition_item.id
                component_data["item_id"] = f"r{requisition_item.id}"
                component_data["product_code"] = (
                    requisition_item.product_code_snapshot
                    or data.get("product_code")
                )
                component_data["product_name"] = (
                    requisition_item.product_name_snapshot
                    or data.get("product_name")
                )
                component_data["specification"] = resolved_product_specification(
                    requisition_item.specification_snapshot,
                    fallback_snapshots=(data.get("specification"),),
                )
                component_data["material"] = (
                    requisition_item.material_snapshot
                    or data.get("material")
                )
                component_data["cardboard_len"] = requisition_item.cardboard_len
                component_data["cardboard_width"] = requisition_item.cardboard_width
                component_data["requisition_qty"] = requisition_item.requisition_qty
                rows.append(component_data)
        elif supplier_rows := supplier_by_order_item.get(data["item_id"], []):
            for supplier_item in supplier_rows:
                component_data = dict(data)
                component_data.update(
                    _supplier_order_item_overlay(db, supplier_item)
                )
                rows.append(component_data)
        elif (
            data["item_id"] not in order_items_with_requisitions
            and data["item_id"] not in order_items_with_current_supplier_orders
        ):
            rows.append(data)

    history_order_ids = {
        int(row["order_id"])
        for row in rows
        if is_history_order_number(row.get("order_number"))
    }
    registry = build_display_registry_for_order_ids(
        db,
        history_order_ids,
    )
    for row in rows:
        display = (
            registry.by_order_id.get(row["order_id"], row.get("order_number"))
            if history_order_ids
            else row.get("order_number")
        )
        row["order_number"] = display
        row.pop("order_id", None)

    stock_query = (
        select(
            StockReplenishmentOrderItem.id.label(
                "stock_replenishment_item_id"
            ),
            StockReplenishmentOrderItem.customer_id,
            Customer.name.label("customer_name"),
            Customer.customer_code.label("customer_code"),
            StockReplenishmentOrder.order_number,
            StockReplenishmentOrder.supplier_name.label("snapshot_supplier_name"),
            StockReplenishmentOrder.created_at,
            StockReplenishmentOrderItem.product_code_snapshot.label(
                "product_code"
            ),
            StockReplenishmentOrderItem.product_name_snapshot.label("product_name"),
            StockReplenishmentOrderItem.material_code_snapshot.label("material"),
            StockReplenishmentOrderItem.flute_type,
            StockReplenishmentOrderItem.report_length_mm.label("cardboard_len"),
            StockReplenishmentOrderItem.report_width_mm.label("cardboard_width"),
            StockReplenishmentOrderItem.crease_type.label("snapshot_crease_type"),
            StockReplenishmentOrderItem.crease_left_mm.label(
                "snapshot_crease_left_mm"
            ),
            StockReplenishmentOrderItem.crease_middle_mm.label(
                "snapshot_crease_middle_mm"
            ),
            StockReplenishmentOrderItem.crease_right_mm.label(
                "snapshot_crease_right_mm"
            ),
            StockReplenishmentOrderItem.quantity,
            (
                StockReplenishmentOrderItem.quantity
                - StockReplenishmentOrderItem.stocked_quantity
            ).label("requisition_qty"),
        )
        .join(
            StockReplenishmentOrder,
            StockReplenishmentOrder.id
            == StockReplenishmentOrderItem.replenishment_order_id,
        )
        .outerjoin(Customer, Customer.id == StockReplenishmentOrderItem.customer_id)
    )
    stock_query = _stock_replenishment_pending_query(
        stock_query,
        db=db,
        user=user,
    )
    for result in db.execute(stock_query):
        stock_row = dict(result._mapping)
        stock_row["item_id"] = (
            f"sr{stock_row['stock_replenishment_item_id']}"
        )
        stock_row["customer_name"] = stock_row.get("customer_name") or ""
        stock_row["delivery_date"] = None
        rows.append(stock_row)
    return rows


def dashboard_pending_incoming_rows(db: Session, user: User) -> list[dict]:
    """Return the stable narrow projection consumed by the dashboard."""

    projection = [
        {key: row.get(key) for key in _DASHBOARD_PENDING_INCOMING_KEYS}
        for row in _pending_incoming_route_rows(db, user)
    ]
    # Preserve the historical dashboard contract: ordinary order-item routes
    # expose their identity through ``item_id`` only.  The internal route plan
    # retains ``order_item_id`` for F1's exact production-detail navigation.
    for row in projection:
        if (
            row.get("requisition_item_id") is None
            and row.get("supplier_order_item_id") is None
        ):
            row["order_item_id"] = None
    return projection


def _rows(
    db: Session,
    *,
    user: User,
    received_since: datetime | None = None,
    selected_pending_routes: list[dict] | None = None,
) -> list[dict]:
    if received_since is not None and selected_pending_routes is not None:
        raise ValueError("received rows cannot use pending-route selection")

    selected_route_ids: list[int | str] | None = None
    selected_order_item_ids: set[int] | None = None
    selected_ordinary_item_ids: set[int] | None = None
    selected_requisition_item_ids: set[int] | None = None
    selected_supplier_order_item_ids: set[int] | None = None
    selected_stock_item_ids: set[int] | None = None
    if selected_pending_routes is not None:
        selected_route_ids = [row["item_id"] for row in selected_pending_routes]
        selected_order_item_ids = {
            int(row["order_item_id"])
            for row in selected_pending_routes
            if row.get("order_item_id") is not None
        }
        selected_ordinary_item_ids = {
            int(row["item_id"])
            for row in selected_pending_routes
            if isinstance(row.get("item_id"), int)
        }
        selected_order_item_ids.update(selected_ordinary_item_ids)
        selected_requisition_item_ids = {
            int(row["requisition_item_id"])
            for row in selected_pending_routes
            if row.get("requisition_item_id") is not None
        }
        selected_supplier_order_item_ids = {
            int(row["supplier_order_item_id"])
            for row in selected_pending_routes
            if row.get("supplier_order_item_id") is not None
        }
        selected_stock_item_ids = {
            int(row["stock_replenishment_item_id"])
            for row in selected_pending_routes
            if row.get("stock_replenishment_item_id") is not None
        }

    receiver = aliased(User)
    query = (
        select(
            OrderItem.id.label("item_id"),
            OrderItem.product_id,
            Order.id.label("order_id"),
            Customer.id.label("customer_id"),
            Order.order_number,
            Order.customer_po,
            Order.created_at,
            Customer.name.label("customer_name"),
            OrderItem.snapshot_product_name.label("product_name"),
            func.coalesce(
                OrderItem.snapshot_product_code,
                Product.product_code,
            ).label("product_code"),
            OrderItem.snapshot_spec.label("specification"),
            Product.length_mm.label("product_length_mm"),
            Product.width_mm.label("product_width_mm"),
            Product.height_mm.label("product_height_mm"),
            OrderItem.snapshot_material.label("material"),
            OrderItem.flute_type,
            OrderItem.quantity,
            Order.delivery_date,
            Order.status.label("order_status"),
            OrderItem.material_status,
            OrderItem.requisition_status,
            OrderItem.requisition_qty,
            OrderItem.requisition_date,
            OrderItem.requisition_spec,
            OrderItem.cardboard_len,
            OrderItem.cardboard_width,
            OrderItem.snapshot_crease_type,
            OrderItem.snapshot_crease_left_mm,
            OrderItem.snapshot_crease_middle_mm,
            OrderItem.snapshot_crease_right_mm,
            OrderItem.snapshot_base_crease_type,
            OrderItem.snapshot_base_crease_left_mm,
            OrderItem.snapshot_base_crease_middle_mm,
            OrderItem.snapshot_base_crease_right_mm,
            OrderItem.snapshot_supplier_name,
            OrderItem.requisition_remark,
            OrderItem.special_process,
            OrderItem.supplier_delivery_time,
            OrderItem.supplier_order_number,
            OrderItem.material_received_at,
            OrderItem.material_received_by,
            OrderItem.drawing_file.label("order_item_drawing_file"),
            receiver.real_name.label("received_by_name"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .join(Product, Product.id == OrderItem.product_id)
        .join(Customer, Customer.id == Order.customer_id)
        .outerjoin(receiver, receiver.id == OrderItem.material_received_by)
    )
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is not None:
        query = query.where(Order.customer_id.in_(visible_customer_ids))
    if selected_order_item_ids is not None:
        query = query.where(OrderItem.id.in_(selected_order_item_ids))
    if received_since is None:
        query = _pending_order_item_query(query)
    else:
        query = query.where(
            OrderItem.material_status == "received",
            OrderItem.material_received_at >= received_since,
        ).order_by(
            OrderItem.material_received_at.desc(),
            OrderItem.id.desc(),
        )
    registry = (
        build_display_registry(db)
        if selected_pending_routes is None
        else None
    )
    base_rows = []
    for row in db.execute(query):
        data = dict(row._mapping)
        data["specification"] = resolved_product_specification(
            data.get("specification"),
            length_mm=data.pop("product_length_mm", None),
            width_mm=data.pop("product_width_mm", None),
            height_mm=data.pop("product_height_mm", None),
        )
        data["incoming_quantity"] = (
            data["requisition_qty"]
            if data.get("requisition_qty") is not None
            else data["quantity"]
        )
        base_rows.append(data)
    if selected_pending_routes is not None:
        history_order_ids = {
            int(row["order_id"])
            for row in base_rows
            if is_history_order_number(row.get("order_number"))
        }
        registry = build_display_registry_for_order_ids(db, history_order_ids)
    assert registry is not None

    rows = []
    received_component_order_item_ids: set[int] = set()
    component_types_by_requisition_item: dict[int, str] = {}
    if received_since is not None:
        receive_times = _component_receive_times(db, received_since=received_since)
        component_query = (
            select(
                RequisitionItem,
                OrderItem,
                Order,
                Product,
                Customer,
                receiver.real_name.label("received_by_name"),
            )
            .join(OrderItem, OrderItem.id == RequisitionItem.order_item_id)
            .join(Order, Order.id == OrderItem.order_id)
            .join(Product, Product.id == OrderItem.product_id)
            .join(Customer, Customer.id == Order.customer_id)
            .outerjoin(receiver, receiver.id == OrderItem.material_received_by)
            .where(RequisitionItem.status == "已入库")
            .order_by(RequisitionItem.id.desc())
        )
        if visible_customer_ids is not None:
            component_query = component_query.where(
                Order.customer_id.in_(visible_customer_ids)
            )
        component_records = db.execute(component_query).all()
        component_types_by_requisition_item.update(
            _source_component_types(
                db,
                [record[0].id for record in component_records],
            )
        )
        for (
            req,
            item,
            order,
            product,
            customer,
            received_by_name,
        ) in component_records:
            component = component_types_by_requisition_item.get(
                req.id,
                _component_kind(req.product_name_snapshot),
            )
            if not component:
                continue
            received_at = (
                receive_times.get(req.id)
                or item.material_received_at
                or req.created_at
            )
            if received_since.year > 2000 and received_at < received_since:
                continue
            received_component_order_item_ids.add(item.id)
            component_data = {
                "item_id": f"r{req.id}",
                "order_item_id": item.id,
                "requisition_item_id": req.id,
                "component_type": component,
                "product_id": item.product_id,
                "order_id": order.id,
                "customer_id": order.customer_id,
                "order_number": order.order_number,
                "customer_po": order.customer_po,
                "customer_name": customer.name,
                "product_name": req.product_name_snapshot or item.snapshot_product_name,
                "product_code": req.product_code_snapshot
                or item.snapshot_product_code
                or product.product_code,
                "specification": resolved_product_specification(
                    req.specification_snapshot,
                    product,
                    fallback_snapshots=(item.snapshot_spec,),
                ),
                "material": req.material_snapshot or item.snapshot_material,
                "flute_type": item.flute_type,
                "quantity": item.quantity,
                "delivery_date": order.delivery_date,
                "order_status": order.status,
                "material_status": item.material_status,
                "requisition_status": item.requisition_status,
                "requisition_qty": req.requisition_qty,
                "incoming_quantity": req.requisition_qty,
                "requisition_date": item.requisition_date,
                "requisition_spec": item.requisition_spec,
                "cardboard_len": req.cardboard_len,
                "cardboard_width": req.cardboard_width,
                "snapshot_crease_type": item.snapshot_crease_type,
                "snapshot_crease_left_mm": item.snapshot_crease_left_mm,
                "snapshot_crease_middle_mm": item.snapshot_crease_middle_mm,
                "snapshot_crease_right_mm": item.snapshot_crease_right_mm,
                "snapshot_base_crease_type": item.snapshot_base_crease_type,
                "snapshot_base_crease_left_mm": item.snapshot_base_crease_left_mm,
                "snapshot_base_crease_middle_mm": item.snapshot_base_crease_middle_mm,
                "snapshot_base_crease_right_mm": item.snapshot_base_crease_right_mm,
                "snapshot_supplier_name": item.snapshot_supplier_name,
                "requisition_remark": req.remark or item.requisition_remark,
                "special_process": req.special_process,
                "supplier_delivery_time": item.supplier_delivery_time,
                "supplier_order_number": item.supplier_order_number,
                "material_received_at": received_at,
                "material_received_by": item.material_received_by,
                "order_item_drawing_file": item.drawing_file,
                "received_by_name": received_by_name,
            }
            _apply_component_crease(component_data, component)
            rows.append(component_data)

    component_requisition_items: dict[int, list[RequisitionItem]] = {}
    supplier_order_items: dict[int, list[SupplierRequisitionOrderItem]] = {}
    order_items_with_requisitions: set[int] = set()
    order_items_with_current_supplier_orders: set[int] = set()
    order_item_ids = [row["item_id"] for row in base_rows if row.get("item_id")]
    if order_item_ids:
        order_items_with_requisitions = set(
            db.scalars(
                select(RequisitionItem.order_item_id).where(
                    RequisitionItem.order_item_id.in_(order_item_ids)
                )
            ).all()
        )
        if received_since is None:
            req_rows = _active_requisition_components(
                db,
                order_item_ids=order_item_ids,
            )
        else:
            req_rows = db.scalars(
                select(RequisitionItem)
                .where(
                    RequisitionItem.order_item_id.in_(order_item_ids),
                    RequisitionItem.status == "已入库",
                )
                .order_by(RequisitionItem.order_item_id, RequisitionItem.id)
            ).all()
        for req in req_rows:
            if received_since is not None and req.status != "已入库":
                continue
            if (
                selected_requisition_item_ids is not None
                and req.id not in selected_requisition_item_ids
            ):
                continue
            component_requisition_items.setdefault(req.order_item_id, []).append(req)
        if received_since is None:
            current_supplier_items = current_supplier_order_items(
                db,
                order_item_ids,
            )
            order_items_with_current_supplier_orders = {
                int(supplier_item.order_item_id)
                for supplier_item in current_supplier_items
                if supplier_item.order_item_id is not None
            }
            for supplier_item in _open_supplier_order_items(
                db,
                current_supplier_items,
            ):
                if supplier_item.order_item_id is None:
                    continue
                # Keep old requisition history as a fallback-suppression fact,
                # but do not let it suppress the current confirmed supplier
                # lines unless an active requisition route actually exists.
                if int(supplier_item.order_item_id) in component_requisition_items:
                    continue
                if (
                    selected_supplier_order_item_ids is not None
                    and supplier_item.id not in selected_supplier_order_item_ids
                ):
                    continue
                supplier_order_items.setdefault(
                    int(supplier_item.order_item_id), []
                ).append(supplier_item)
    component_types_by_requisition_item.update(
        _source_component_types(
            db,
            [
                req.id
                for requisition_rows in component_requisition_items.values()
                for req in requisition_rows
            ],
        )
    )

    for data in base_rows:
        if data["item_id"] in received_component_order_item_ids:
            continue
        req_rows = component_requisition_items.get(data["item_id"], [])
        if req_rows:
            for req in req_rows:
                if (
                    selected_requisition_item_ids is not None
                    and req.id not in selected_requisition_item_ids
                ):
                    continue
                component = component_types_by_requisition_item.get(
                    req.id,
                    _component_kind(req.product_name_snapshot),
                )
                component_data = dict(data)
                component_data["order_item_id"] = data["item_id"]
                component_data["requisition_item_id"] = req.id
                # Any row backed by a concrete requisition item must use its
                # own route key.  Otherwise a normal single-piece row falls
                # through to the order-item receive/revert path and leaves the
                # selected supplier requisition row in the wrong status.
                component_data["item_id"] = f"r{req.id}"
                component_data["component_type"] = component or "single"
                component_data["product_code"] = req.product_code_snapshot or data.get("product_code")
                component_data["product_name"] = req.product_name_snapshot or data.get("product_name")
                component_data["material"] = req.material_snapshot or data.get("material")
                component_data["incoming_quantity"] = req.requisition_qty
                component_data["requisition_qty"] = req.requisition_qty
                component_data["cardboard_len"] = req.cardboard_len
                component_data["cardboard_width"] = req.cardboard_width
                component_data["special_process"] = req.special_process
                component_data["requisition_remark"] = req.remark or data.get("requisition_remark")
                _apply_component_crease(component_data, component)
                rows.append(component_data)
        elif supplier_rows := supplier_order_items.get(data["item_id"], []):
            for supplier_item in supplier_rows:
                component_data = dict(data)
                component_data.update(
                    _supplier_order_item_overlay(db, supplier_item)
                )
                _apply_component_crease(
                    component_data,
                    component_data["component_type"],
                )
                rows.append(component_data)
        elif (
            data["item_id"] in order_items_with_requisitions
            or data["item_id"] in order_items_with_current_supplier_orders
        ):
            continue
        elif (
            selected_ordinary_item_ids is not None
            and data["item_id"] not in selected_ordinary_item_ids
        ):
            continue
        else:
            rows.append(data)
    rows = _decorate_rows_with_display_numbers(db, rows, registry)
    product_ids = {row["product_id"] for row in rows if row.get("product_id")}

    # v0.23.0 P0-4/P0-5：明细快照（订单/报料时写入）优先；快照缺失时才回退到
    # 常用箱当前值——绝不从材质字典反查楞型，只读常用箱自身的 flute_type /
    # crease_*_mm 字段。历史数据不做任何回写，只在展示时按需回退。
    products_by_id: dict[int, Product] = {}
    if product_ids:
        products_by_id = {
            product.id: product
            for product in db.scalars(
                select(Product)
                .options(selectinload(Product.material))
                .where(Product.id.in_(product_ids))
            ).all()
        }
    for data in rows:
        product = products_by_id.get(data.get("product_id"))

        material_code = (data.get("material") or "").strip()
        if not material_code and product is not None:
            fallback_code = (
                product.material.code if product.material is not None else None
            ) or product.legacy_material_text
            material_code = (fallback_code or "").strip()

        flute_type = (data.get("flute_type") or "").strip()
        if not flute_type and product is not None:
            flute_type = (product.flute_type or "").strip()

        data["material_code"] = material_code
        data["flute_type"] = flute_type
        if material_code and flute_type:
            data["material_display"] = f"{material_code} / {flute_type}"
        else:
            data["material_display"] = material_code

        if product is not None:
            if not (data.get("snapshot_crease_type") or "").strip():
                data["snapshot_crease_type"] = product.crease_type
            if data.get("snapshot_crease_left_mm") is None:
                data["snapshot_crease_left_mm"] = product.crease_left_mm
            if data.get("snapshot_crease_middle_mm") is None:
                data["snapshot_crease_middle_mm"] = product.crease_middle_mm
            if data.get("snapshot_crease_right_mm") is None:
                data["snapshot_crease_right_mm"] = product.crease_right_mm

    latest_drawings: dict[int, ProductDrawing] = {}
    if product_ids:
        drawings = db.scalars(
            select(ProductDrawing)
            .where(ProductDrawing.product_id.in_(product_ids))
            .order_by(
                ProductDrawing.product_id,
                ProductDrawing.uploaded_at.desc(),
                ProductDrawing.id.desc(),
            )
        ).all()
        for drawing in drawings:
            latest_drawings.setdefault(drawing.product_id, drawing)
    stock_replenishment_rows = (
        _stock_replenishment_pending_rows(
            db,
            user=user,
            stock_replenishment_item_ids=selected_stock_item_ids,
        )
        if received_since is None
        else []
    )
    read_context = _PendingIncomingReadContext(
        db,
        [*rows, *stock_replenishment_rows],
    )
    for row in rows:
        # v0.23.0 P0-3：订单/明细上传的图纸优先于常用箱图纸——车间来料页面
        # 需要能看到"这一单"实际上传的图纸，而不仅仅是常用箱历史图纸。
        product_drawing = latest_drawings.get(row.get("product_id"))
        product_drawing_reference = product_drawing.image_path if product_drawing else None
        order_drawing_reference = (row.get("order_item_drawing_file") or "").strip() or None
        product_drawing_path = _product_drawing_url(product_drawing)
        drawing_order_item_id = row.get("order_item_id") or row["item_id"]
        order_drawing_path = _order_drawing_url(
            int(drawing_order_item_id),
            order_drawing_reference,
        )
        final_path = order_drawing_path or product_drawing_path
        row["order_drawing_path"] = order_drawing_path
        row["product_drawing_path"] = product_drawing_path
        row["drawing_path"] = final_path
        final_reference = order_drawing_reference or product_drawing_reference
        row["drawing_is_pdf"] = bool(
            final_reference and final_reference.lower().endswith(".pdf")
        )
        handled, summary = read_context.summary_for(row)
        if not handled:
            summary = source_summary_for_item(db, row["item_id"])
        if summary is not None:
            row.update(summary)
            if received_since is None:
                row["incoming_quantity"] = summary["remaining_quantity"]
        else:
            planned = int(row.get("requisition_qty") or row.get("quantity") or 0)
            row.update(
                {
                    "planned_quantity": planned,
                    "cumulative_received_quantity": 0,
                    "remaining_quantity": planned,
                    "variance_quantity": 0,
                    "variance_type": None,
                    "resolution_status": "not_required",
                    "resolution_action": None,
                    "pending_receipt_item_id": None,
                    "latest_receipt_item_id": None,
                    "latest_receipt_id": None,
                    "surplus_inventory_lot_id": None,
                }
            )
    for row in stock_replenishment_rows:
        handled, summary = read_context.summary_for(row)
        if not handled or summary is None:
            # Defensive fallback for an unexpected route shape; ordinary page
            # rows always use the batched branch above.
            summary = stock_source_summary(db, row["item_id"])
        row.update(summary)
        row["incoming_quantity"] = summary["remaining_quantity"]
    rows.extend(stock_replenishment_rows)
    if selected_route_ids is not None:
        rows_by_id = {row["item_id"]: row for row in rows}
        rows = [
            rows_by_id[item_id]
            for item_id in selected_route_ids
            if item_id in rows_by_id
        ]
    _decorate_rows_with_receipt_purpose(db, rows)
    return rows


def _decorate_rows_with_display_numbers(
    db: Session, rows: list[dict], registry
) -> list[dict]:
    if not rows:
        return rows
    order_ids = {row["order_id"] for row in rows if row.get("order_id")}
    history_orders = {
        order.id: order
        for order in db.scalars(select(Order).where(Order.id.in_(order_ids))).all()
    }
    for row in rows:
        order = history_orders.get(row.get("order_id"))
        display = display_order_number(order, registry) if order is not None else row.get("order_number")
        row["order_number"] = display
        row["display_order_number"] = display
    return rows


def _audit(
    db: Session,
    *,
    user: User,
    action: str,
    item_id: int,
    details: dict,
    audit_context: dict[str, object] | None = None,
) -> None:
    context = audit_context or {}
    row = db.execute(
        select(OrderItem, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.id == item_id)
    ).one_or_none()
    order_item, order = row if row is not None else (None, None)
    customer = (
        db.get(Customer, order.customer_id)
        if order is not None
        else None
    )
    append_audit_event(
        db,
        request=context.get("request"),
        actor=user,
        event_category="business",
        result="success",
        source=str(context.get("source") or "web"),
        module_code="incoming",
        action_code=(
            "incoming.receive"
            if action == "RECEIVE_MATERIAL"
            else "incoming.revert"
        ),
        legacy_action=action,
        resource="OrderItem",
        entity_type="order_item",
        entity_id=item_id,
        object_ref=(
            order_item.item_order_number
            if order_item is not None and order_item.item_order_number
            else f"order_item:{item_id}"
        ),
        customer_id=order.customer_id if order is not None else None,
        customer_name=customer.name if customer is not None else None,
        batch_id=(
            str(context["batch_id"])
            if context.get("batch_id") is not None
            else None
        ),
        description=(
            "车间来料入库"
            if action == "RECEIVE_MATERIAL"
            else "撤回来料入库"
        ),
        details=details,
    )


def _item_response(db: Session, item_id: int) -> dict:
    item = db.get(OrderItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="订单明细不存在")
    return {
        "item_id": item.id,
        "material_status": item.material_status,
        "requisition_status": item.requisition_status,
        "requisition_qty": item.requisition_qty,
        "incoming_quantity": item.requisition_qty or item.quantity,
        "material_received_at": (
            utc_naive_to_api(item.material_received_at)
            if item.material_received_at
            else None
        ),
        "material_received_by": item.material_received_by,
    }


def _component_response(db: Session, requisition_item_id: int) -> dict:
    row = db.execute(
        select(RequisitionItem, OrderItem)
        .join(OrderItem, OrderItem.id == RequisitionItem.order_item_id)
        .where(RequisitionItem.id == requisition_item_id)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="报料明细不存在")
    requisition_item, order_item = row
    return {
        "item_id": f"r{requisition_item.id}",
        "order_item_id": order_item.id,
        "requisition_item_id": requisition_item.id,
        "material_status": order_item.material_status,
        "requisition_status": order_item.requisition_status,
        "requisition_qty": requisition_item.requisition_qty,
        "incoming_quantity": requisition_item.requisition_qty,
        "material_received_at": (
            utc_naive_to_api(order_item.material_received_at)
            if order_item.material_received_at
            else None
        ),
        "material_received_by": order_item.material_received_by,
        "component_status": requisition_item.status,
    }


def _supplier_order_item_response(
    db: Session,
    supplier_order_item_id: int,
) -> dict:
    row = db.execute(
        select(
            SupplierRequisitionOrderItem,
            SupplierRequisitionOrder,
            OrderItem,
        )
        .join(
            SupplierRequisitionOrder,
            SupplierRequisitionOrder.id
            == SupplierRequisitionOrderItem.supplier_order_id,
        )
        .join(
            OrderItem,
            OrderItem.id == SupplierRequisitionOrderItem.order_item_id,
        )
        .where(SupplierRequisitionOrderItem.id == supplier_order_item_id)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="供应商报料明细不存在")
    supplier_item, supplier_order, order_item = row
    return {
        "item_id": supplier_order_item_key(supplier_item.id),
        "order_item_id": order_item.id,
        "supplier_order_id": supplier_order.id,
        "supplier_order_item_id": supplier_item.id,
        "component_type": supplier_order_item_component_type(supplier_item),
        "material_status": order_item.material_status,
        "requisition_status": order_item.requisition_status,
        "requisition_qty": int(supplier_item.requisition_qty or 0),
        "incoming_quantity": int(supplier_item.requisition_qty or 0),
        "material_received_at": (
            utc_naive_to_api(order_item.material_received_at)
            if order_item.material_received_at
            else None
        ),
        "material_received_by": order_item.material_received_by,
        "supplier_order_number": supplier_order.order_number,
    }


def _receive_requisition_component(
    db: Session,
    *,
    user: User,
    requisition_item_id: int,
    received_quantity: int | None,
) -> dict:
    received_at = _utc_now()
    row = db.execute(
        select(RequisitionItem, OrderItem)
        .join(OrderItem, OrderItem.id == RequisitionItem.order_item_id)
        .where(RequisitionItem.id == requisition_item_id)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="报料明细不存在")
    requisition_item, order_item = row
    _require_order_item_customer_access(
        db,
        order_item_id=order_item.id,
        user=user,
    )
    active_component_ids = {
        item.id
        for item in _active_requisition_components(
            db,
            order_item_ids=[order_item.id],
        )
    }
    if requisition_item.id not in active_component_ids:
        raise HTTPException(status_code=409, detail="该报料明细当前不可入库")
    if order_item.material_status != "pending" or order_item.requisition_status not in {
        "已报料",
        "供应商已排单",
    }:
        raise HTTPException(status_code=409, detail="该明细当前不可入库，可能已入库、已作废或状态已变化")

    final_quantity = (
        received_quantity
        if received_quantity is not None
        else int(requisition_item.requisition_qty or 0)
    )
    if final_quantity <= 0:
        raise HTTPException(status_code=400, detail="入库数量必须大于0")

    previous_requisition_qty = requisition_item.requisition_qty
    requisition_item.requisition_qty = final_quantity
    requisition_item.status = "已入库"

    remaining_components = len(
        _active_requisition_components(
            db,
            order_item_ids=[order_item.id],
        )
    )
    if (
        remaining_components == 0
        and _all_expected_bom_material_received(
            db,
            order_item_id=order_item.id,
        )
    ):
        total_received = sum(
            int(item.requisition_qty or 0)
            for item in _active_requisition_components(
                db,
                order_item_ids=[order_item.id],
                include_received=True,
            )
            if item.status == "已入库"
        )
        order_item.material_status = "received"
        order_item.requisition_status = "已入库"
        order_item.material_received_at = received_at
        order_item.material_received_by = user.id
        order_item.requisition_qty = int(total_received)
        db.flush()
        if not _refresh_production_after_material_change(db, order_item):
            remaining_pending = db.scalar(
                select(func.count(OrderItem.id)).where(
                    OrderItem.order_id == order_item.order_id,
                    OrderItem.material_status != "received",
                )
            ) or 0
            if remaining_pending == 0:
                db.execute(
                    update(Order)
                    .where(
                        Order.id == order_item.order_id,
                        Order.status.in_(["pending_production", "production"]),
                    )
                    .values(status="pending_delivery")
                )

    _audit(
        db,
        user=user,
        action="RECEIVE_MATERIAL",
        item_id=order_item.id,
        details={
            "received_at": received_at,
            "requisition_item_id": requisition_item.id,
            "component_type": _requisition_component_kind(
                db,
                requisition_item,
            ),
            "previous_requisition_qty": previous_requisition_qty,
            "received_quantity": final_quantity,
        },
    )
    db.flush()
    return _component_response(db, requisition_item.id)


def _receive_material(
    db: Session,
    *,
    user: User,
    item_id: int | str,
    received_quantity: int | None,
) -> dict:
    if _is_component_key(item_id):
        return _receive_requisition_component(
            db,
            user=user,
            requisition_item_id=_component_id(item_id),
            received_quantity=received_quantity,
        )
    received_at = _utc_now()
    current = db.get(OrderItem, _component_id(item_id))
    if current is None:
        raise HTTPException(status_code=404, detail="订单明细不存在")
    _require_order_item_customer_access(
        db,
        order_item_id=current.id,
        user=user,
    )
    final_quantity = (
        received_quantity
        if received_quantity is not None
        else (current.requisition_qty or current.quantity)
    )
    if final_quantity <= 0:
        raise HTTPException(status_code=400, detail="入库数量必须大于0")
    previous_requisition_qty = current.requisition_qty
    result = db.execute(
        update(OrderItem)
        .where(
            OrderItem.id == item_id,
            OrderItem.material_status == "pending",
            OrderItem.requisition_status.in_(["已报料", "供应商已排单"]),
        )
        .values(
            material_status="received",
            requisition_status="已入库",
            material_received_at=received_at,
            material_received_by=user.id,
            requisition_qty=final_quantity,
        )
    )
    current.material_status = "received"
    current.requisition_status = "已入库"
    current.material_received_at = received_at
    current.material_received_by = user.id
    current.requisition_qty = final_quantity
    if result.rowcount != 1:
        exists = db.scalar(select(OrderItem.id).where(OrderItem.id == item_id))
        if exists is None:
            raise HTTPException(status_code=404, detail="订单明细不存在")
        raise HTTPException(
            status_code=409,
            detail="该明细当前不可入库，可能已入库、已作废或状态已变化",
        )
    db.flush()
    if not _refresh_production_after_material_change(db, current):
        remaining_pending = db.scalar(
            select(func.count(OrderItem.id)).where(
                OrderItem.order_id == current.order_id,
                OrderItem.material_status != "received",
            )
        ) or 0
        if remaining_pending == 0:
            db.execute(
                update(Order)
                .where(
                    Order.id == current.order_id,
                    Order.status.in_(["pending_production", "production"]),
                )
                .values(status="pending_delivery")
            )
    active_requisition_items = db.scalars(
        select(RequisitionItem)
        .where(
            RequisitionItem.order_item_id == item_id,
            RequisitionItem.status == "有效",
        )
        .order_by(RequisitionItem.id)
    ).all()
    if len(active_requisition_items) <= 1:
        db.execute(
            update(RequisitionItem)
            .where(
                RequisitionItem.order_item_id == item_id,
                RequisitionItem.status == "有效",
            )
            .values(requisition_qty=final_quantity)
        )
    else:
        remaining = int(final_quantity)
        for index, requisition_item in enumerate(active_requisition_items):
            if index == len(active_requisition_items) - 1:
                assigned = max(remaining, 0)
            else:
                assigned = min(
                    int(requisition_item.requisition_qty or 0),
                    max(remaining, 0),
                )
            requisition_item.requisition_qty = assigned
            remaining -= assigned
    _audit(
        db,
        user=user,
        action="RECEIVE_MATERIAL",
        item_id=item_id,
        details={
            "received_at": received_at,
            "previous_requisition_qty": previous_requisition_qty,
            "received_quantity": final_quantity,
        },
    )
    db.flush()
    return _item_response(db, current.id)


def _stock_replenishment_receipt_row(
    db: Session,
    fact: IncomingReceiptItem,
) -> dict | None:
    item = (
        db.get(StockReplenishmentOrderItem, fact.stock_replenishment_item_id)
        if fact.stock_replenishment_item_id
        else None
    )
    order = db.get(StockReplenishmentOrder, item.replenishment_order_id) if item else None
    if item is None or order is None:
        return None
    customer = db.get(Customer, item.customer_id) if item.customer_id else None
    receiver = db.get(User, fact.receipt.received_by) if fact.receipt.received_by else None
    return {
        "history_key": f"receipt-{fact.id}",
        "item_id": f"sr{item.id}",
        "stock_replenishment_order_id": order.id,
        "stock_replenishment_item_id": item.id,
        "source_type": "stock_replenishment",
        "order_item_id": None,
        "requisition_item_id": None,
        "receipt_id": fact.receipt_id,
        "receipt_item_id": fact.id,
        "receipt_number": fact.receipt.receipt_number,
        "receipt_status": fact.status,
        "product_id": item.product_id,
        "order_id": None,
        "order_number": order.order_number,
        "display_order_number": order.order_number,
        "customer_po": None,
        "customer_name": customer.name if customer else "",
        "product_name": item.product_name_snapshot,
        "product_code": item.product_code_snapshot,
        "specification": (
            f"{item.report_length_mm or '-'}×{item.report_width_mm or '-'}"
        ),
        "material": item.material_code_snapshot or "",
        "material_code": item.material_code_snapshot or "",
        "flute_type": item.flute_type,
        "material_display": " / ".join(
            value
            for value in (
                (item.material_code_snapshot or "").strip(),
                (item.flute_type or "").strip(),
            )
            if value
        ),
        "quantity": item.quantity,
        "delivery_date": None,
        "order_status": order.status,
        "material_status": "received",
        "requisition_status": "已入库",
        "requisition_qty": fact.planned_quantity,
        "incoming_quantity": fact.received_quantity,
        "planned_quantity": fact.planned_quantity,
        "received_quantity_this_time": fact.received_quantity,
        "cumulative_received_quantity": fact.cumulative_received_quantity,
        "remaining_quantity": max(
            fact.planned_quantity - fact.cumulative_received_quantity, 0
        ),
        "variance_quantity": fact.variance_quantity,
        "variance_type": fact.variance_type,
        "resolution_status": fact.resolution_status,
        "resolution_action": fact.resolution_action,
        "resolution_reason": fact.resolution_reason,
        "received_inventory_lot_id": fact.received_inventory_lot_id,
        "surplus_inventory_lot_id": None,
        "requisition_date": order.confirmed_at or order.created_at,
        "requisition_spec": None,
        "cardboard_len": item.report_length_mm,
        "cardboard_width": item.report_width_mm,
        "snapshot_crease_type": item.crease_type,
        "snapshot_crease_left_mm": item.crease_left_mm,
        "snapshot_crease_middle_mm": item.crease_middle_mm,
        "snapshot_crease_right_mm": item.crease_right_mm,
        "snapshot_base_crease_type": None,
        "snapshot_base_crease_left_mm": None,
        "snapshot_base_crease_middle_mm": None,
        "snapshot_base_crease_right_mm": None,
        "snapshot_supplier_name": order.supplier_name,
        "requisition_remark": item.remark or order.remark,
        "special_process": None,
        "supplier_delivery_time": None,
        "supplier_order_number": order.order_number,
        "material_received_at": fact.receipt.received_at,
        "material_received_by": fact.receipt.received_by,
        "received_by_name": receiver.real_name if receiver else None,
        "component_type": item.component_type,
        "drawing_path": None,
        "drawing_is_pdf": False,
        "can_revert_receipt": False,
    }


def _decorate_received_rows_with_purpose(
    db: Session,
    *,
    rows: list[dict],
    user: User,
) -> None:
    receipt_item_ids = {
        int(row["receipt_item_id"])
        for row in rows
        if row.get("receipt_item_id") is not None
    }
    if not receipt_item_ids:
        return
    allocations = list(
        db.scalars(
            select(IncomingReceiptPurposeAllocation).where(
                IncomingReceiptPurposeAllocation.incoming_receipt_item_id.in_(
                    receipt_item_ids
                )
            )
        ).all()
    )
    allocation_by_receipt = {
        int(row.incoming_receipt_item_id): row for row in allocations
    }
    allocation_payloads = serialize_receipt_purpose_allocations(db, allocations)
    reversals = list(
        db.scalars(
            select(IncomingReceiptPurposeReversal).where(
                IncomingReceiptPurposeReversal.incoming_receipt_item_id.in_(
                    receipt_item_ids
                )
            )
        ).all()
    )
    reversal_by_receipt = {
        int(row.incoming_receipt_item_id): row for row in reversals
    }
    can_view_cost = has_permission(user, "cost.view")
    for row in rows:
        receipt_item_id = row.get("receipt_item_id")
        allocation = allocation_by_receipt.get(int(receipt_item_id or 0))
        if allocation is None:
            row["purpose_status"] = "legacy_unset"
            row["purpose_allocation"] = None
            continue
        row["purpose_status"] = allocation.purpose_contract_status_snapshot
        row["purpose_allocation"] = _visible_purpose_allocation(
            allocation_payloads.get(allocation.id), can_view_cost=can_view_cost
        )
        reversal = reversal_by_receipt.get(int(receipt_item_id))
        if reversal is not None:
            row["purpose_reversal"] = {
                "purpose_reversal_id": reversal.id,
                "valid_received_cumulative": reversal.cumulative_total_sheet_qty_after,
                "order_sheet_cumulative": reversal.cumulative_order_purpose_sheet_qty_after,
                "reserve_sheet_cumulative": reversal.cumulative_reserve_purpose_sheet_qty_after,
                "cumulative_total_sheet_qty_after": reversal.cumulative_total_sheet_qty_after,
                "cumulative_order_purpose_sheet_qty_after": reversal.cumulative_order_purpose_sheet_qty_after,
                "cumulative_reserve_purpose_sheet_qty_after": reversal.cumulative_reserve_purpose_sheet_qty_after,
                "reversed_at": reversal.reversed_at,
            }


def _receipt_fact_rows(
    db: Session,
    *,
    user: User,
    received_since: datetime | None,
    include_reversed: bool = False,
) -> list[dict]:
    registry = build_display_registry(db)
    visible_customer_ids = _visible_customer_ids(user, db)
    rows: list[dict] = []
    for fact in receipt_history(
        db,
        received_since=received_since,
        include_reversed=include_reversed,
    ):
        if fact.stock_replenishment_item_id is not None:
            stock_row = _stock_replenishment_receipt_row(db, fact)
            if stock_row is None:
                continue
            if (
                visible_customer_ids is not None
                and stock_row.get("customer_name")
                and db.get(
                    StockReplenishmentOrderItem,
                    fact.stock_replenishment_item_id,
                ).customer_id
                not in visible_customer_ids
            ):
                continue
            rows.append(stock_row)
            continue
        item = db.get(OrderItem, fact.order_item_id)
        order = db.get(Order, fact.order_id)
        if item is None or order is None:
            continue
        if (
            visible_customer_ids is not None
            and order.customer_id not in visible_customer_ids
        ):
            continue
        product = db.get(Product, item.product_id)
        customer = db.get(Customer, order.customer_id)
        requisition_item = (
            db.get(RequisitionItem, fact.requisition_item_id)
            if fact.requisition_item_id
            else None
        )
        supplier_item = (
            db.get(SupplierRequisitionOrderItem, fact.supplier_order_item_id)
            if fact.supplier_order_item_id
            else None
        )
        supplier_order = (
            db.get(SupplierRequisitionOrder, fact.supplier_order_id)
            if fact.supplier_order_id
            else None
        )
        component = (
            supplier_order_item_component_type(supplier_item)
            if supplier_item is not None
            else _requisition_component_kind(db, requisition_item)
            if requisition_item is not None
            else ""
        )
        product_code = (
            supplier_item.product_code
            if supplier_item is not None
            else requisition_item.product_code_snapshot
            if requisition_item
            else None
        ) or item.snapshot_product_code or (product.product_code if product else None)
        product_name = (
            supplier_item.product_name
            if supplier_item is not None
            else requisition_item.product_name_snapshot
            if requisition_item
            else None
        ) or item.snapshot_product_name
        material_code = (
            supplier_item.material_code_snapshot
            if supplier_item is not None
            else requisition_item.material_snapshot
            if requisition_item
            else None
        ) or item.snapshot_material or ""
        display_number = display_order_number(order, registry)
        drawing_reference = (item.drawing_file or "").strip() or None
        drawing_path = _order_drawing_url(item.id, drawing_reference)
        if drawing_reference is None and product is not None:
            drawing = db.scalar(
                select(ProductDrawing)
                .where(ProductDrawing.product_id == product.id)
                .order_by(ProductDrawing.uploaded_at.desc(), ProductDrawing.id.desc())
            )
            drawing_reference = drawing.image_path if drawing else None
            drawing_path = _product_drawing_url(drawing)
        receiver = db.get(User, fact.receipt.received_by) if fact.receipt.received_by else None
        row = {
            "history_key": f"receipt-{fact.id}",
            "item_id": (
                supplier_order_item_key(fact.supplier_order_item_id)
                if fact.supplier_order_item_id
                else f"r{fact.requisition_item_id}"
                if fact.requisition_item_id
                else fact.order_item_id
            ),
            "order_item_id": fact.order_item_id,
            "requisition_item_id": fact.requisition_item_id,
            "supplier_order_id": fact.supplier_order_id,
            "supplier_order_item_id": fact.supplier_order_item_id,
            "receipt_id": fact.receipt_id,
            "receipt_item_id": fact.id,
            "receipt_number": fact.receipt.receipt_number,
            "receipt_status": fact.status,
            "product_id": (
                supplier_item.product_id
                if supplier_item is not None and supplier_item.product_id is not None
                else item.product_id
            ),
            "order_id": order.id,
            "customer_id": order.customer_id,
            "order_number": display_number,
            "display_order_number": display_number,
            "customer_po": order.customer_po,
            "customer_name": customer.name if customer else "",
            "product_name": product_name,
            "product_code": product_code,
            "specification": (
                f"{supplier_item.report_length_mm or '-'}×{supplier_item.report_width_mm or '-'}"
                if supplier_item is not None
                else resolved_product_specification(
                    requisition_item.specification_snapshot
                    if requisition_item
                    else None,
                    product,
                    fallback_snapshots=(item.snapshot_spec,),
                )
                if product is not None or requisition_item is not None
                else None
            )
            or resolved_product_specification(item.snapshot_spec, product),
            "material": material_code,
            "material_code": material_code,
            "flute_type": (
                supplier_item.flute_type_snapshot
                if supplier_item is not None
                else item.flute_type
            ) or (product.flute_type if product else None),
            "material_display": (
                f"{material_code} / {item.flute_type}" if material_code and item.flute_type else material_code
            ),
            "quantity": item.quantity,
            "delivery_date": order.delivery_date,
            "order_status": order.status,
            "material_status": item.material_status,
            "requisition_status": item.requisition_status,
            "requisition_qty": fact.planned_quantity,
            "incoming_quantity": fact.received_quantity,
            "planned_quantity": fact.planned_quantity,
            "received_quantity_this_time": fact.received_quantity,
            "cumulative_received_quantity": fact.cumulative_received_quantity,
            "remaining_quantity": max(
                fact.planned_quantity - fact.cumulative_received_quantity, 0
            ),
            "variance_quantity": fact.variance_quantity,
            "variance_type": fact.variance_type,
            "resolution_status": fact.resolution_status,
            "resolution_action": fact.resolution_action,
            "resolution_reason": fact.resolution_reason,
            "surplus_inventory_lot_id": fact.surplus_inventory_lot_id,
            "requisition_date": item.requisition_date,
            "requisition_spec": item.requisition_spec,
            "cardboard_len": (
                supplier_item.report_length_mm
                if supplier_item is not None
                else requisition_item.cardboard_len
                if requisition_item
                else item.cardboard_len
            ),
            "cardboard_width": (
                supplier_item.report_width_mm
                if supplier_item is not None
                else requisition_item.cardboard_width
                if requisition_item
                else item.cardboard_width
            ),
            "snapshot_crease_type": item.snapshot_crease_type,
            "snapshot_crease_left_mm": item.snapshot_crease_left_mm,
            "snapshot_crease_middle_mm": item.snapshot_crease_middle_mm,
            "snapshot_crease_right_mm": item.snapshot_crease_right_mm,
            "snapshot_base_crease_type": item.snapshot_base_crease_type,
            "snapshot_base_crease_left_mm": item.snapshot_base_crease_left_mm,
            "snapshot_base_crease_middle_mm": item.snapshot_base_crease_middle_mm,
            "snapshot_base_crease_right_mm": item.snapshot_base_crease_right_mm,
            "snapshot_supplier_name": (
                supplier_item.supplier_name_snapshot
                if supplier_item is not None
                else item.snapshot_supplier_name
            ) or (supplier_order.supplier_name if supplier_order else None),
            "requisition_remark": (
                requisition_item.remark if requisition_item else item.requisition_remark
            ),
            "special_process": (
                supplier_item.cutting_mode
                if supplier_item is not None
                else requisition_item.special_process
                if requisition_item
                else item.special_process
            ),
            "supplier_delivery_time": item.supplier_delivery_time,
            "supplier_order_number": (
                supplier_order.order_number
                if supplier_order is not None
                else item.supplier_order_number
            ),
            "material_received_at": fact.receipt.received_at,
            "material_received_by": fact.receipt.received_by,
            "received_by_name": receiver.real_name if receiver else None,
            "component_type": component or "single",
            "drawing_path": drawing_path,
            "drawing_is_pdf": bool(
                drawing_reference and drawing_reference.lower().endswith(".pdf")
            ),
        }
        _apply_component_crease(row, component)
        rows.append(row)
    _decorate_received_rows_with_purpose(db, rows=rows, user=user)
    return rows


def _received_rows(
    db: Session,
    *,
    user: User,
    received_since: datetime,
    include_reversed: bool = False,
) -> list[dict]:
    facts = _receipt_fact_rows(
        db,
        user=user,
        received_since=received_since,
        include_reversed=include_reversed,
    )
    fact_keys = {str(row["item_id"]) for row in facts}
    fact_order_item_ids = {
        int(row["order_item_id"])
        for row in facts
        if row.get("order_item_id") is not None
    }
    legacy = [
        row for row in _rows(db, user=user, received_since=received_since)
        if str(row["item_id"]) not in fact_keys
        and int(
            row.get("order_item_id")
            or (row.get("item_id") if isinstance(row.get("item_id"), int) else 0)
            or 0
        )
        not in fact_order_item_ids
    ]
    combined = [*facts, *legacy]
    combined.sort(
        key=lambda row: (
            row.get("material_received_at") or datetime.min,
            str(row.get("history_key") or row.get("receipt_item_id") or row.get("item_id") or ""),
        ),
        reverse=True,
    )
    return combined


def _filter_received_history_rows(
    rows: list[dict],
    *,
    customer_ids: set[int],
    document_keyword: str | None,
    product_code: str | None,
    product_name: str | None,
    supplier_name: str | None,
    board_length_mm: int | None,
    board_width_mm: int | None,
    date_from: date | None,
    date_to: date | None,
    receipt_status: str | None,
) -> list[dict]:
    document_text = (document_keyword or "").strip().lower()
    code_text = (product_code or "").strip().lower()
    name_text = (product_name or "").strip().lower()
    supplier_text = (supplier_name or "").strip().lower()

    def matches(row: dict) -> bool:
        if customer_ids and int(row.get("customer_id") or 0) not in customer_ids:
            return False
        if document_text and document_text not in " ".join(
            str(row.get(key) or "").lower()
            for key in (
                "receipt_number",
                "order_number",
                "customer_po",
                "supplier_order_number",
            )
        ):
            return False
        if code_text and code_text not in str(row.get("product_code") or "").lower():
            return False
        if name_text and name_text not in str(row.get("product_name") or "").lower():
            return False
        if supplier_text and supplier_text not in str(
            row.get("snapshot_supplier_name") or ""
        ).lower():
            return False
        if board_length_mm is not None and int(row.get("cardboard_len") or 0) != board_length_mm:
            return False
        if board_width_mm is not None and int(row.get("cardboard_width") or 0) != board_width_mm:
            return False
        received_at = row.get("material_received_at")
        received_date = received_at.date() if isinstance(received_at, datetime) else None
        if date_from is not None and (received_date is None or received_date < date_from):
            return False
        if date_to is not None and (received_date is None or received_date > date_to):
            return False
        if receipt_status:
            actual_status = str(
                row.get("receipt_status")
                or ("posted" if row.get("material_status") == "received" else "")
            )
            if actual_status != receipt_status:
                return False
        return True

    return [row for row in rows if matches(row)]



def _incoming_row_response(row: dict) -> dict:
    """Serialize incoming-list datetime fields using their storage contracts."""

    response = dict(row)
    if response.get("created_at"):
        response["created_at"] = utc_naive_to_api(response["created_at"])
    if response.get("supplier_delivery_time"):
        response["supplier_delivery_time"] = beijing_naive_to_api(
            response["supplier_delivery_time"]
        )
    if response.get("material_received_at"):
        response["material_received_at"] = utc_naive_to_api(
            response["material_received_at"]
        )
    return response


def _production_card_steps(notes: str | None) -> list[str]:
    """Return only process steps explicitly present in the frozen order notes."""

    text = str(notes or "").strip()
    if not text:
        return []
    markers = (
        ("无印刷", "无印刷"),
        ("不印刷", "无印刷"),
        ("印刷", "印刷"),
        ("压线", "压线"),
        ("开槽", "开槽"),
        ("模切", "模切"),
        ("清料", "清料"),
        ("打钉", "钉箱"),
        ("钉箱", "钉箱"),
        ("粘贴", "粘箱"),
        ("粘箱", "粘箱"),
        ("衬板", "衬板"),
        ("隔板", "隔板"),
        ("刀卡", "刀卡"),
    )
    hits: list[tuple[int, str]] = []
    for token, label in markers:
        start = text.find(token)
        if start >= 0:
            hits.append((start, label))
    hits.sort(key=lambda row: row[0])
    steps: list[str] = []
    for _position, label in hits:
        if label == "印刷" and "无印刷" in steps:
            continue
        if label not in steps:
            steps.append(label)
    return steps


def _legacy_receipt_production_package(card: dict) -> dict:
    """Keep old receipt facts printable through the unified task-card layout."""

    output_factor = max(int(card.get("output_factor") or 1), 1)
    receipt_item_id = int(card["receipt_item_id"])
    component = {
        "supplier_order_item_id": card.get("requisition_item_id"),
        "source_identity": f"legacy-receipt:{receipt_item_id}",
        "component_label": card.get("component_type") or "整片",
        "display_order": 1,
        "product_code": card.get("product_code"),
        "product_name": card.get("product_name"),
        "specification": card.get("specification"),
        "planned_finished_quantity": card.get("production_capacity_quantity"),
        "finished_unit": card.get("production_unit") or "只",
        "requisition_quantity": card.get("planned_sheet_quantity")
        or card.get("received_sheet_quantity"),
        "requisition_unit": "张",
        "report_length_mm": card.get("board_length_mm"),
        "report_width_mm": card.get("board_width_mm"),
        "cutting_mode": card.get("cutting_mode"),
        "pieces_per_box": output_factor,
        "required_piece_quantity": card.get("production_capacity_quantity"),
        "material_code": None,
        "layer_count": card.get("layer_count"),
        "flute_type": card.get("flute_type"),
        "crease_type": card.get("crease_type"),
        "crease_display": " + ".join(
            str(value)
            for value in (
                card.get("crease_left_mm"),
                card.get("crease_middle_mm"),
                card.get("crease_right_mm"),
            )
            if value is not None
        )
        or None,
        "production_notes": (
            [card["production_notes"]] if card.get("production_notes") else []
        ),
        "drawing_reference": None,
        "drawing_url": card.get("drawing_path"),
        "drawing_kind": "pdf" if card.get("drawing_is_pdf") else "image",
        "drawing_source": "订单冻结图纸" if card.get("drawing_path") else None,
        "mold_code": card.get("mold_tool_code"),
        "mold_name": card.get("mold_tool_name"),
        "joining_method": "无需结合",
        "production_task_id": card.get("production_task_id"),
        "production_task_version": card.get("production_task_version"),
        "output_factor": output_factor,
        "print_content": None,
        "printing_situation": None,
        "printing_colors": [],
        "printing_colors_frozen": False,
        "printing_plate_mode": "no_plate",
        "printing_plates": [],
    }
    unified_card = {
        "supplier_order_item_id": card.get("requisition_item_id"),
        "source_identity": component["source_identity"],
        "component_label": component["component_label"],
        "customer_name": card.get("customer_name"),
        "product_code": card.get("product_code"),
        "product_name": card.get("product_name"),
        "specifications": [card["specification"]]
        if card.get("specification")
        else [],
        "order_numbers": [card["order_number"]]
        if card.get("order_number")
        else [],
        "item_order_numbers": [],
        "customer_pos": [card["customer_po"]] if card.get("customer_po") else [],
        "delivery_dates": [card["delivery_date"]]
        if card.get("delivery_date")
        else [],
        "planned_finished_quantity": card.get("production_capacity_quantity"),
        "requisition_quantity": card.get("received_sheet_quantity"),
        "paper_phase": "actual_receipt",
        "paper_phase_label": "历史实收版",
        "paper_version_key": f"actual:receipt:{receipt_item_id}",
        "planned_sheet_quantity": card.get("planned_sheet_quantity")
        or card.get("received_sheet_quantity"),
        "received_sheet_quantity": card.get("received_sheet_quantity"),
        "cumulative_received_sheet_quantity": card.get(
            "cumulative_received_sheet_quantity"
        )
        or card.get("received_sheet_quantity"),
        "production_capacity_quantity": card.get("production_capacity_quantity"),
        "output_factor": output_factor,
        "receipt_number": card.get("receipt_number"),
        "receipt_item_id": receipt_item_id,
        "received_at": card.get("received_at"),
        "variance_type": card.get("variance_type"),
        "layout_kind": "carton",
        "box_style": None,
        "box_type_code": None,
        "printing_colors": [],
        "joining_method": "无需结合",
        "production_steps": list(card.get("process_steps") or []),
        "review_required": not bool(card.get("process_steps")),
        "review_messages": (
            [] if card.get("process_steps") else ["历史任务工艺待人工核对"]
        ),
        "status_label": "历史实收版",
        "components": [component],
        "structure_reference": (
            {
                "name": "订单冻结图纸",
                "url": card.get("drawing_path"),
                "kind": "pdf" if card.get("drawing_is_pdf") else "image",
                "source": "订单冻结图纸",
            }
            if card.get("drawing_path")
            else None
        ),
        "fulfillment_reminders": [],
    }
    paper_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "paper_version_key": unified_card["paper_version_key"],
                "received_sheet_quantity": card.get("received_sheet_quantity"),
                "receipt_number": card.get("receipt_number"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "supplier_order_id": None,
        "supplier_order_number": card.get("requisition_number"),
        "status": "posted",
        "status_label": "历史实收版",
        "created_at": card.get("received_at"),
        "plan_fingerprint": None,
        "paper_fingerprint": paper_fingerprint,
        "paper_phase": "actual_receipt",
        "paper_phase_label": "历史实收版",
        "paper_version_key": unified_card["paper_version_key"],
        "reuses_planned_card": False,
        "reprint_required": True,
        "receipt_item_id": receipt_item_id,
        "card_count": 1,
        "page_count": 1,
        "review_required": unified_card["review_required"],
        "review_messages": unified_card["review_messages"],
        "layout_overflow": False,
        "printable": True,
        "production_label_task_count": 0,
        "production_label_count": 0,
        "cards": [unified_card],
        "pages": [{"page_number": 1, "top": unified_card, "bottom": None}],
    }


@router.get("/receipt-items/{receipt_item_id}/production-card")
def incoming_production_card(
    receipt_item_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    """Build a read-only physical-board card from one posted receipt fact."""

    rows = _receipt_fact_rows(
        db,
        user=user,
        received_since=datetime(2000, 1, 1),
        include_reversed=True,
    )
    row = next(
        (
            candidate
            for candidate in rows
            if int(candidate.get("receipt_item_id") or 0) == receipt_item_id
        ),
        None,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="来料实收记录不存在或无权查看")
    if row.get("receipt_status") != "posted":
        raise HTTPException(status_code=409, detail="该来料实收已撤销，不能打印生产随料卡")
    if not row.get("order_item_id"):
        raise HTTPException(
            status_code=409,
            detail="该来料属于库存补库，没有对应生产任务，不能打印生产随料卡",
        )

    order_item = db.get(OrderItem, int(row["order_item_id"]))
    if order_item is None:
        raise HTTPException(status_code=404, detail="关联订单明细不存在")
    product = db.get(Product, order_item.product_id)
    fact = db.get(IncomingReceiptItem, receipt_item_id)
    if fact is None:
        raise HTTPException(status_code=404, detail="来料实收记录不存在")

    bom_component_id = None
    source = None
    if fact.requisition_item_id is not None:
        source = db.scalar(
            select(RequisitionItemBomSource)
            .where(
                RequisitionItemBomSource.requisition_item_id
                == fact.requisition_item_id,
                RequisitionItemBomSource.active_guard == 1,
            )
            .order_by(RequisitionItemBomSource.id.asc())
        )
        bom_component_id = (
            source.sales_order_item_bom_component_id if source is not None else None
        )
    task_query = select(ProductionTask).where(
        ProductionTask.order_item_id == order_item.id
    )
    if bom_component_id is None:
        task_query = task_query.where(
            ProductionTask.sales_order_item_bom_component_id.is_(None)
        )
    else:
        task_query = task_query.where(
            ProductionTask.sales_order_item_bom_component_id == bom_component_id
        )
    task = db.scalar(task_query.order_by(ProductionTask.id.asc()))

    component_snapshot = (
        source.sales_order_item_bom_component if source is not None else None
    )
    frozen_process = (
        component_snapshot.snapshot_component_production_process
        if component_snapshot is not None
        else order_item.snapshot_production_notes
    )
    frozen_notes_parts = [str(frozen_process or "").strip()]
    if component_snapshot is not None:
        frozen_notes_parts.extend(
            [
                str(component_snapshot.snapshot_component_report_notes or "").strip(),
                str(component_snapshot.remark or "").strip(),
                str(source.direction_note or "").strip(),
            ]
        )
    frozen_notes = "；".join(dict.fromkeys(part for part in frozen_notes_parts if part))
    process_steps = _production_card_steps(frozen_process)
    output_factor = max(
        int(task.output_factor if task is not None else 0)
        or cutting_output_factor(row.get("special_process")),
        1,
    )
    supplier_order = (
        db.get(SupplierRequisitionOrder, fact.supplier_order_id)
        if fact.supplier_order_id is not None
        else None
    )
    receipt_number = str(row.get("receipt_number") or "").strip()
    legacy_card = {
        "card_type": "incoming_production_material_card",
        "card_version": 1,
        "card_number": f"SC-{receipt_number}-{receipt_item_id}",
        "receipt_item_id": receipt_item_id,
        "receipt_number": receipt_number,
        "receipt_status": "posted",
        "production_task_id": task.id if task is not None else None,
        "production_task_version": task.version if task is not None else 1,
        "requisition_number": (
            supplier_order.order_number
            if supplier_order is not None
            else row.get("supplier_order_number")
        ),
        "requisition_item_id": fact.supplier_order_item_id
        or fact.requisition_item_id,
        "order_number": row.get("display_order_number") or row.get("order_number"),
        "customer_po": row.get("customer_po"),
        "customer_name": row.get("customer_name"),
        "product_code": row.get("product_code"),
        "product_name": row.get("product_name"),
        "specification": row.get("specification"),
        "component_type": row.get("component_type") or "single",
        "delivery_date": row.get("delivery_date"),
        "received_at": utc_naive_to_api(fact.receipt.received_at),
        "planned_sheet_quantity": int(fact.planned_quantity),
        "received_sheet_quantity": int(fact.received_quantity),
        "cumulative_received_sheet_quantity": int(
            fact.cumulative_received_quantity
        ),
        "variance_sheet_quantity": int(fact.variance_quantity),
        "variance_type": fact.variance_type,
        "resolution_status": fact.resolution_status,
        "resolution_action": fact.resolution_action,
        "output_factor": output_factor,
        "production_capacity_quantity": int(fact.received_quantity) * output_factor,
        "production_unit": product.unit if product is not None else "只",
        "cutting_mode": row.get("special_process") or "一开一",
        "board_length_mm": (
            int(row["cardboard_len"]) if row.get("cardboard_len") is not None else None
        ),
        "board_width_mm": (
            int(row["cardboard_width"])
            if row.get("cardboard_width") is not None
            else None
        ),
        "layer_count": (
            component_snapshot.snapshot_component_layer_count
            if component_snapshot is not None
            else order_item.layer_count
        ),
        "flute_type": (
            component_snapshot.snapshot_component_flute_type
            if component_snapshot is not None
            else row.get("flute_type")
        ),
        "crease_type": (
            component_snapshot.snapshot_component_crease_type
            if component_snapshot is not None
            else row.get("snapshot_crease_type")
        ),
        "crease_left_mm": (
            component_snapshot.snapshot_component_crease_left_mm
            if component_snapshot is not None
            else row.get("snapshot_crease_left_mm")
        ),
        "crease_middle_mm": (
            component_snapshot.snapshot_component_crease_middle_mm
            if component_snapshot is not None
            else row.get("snapshot_crease_middle_mm")
        ),
        "crease_right_mm": (
            component_snapshot.snapshot_component_crease_right_mm
            if component_snapshot is not None
            else row.get("snapshot_crease_right_mm")
        ),
        "mold_tool_code": (
            component_snapshot.snapshot_mold_tool_code
            if component_snapshot is not None
            else None
        ),
        "mold_tool_name": (
            component_snapshot.snapshot_mold_tool_name
            if component_snapshot is not None
            else None
        ),
        "production_notes": frozen_notes or None,
        "process_steps": process_steps,
        "process_status": "confirmed" if process_steps else "needs_confirmation",
        "drawing_path": row.get("drawing_path"),
        "drawing_is_pdf": bool(row.get("drawing_is_pdf")),
        "printed_by": user.real_name or user.display_name or user.username,
        "generated_at": utc_naive_to_api(_utc_now()),
    }
    package = build_receipt_production_print_package(db, fact)
    if package is None:
        package = _legacy_receipt_production_package(legacy_card)
    return {**legacy_card, **package}


@router.get("/surplus-locations")
def surplus_inventory_locations(
    db: Session = Depends(get_db),
    _user: User = Depends(can_operate),
) -> dict:
    """Return only locations that can receive an incoming surplus transfer."""
    rows = [
        row.location
        for row in list_operational_locations(
            db,
            warehouse_types={"semi_finished", "shared"},
        )
    ]
    rows.sort(key=lambda row: (row.location_code, row.id))
    return {
        "items": [
            {
                "id": row.id,
                "location_code": row.location_code,
                "location_name": row.location_name,
                "warehouse_type": row.warehouse_type,
                "layout_version": (
                    int(row.floor3_layout.version)
                    if row.floor3_layout is not None
                    else None
                ),
            }
            for row in rows
        ]
    }


@router.get("/pending")
def pending_items(
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
    page: Annotated[int | None, Query(ge=1)] = None,
    page_size: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> dict:
    response.headers["X-ERP-Session-Identity"] = f"{user.id}:{user.auth_version}"
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Vary"] = "Cookie"
    if page is None and page_size is None:
        return {
            "items": [
                _incoming_row_response(row)
                for row in _rows(db, user=user)
            ]
        }

    eligible_routes = dashboard_pending_incoming_rows(db, user)
    total = len(eligible_routes)
    resolved_page_size = min(max(int(page_size or 25), 1), 200)
    requested_page = max(int(page or 1), 1)
    last_page = max(1, (total + resolved_page_size - 1) // resolved_page_size)
    resolved_page = min(requested_page, last_page)
    start = (resolved_page - 1) * resolved_page_size
    selected_routes = eligible_routes[start : start + resolved_page_size]
    return {
        "items": [
            _incoming_row_response(row)
            for row in _rows(
                db,
                user=user,
                selected_pending_routes=selected_routes,
            )
        ],
        "total": total,
        "page": resolved_page,
        "page_size": resolved_page_size,
    }


@router.get("/received")
def recently_received_items(
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    return {
        "items": [
            _incoming_row_response(row)
            for row in _received_rows(
                db,
                user=user,
                received_since=_utc_now() - timedelta(hours=24),
            )
        ]
    }


@router.get("/history")
def history_received_items(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    customer_id: list[int] | None = Query(default=None),
    document_keyword: str | None = None,
    product_code: str | None = None,
    product_name: str | None = None,
    supplier_name: str | None = None,
    board_length_mm: int | None = Query(default=None, ge=1),
    board_width_mm: int | None = Query(default=None, ge=1),
    date_from: date | None = None,
    date_to: date | None = None,
    receipt_status: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    """Return scoped, stable pages of historical incoming receipt facts."""
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=422, detail="入库开始日期不能晚于结束日期")
    requested_customer_ids = {value for value in (customer_id or []) if value > 0}
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is not None:
        denied = requested_customer_ids - visible_customer_ids
        if denied:
            raise HTTPException(status_code=403, detail="无客户访问权限")
    for requested_id in requested_customer_ids:
        require_customer_access(requested_id, current_user=user, db=db)

    epoch_start = datetime(2000, 1, 1)
    rows = _filter_received_history_rows(
        _received_rows(
            db,
            user=user,
            received_since=epoch_start,
            include_reversed=True,
        ),
        customer_ids=requested_customer_ids,
        document_keyword=document_keyword,
        product_code=product_code,
        product_name=product_name,
        supplier_name=supplier_name,
        board_length_mm=board_length_mm,
        board_width_mm=board_width_mm,
        date_from=date_from,
        date_to=date_to,
        receipt_status=receipt_status,
    )
    total = len(rows)
    start = (page - 1) * page_size
    return {
        "items": [_incoming_row_response(row) for row in rows[start : start + page_size]],
        "total": total,
        "page": page,
        "page_size": page_size,
        "filters": {
            "customer_ids": sorted(requested_customer_ids),
            "document_keyword": (document_keyword or "").strip(),
            "product_code": (product_code or "").strip(),
            "product_name": (product_name or "").strip(),
            "supplier_name": (supplier_name or "").strip(),
            "board_length_mm": board_length_mm,
            "board_width_mm": board_width_mm,
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
            "receipt_status": receipt_status,
        },
        "sort": ["material_received_at:desc", "history_key:desc"],
    }


def _lan_ip() -> str:
    connection = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        connection.connect(("8.8.8.8", 80))
        return connection.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())
    finally:
        connection.close()


@router.get("/mobile-entry")
def mobile_entry(
    request: Request,
    _user: User = Depends(can_read),
) -> dict:
    port = request.url.port or 8000
    url = f"http://{_lan_ip()}:{port}/incoming.html"
    image = qrcode.make(url)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return {"url": url, "qr_data_url": f"data:image/png;base64,{encoded}"}


_COST_RESPONSE_KEYS = {
    "sheet_cost",
    "order_cost",
    "reserve_cost",
    "receipt_total_cost",
    "currency",
}


def _visible_purpose_allocation(payload: dict | None, *, can_view_cost: bool) -> dict | None:
    if payload is None or can_view_cost:
        return payload
    return {key: value for key, value in payload.items() if key not in _COST_RESPONSE_KEYS}


def _new_receipt_response(
    db: Session,
    fact: IncomingReceiptItem,
    *,
    can_view_cost: bool,
    allocation_by_receipt: dict[int, IncomingReceiptPurposeAllocation] | None = None,
    allocation_payloads: dict[int, dict] | None = None,
    reversal_by_receipt: dict[int, IncomingReceiptPurposeReversal] | None = None,
) -> dict:
    if fact.stock_replenishment_item_id is not None:
        response = _stock_replenishment_receipt_row(db, fact)
        if response is None:
            raise HTTPException(status_code=409, detail="补库来料收货事实关联不完整")
        response.update(receipt_item_dict(fact))
        response.update(stock_source_summary(db, f"sr{fact.stock_replenishment_item_id}"))
        return response
    item_key: int | str = (
        supplier_order_item_key(fact.supplier_order_item_id)
        if fact.supplier_order_item_id
        else f"r{fact.requisition_item_id}"
        if fact.requisition_item_id
        else fact.order_item_id
    )
    response = (
        _supplier_order_item_response(db, fact.supplier_order_item_id)
        if fact.supplier_order_item_id
        else _component_response(db, fact.requisition_item_id)
        if fact.requisition_item_id
        else _item_response(db, fact.order_item_id)
    )
    response.update(receipt_item_dict(fact))
    summary = source_summary_for_item(db, item_key)
    if summary is not None:
        response.update(summary)
        response["incoming_quantity"] = (
            summary["remaining_quantity"]
            if summary["resolution_action"] == "await_supplier"
            and summary["remaining_quantity"] > 0
            else fact.received_quantity
        )
    response["idempotency_key"] = fact.receipt.idempotency_key
    allocation = (
        allocation_by_receipt.get(fact.id)
        if allocation_by_receipt is not None
        else db.scalar(
            select(IncomingReceiptPurposeAllocation).where(
                IncomingReceiptPurposeAllocation.incoming_receipt_item_id == fact.id
            )
        )
    )
    if allocation is None:
        response["purpose_status"] = "legacy_unset"
        response["purpose_allocation"] = None
    else:
        response["purpose_status"] = allocation.purpose_contract_status_snapshot
        allocation_payload = (
            allocation_payloads.get(allocation.id)
            if allocation_payloads is not None
            else serialize_receipt_purpose_allocation(db, allocation)
        )
        response["purpose_allocation"] = _visible_purpose_allocation(
            allocation_payload,
            can_view_cost=can_view_cost,
        )
        reversal = (
            reversal_by_receipt.get(fact.id)
            if reversal_by_receipt is not None
            else db.scalar(
                select(IncomingReceiptPurposeReversal).where(
                    IncomingReceiptPurposeReversal.incoming_receipt_item_id == fact.id
                )
            )
        )
        if reversal is not None:
            response["purpose_reversal"] = serialize_receipt_purpose_reversal(
                db, reversal
            )
    return response


def _raise_receipt_error(error: IncomingReceiptError) -> None:
    detail: str | dict[str, str] = str(error)
    if error.code:
        detail = {"code": error.code, "message": str(error)}
    raise HTTPException(status_code=error.status_code, detail=detail) from error


def _preflight_receipt_item_customer_access(
    db: Session,
    *,
    receipt_item_id: int,
    user: User,
) -> None:
    fact = db.get(IncomingReceiptItem, receipt_item_id)
    if fact is not None:
        if fact.stock_replenishment_item_id is not None:
            item = db.get(
                StockReplenishmentOrderItem,
                fact.stock_replenishment_item_id,
            )
            if item is not None and item.customer_id is not None:
                require_customer_access(item.customer_id, user, db)
            return
        _require_order_item_customer_access(
            db,
            order_item_id=fact.order_item_id,
            user=user,
        )


@router.put("/receive/{item_id}")
def receive_item(
    item_id: str,
    request: Request = None,
    payload: ReceiveRequest | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    _preflight_item_customer_access(db, item_id=item_id, user=user)
    try:
        fact = receive_one(
            db,
            user=user,
            item_key=item_id,
            received_quantity=(
                payload.received_quantity if payload is not None else None
            ),
            resolution_action=(payload.resolution_action if payload else None),
            resolution_reason=(payload.resolution_reason if payload else None),
            surplus_location_id=(payload.surplus_location_id if payload else None),
            expected_surplus_layout_version=(
                payload.expected_surplus_layout_version if payload else None
            ),
            expected_receipt_fact_version=(
                payload.expected_receipt_fact_version if payload else None
            ),
            purchase_purpose_source_snapshot_id=(
                payload.purchase_purpose_source_snapshot_id if payload else None
            ),
            expected_purpose_snapshot_version=(
                payload.expected_purpose_snapshot_version if payload else None
            ),
            receipt_plan_fingerprint=(
                payload.receipt_plan_fingerprint if payload else None
            ),
            expected_actual_material_version=(
                payload.expected_actual_material_version if payload else None
            ),
            actual_material_fingerprint=(
                payload.actual_material_fingerprint if payload else None
            ),
            idempotency_key=(payload.idempotency_key if payload else None),
            audit_context={"request": request},
        )
        response = _new_receipt_response(
            db, fact, can_view_cost=has_permission(user, "cost.view")
        )
        db.commit()
        return response
    except IncomingReceiptError as error:
        db.rollback()
        _raise_receipt_error(error)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@router.put("/batch-receive")
def batch_receive_items(
    payload: BatchReceiveRequest,
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    batch_key = (payload.idempotency_key or "").strip()
    formal_lines = [
        line
        for line in payload.items
        if line.purchase_purpose_source_snapshot_id is not None
    ]
    if formal_lines and not batch_key:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "INCOMING_BATCH_IDEMPOTENCY_KEY_REQUIRED",
                "message": "正式采购用途批量收料必须提交非空批次幂等键。",
            },
        )
    if any(not str(line.idempotency_key or "").strip() for line in formal_lines):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "INCOMING_IDEMPOTENCY_KEY_REQUIRED",
                "message": "正式采购用途批量收料的每一行都必须提交独立幂等键。",
            },
        )
    request_hash = canonical_purchase_receipt_hash(
        {"items": [line.model_dump(mode="json") for line in payload.items]}
    )
    scope_value: str | list[int] = (
        "unrestricted"
        if has_unrestricted_customer_access(user, db)
        else sorted(customer_scope_ids(user, db))
    )
    scope_hash = canonical_purchase_receipt_hash({"customer_scope": scope_value})
    if batch_key:
        replay = db.scalar(
            select(IncomingReceiptBatchFact).where(
                IncomingReceiptBatchFact.idempotency_key == batch_key
            )
        )
        if replay is not None:
            if (
                int(replay.received_by) != int(user.id)
                or not hmac.compare_digest(replay.request_hash, request_hash)
                or not hmac.compare_digest(replay.scope_hash, scope_hash)
            ):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "INCOMING_BATCH_IDEMPOTENCY_CONFLICT",
                        "message": "同一批次幂等键不能由不同操作者、权限范围或载荷重放。",
                    },
                )
            return json.loads(replay.response_json)
    # Check all targets before the first write.  A cross-customer item must not
    # turn a batch into a partial write that happens before the 403 response.
    for line in payload.items:
        _preflight_item_customer_access(db, item_id=line.item_id, user=user)
    seen: set[int | str] = set()
    batch_id = batch_key or uuid4().hex
    results: list[dict] = []
    succeeded = 0
    for line in payload.items:
        if line.item_id in seen:
            results.append(
                {
                    "item_id": line.item_id,
                    "success": False,
                    "message": "同一明细不能重复提交",
                }
            )
            continue
        seen.add(line.item_id)
        try:
            with db.begin_nested():
                fact = receive_one(
                    db,
                    user=user,
                    item_key=line.item_id,
                    received_quantity=line.received_quantity,
                    resolution_action=line.resolution_action,
                    resolution_reason=line.resolution_reason,
                    surplus_location_id=line.surplus_location_id,
                    expected_surplus_layout_version=(
                        line.expected_surplus_layout_version
                    ),
                    expected_receipt_fact_version=(
                        line.expected_receipt_fact_version
                    ),
                    purchase_purpose_source_snapshot_id=(
                        line.purchase_purpose_source_snapshot_id
                    ),
                    expected_purpose_snapshot_version=(
                        line.expected_purpose_snapshot_version
                    ),
                    receipt_plan_fingerprint=line.receipt_plan_fingerprint,
                    expected_actual_material_version=(
                        line.expected_actual_material_version
                    ),
                    actual_material_fingerprint=line.actual_material_fingerprint,
                    idempotency_key=line.idempotency_key,
                    audit_context={"request": request, "batch_id": batch_id},
                )
            results.append(
                {
                    "item_id": line.item_id,
                    "success": True,
                    "message": "入库成功",
                    "_fact": fact,
                }
            )
            succeeded += 1
        except (HTTPException, IncomingReceiptError) as error:
            results.append(
                {
                    "item_id": line.item_id,
                    "success": False,
                    "message": str(
                        error.detail if isinstance(error, HTTPException) else error
                    ),
                }
            )
        except Exception:
            results.append(
                {
                    "item_id": line.item_id,
                    "success": False,
                    "message": "系统处理失败，请刷新后重试",
                }
            )
    successful_facts = [row["_fact"] for row in results if row.get("success")]
    receipt_item_ids = [int(row.id) for row in successful_facts]
    allocations = (
        list(
            db.scalars(
                select(IncomingReceiptPurposeAllocation).where(
                    IncomingReceiptPurposeAllocation.incoming_receipt_item_id.in_(
                        receipt_item_ids
                    )
                )
            ).all()
        )
        if receipt_item_ids
        else []
    )
    allocation_by_receipt = {
        int(row.incoming_receipt_item_id): row for row in allocations
    }
    allocation_payloads = serialize_receipt_purpose_allocations(db, allocations)
    reversals = (
        list(
            db.scalars(
                select(IncomingReceiptPurposeReversal).where(
                    IncomingReceiptPurposeReversal.incoming_receipt_item_id.in_(
                        receipt_item_ids
                    )
                )
            ).all()
        )
        if receipt_item_ids
        else []
    )
    reversal_by_receipt = {
        int(row.incoming_receipt_item_id): row for row in reversals
    }
    can_view_cost = has_permission(user, "cost.view")
    for row in results:
        fact = row.pop("_fact", None)
        if fact is not None:
            row["item"] = _new_receipt_response(
                db,
                fact,
                can_view_cost=can_view_cost,
                allocation_by_receipt=allocation_by_receipt,
                allocation_payloads=allocation_payloads,
                reversal_by_receipt=reversal_by_receipt,
            )
    response = jsonable_encoder(
        {
            "batch_id": batch_id,
            "idempotency_key": batch_key or None,
            "total": len(payload.items),
            "succeeded": succeeded,
            "failed": len(payload.items) - succeeded,
            "results": results,
        }
    )
    try:
        append_audit_event(
            db,
            request=request,
            actor=user,
            event_category="business",
            result=(
                "success"
                if succeeded == len(payload.items)
                else "failed"
                if succeeded == 0
                else "partial"
            ),
            source="web",
            module_code="incoming",
            action_code="incoming.batch_receive",
            legacy_action="BATCH_RECEIVE_MATERIAL",
            resource="IncomingReceiptBatch",
            entity_type="incoming_batch",
            object_ref=batch_id,
            batch_id=batch_id,
            description="批量来料实收",
            details={
                "total": len(payload.items),
                "succeeded": succeeded,
                "failed": len(payload.items) - succeeded,
                "item_ids": [str(line.item_id) for line in payload.items],
            },
        )
        if batch_key:
            db.add(
                IncomingReceiptBatchFact(
                    idempotency_key=batch_key,
                    request_hash=request_hash,
                    scope_hash=scope_hash,
                    response_json=json.dumps(response, ensure_ascii=False, sort_keys=True),
                    received_by=user.id,
                )
            )
            db.flush()
        db.commit()
    except Exception:
        db.rollback()
        raise
    return response


@router.put("/receipt-items/{receipt_item_id}/accept-short")
def accept_short_receipt_item(
    receipt_item_id: int,
    payload: AcceptShortRequest,
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    _preflight_receipt_item_customer_access(
        db,
        receipt_item_id=receipt_item_id,
        user=user,
    )
    try:
        fact = accept_short(
            db,
            user=user,
            receipt_item_id=receipt_item_id,
            reason=payload.reason,
            audit_context={"request": request},
        )
        response = _new_receipt_response(
            db, fact, can_view_cost=has_permission(user, "cost.view")
        )
        db.commit()
        return response
    except IncomingReceiptError as error:
        db.rollback()
        _raise_receipt_error(error)


@router.put("/receipt-items/{receipt_item_id}/revert")
def revert_new_receipt_item(
    receipt_item_id: int,
    payload: RevertRequest,
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(admin_rollback),
) -> dict:
    _preflight_receipt_item_customer_access(
        db,
        receipt_item_id=receipt_item_id,
        user=user,
    )
    idempotency_key, request_hash, replay = _reversal_idempotency_contract(
        db,
        user=user,
        target_kind="receipt_item",
        target_id=receipt_item_id,
        payload=payload,
    )
    if replay is not None:
        return replay
    try:
        fact = revert_receipt_item(
            db,
            user=user,
            receipt_item_id=receipt_item_id,
            reason=payload.reason,
            idempotency_key=idempotency_key,
            audit_context={"request": request},
        )
        response = _new_receipt_response(
            db, fact, can_view_cost=has_permission(user, "cost.view")
        )
        purpose_reversal = db.scalar(
            select(IncomingReceiptPurposeReversal).where(
                IncomingReceiptPurposeReversal.incoming_receipt_item_id == fact.id
            )
        )
        response = _record_reversal_fact(
            db,
            user=user,
            target_kind="receipt_item",
            target_id=receipt_item_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            response=response,
            incoming_receipt_item_id=fact.id,
            incoming_receipt_purpose_reversal_id=(
                purpose_reversal.id if purpose_reversal is not None else None
            ),
        )
        db.commit()
        return response
    except IncomingReceiptError as error:
        db.rollback()
        _raise_receipt_error(error)


def _revert_requisition_component(
    db: Session,
    *,
    requisition_item_id: int,
    payload: RevertRequest,
    user: User,
    request: Request | None = None,
) -> dict:
    reason = _material_revert_reason(payload.reason)
    row = db.execute(
        select(RequisitionItem, OrderItem, Order)
        .join(OrderItem, OrderItem.id == RequisitionItem.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(RequisitionItem.id == requisition_item_id)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="报料明细不存在")
    requisition_item, order_item, order = row
    _require_order_item_customer_access(
        db,
        order_item_id=order_item.id,
        user=user,
    )
    idempotency_key, request_hash, replay = _reversal_idempotency_contract(
        db,
        user=user,
        target_kind="requisition_item",
        target_id=requisition_item_id,
        payload=payload,
    )
    if replay is not None:
        return replay
    _lock_order_for_material_revert(db, order.id)
    row = db.execute(
        select(RequisitionItem, OrderItem, Order)
        .join(OrderItem, OrderItem.id == RequisitionItem.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(RequisitionItem.id == requisition_item_id)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=409, detail="报料明细或关联订单已被删除，请刷新后重试")
    requisition_item, order_item, order = row
    if requisition_item.purpose_contract_status == "frozen":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "FROZEN_RECEIPT_LEGACY_REVERT_FORBIDDEN",
                "message": "冻结采购用途必须按具体收料事实撤销，不能走旧撤销入口。",
            },
        )
    if order.status in {"partially_delivered", "delivered"}:
        raise HTTPException(status_code=409, detail="订单已发货，禁止撤回来料")
    if requisition_item.status != "已入库":
        raise HTTPException(status_code=409, detail="该报料明细当前不是已入库状态")
    if has_production_completion_facts(db, [order_item.id]):
        raise HTTPException(status_code=409, detail="订单明细已有生产完工事实，不能撤销来料实收")

    active_components = _active_requisition_components(
        db,
        order_item_ids=[order_item.id],
        include_received=True,
    )
    if requisition_item.id not in {item.id for item in active_components}:
        raise HTTPException(status_code=409, detail="该报料明细当前不可撤回")
    restore_status = (
        "supplier_requisition_created"
        if order_item.id
        in _confirmed_supplier_order_item_ids(
            db,
            order_item_ids=[order_item.id],
        )
        else "有效"
    )

    previous_received_at = order_item.material_received_at
    previous_received_by = order_item.material_received_by
    try:
        result = db.execute(
            update(RequisitionItem)
            .where(
                RequisitionItem.id == requisition_item_id,
                RequisitionItem.status == "已入库",
            )
            .values(status=restore_status)
        )
        if result.rowcount != 1:
            raise HTTPException(status_code=409, detail="状态已变化，请刷新后重试")
        db.execute(
            update(OrderItem)
            .where(OrderItem.id == order_item.id)
            .values(
                material_status="pending",
                requisition_status="已报料",
                material_received_at=None,
                material_received_by=None,
            )
        )
        db.flush()
        db.refresh(order_item)
        _refresh_production_after_material_change(db, order_item)
        _audit(
            db,
            user=user,
            action="REVERT_MATERIAL",
            item_id=order_item.id,
            details={
                "reason": reason,
                "requisition_item_id": requisition_item_id,
                "component_type": _requisition_component_kind(
                    db,
                    requisition_item,
                ),
                "previous_received_at": previous_received_at,
                "previous_received_by": previous_received_by,
            },
            audit_context={"request": request},
        )
        response = _record_reversal_fact(
            db,
            user=user,
            target_kind="requisition_item",
            target_id=requisition_item_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            response=_component_response(db, requisition_item_id),
        )
        db.commit()
        return response
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@router.put("/revert/{item_id}")
def revert_item(
    item_id: str,
    payload: RevertRequest,
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(admin_rollback),
) -> dict:
    reason = _material_revert_reason(payload.reason)
    if _is_component_key(item_id):
        return _revert_requisition_component(
            db,
            requisition_item_id=_component_id(item_id),
            payload=payload,
            user=user,
            request=request,
        )
    try:
        item_id_int = int(item_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="入库明细ID无效") from error
    row = db.execute(
        select(OrderItem, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.id == item_id_int)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="订单明细不存在")
    item, order = row
    _require_order_item_customer_access(
        db,
        order_item_id=item.id,
        user=user,
    )
    idempotency_key, request_hash, replay = _reversal_idempotency_contract(
        db,
        user=user,
        target_kind="order_item",
        target_id=item_id_int,
        payload=payload,
    )
    if replay is not None:
        return replay
    _lock_order_for_material_revert(db, order.id)
    row = db.execute(
        select(OrderItem, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.id == item_id_int)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=409, detail="订单明细或关联订单已被删除，请刷新后重试")
    item, order = row
    frozen_source_exists = db.scalar(
        select(RequisitionItem.id)
        .where(
            RequisitionItem.order_item_id == item.id,
            RequisitionItem.purpose_contract_status == "frozen",
        )
        .limit(1)
    ) is not None or db.scalar(
        select(SupplierRequisitionOrderItem.id)
        .where(
            SupplierRequisitionOrderItem.order_item_id == item.id,
            SupplierRequisitionOrderItem.purpose_contract_status == "frozen",
        )
        .limit(1)
    ) is not None or db.scalar(
        select(IncomingReceiptPurposeAllocation.id)
        .join(
            IncomingReceiptItem,
            IncomingReceiptItem.id
            == IncomingReceiptPurposeAllocation.incoming_receipt_item_id,
        )
        .where(IncomingReceiptItem.order_item_id == item.id)
        .limit(1)
    ) is not None
    if frozen_source_exists:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "FROZEN_RECEIPT_LEGACY_REVERT_FORBIDDEN",
                "message": "冻结采购用途必须按具体收料事实撤销，不能走旧撤销入口。",
            },
        )
    if order.status in {"partially_delivered", "delivered"}:
        raise HTTPException(status_code=409, detail="订单已发货，禁止撤回来料")
    if item.material_status != "received":
        raise HTTPException(status_code=409, detail="该明细当前不是已入库状态")
    if has_production_completion_facts(db, [item.id]):
        raise HTTPException(status_code=409, detail="订单明细已有生产完工事实，不能撤销来料实收")

    previous_received_at = item.material_received_at
    previous_received_by = item.material_received_by
    try:
        result = db.execute(
            update(OrderItem)
            .where(
                OrderItem.id == item_id_int,
                OrderItem.material_status == "received",
            )
            .values(
                material_status="pending",
                requisition_status="已报料",
                material_received_at=None,
                material_received_by=None,
            )
        )
        if result.rowcount != 1:
            raise HTTPException(status_code=409, detail="状态已变化，请刷新后重试")
        db.flush()
        db.refresh(item)
        _refresh_production_after_material_change(db, item)
        _audit(
            db,
            user=user,
            action="REVERT_MATERIAL",
            item_id=item_id_int,
            details={
                "reason": reason,
                "before_status": "received",
                "after_status": item.material_status,
                "previous_received_at": previous_received_at,
                "previous_received_by": previous_received_by,
            },
            audit_context={"request": request},
        )
        db.execute(
            update(RequisitionItem)
            .where(
                RequisitionItem.order_item_id == item_id_int,
                RequisitionItem.status == "已入库",
            )
            .values(status="有效")
        )
        response = _record_reversal_fact(
            db,
            user=user,
            target_kind="order_item",
            target_id=item_id_int,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            response=_item_response(db, item_id_int),
        )
        db.commit()
        return response
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
