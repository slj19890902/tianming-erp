from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import RoleChecker, get_db
from app.models.user import User
from app.services.xinzhen_carton_marking import XinzhenCartonMarkingImportService

router = APIRouter()
can_read = RoleChecker(["admin", "finance", "sales", "workshop"])
can_operate = RoleChecker(["admin", "sales"])


class ImportJsonRequest(BaseModel):
    customer_id: int = Field(ge=1)
    source_file_name: str = Field(min_length=1, max_length=255)
    source_file_hash: str = Field(min_length=1, max_length=128)
    parsed_json: Any
    mode: Literal["dry_run", "apply"] = "apply"


@router.post("/import-json", status_code=status.HTTP_201_CREATED)
def import_json(
    payload: ImportJsonRequest,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
):
    service = XinzhenCartonMarkingImportService(db)
    try:
        result = service.import_parsed_json(
            customer_id=payload.customer_id,
            source_file_name=payload.source_file_name,
            source_file_hash=payload.source_file_hash,
            parsed_json=payload.parsed_json,
            mode=payload.mode,
            user_id=user.id,
        )
    except ValueError as error:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(error)) from error
    return result


@router.get("/import-batches/{batch_id}")
def get_import_batch(
    batch_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
):
    service = XinzhenCartonMarkingImportService(db)
    try:
        return service.get_import_batch_detail(batch_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/import-batches/{batch_id}/mobile-view")
def get_mobile_view(
    batch_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
):
    service = XinzhenCartonMarkingImportService(db)
    try:
        return service.get_mobile_view(batch_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
