import hashlib
import json
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.api.deps import get_db, PermissionChecker
from app.api.warehouse import _require_lot_customer_access, _visible_customer_ids, can_read, admin_only
from app.models.user import User
from app.models.audit import OperationLog
from app.models.warehouse_inventory import InventoryLot
from app.models.material_candidate import MaterialCandidateSelection
from app.services.material_candidates import candidate_items, candidate_response

router = APIRouter()


class CandidatePayload(BaseModel):
    product_ids: list[int] = Field(max_length=500)
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=100)


@router.get("/{lot_id}/material-candidates")
def get_candidates(lot_id: int, db: Session = Depends(get_db), user: User = Depends(can_read)):
    lot = _require_lot_customer_access(db, lot_id, user)
    return candidate_response(db, lot, _visible_customer_ids(user, db))


@router.put("/{lot_id}/material-candidates", dependencies=[Depends(PermissionChecker("warehouse.correct"))])
def save_candidates(lot_id: int, payload: CandidatePayload, db: Session = Depends(get_db), user: User = Depends(admin_only)):
    lot = _require_lot_customer_access(db, lot_id, user)
    ids = sorted(set(payload.product_ids))
    request_hash = hashlib.sha256(json.dumps([lot_id, user.id, payload.expected_version, ids]).encode()).hexdigest()
    previous = db.scalar(select(MaterialCandidateSelection).where(MaterialCandidateSelection.idempotency_key == payload.idempotency_key))
    if previous:
        if previous.request_hash != request_hash:
            raise HTTPException(409, "重复请求内容不同，请刷新重试")
        return candidate_response(db, lot, _visible_customer_ids(user, db))
    scope = _visible_customer_ids(user, db)
    items = candidate_items(db, lot, scope)
    eligible = {item["product_id"]: item for item in items if item["selectable"]}
    if any(product_id not in eligible for product_id in ids):
        raise HTTPException(422, "仅可保存当前匹配度超过70%的有效产品，请刷新匹配结果")
    # Preserve any inaccessible saved uses when a scoped administrator edits visible uses.
    latest = db.scalar(select(MaterialCandidateSelection).where(MaterialCandidateSelection.lot_id == lot_id)
        .order_by(MaterialCandidateSelection.id.desc()).limit(1))
    hidden = [item for item in json.loads(latest.candidates_json) if scope is not None and item["customer_id"] not in scope] if latest else []
    selected = hidden + [eligible[product_id] for product_id in ids]
    try:
        changed = db.execute(update(InventoryLot).where(InventoryLot.id == lot_id,
            InventoryLot.version == payload.expected_version, InventoryLot.status == "active",
            InventoryLot.quantity_available > 0).values(version=InventoryLot.version + 1),
            execution_options={"synchronize_session": False})
        if changed.rowcount != 1:
            raise HTTPException(409, "批次已变化或不可用，请刷新后重新匹配")
        snapshot = json.dumps(selected, ensure_ascii=False)
        db.add(MaterialCandidateSelection(lot_id=lot_id, lot_version=payload.expected_version + 1,
            idempotency_key=payload.idempotency_key, request_hash=request_hash,
            candidates_json=snapshot, created_by=user.id))
        db.add(OperationLog(user_id=user.id, username=user.username, role=user.role, action="UPDATE",
            resource=f"warehouse/lots/{lot_id}/material-candidates", entity_type="material_candidate_selection",
            entity_id=lot_id, description="保存材料批次候选用途（不预占或领料）", details=snapshot))
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(409, "批次候选用途已变化，请刷新后重试") from error
    except Exception:
        db.rollback()
        raise
    db.refresh(lot)
    return candidate_response(db, lot, scope)
