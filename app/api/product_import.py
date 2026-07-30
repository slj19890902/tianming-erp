from __future__ import annotations

from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import (
    PermissionChecker,
    RoleChecker,
    get_db,
    require_customer_access,
)
from app.models.customer import Customer
from app.models.user import User
from app.services import product_import_workbook


router = APIRouter()
can_read = PermissionChecker("products.view")
admin_only = RoleChecker({"admin"})


class ProductWorkbookApplyPayload(BaseModel):
    preview_token: str = Field(min_length=20, max_length=200)


def _workbook_error(
    error: product_import_workbook.ProductWorkbookError,
) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.message},
    )


@router.get("/import-template.xlsx")
def download_product_workbook(
    customer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> Response:
    require_customer_access(customer_id, current_user=user, db=db)
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="客户不存在")
    content = product_import_workbook.build_product_workbook(
        db,
        customer=customer,
    )
    filename = f"天明ERP_{customer.name}_样品常用箱整理预览.xlsx"
    encoded = quote(filename)
    return Response(
        content=content,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": (
                'attachment; filename="product_import.xlsx"; '
                f"filename*=UTF-8''{encoded}"
            ),
            "Cache-Control": "private, no-store",
        },
    )


@router.post("/import/preview")
async def preview_product_workbook(
    customer_id: int = Form(...),
    file: UploadFile = File(...),
    drawing_files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    require_customer_access(customer_id, current_user=user, db=db)
    try:
        return await product_import_workbook.preview_product_workbook(
            db,
            customer_id=customer_id,
            workbook_upload=file,
            drawing_files=drawing_files,
            user=user,
        )
    except product_import_workbook.ProductWorkbookError as error:
        raise _workbook_error(error) from error


@router.post("/import/apply")
def apply_product_workbook(
    payload: ProductWorkbookApplyPayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    try:
        return product_import_workbook.apply_product_workbook(
            db,
            preview_token=payload.preview_token,
            user=user,
        )
    except product_import_workbook.ProductWorkbookError as error:
        raise _workbook_error(error) from error
