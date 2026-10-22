"""Read-only qualification of an explicitly sourced historical closed batch.

No mutation/apply function is exposed. Ordinary receiving cannot create this
history without fabricating a present warehouse position. Posting remains closed
until the dedicated historical ledger, cost projection and reversal guards exist.
Current master prices and newly captured order estimates are never price evidence.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import inspect, or_, select
from sqlalchemy.orm import object_session

from app.api.deps import has_permission, has_unrestricted_customer_access, customer_scope_ids
from app.core.time_contract import beijing_today
from app.models.delivery import Delivery, DeliveryItem
from app.models.customer_charge import CustomerCharge
from app.models.incoming_receipt import IncomingReceiptItem
from app.models.order import Order, OrderItem
from app.models.production import ProductionCompletion, ProductionTask
from app.models.product_bom import SalesOrderItemBomComponent
from app.models.multilevel_bom import OrderBomGraph
from app.models.purchase_receipt import PurchaseReceiptFact
from app.models.requisition import RequisitionItem
from app.models.supplier_requisition_order import (
    PurchasePurposeSourceSnapshot, SupplierRequisitionOrder, SupplierRequisitionOrderItem,
)
from app.models.warehouse_inventory import InventoryMovement, InventoryReservation
from app.services.supplier_delivery_contract import delivery_contract
from app.services.sheet_cutting_contract import SheetCuttingContract, SheetCuttingContractError


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceEvidence(StrictModel):
    file_name: str = Field(min_length=1, max_length=250)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sheet: str = Field(min_length=1, max_length=150)
    row: int = Field(gt=0)


class BusinessDate(StrictModel):
    value: date
    is_inferred: bool
    basis: str = Field(min_length=5, max_length=500)


class HistoricalPrice(StrictModel):
    basis: Literal["verified_historical_purchase_contract"]
    unit_price: Decimal = Field(gt=0, max_digits=18, decimal_places=6)
    price_unit: Literal["per_sheet", "per_square_meter"]
    currency: Literal["CNY"]
    tax_included: bool
    tax_rate: Decimal = Field(ge=0, le=1, max_digits=6, decimal_places=4)
    shipping_fee_mode: Literal["included"]
    material_id: int = Field(gt=0)
    supplier_name: str = Field(min_length=1, max_length=200)
    confirmation: str = Field(min_length=5, max_length=500)
    evidence: SourceEvidence


class ClosedLine(StrictModel):
    order_item_id: int = Field(gt=0)
    supplier_item_id: int = Field(gt=0)
    received_sheets: int = Field(gt=0)
    finished_quantity: int = Field(gt=0)
    delivered_quantity: int = Field(gt=0)
    purchase_evidence: SourceEvidence
    delivery_evidence: SourceEvidence
    historical_price: HistoricalPrice | None = None


class ClosedBatchRequest(StrictModel):
    order_id: int = Field(gt=0)
    supplier_order_id: int = Field(gt=0)
    customer_id: int = Field(gt=0)
    order_date: BusinessDate
    received_date: BusinessDate
    delivery_date: BusinessDate
    original_delivery_number: str = Field(min_length=1, max_length=80)
    authorization: str = Field(min_length=10, max_length=1000)
    lines: list[ClosedLine] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique_complete_lines(self):
        if len({r.order_item_id for r in self.lines}) != len(self.lines):
            raise ValueError("同一订单明细不能重复")
        if len({r.supplier_item_id for r in self.lines}) != len(self.lines):
            raise ValueError("同一采购来源不能重复")
        return self


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def _snapshot(row):
    # All mapped values participate in the review token, including immutable
    # sources and fields which currently have no optimistic version column.
    return {prop.key: getattr(row, prop.key)
            for prop in inspect(type(row)).column_attrs}


def _rows(db, model, condition):
    return [_snapshot(row) for row in db.scalars(select(model).where(condition).order_by(model.id))]


def preview(db, *, actor, request: ClosedBatchRequest) -> dict:
    """Qualify facts without flushing, mutating, logging, reserving or pricing."""
    if actor is None:
        raise PermissionError("需要有效操作员")
    if object_session(actor) is not db:
        raise ValueError("操作员必须来自本次只读会话")
    with db.no_autoflush:
        if not actor.is_active:
            raise PermissionError("需要有效操作员")
        permissions = ("orders.view", "incoming.view", "deliveries.view", "requisition.purchase_price.confirm")
        if not all(has_permission(actor, p) for p in permissions):
            raise PermissionError("历史闭合批预览需要订单、收料、送货查看及采购价格确认权限")
        if not has_unrestricted_customer_access(actor, db) and request.customer_id not in customer_scope_ids(actor, db):
            raise PermissionError("无客户访问权限")
        order = db.get(Order, request.order_id, populate_existing=True)
        supplier = db.get(SupplierRequisitionOrder, request.supplier_order_id, populate_existing=True)
        if order is None or order.customer_id != request.customer_id:
            raise ValueError("订单身份或客户不匹配")
        if supplier is None:
            raise ValueError("采购单不存在")
        items = list(db.scalars(select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.id)))
        sources = list(db.scalars(select(SupplierRequisitionOrderItem).where(
            SupplierRequisitionOrderItem.supplier_order_id == supplier.id).order_by(SupplierRequisitionOrderItem.id)))
        item_ids = [row.id for row in items]
        source_ids = [row.id for row in sources]
        # Validate source ownership before taking or returning any PO snapshot.
        # A qualification blocker is not a customer-scope authorization check.
        if not sources or any(row.order_item_id not in item_ids for row in sources):
            raise PermissionError("采购来源不完整属于本次授权订单")
        purposes = list(db.scalars(select(PurchasePurposeSourceSnapshot).where(
            PurchasePurposeSourceSnapshot.supplier_requisition_order_item_id.in_(source_ids)
        ).order_by(PurchasePurposeSourceSnapshot.id)))
        if any(p.customer_id != request.customer_id or p.source_order_item_id not in item_ids for p in purposes):
            raise PermissionError("采购用途包含本次订单以外的客户或来源")
        # Include any other supplier source for this order, including voided
        # historical rows; never silently assume the selected PO is the full chain.
        other_sources = _rows(db, SupplierRequisitionOrderItem,
            SupplierRequisitionOrderItem.order_item_id.in_(item_ids)
            & (SupplierRequisitionOrderItem.supplier_order_id != supplier.id))
        downstream = {
            "receipts": _rows(db, IncomingReceiptItem, or_(IncomingReceiptItem.order_item_id.in_(item_ids),
                IncomingReceiptItem.supplier_order_item_id.in_(source_ids))),
            "production_completions": _rows(db, ProductionCompletion, ProductionCompletion.order_item_id.in_(item_ids)),
            "inventory_reservations": _rows(db, InventoryReservation, InventoryReservation.order_item_id.in_(item_ids)),
            "inventory_movements": _rows(db, InventoryMovement, InventoryMovement.related_order_item_id.in_(item_ids)),
            "deliveries": _rows(db, DeliveryItem, DeliveryItem.order_item_id.in_(item_ids)),
            "purchase_price_facts": _rows(db, PurchaseReceiptFact,
                PurchaseReceiptFact.supplier_requisition_order_item_id.in_(source_ids)),
            "customer_charges": _rows(db, CustomerCharge, CustomerCharge.order_id == order.id),
            "other_requisition_sources": _rows(db, RequisitionItem, RequisitionItem.order_item_id.in_(item_ids)),
        }
        related_deliveries = [_snapshot(row) for row in db.scalars(select(DeliveryItem).join(
            Delivery, Delivery.id == DeliveryItem.delivery_id).where(
                Delivery.customer_id == order.customer_id,
                or_(Delivery.delivery_number == request.original_delivery_number,
                    DeliveryItem.customer_po_snapshot == order.customer_po if order.customer_po else False),
            ).order_by(DeliveryItem.id))]
        bom_components = _rows(db, SalesOrderItemBomComponent, SalesOrderItemBomComponent.sales_order_item_id.in_(item_ids))
        bom_graphs = [_snapshot(row) for row in db.scalars(select(OrderBomGraph).where(
            OrderBomGraph.order_item_id.in_(item_ids)).order_by(OrderBomGraph.order_item_id))]
        tasks = _rows(db, ProductionTask, ProductionTask.order_item_id.in_(item_ids))
        facts = {"order": _snapshot(order), "items": [_snapshot(r) for r in items],
                 "supplier_order": _snapshot(supplier), "supplier_items": [_snapshot(r) for r in sources],
                 "purposes": [_snapshot(r) for r in purposes], "other_supplier_sources": other_sources,
                 "production_tasks": tasks, "downstream": downstream,
                 "related_deliveries": related_deliveries,
                 "bom_components": bom_components, "bom_graphs": bom_graphs}
        blockers = []

        def block(code, detail, item_id=None):
            blockers.append({"code": code, "detail": detail, "order_item_id": item_id})

        if order.status != "pending_production" or order.payment_status != "unpaid":
            block("order_state", "仅接受本次尚未执行的新补录订单")
        if (not order.created_at or order.created_at.date() < date(2026, 10, 22)
                or "旧ERP历史补录" not in (order.remark or "")):
            block("not_current_task_backfill", "仅接受2026-10-22任务明确登记的新补录订单")
        if supplier.status != "confirmed":
            block("purchase_not_confirmed", "必须已有正式确认采购")
        if other_sources:
            block("additional_purchase_source", "订单还存在其他采购来源，不能作为完整单批承接")
        if set(item_ids) != {r.order_item_id for r in request.lines} or set(source_ids) != {r.supplier_item_id for r in request.lines}:
            block("incomplete_batch", "必须完整覆盖订单与采购全部明细")
        if any(downstream.values()):
            block("existing_downstream", "已存在收料、库存、生产、送货或价格事实；不得重复承接")
        if related_deliveries:
            block("possible_existing_delivery", "同客户PO或原送货号已有送货记录，需先排除重复")
        if bom_components or bom_graphs:
            block("bom_not_supported", "首版不承接BOM或组件历史链")
        if any(t["status"] != "waiting_material" or t["material_received_quantity"] or t["material_input_quantity"] for t in tasks):
            block("production_already_started", "生产任务已具有执行事实")
        if request.order_date.value != order.order_date:
            block("order_date_mismatch", "保留已创建订单日及其推算来源")
        if "推算" in (order.remark or "") and not request.order_date.is_inferred:
            block("inferred_order_date_not_marked", "本单原备注已标推算日期，不能改称真实日期")
        if not order.order_date <= request.received_date.value <= request.delivery_date.value < beijing_today():
            block("historical_date_sequence", "须满足订单日≤收料日≤历史送货日<今天")
        by_item = {r.id: r for r in items}
        by_source = {r.id: r for r in sources}
        for line in request.lines:
            item, source = by_item.get(line.order_item_id), by_source.get(line.supplier_item_id)
            if not item or not source:
                continue
            ps = [p for p in purposes if p.supplier_requisition_order_item_id == source.id]
            if (source.order_item_id != item.id or source.product_id != item.product_id
                    or source.status != "active" or source.purpose_contract_status != "frozen"
                    or item.delivered_quantity or item.material_status != "pending" or item.is_force_closed):
                block("source_state", "订单与采购来源身份或待收料状态不符", item.id)
            if (not source.material_id or not source.material_code_snapshot
                    or not source.report_length_mm or source.report_length_mm <= 0
                    or not source.report_width_mm or source.report_width_mm <= 0
                    or not source.flute_type_snapshot or not source.layer_count_snapshot
                    or not (source.supplier_name_snapshot or supplier.supplier_name)):
                block("purchase_spec_incomplete", "采购材料、长宽、层楞或供应商冻结资料不全", item.id)
            frozen_spec = dict(material_id=source.material_id, material_code=source.material_code_snapshot,
                flute_type=source.flute_type_snapshot, layer_count=source.layer_count_snapshot,
                report_length_mm=source.report_length_mm, report_width_mm=source.report_width_mm,
                crease_type=item.snapshot_crease_type, crease_left_mm=item.snapshot_crease_left_mm,
                crease_middle_mm=item.snapshot_crease_middle_mm, crease_right_mm=item.snapshot_crease_right_mm,
                cutting_mode=source.cutting_mode, sheet_cutting_snapshot=source.sheet_cutting_snapshot)
            _, _, spec_issues = delivery_contract(source.supplier_name_snapshot or supplier.supplier_name,
                                                  frozen_spec, typed_snapshot={})
            try:
                cutting = SheetCuttingContract.from_snapshot(source.sheet_cutting_snapshot)
                if cutting.yield_per_supplier_sheet != 1 or cutting.is_die_cut:
                    spec_issues.append("only_non_diecut_1_to_1")
            except SheetCuttingContractError:
                spec_issues.append("sheet_cutting_snapshot")
            if item.snapshot_crease_type == "压线":
                segments = (item.snapshot_crease_left_mm, item.snapshot_crease_middle_mm, item.snapshot_crease_right_mm)
                if any(v is None or v <= 0 for v in segments) or sum(v or 0 for v in segments) != source.report_width_mm:
                    spec_issues.append("crease_sum_report_width")
            if spec_issues:
                block("purchase_delivery_contract_incomplete", "冻结供应纸合同不完整或不一致：" + ",".join(sorted(set(spec_issues))), item.id)
            if (item.supply_mode_snapshot != "corrugated_production"
                    or item.snapshot_pieces_per_box != 1 or item.sales_unit_snapshot not in ("只", "片")
                    or item.inventory_deducted_qty or source.stock_deduction_qty):
                block("unsupported_source", "首版只接受无库存抵扣、单片1:1的纸箱订单", item.id)
            if (len(ps) != 1 or ps[0].customer_id != request.customer_id
                    or ps[0].source_order_item_id != item.id or ps[0].component_type != "whole"
                    or ps[0].yield_per_sheet_snapshot != 1 or ps[0].pieces_per_finished_snapshot != 1
                    or ps[0].reserve_purpose_sheet_qty or ps[0].source_semi_reserved_piece_qty_snapshot
                    or ps[0].order_purpose_sheet_qty != source.requisition_qty):
                block("purpose_not_simple_closed", "冻结采购用途不是完整单片零余料闭合批", item.id)
            if not (line.received_sheets == line.finished_quantity == line.delivered_quantity
                    == item.quantity == source.requisition_qty):
                block("quantity_not_closed", "首版要求采购实收、产出和已送全部相等且无余量", item.id)
            price = line.historical_price
            if price is None:
                block("cost_missing", "缺少原采购历史单价；当前材质价及新订单预计成本不能替代", item.id)
            elif price.material_id != source.material_id or price.supplier_name != (source.supplier_name_snapshot or supplier.supplier_name):
                block("historical_price_identity", "历史价格的实际材料或供应商与采购来源不一致", item.id)
        ready = not blockers
        # Deliberately unavailable: no UI can interpret a read-only candidate as
        # permission to post through the ordinary receiving endpoint.
        block("historical_posting_unavailable", "专用历史追加台账、成本投影与撤销保护尚未实现；不可提交")
        content = {"schema_version": 1, "actor_id": actor.id,
                   "request": request.model_dump(mode="json"), "facts": facts, "blockers": blockers}
        digest = hashlib.sha256(_canonical(content).encode()).hexdigest()
        return {"can_submit": False, "business_facts_ready": ready, "read_only": True,
                "reviewed_hash": digest, "blockers": blockers, "before": json.loads(_canonical(facts)),
                "net_current_inventory_change": 0, "current_location_required": False,
                "prices_derived_from_current_master": False,
                "requires_new_historical_ledger": True}
