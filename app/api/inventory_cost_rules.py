from uuid import uuid4
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from app.api.deps import get_db, get_current_user, require_customer_access, PermissionChecker
from app.models.product import Product
from app.models.material import Material
from app.models.user import User
from app.services.inventory_valuation import can_view_inventory_cost
from app.services import inventory_cost_rules as service

router = APIRouter(prefix="/cost-rules")


def cost_reader(response: Response, user: User = Depends(get_current_user)):
    if not can_view_inventory_cost(user):
        raise HTTPException(403, "仅管理员和老板可查看成本")
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    return user


def cost_writer(user: User = Depends(cost_reader), _: User = Depends(PermissionChecker("warehouse.correct"))):
    if user.role != "admin":
        raise HTTPException(403, "仅管理员可维护成本依据")
    return user


def product_for(db, user, product_id):
    product = db.get(Product, product_id)
    if not product or product.deleted_at is not None:
        raise HTTPException(404, "产品不存在")
    require_customer_access(product.customer_id, user, db)
    return product


class RuleSave(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    expected_product_version: int = Field(gt=0)
    config: service.CostRuleConfig


class Lots(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lot_ids: list[int] = Field(min_length=1, max_length=1000)


class Adopt(Lots):
    expected: str = Field(pattern=r"^[0-9a-f]{64}$")
    batch_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=64)


@router.get("/materials")
def materials(db: Session = Depends(get_db), user=Depends(cost_reader)):
    return [dict(id=m.id, code=m.code, supplier_name=m.supplier_name,
                 layer_count=m.layer_count, flute_type=m.flute_type)
        for m in db.scalars(select(Material).where(Material.is_active.is_(True), Material.purchase_currency == "CNY").order_by(Material.code, Material.id))]


@router.get("/{product_id}/entry-preview")
def entry_preview(product_id: int, stock_stage: Literal["complete", "body"] = "complete",
                  db: Session = Depends(get_db), user=Depends(cost_reader)):
    from app.services.inventory_valuation import resolve_product_cost
    product = product_for(db, user, product_id)
    result = resolve_product_cost(db, product, stock_stage=stock_stage, for_entry=True)
    return dict(product_id=product.id, product_version=product.version,
        ready=bool(result.estimate), display_unit=product.unit,
        repair_product_id=product.id, repair_material_id=product.material_id,
        unit_cost=result.estimate.unit_cost if result.estimate else None,
        evidence=result.estimate.detail if result.estimate else None, missing=result.missing,
        note="当前资料计算；保存入库时重新核对并冻结本批成本，不改已入库批次")


@router.get("/{product_id}")
def get_rule(product_id: int, db: Session = Depends(get_db), user=Depends(cost_reader)):
    return service.rule_payload(db, product_for(db, user, product_id))


@router.put("/{product_id}")
def put_rule(product_id: int, payload: RuleSave, db: Session = Depends(get_db), user=Depends(cost_writer)):
    product = product_for(db, user, product_id)
    try:
        result = service.save_rule(db, product, payload.config.model_dump(mode="json"), user=user,
            expected_version=payload.expected_version, expected_product_version=payload.expected_product_version)
        db.commit()
        return result
    except (ValueError, IntegrityError, OperationalError) as error:
        db.rollback()
        raise HTTPException(409, str(error) if isinstance(error, ValueError) else "数据已变化，请刷新重试") from error


@router.post("/{product_id}/preview")
def preview(product_id: int, payload: Lots, db: Session = Depends(get_db), user=Depends(cost_writer)):
    product = product_for(db, user, product_id)
    try:
        return service.preview_revalue(db, product, payload.lot_ids)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.post("/{product_id}/apply")
def apply(product_id: int, payload: Adopt, db: Session = Depends(get_db), user=Depends(cost_writer)):
    product = product_for(db, user, product_id)
    try:
        result = service.revalue(db, product, payload.lot_ids, user=user, expected=payload.expected, batch_id=payload.batch_id)
        db.commit()
        return result
    except (ValueError, IntegrityError, OperationalError) as error:
        db.rollback()
        raise HTTPException(409, str(error) if isinstance(error, ValueError) else "库存已变化，请重新预览") from error
