"""Unified read-only product search and actual-lot reverse discovery."""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_current_user, has_permission, require_customer_access
from app.api.mobile_erp import _visible_customer_ids
from app.api.warehouse import _require_lot_customer_access
from app.models.product import Product
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot
from app.services.material_candidates import candidate_items
from app.services.mobile_product_drawings import product_drawing_metadata
from app.services.product_workbench import (
    action_card, find_products, inventory_details, inventory_summary,
    order_card, product_card, production_card, visible_products,
)

router = APIRouter()


def _headers(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"


def _can_view(user: User) -> bool:
    return any(has_permission(user, code) for code in
               ("products.view", "orders.view", "warehouse.view", "incoming.view",
                "production.printing.view", "production.die_cut.view"))


def _require_view(user: User) -> None:
    if not _can_view(user):
        raise HTTPException(403, "当前账号没有产品查看权限")


@router.get("/search")
def search_products(response: Response,
                    q: str = Query(default="", max_length=100),
                    customer_id: int | None = Query(default=None, gt=0),
                    dimension_basis: Literal["finished", "net", "report"] = "finished",
                    length: Decimal | None = Query(default=None, gt=0, decimal_places=2),
                    width: Decimal | None = Query(default=None, gt=0, decimal_places=2),
                    height: Decimal | None = Query(default=None, gt=0, decimal_places=2),
                    page: int = Query(default=1, ge=1),
                    page_size: int = Query(default=20, ge=1, le=50),
                    db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    _headers(response)
    _require_view(user)
    if not q.strip() and all(value is None for value in (length, width, height)):
        raise HTTPException(422, "请输入关键词或至少一项尺寸")
    if customer_id is not None:
        require_customer_access(customer_id, user, db)
    scope = _visible_customer_ids(user, db)
    products, total = find_products(db, scope, q=q, customer_id=customer_id,
                                    dimension_basis=dimension_basis, length=length,
                                    width=width, height=height, page=page, page_size=page_size)
    summary = inventory_summary(db, products) if has_permission(user, "warehouse.view") else {}
    drawings = product_drawing_metadata(db, [p.id for p in products], user=user,
                                        visible_customer_ids=scope)
    items = [product_card(p, summary.get(p.id, {"visibility": "hidden_by_permission"}),
                          drawings.get(p.id, {"status": "none", "items": []})) for p in products]
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "has_more": page * page_size < total}


@router.get("/reverse")
def reverse_products(response: Response,
                     length: Decimal = Query(gt=0, decimal_places=2),
                     width: Decimal = Query(gt=0, decimal_places=2),
                     material_code: str | None = Query(default=None, max_length=100),
                     flute_type: str | None = Query(default=None, max_length=20),
                     layer_count: int | None = Query(default=None, ge=1, le=7),
                     processed_state: Literal["raw", "printed", "creased", "die_cut"] = "raw",
                     known_customer_id: int | None = Query(default=None, gt=0),
                     lot_id: int | None = Query(default=None, gt=0),
                     page: int = Query(default=1, ge=1),
                     page_size: int = Query(default=20, ge=1, le=50),
                     db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    _headers(response)
    _require_view(user)
    if known_customer_id is not None:
        require_customer_access(known_customer_id, user, db)
    scope = _visible_customer_ids(user, db)
    if lot_id is not None:
        if not has_permission(user, "warehouse.view"):
            raise HTTPException(403, "当前账号无权查看实际库存批次")
        lot = _require_lot_customer_access(db, lot_id, user)
        detail = lot.semi_finished_detail
        if detail is None or lot.inventory_type != "semi_finished":
            raise HTTPException(422, "请选择实际片料批次")
        if Decimal(detail.board_length_mm) != length or Decimal(detail.board_width_mm) != width:
            raise HTTPException(422, "实测长宽与所选批次不一致，请重新核对")
        if material_code and material_code.strip().upper() != detail.material_code_snapshot.strip().upper():
            raise HTTPException(422, "材质与所选批次登记事实不一致")
        if flute_type and flute_type.strip().upper() != detail.flute_type.upper():
            raise HTTPException(422, "楞型与所选批次登记事实不一致")
        if layer_count and layer_count != detail.layer_count:
            raise HTTPException(422, "层数与所选批次登记事实不一致")
        from app.services.warehouse_goods import goods_profile
        profile = goods_profile(db, lot)
        actual_state = (profile or {}).get("processing") or {
            "raw_board": "raw", "creased_sheet": "creased"}.get(detail.sheet_type)
        if actual_state and actual_state != processed_state:
            raise HTTPException(422, "加工状态与所选批次登记事实不一致")
        matches = candidate_items(db, lot, scope)
        if known_customer_id is not None:
            matches = [row for row in matches if row["customer_id"] == known_customer_id]
        total = len(matches)
        selected = matches[(page - 1) * page_size:page * page_size]
        products = {p.id: p for p in db.scalars(visible_products(db, scope).where(
            Product.id.in_([row["product_id"] for row in selected])))}
        summaries = inventory_summary(db, list(products.values()))
        drawings = product_drawing_metadata(db, products, user=user, visible_customer_ids=scope)
        items = []
        for row in selected:
            product = products.get(row["product_id"])
            if product is None:
                continue
            item = product_card(product, summaries[product.id], drawings[product.id])
            item.update({"match_class": row["match_kind"], "match_reasons": [row["match_reason"]],
                         "check_items": row.get("warnings", []), "actual_lot_id": lot.id,
                         "deductible": False, "selection_requires_existing_validation": True})
            items.append(item)
        return {"items": items, "total": total, "page": page, "page_size": page_size,
                "has_more": page * page_size < total, "source": "actual_lot",
                "registered_owner_customer_id": detail.owner_customer_id}
    # A free measurement has no real material, processing, identity, or quantity
    # evidence. Use dimensions for discovery only; never claim a cut plan.
    query = visible_products(db, scope).where(Product.is_active.is_(True))
    if known_customer_id is not None:
        query = query.where(Product.customer_id == known_customer_id)
    if flute_type:
        query = query.where(Product.flute_type == flute_type.strip().upper())
    if layer_count:
        query = query.where(Product.layer_count == layer_count)
    if material_code:
        query = query.where(Product.default_material_code == material_code.strip().upper())
    query = query.where(Product.report_length_mm == length, Product.report_width_mm == width)
    from sqlalchemy import func
    total = int(db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0)
    products = list(db.scalars(query.order_by(Product.customer_id, Product.id)
                               .offset((page - 1) * page_size).limit(page_size)))
    summaries = inventory_summary(db, products) if has_permission(user, "warehouse.view") else {}
    drawings = product_drawing_metadata(db, [p.id for p in products], user=user, visible_customer_ids=scope)
    items = []
    for product in products:
        item = product_card(product, summaries.get(product.id, {"visibility": "hidden_by_permission"}), drawings[product.id])
        item.update({"match_class": "needs_review", "match_reasons": ["报料尺寸相同，仅供查找用途"],
                     "check_items": ["核对实际批次、材质、楞型、加工状态、客户及用途"],
                     "actual_lot_id": None, "deductible": False,
                     "selection_requires_existing_validation": True})
        items.append(item)
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "has_more": page * page_size < total, "source": "free_measurement",
            "registered_owner_customer_id": None}


@router.get("/products/{product_id}")
def product_details(product_id: int, response: Response,
                    include_history: bool = Query(default=False),
                    db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    _headers(response)
    _require_view(user)
    scope = _visible_customer_ids(user, db)
    product = db.scalar(visible_products(db, scope).where(Product.id == product_id))
    if product is None:
        raise HTTPException(404, "产品不存在或当前账号无权查看")
    warehouse_allowed = has_permission(user, "warehouse.view")
    orders_allowed = has_permission(user, "orders.view")
    summary = inventory_summary(db, [product])[product.id] if warehouse_allowed else {"visibility": "hidden_by_permission"}
    drawing = product_drawing_metadata(db, [product.id], user=user, visible_customer_ids=scope)[product.id]
    inventory = inventory_details(db, product) if warehouse_allowed else {"visibility": "hidden_by_permission", "items": []}
    orders = order_card(db, product, include_history=include_history) if orders_allowed else {"visibility": "hidden_by_permission", "items": []}
    return {"product": product_card(product, summary, drawing),
            "production": production_card(db, product, scope, can_see_mold_location=warehouse_allowed),
            "inventory": inventory, "orders": orders,
            "actions": action_card(db, product,
                                   can_edit_requisition=has_permission(user, "requisition.execute"),
                                   can_request=has_permission(user, "business_requests.submit")),
            "read_only": True}
