"""Candidate read-only route; deliberately not installed in the production app."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.api.deps import get_current_user, get_db
from app.models.user import User
from app.services.historical_closed_batch_preview import ClosedBatchRequest, preview

router = APIRouter()


@router.post("/preview")
def preview_closed_history(payload: ClosedBatchRequest,
                           db: Session = Depends(get_db),
                           user: User = Depends(get_current_user)):
    try:
        return preview(db, actor=user, request=payload)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
