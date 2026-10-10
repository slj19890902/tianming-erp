"""Proof of a main replenishment request from its hash and durable source rows.

This is deliberately not a historical response snapshot. No write or fresh
master-data qualification belongs in these projections.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR
import hashlib
import re

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.encoders import jsonable_encoder
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.product import Product
from app.models.stock_replenishment import StockReplenishmentOrder, InventoryStockPolicy
from app.models.external_packaging_purchase import ExternalPackagingPurchaseBatch, ExternalPackagingPurchaseOrder
from app.services.purchase_purpose_allocation import canonical_purchase_purpose_hash
from app.services.replenishment_receipt_progress import receipt_progress_map
from app.services.external_packaging_purchase_lifecycle import cancelled_external_purchase_order_ids

HEADERS = {"Cache-Control": "no-store", "X-Stock-Replenishment-Preserve": "1"}


class SaveRecoveryRoute(APIRoute):
    """Apply the local protocol also to authentication and schema failures."""
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request):
            try:
                response = await original(request)
                response.headers["Cache-Control"] = "no-store"
                return response
            except RequestValidationError as error:
                return JSONResponse({"detail": jsonable_encoder(error.errors())}, status_code=422, headers=HEADERS)
            except StarletteHTTPException as error:
                headers = {**HEADERS, **(error.headers or {})}
                if headers.get("X-Stock-Replenishment-Rejected") == "1":
                    headers.pop("X-Stock-Replenishment-Preserve", None)
                return JSONResponse({"detail": error.detail}, status_code=error.status_code, headers=headers)
            except Exception:
                return JSONResponse({"detail": "保存结果暂无法确认，请保留原请求并核对结果。"}, status_code=500, headers=HEADERS)
        return handle


def normalized_request(payload):
    return payload.model_dump(mode="json", exclude_none=False, exclude={"expected_actor_id"})


def request_digest(actor_id, payload):
    return canonical_purchase_purpose_hash({"actor_id": actor_id, "payload": normalized_request(payload)})


def check_actor(expected, actor_id):
    if expected is not None and expected != actor_id:
        raise HTTPException(409, "当前账号已变化，请保留原请求并切回原账号核对。", headers={**HEADERS, "X-Stock-Replenishment-Actor-Mismatch": "1"})


def find_source(db, key):
    suffix = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20].upper()
    rows = list(db.scalars(select(StockReplenishmentOrder).where(
        StockReplenishmentOrder.order_number.like(f"CBR-%-{suffix}") |
        StockReplenishmentOrder.order_number.like(f"CBW-%-{suffix}")
    ).options(selectinload(StockReplenishmentOrder.items)).order_by(StockReplenishmentOrder.id).limit(2)))
    batch = db.scalar(select(ExternalPackagingPurchaseBatch).where(ExternalPackagingPurchaseBatch.idempotency_key == key))
    if len(rows) > 1 or (batch is not None and (not rows or batch.stock_replenishment_order_id != rows[0].id)):
        raise HTTPException(409, "原提交标识的来源事实不一致，请保留请求并联系管理员核对。", headers=HEADERS)
    return rows[0] if rows else None


def require_source_scope(db, order, allowed):
    """Customer identity only: inactive masters are not fresh eligibility."""
    customer_ids = set()
    if order.customer_id is not None:
        customer_ids.add(order.customer_id)
    if not order.items:
        raise HTTPException(409, "原单明细缺失，请保留请求并核对原单。", headers=HEADERS)
    for row in order.items:
        ids = {row.customer_id} if row.customer_id is not None else set()
        for ident in (row.product_id, row.reference_product_id):
            if ident is not None:
                product = db.get(Product, ident)
                if product is None or product.customer_id is None:
                    raise HTTPException(403, "原单客户身份不完整，无法确认访问范围。", headers=HEADERS)
                ids.add(product.customer_id)
        if row.stock_policy_id is not None:
            policy = db.get(InventoryStockPolicy, row.stock_policy_id)
            if policy is None:
                raise HTTPException(403, "原单预警客户身份不完整。", headers=HEADERS)
            if policy.customer_id is not None:
                ids.add(policy.customer_id)
            if policy.product_id is not None:
                product = db.get(Product, policy.product_id)
                if product is None or product.customer_id is None:
                    raise HTTPException(403, "原单预警产品客户身份不完整。", headers=HEADERS)
                ids.add(product.customer_id)
        if len(ids) != 1 or (allowed is not None and not ids.issubset(allowed)):
            raise HTTPException(403, "无客户补库结果访问权限。", headers=HEADERS)
        customer_ids.update(ids)
    if order.customer_id is not None and any(row.customer_id != order.customer_id for row in order.items):
        raise HTTPException(403, "原单及明细客户身份不一致。", headers=HEADERS)
    if allowed is not None and not customer_ids.issubset(allowed):
        raise HTTPException(403, "无客户补库结果访问权限。", headers=HEADERS)


def external_batch(db, order):
    return db.scalar(select(ExternalPackagingPurchaseBatch).where(
        ExternalPackagingPurchaseBatch.stock_replenishment_order_id == order.id
    ).options(selectinload(ExternalPackagingPurchaseBatch.purchase_orders).selectinload(ExternalPackagingPurchaseOrder.items)))


def _text_decimal(value):
    return format(Decimal(value), "f").rstrip("0").rstrip(".") if Decimal(value) % 1 else str(int(Decimal(value)))


def build_receipt(db, order, payload, actor_id):
    request = normalized_request(payload)
    digest = request_digest(actor_id, payload)
    key = payload.idempotency_key
    suffix = hashlib.sha256(key.encode()).hexdigest()[:20].upper()
    expected_prefix = "CBR" if payload.source_type == "customer_request" else "CBW"
    if order.created_by != actor_id or order.source_type != payload.source_type:
        raise HTTPException(409, "原提交来源或操作人不匹配，请保留原请求核对。", headers=HEADERS)
    if order.request_hash is not None and order.request_hash != digest:
        raise HTTPException(409, "原提交内容不匹配，请保留原请求核对。", headers=HEADERS)
    if not re.fullmatch(rf"{expected_prefix}-\d{{8}}-{suffix}", order.order_number):
        return None
    if order.request_hash is None:
        return None
    rows = sorted(order.items, key=lambda item: item.id)
    if len(rows) != len(payload.items):
        return None
    batch = external_batch(db, order)
    purchases = {}
    external = None
    if batch is not None:
        if batch.idempotency_key != key or batch.confirmed_by != actor_id or len(rows) != 1:
            return None
        purchase_rows = []
        for purchase_order in batch.purchase_orders:
            if purchase_order.confirmed_by != actor_id:
                return None
            for line in purchase_order.items:
                if line.stock_replenishment_item_id in purchases:
                    return None
                purchases[line.stock_replenishment_item_id] = line
                if not line.order_quantity_basis_snapshot or not line.purchase_quantity_basis_snapshot:
                    return None
                converted = int((line.purchase_quantity * line.order_quantity_basis_snapshot / line.purchase_quantity_basis_snapshot).to_integral_value(rounding=ROUND_FLOOR))
                purchase_rows.append({"source_item_id": line.stock_replenishment_item_id,
                    "purchase_order_id": purchase_order.id, "purchase_item_id": line.id,
                    "external_product_id_snapshot": line.external_product_id_snapshot,
                    "purchase_quantity": _text_decimal(line.purchase_quantity), "purchase_unit": line.purchase_unit,
                    "order_quantity_basis": _text_decimal(line.order_quantity_basis_snapshot),
                    "purchase_quantity_basis": _text_decimal(line.purchase_quantity_basis_snapshot),
                    "converted_source_quantity": converted})
        if set(purchases) != {row.id for row in rows}:
            return None
        external = {"batch_id": batch.id, "idempotency_key": key, "source_order_id": order.id,
            "purchase_order_ids": [row.id for row in batch.purchase_orders], "lines": purchase_rows}
    projected = []
    for index, (original, row) in enumerate(zip(payload.items, rows)):
        reference = original.reference_product_id or original.product_id
        if original.stock_policy_id is not None and row.stock_policy_id != original.stock_policy_id:
            return None
        if reference is not None and row.reference_product_id != reference:
            return None
        if original.customer_id is not None and row.customer_id != original.customer_id:
            return None
        if row.target_inventory_type != original.target_inventory_type:
            return None
        if batch is None:
            if row.quantity != original.quantity or row.procurement_route_snapshot not in (None, "paperboard"):
                return None
            if row.target_inventory_type == "semi_finished" and (
                not row.material_code_snapshot or row.layer_count not in (1, 3, 5, 7)
                or not row.flute_type or row.report_length_mm is None or row.report_width_mm is None
            ):
                return None
            for field in ("material_id", "layer_count", "flute_type", "report_length_mm", "report_width_mm", "crease_type", "crease_left_mm", "crease_middle_mm", "crease_right_mm"):
                value = getattr(original, field)
                if field == "layer_count" and original.material_id is not None:
                    # The existing builder takes material.layer_count rather
                    # than this request hint; preserve the actual source value.
                    continue
                if value is not None and getattr(row, field) != value:
                    return None
            if any(getattr(row, field) != getattr(original, field) for field in ("sheet_type", "component_type", "pieces_per_box", "stock_yield_per_sheet")):
                return None
        else:
            line = purchases[row.id]
            converted = external["lines"][0]["converted_source_quantity"]
            if row.procurement_route_snapshot != "external_packaging" or row.quantity != converted or line.customer_product_id_snapshot != row.reference_product_id:
                return None
            if original.external_purchase_quantity is not None:
                if original.external_purchase_quantity != line.purchase_quantity:
                    return None
            elif row.quantity != original.quantity:
                return None
            # Legacy create accepts these request hints but prepare determines
            # the actual unit/bases. Their original values remain hash-bound.
        fields = {field: getattr(row, field) for field in (
            "stock_policy_id", "product_id", "reference_product_id", "customer_id", "material_id", "target_inventory_type",
            "internal_name", "layer_count", "flute_type", "report_length_mm", "report_width_mm", "crease_type",
            "crease_left_mm", "crease_middle_mm", "crease_right_mm", "sheet_type", "component_type", "pieces_per_box", "stock_yield_per_sheet", "remark")}
        fields.update({"request_index": index, "source_item_id": row.id, "requested_quantity": original.quantity,
            "saved_quantity": row.quantity, "procurement_route": row.procurement_route_snapshot,
            "product_code": row.product_code_snapshot, "product_name": row.product_name_snapshot, "material_code": row.material_code_snapshot})
        projected.append(fields)
    return jsonable_encoder({"schema_version": 1, "idempotency_key": key, "actor_id": actor_id,
        "request_hash": digest, "request": request, "request_match": True,
        "outcome": "direct_external_purchase_confirmed" if batch else "draft_saved",
        "source": {field: getattr(order, field) for field in ("id", "order_number", "source_type", "created_by", "created_at", "customer_id", "supplier_name", "remark")},
        "lines": projected, "external_purchase": external})


def current_summary(db, order):
    progress = receipt_progress_map(db, order.items)
    batch = external_batch(db, order)
    external = None
    if batch is not None:
        cancelled = cancelled_external_purchase_order_ids(db, {row.id for row in batch.purchase_orders})
        external = {"batch_id": batch.id, "purchase_orders": [{"id": row.id,
            "status": "cancelled" if row.id in cancelled else row.status} for row in batch.purchase_orders]}
    return {"source_id": order.id, "status": order.status, "stocked_quantity": sum(progress[row.id]["received_quantity"] for row in order.items),
        "items": [{"source_item_id": row.id, "stocked_quantity": row.stocked_quantity,
            "receipt_progress": progress[row.id]} for row in order.items],
        "external_purchase_status": external}


def trace(order):
    return {"source_id": order.id, "order_number": order.order_number, "url": f"/api/requisition/stock-replenishment/orders/{order.id}"}
