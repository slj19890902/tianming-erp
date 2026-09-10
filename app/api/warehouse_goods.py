import hashlib
import json
from datetime import date
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.api.deps import get_db, PermissionChecker
from app.api.warehouse import _require_lot_customer_access, _visible_customer_ids, can_read, admin_only
from app.models.user import User
from app.models.customer import Customer
from app.models.product import Product
from app.models.material import Material
from app.models.mold_tool import MoldTool
from app.models.audit import OperationLog
from app.models.warehouse_inventory import InventoryLot, InventoryMovement
from app.models.warehouse_goods import WarehouseGoodsProfile, WarehouseGoodsMutation
from app.services.warehouse_goods import goods_profile, lot_face
from app.services.warehouse_inventory import manual_semi_finished_in, WarehouseInventoryError
from app.services.warehouse_stocktake_batch import (
    _supported_formal_location, _location_live_lots, _is_dispatch_location,
    assert_location_add_compatible, WarehouseStocktakeBatchError,
)

router = APIRouter()


class GoodsFacts(BaseModel):
    scope: Literal["general", "customers"] = "general"
    customer_ids: list[int] = Field(default_factory=list, max_length=500)
    product_ids: list[int] = Field(default_factory=list, max_length=500)
    material_confidence: Literal["unknown", "estimated", "confirmed"] = "unknown"
    estimated_material: str = Field(default="", max_length=200)
    verified_material_id: int | None = Field(default=None, ge=1)
    face_paper: Literal["kraft", "white", "unknown"] = "kraft"
    processing: Literal["raw", "cut", "die_cut", "creased", "printed"] = "raw"
    mold_tool_id: int | None = Field(default=None, ge=1)
    allow_material_substitution: bool = False
    usage_confirmed: bool = False
    note: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def check_facts(self):
        self.customer_ids = sorted(set(self.customer_ids))
        self.product_ids = sorted(set(self.product_ids))
        if self.scope == "customers" and not self.customer_ids:
            raise ValueError("指定客户时至少选择一家客户")
        if self.scope == "general" and self.customer_ids:
            raise ValueError("通用库存不能同时保留指定客户，请先清空客户选择")
        if self.material_confidence != "confirmed" and self.verified_material_id:
            raise ValueError("估计材质请填在估计说明中，不能登记为已确认材质")
        if self.usage_confirmed and (self.material_confidence != "confirmed" or self.face_paper == "unknown"):
            raise ValueError("确认生产用途前请先核实材质和面纸颜色")
        if self.usage_confirmed and self.processing in {"die_cut", "printed", "creased"} and not self.product_ids:
            raise ValueError("加工片料请逐款勾选已确认的可用产品")
        return self


class GoodsUpdate(BaseModel):
    facts: GoodsFacts
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=80)


class SheetEntry(BaseModel):
    facts: GoodsFacts
    location_id: int = Field(ge=1)
    expected_layout_version: int = Field(ge=1)
    quantity: int = Field(gt=0, le=10000000)
    stock_date: date
    internal_name: str = Field(min_length=1, max_length=200)
    board_length_mm: int = Field(gt=0, le=20000)
    board_width_mm: int = Field(gt=0, le=20000)
    layer_count: Literal[3, 5, 7]
    flute_type: Literal["A", "B", "E", "AB", "BE", "AAA", "ABC"]
    pieces_per_box: int = Field(default=1, gt=0, le=100)
    stock_yield_per_sheet: int = Field(default=1, gt=0, le=100)
    component_type: Literal["whole", "cover", "base"] = "whole"
    crease_type: str | None = Field(default=None, max_length=30)
    crease_left_mm: int | None = Field(default=None, ge=0)
    crease_middle_mm: int | None = Field(default=None, ge=0)
    crease_right_mm: int | None = Field(default=None, ge=0)
    source_kind: Literal["existing_stocktake", "partner_transfer"] = "existing_stocktake"
    idempotency_key: str = Field(min_length=8, max_length=80)


def unrestricted(user, db):
    if _visible_customer_ids(user, db) is not None:
        raise HTTPException(403, "维护多客户及通用货物需要完整客户访问权限")


def validate_references(db, facts):
    if facts.customer_ids:
        ids = set(db.scalars(select(Customer.id).where(Customer.id.in_(facts.customer_ids), Customer.status == "active")))
        if ids != set(facts.customer_ids):
            raise HTTPException(422, "适用客户不存在或已停用")
    products = list(db.scalars(select(Product).where(Product.id.in_(facts.product_ids), Product.is_active.is_(True), Product.deleted_at.is_(None))))
    if len(products) != len(facts.product_ids):
        raise HTTPException(422, "适用产品不存在或已停用")
    if facts.scope == "customers" and any(p.customer_id not in facts.customer_ids for p in products):
        raise HTTPException(422, "已选产品不属于适用客户，请调整客户或产品选择")
    if facts.mold_tool_id:
        mold = db.get(MoldTool, facts.mold_tool_id)
        if not mold or not mold.is_active or mold.archive_status != "active":
            raise HTTPException(422, "模具不存在或不可用")
        if any(p.mold_tool_id != mold.id for p in products):
            raise HTTPException(422, "已选产品的模具不一致")
    material = db.get(Material, facts.verified_material_id) if facts.verified_material_id else None
    if facts.verified_material_id and (not material or not material.is_active):
        raise HTTPException(422, "材质不存在或已停用")
    if material and facts.face_paper != ("white" if material.is_white_face else "kraft"):
        raise HTTPException(422, "面纸颜色与所选供应商材质不一致")
    if facts.processing == "raw" and facts.allow_material_substitution:
        raise HTTPException(422, "原材料不能开启材质替代")
    return material


def request_hash(user, action, payload):
    return hashlib.sha256(json.dumps([user.id, action, payload.model_dump(mode="json")], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def replay(db, key, digest):
    row = db.scalar(select(WarehouseGoodsMutation).where(WarehouseGoodsMutation.idempotency_key == key))
    if row:
        if row.request_hash != digest:
            raise HTTPException(409, "重复请求内容不同，请刷新核对")
        return json.loads(row.response_json)


def record(db, user, lot, facts, key, digest, before, action):
    data = facts.model_dump()
    row = db.get(WarehouseGoodsProfile, lot.id)
    if row is None:
        row = WarehouseGoodsProfile(lot_id=lot.id)
        db.add(row)
    row.data_json = json.dumps(data, ensure_ascii=False)
    result = dict(lot_id=lot.id, version=lot.version, facts=data)
    db.add(WarehouseGoodsMutation(idempotency_key=key, request_hash=digest, lot_id=lot.id,
        response_json=json.dumps(result, ensure_ascii=False)))
    db.add(OperationLog(user_id=user.id, username=user.username, role=user.role, action=action,
        resource=f"warehouse/goods/{lot.id}", entity_type="warehouse_goods_profile", entity_id=lot.id,
        description="人工登记货物及适用范围" if action == "CREATE" else "人工修改货物适用资料（不改变数量及历史材质）",
        details=json.dumps(dict(before=before, after=data), ensure_ascii=False)))
    db.commit()
    return result


@router.get("/options")
def options(q: str = "", db: Session = Depends(get_db), user: User = Depends(can_read)):
    scope = _visible_customer_ids(user, db)
    customers = select(Customer).where(Customer.status == "active")
    products = select(Product).where(Product.is_active.is_(True), Product.deleted_at.is_(None))
    if scope is not None:
        customers = customers.where(Customer.id.in_(scope))
        products = products.where(Product.customer_id.in_(scope))
    # The picker searches in the browser; complete authorized options avoid a silent 50-row cutoff.
    return dict(customers=[dict(id=c.id, name=c.chinese_short_name or c.name) for c in db.scalars(customers.order_by(Customer.id))],
        products=[dict(id=p.id, customer_id=p.customer_id, name=p.product_name, code=p.product_code,
            mold_tool_id=p.mold_tool_id) for p in db.scalars(products.order_by(Product.id))],
        materials=[dict(id=m.id, code=m.code, supplier=m.supplier_name, layer_count=m.layer_count,
            is_white_face=m.is_white_face) for m in db.scalars(select(Material).where(Material.is_active.is_(True)).order_by(Material.supplier_name, Material.code))],
        molds=[dict(id=m.id, name=m.mold_code) for m in db.scalars(select(MoldTool).where(MoldTool.is_active.is_(True), MoldTool.archive_status == "active"))])


@router.get("/{lot_id}")
def get_goods(lot_id: int, db: Session = Depends(get_db), user: User = Depends(can_read)):
    lot = _require_lot_customer_access(db, lot_id, user)
    if not lot.semi_finished_detail:
        raise HTTPException(422, "成品仍按其客户存货编码管理")
    profile = goods_profile(db, lot)
    detail = lot.semi_finished_detail
    if profile is None:
        profile = GoodsFacts(scope="customers" if detail.owner_customer_id else "general",
            customer_ids=[detail.owner_customer_id] if detail.owner_customer_id else [],
            processing="raw" if detail.sheet_type == "raw_board" else "creased" if detail.sheet_type == "creased_sheet" else "cut",
            face_paper=lot_face(db, lot), estimated_material=detail.material_code_snapshot or "").model_dump()
    return dict(lot_id=lot.id, version=lot.version, facts=profile,
        physical=dict(length=detail.board_length_mm, width=detail.board_width_mm, flute=detail.flute_type,
            material=detail.material_code_snapshot, name=detail.internal_name),
        editable=lot.status == "active" and lot.quantity_reserved == 0 and lot.quantity_available > 0)


@router.put("/{lot_id}", dependencies=[Depends(PermissionChecker("warehouse.correct"))])
def update_goods(lot_id: int, payload: GoodsUpdate, db: Session = Depends(get_db), user: User = Depends(admin_only)):
    unrestricted(user, db)
    lot = _require_lot_customer_access(db, lot_id, user)
    digest = request_hash(user, f"update:{lot_id}", payload)
    previous = replay(db, payload.idempotency_key, digest)
    if previous is not None:
        return previous
    if not lot.semi_finished_detail:
        raise HTTPException(422, "成品不能改为通用片料")
    facts = payload.facts
    material = validate_references(db, facts)
    raw = lot.semi_finished_detail.sheet_type == "raw_board"
    if raw != (facts.processing == "raw"):
        raise HTTPException(422, "不能通过用途编辑改变原材料或半成品的入库类型")
    if (facts.processing == "creased") != (lot.semi_finished_detail.sheet_type == "creased_sheet"):
        raise HTTPException(422, "压线状态应按原始入库事实维护，不能通过用途编辑新增或取消压线")
    if material and material.layer_count and material.layer_count != lot.semi_finished_detail.layer_count:
        raise HTTPException(422, "核实材质的层数与原始库存不一致，请先核对")
    if facts.material_confidence == "confirmed" and not facts.verified_material_id:
        raise HTTPException(422, "确认材质请选定供应商材质代码；原始入库材质仍保留")
    before = goods_profile(db, lot)
    try:
        changed = db.execute(update(InventoryLot).where(InventoryLot.id == lot_id,
            InventoryLot.version == payload.expected_version, InventoryLot.status == "active",
            InventoryLot.quantity_reserved == 0, InventoryLot.quantity_available > 0).values(version=InventoryLot.version + 1),
            execution_options={"synchronize_session": False})
        if changed.rowcount != 1:
            raise HTTPException(409, "批次已变化、已预占或不可用，请刷新后核对")
        db.refresh(lot)
        return record(db, user, lot, facts, payload.idempotency_key, digest, before, "UPDATE")
    except Exception:
        db.rollback()
        raise


@router.post("/sheet-entry", dependencies=[Depends(PermissionChecker("warehouse.correct"))])
def create_sheet(payload: SheetEntry, db: Session = Depends(get_db), user: User = Depends(admin_only)):
    unrestricted(user, db)
    digest = request_hash(user, "entry", payload)
    previous = replay(db, payload.idempotency_key, digest)
    if previous is not None:
        return previous
    facts = payload.facts
    movement_key = "goods:" + payload.idempotency_key
    if db.scalar(select(InventoryMovement.id).where(InventoryMovement.idempotency_key == movement_key)):
        raise HTTPException(409, "入库流水已存在但用途回执不完整，请刷新核对，不重复入库")
    material = validate_references(db, facts)
    if facts.material_confidence == "confirmed" and material is None:
        raise HTTPException(422, "确认材质请选定供应商材质代码")
    if material and material.layer_count and material.layer_count != payload.layer_count:
        raise HTTPException(422, "层数与材质主数据不一致")
    try:
        occupied = bool(_location_live_lots(db, payload.location_id))
        location = _supported_formal_location(db, location_id=payload.location_id,
            inventory_type="raw_material" if facts.processing == "raw" else "semi_finished",
            capacity_source_location_id=payload.location_id if occupied else None)
        if _is_dispatch_location(area_code=location.area_code, location_code=location.location_code):
            raise HTTPException(409, "待送区不能新增片料库存")
        assert_location_add_compatible(db, payload.location_id)
        if facts.processing == "creased" and (not payload.crease_type or any(v is None for v in
                [payload.crease_left_mm, payload.crease_middle_mm, payload.crease_right_mm])):
            raise HTTPException(422, "压线片料请完整登记压线类型及三段尺寸")
        lot = manual_semi_finished_in(db, location_id=payload.location_id, quantity=payload.quantity,
            stock_date=payload.stock_date, source_type="stocktake" if payload.source_kind == "existing_stocktake" else "transfer",
            material_code=material.code if material else "未知", material_id=material.id if material else None,
            material_is_unknown=material is None,
            supplier_name=material.supplier_name if material else None, layer_count=payload.layer_count,
            flute_type=payload.flute_type, board_length_mm=payload.board_length_mm, board_width_mm=payload.board_width_mm,
            sheet_type="raw_board" if facts.processing == "raw" else "creased_sheet" if facts.processing == "creased" else "net_sheet",
            component_type=payload.component_type, pieces_per_box=payload.pieces_per_box,
            stock_yield_per_sheet=payload.stock_yield_per_sheet, internal_name=payload.internal_name,
            customer_id=None, operator_id=user.id, idempotency_key=movement_key,
            expected_layout_version=payload.expected_layout_version,
            capacity_source_location_id=payload.location_id if occupied else None,
            crease_type=payload.crease_type, crease_left_mm=payload.crease_left_mm,
            crease_middle_mm=payload.crease_middle_mm, crease_right_mm=payload.crease_right_mm,
            movement_reason="人工补录原材料" if facts.processing == "raw" else "人工补录半成品",
            remarks=facts.note, cutting_note=None)
        return record(db, user, lot, facts, payload.idempotency_key, digest, None, "CREATE")
    except (WarehouseInventoryError, WarehouseStocktakeBatchError) as error:
        db.rollback()
        raise HTTPException(error.status_code, str(error)) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(409, "请求已处理或库存已变化，请刷新核对") from error
    except Exception:
        db.rollback()
        raise
