from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import (
    get_current_user,
    get_db,
    has_permission,
    require_customer_access,
)
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.product_drawing import ProductDrawing
from app.models.user import User
from app.services.product_drawings import (
    DRAWING_URL_PREFIX,
    ORDER_DRAFT_URL_PREFIX,
    DrawingValidationError,
    load_order_draft_drawing,
    load_stored_drawing,
)


router = APIRouter()


def _private_file_response(
    request: Request,
    *,
    content: bytes,
    content_type: str,
    filename: str,
) -> Response:
    encoded_name = quote(filename, safe="")
    fallback_suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    headers = {
        "Cache-Control": "private, no-store",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": (
            f'inline; filename="drawing.{fallback_suffix}"; '
            f"filename*=UTF-8''{encoded_name}"
        ),
        "Content-Length": str(len(content)),
    }
    return Response(
        content=b"" if request.method == "HEAD" else content,
        media_type=content_type,
        headers=headers,
    )


def _drawing_customer_and_reference_types(
    db: Session,
    filename: str,
) -> tuple[int, frozenset[str]]:
    canonical_path = f"{DRAWING_URL_PREFIX}/{filename}"
    accepted_paths = (canonical_path, canonical_path.lstrip("/"))
    order_customer_ids = set(
        db.scalars(
            select(Order.customer_id)
            .join(OrderItem, OrderItem.order_id == Order.id)
            .where(OrderItem.drawing_file.in_(accepted_paths))
        ).all()
    )
    product_customer_ids = set(
        db.scalars(
            select(Product.customer_id)
            .join(ProductDrawing, ProductDrawing.product_id == Product.id)
            .where(
                or_(
                    ProductDrawing.image_path.in_(accepted_paths),
                    ProductDrawing.thumbnail_path.in_(accepted_paths),
                )
            )
        ).all()
    )
    customer_ids = order_customer_ids | product_customer_ids
    if len(customer_ids) != 1:
        # Unknown and cross-customer ambiguous references are intentionally
        # indistinguishable to callers.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="图纸不存在")
    reference_types = set()
    if order_customer_ids:
        reference_types.add("order")
    if product_customer_ids:
        reference_types.add("product")
    return next(iter(customer_ids)), frozenset(reference_types)


@router.api_route("/files/{filename}", methods=["GET", "HEAD"])
def read_formal_drawing(
    filename: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    customer_id, reference_types = _drawing_customer_and_reference_types(db, filename)
    require_customer_access(customer_id, current_user=user, db=db)
    can_view_reference = (
        "order" in reference_types and has_permission(user, "orders.view")
    ) or (
        "product" in reference_types and has_permission(user, "products.view")
    )
    if not can_view_reference:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="权限不足")
    try:
        drawing = load_stored_drawing(filename)
    except DrawingValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="图纸不存在",
        ) from error
    return _private_file_response(
        request,
        content=drawing.content,
        content_type=drawing.content_type,
        filename=drawing.filename,
    )


@router.api_route("/drafts/{token}/{filename}", methods=["GET", "HEAD"])
def read_order_draft_drawing(
    token: str,
    filename: str,
    request: Request,
    user: User = Depends(get_current_user),
) -> Response:
    reference = f"{ORDER_DRAFT_URL_PREFIX}/{token}/{filename}"
    try:
        content, content_type = load_order_draft_drawing(
            reference,
            owner_user_id=user.id,
            owner_auth_version=user.auth_version,
        )
    except DrawingValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="图纸草稿不存在或已失效",
        ) from error
    return _private_file_response(
        request,
        content=content,
        content_type=content_type,
        filename=filename,
    )
