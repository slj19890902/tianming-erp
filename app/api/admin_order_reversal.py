"""Admin-only disposition API. Preview performs no writes."""
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session
from app.api.deps import get_db, PermissionChecker
from app.models.user import User
from app.services import admin_order_reversal as service
from app.services.external_packaging_purchase_lifecycle import ExternalPackagingPurchaseLifecycleError

router = APIRouter()
can_dispose = PermissionChecker('orders.delete')


class Preview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    order_ids: list[int] = Field(min_length=1, max_length=50)
    mode: Literal['withdraw','keep_stock','delete_trial']

    @field_validator('order_ids')
    @classmethod
    def unique_positive(cls, value):
        if any(i <= 0 for i in value) or len(value) != len(set(value)):
            raise ValueError('订单编号必须唯一且有效')
        return sorted(value)


class Execute(Preview):
    reviewed_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    operation_key: str = Field(pattern=r'^[A-Za-z0-9_-]{8,64}$')
    trial_confirmed: bool = False
    reason: str = Field(default='管理员订单关联处理', min_length=1, max_length=300)


@router.post('/admin-disposition/preview')
def preview(payload: Preview, db: Session = Depends(get_db), user: User = Depends(can_dispose)):
    return service.preview(db, user=user, **payload.model_dump())


@router.post('/admin-disposition/execute')
def execute(payload: Execute, db: Session = Depends(get_db), user: User = Depends(can_dispose)):
    try:
        result = service.execute(db, user=user, **payload.model_dump())
        db.commit()
        return result
    except HTTPException:
        db.rollback()
        raise
    except ExternalPackagingPurchaseLifecycleError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        db.rollback()
        # Keep domain explanations, never expose SQL or a traceback to a user.
        if hasattr(exc, 'status_code'):
            raise HTTPException(exc.status_code, str(exc)) from exc
        raise
