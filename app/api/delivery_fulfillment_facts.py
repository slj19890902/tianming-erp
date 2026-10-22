"""Candidate router requiring the reviewed migration and reader compatibility."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.user import User
from app.services.delivery_fulfillment_facts import (
    FulfillmentError, FulfillmentRequest, FulfillmentSubmitRequest,
    append_delivery_fulfillment, preview_delivery_fulfillment,
)

router = APIRouter()


@router.post("/{delivery_id}/fulfillment-facts/preview")
def preview(delivery_id: int, payload: FulfillmentRequest,
            db: Session = Depends(get_db), actor: User = Depends(get_current_user)):
    try:
        return preview_delivery_fulfillment(db, actor, delivery_id, payload)
    except FulfillmentError as error:
        raise HTTPException(error.status_code, dict(code=error.code, message=str(error))) from error


@router.post("/{delivery_id}/fulfillment-facts", status_code=201)
def append(delivery_id: int, payload: FulfillmentSubmitRequest,
           db: Session = Depends(get_db), actor: User = Depends(get_current_user)):
    try:
        response = append_delivery_fulfillment(db, actor, delivery_id, payload)
        db.commit()
        return response
    except FulfillmentError as error:
        db.rollback()
        raise HTTPException(error.status_code, dict(code=error.code, message=str(error))) from error
    except Exception:
        db.rollback()
        raise
