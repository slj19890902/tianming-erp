import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, update, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.api.deps import get_db, PermissionChecker
from app.api.warehouse import _require_lot_customer_access, _visible_customer_ids, can_read, admin_only
from app.models.user import User
from app.models.customer import Customer
from app.models.product import Product
from app.models.material import Material
from app.models.supplier import Supplier
from app.models.mold_tool import MoldTool
from app.models.audit import OperationLog
from app.models.warehouse_inventory import InventoryLot, InventoryMovement
from app.models.warehouse_goods import WarehouseGoodsProfile, WarehouseGoodsMutation
from app.services.warehouse_goods import goods_profile, lot_face
from app.services.paper_color import material_face
from app.services.box_type_rules import get_box_type_rule
from app.services.inventory_cost_snapshot import InventoryCostEstimate, estimate_semi_finished_cost, apply_cost_snapshot
from app.services.inventory_valuation import can_view_inventory_cost, freeze_entry_cost
from app.services.material_pricing import get_effective_material_price
from app.services.warehouse_inventory import manual_semi_finished_in, WarehouseInventoryError
from app.services.warehouse_stocktake_batch import (
    _supported_formal_location, _location_live_lots, _is_dispatch_location,
    assert_location_add_compatible, WarehouseStocktakeBatchError,
)

router = APIRouter()


class GoodsFacts(BaseModel):
    display_name: str = Field(default="", max_length=200)
    scope: Literal["general", "customers"] = "general"
    customer_ids: list[int] = Field(default_factory=list, max_length=500)
    product_ids: list[int] = Field(default_factory=list, max_length=500)
    material_confidence: Literal["unknown", "estimated", "confirmed"] = "unknown"
    estimated_material: str = Field(default="", max_length=200)
    verified_material_id: int | None = Field(default=None, ge=1)
    material_code: str = Field(default="", max_length=100)
    face_paper: Literal["kraft", "white", "unknown"] = "kraft"
    processing: Literal["raw", "cut", "die_cut", "creased", "printed", "dedicated_component"] = "raw"
    mold_tool_id: int | None = Field(default=None, ge=1)
    mold_version: int | None = Field(default=None, ge=1)
    blank_unprinted: bool = False
    dimension_source: Literal["unknown", "tape", "label"] = "unknown"
    crease_product_id: int | None = Field(default=None, ge=1)
    cut_trim_mm: float = Field(default=0, ge=0, le=500, allow_inf_nan=False)
    cut_kerf_mm: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)
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
        return self


class GoodsUpdate(BaseModel):
    facts: GoodsFacts
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=80)
    correction_reason: str = Field(default="", max_length=500)
    sync_product_id: int | None = Field(default=None, ge=1)
    impact_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)
    correction_quantity: int | None = Field(default=None, gt=0)


class SheetEntry(BaseModel):
    facts: GoodsFacts
    location_id: int = Field(ge=1)
    expected_layout_version: int = Field(ge=1)
    quantity: int = Field(gt=0, le=10000000)
    stock_date: date
    internal_name: str = Field(min_length=1, max_length=200)
    board_length_mm: int = Field(gt=0, le=20000)
    board_width_mm: int = Field(gt=0, le=20000)
    layer_count: Literal[1, 3, 5, 7]
    flute_type: Literal["NONE", "A", "B", "E", "AB", "BE", "AAA", "ABC"]
    supplier_id: int | None = Field(default=None, ge=1)
    sheet_unit_cost: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=4)
    pieces_per_box: int = Field(default=1, gt=0, le=100)
    stock_yield_per_sheet: int = Field(default=1, gt=0, le=100)
    component_type: Literal["whole", "cover", "base"] = "whole"
    crease_type: str | None = Field(default=None, max_length=30)
    crease_left_mm: int | None = Field(default=None, ge=0)
    crease_middle_mm: int | None = Field(default=None, ge=0)
    crease_right_mm: int | None = Field(default=None, ge=0)
    source_kind: Literal["existing_stocktake", "partner_transfer"] = "existing_stocktake"
    idempotency_key: str = Field(min_length=8, max_length=80)

    @model_validator(mode="after")
    def single_layer_entry(self):
        if self.facts.processing == "creased":
            self.crease_type = "压线"
            values = [self.crease_left_mm,self.crease_middle_mm,self.crease_right_mm]
            if any(v is None or v <= 0 for v in values):
                raise ValueError("请从常用箱取用压线，并填写三段实际尺寸")
            tolerance = 0 if self.facts.dimension_source == "label" else 10
            if abs(sum(values)-self.board_width_mm) > tolerance:
                raise ValueError(f"三段压线合计{sum(values)}mm与纸板宽{self.board_width_mm}mm不符；允许测量差{tolerance}mm")
        elif any(v is not None for v in [self.crease_left_mm,self.crease_middle_mm,self.crease_right_mm]):
            raise ValueError("存在压线尺寸，请选择已压线，不可登记为未压线净片")
        if self.layer_count == 1:
            if self.flute_type != "NONE":
                raise ValueError("单层原纸请选择无楞")
            if self.facts.verified_material_id:
                if self.supplier_id is not None or self.sheet_unit_cost is not None:
                    raise ValueError("请选择供应商平方报价或实际按张单价之一，不能混用")
            elif not self.supplier_id or self.sheet_unit_cost is None:
                raise ValueError("单层原纸请选择已报价供应商材质，或填写实际供应商及含税每张单价")
        elif self.flute_type == "NONE" or self.supplier_id is not None or self.sheet_unit_cost is not None:
            raise ValueError("按张原纸报价仅适用于单层无楞原纸")
        return self


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
        facts.mold_version = mold.version
    else:
        facts.mold_version = None
    if facts.processing != "die_cut":
        facts.blank_unprinted = False
    if facts.crease_product_id:
        template=db.get(Product,facts.crease_product_id)
        rule=get_box_type_rule(template.box_style) if template else None
        if (not template or template.deleted_at or not template.is_active or not rule or rule.code != "a1_0201"
                or any(not getattr(template,f"crease_{part}_mm") for part in ("left","middle","right"))):
            raise HTTPException(422,"压线模板请选择三段压线完整的常用箱A1产品")
        if facts.scope == "customers" and template.customer_id not in facts.customer_ids:
            raise HTTPException(422,"压线模板不属于所选客户")
    if facts.processing != "creased":
        facts.crease_product_id=None
    material = db.get(Material, facts.verified_material_id) if facts.verified_material_id else None
    if facts.verified_material_id and (not material or not material.is_active):
        raise HTTPException(422, "材质不存在或已停用")
    # Retain legacy JSON fields for old clients; eligibility no longer depends on approval flags.
    facts.material_code = material.code if material else facts.material_code.strip()
    facts.face_paper = material_face(db, material)
    facts.material_confidence = "confirmed"
    facts.usage_confirmed = True
    facts.allow_material_substitution = facts.processing != "raw"
    return material


def request_hash(user, action, payload):
    return hashlib.sha256(json.dumps([user.id, action, payload.model_dump(mode="json")], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def replay(db, key, digest):
    row = db.scalar(select(WarehouseGoodsMutation).where(WarehouseGoodsMutation.idempotency_key == key))
    if row:
        if row.request_hash != digest:
            raise HTTPException(409, "重复请求内容不同，请刷新核对")
        return json.loads(row.response_json)


def record(db, user, lot, facts, key, digest, before, action, *, reason=""):
    # Applicability edits must not erase server-frozen production provenance.
    data = {**(before or {}), **facts.model_dump()}
    if "source_customer_id" not in data:
        detail = lot.semi_finished_detail or lot.finished_detail
        data["source_customer_id"] = detail.owner_customer_id
        data["source_customer_name"] = detail.owner_customer_name_snapshot
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
        details=json.dumps(dict(before=before, after=data, reason=reason), ensure_ascii=False)))
    db.commit()
    return result


def correction_impact(db, user, lot, payload):
    from app.models.order import OrderItem
    product = db.get(Product, payload.sync_product_id) if payload.sync_product_id else None
    if payload.sync_product_id and (product is None or product.deleted_at or not product.is_active
            or product.id not in payload.facts.product_ids):
        raise HTTPException(422, "同步常用箱必须选择一个已确认适用且有效的产品")
    if product and not payload.facts.display_name.strip():
        raise HTTPException(422, "同步常用箱请填写修正后的产品名称")
    detail = lot.semi_finished_detail or lot.finished_detail
    impact = dict(lot_id=lot.id, lot_version=lot.version, original_customer_id=detail.owner_customer_id,
        product_id=product.id if product else None, product_version=product.version if product else None,
        old_product_name=product.product_name if product else None, new_product_name=payload.facts.display_name.strip(),
        historical_order_lines=int(db.scalar(select(func.count()).select_from(OrderItem).where(OrderItem.product_id==product.id)) or 0) if product else 0,
        history_unchanged=True)
    raw = json.dumps([impact, payload.model_dump(mode="json", exclude={"impact_fingerprint", "idempotency_key"})],sort_keys=True,ensure_ascii=False)
    return {**impact, "fingerprint": hashlib.sha256(raw.encode()).hexdigest()}


@router.post("/{lot_id}/correction-preview", dependencies=[Depends(PermissionChecker("warehouse.correct"))])
def preview_correction(lot_id: int, payload: GoodsUpdate, db: Session=Depends(get_db), user: User=Depends(admin_only)):
    unrestricted(user, db)
    lot = _require_lot_customer_access(db, lot_id, user)
    return correction_impact(db, user, lot, payload)


@router.get("/options")
def options(q: str = "", db: Session = Depends(get_db), user: User = Depends(can_read)):
    scope = _visible_customer_ids(user, db)
    customers = select(Customer).where(Customer.status == "active")
    products = select(Product).where(Product.is_active.is_(True), Product.deleted_at.is_(None))
    if scope is not None:
        customers = customers.where(Customer.id.in_(scope))
        products = products.where(Product.customer_id.in_(scope))
    # The picker searches in the browser; complete authorized options avoid a silent 50-row cutoff.
    return dict(actor_id=user.id, customers=[dict(id=c.id, name=c.chinese_short_name or c.name, full_name=c.name, code=c.customer_code) for c in db.scalars(customers.order_by(Customer.id))],
        products=[dict(id=p.id, customer_id=p.customer_id, name=p.product_name, code=p.product_code,
            mold_tool_id=p.mold_tool_id, box_style=p.box_style,
            is_liner=bool((rule:=get_box_type_rule(p.box_style)) and rule.code=="liner"),
            is_a1=bool(rule and rule.code=="a1_0201"),
            crease_values=[p.crease_left_mm,p.crease_middle_mm,p.crease_right_mm],
            report_width_mm=p.report_width_mm,flute_type=p.flute_type) for p in db.scalars(products.order_by(Product.id))],
        suppliers=[dict(id=s.id, name=s.standard_name) for s in db.scalars(select(Supplier).where(Supplier.is_active.is_(True)).order_by(Supplier.sort_order, Supplier.standard_name))],
        materials=[dict(id=m.id, code=m.code, supplier=m.supplier_name, layer_count=m.layer_count,
            is_white_face=material_face(db, m) == "white") for m in db.scalars(select(Material).where(Material.is_active.is_(True)).order_by(Material.supplier_name, Material.code))],
        molds=[dict(id=m.id, name=" · ".join(dict.fromkeys(filter(None, [m.mold_code, m.mold_name]))),
            search_text=" ".join(filter(None, [m.mold_code, m.mold_name, m.label_name, m.chinese_short_name]))) for m in db.scalars(select(MoldTool).where(MoldTool.is_active.is_(True), MoldTool.archive_status == "active"))])


@router.get("/material-price")
def material_price(material_id: int, flute_type: str = "", length_mm: int = 0, width_mm: int = 0,
                   quantity: int = 0, db: Session = Depends(get_db), user: User = Depends(can_read)):
    if not can_view_inventory_cost(user):
        raise HTTPException(403, "仅管理员和老板可以查看成本")
    material = db.get(Material, material_id)
    if material is None or not material.is_active:
        raise HTTPException(422, "材质不存在或已停用")
    result = get_effective_material_price(db, material=material, supplier_name=material.supplier_name,
        layer_count=material.layer_count, flute_type=flute_type or None)
    estimate = estimate_semi_finished_cost(db, material_id=material.id, material_code=material.code,
        supplier_name=material.supplier_name, layer_count=material.layer_count, flute_type=flute_type,
        board_length_mm=length_mm, board_width_mm=width_mm) if length_mm > 0 and width_mm > 0 else None
    if not result.get('effective_price') or Decimal(str(result['effective_price'])) <= 0:
        raise HTTPException(422, '供应商材质缺有效报价，请点击下方材质/报价维护，保存后返回继续录入')
    return dict(square_price=str(result['effective_price']), unit=material.price_unit,
        currency=material.purchase_currency, tax_included=material.purchase_tax_included,
        face_paper=material_face(db, material), unit_price=str(estimate.unit_cost) if estimate else None,
        total_price=str(estimate.unit_cost * quantity) if estimate and quantity > 0 else None)


@router.get("/{lot_id}")
def get_goods(lot_id: int, db: Session = Depends(get_db), user: User = Depends(can_read)):
    lot = _require_lot_customer_access(db, lot_id, user)
    profile = goods_profile(db, lot)
    if lot.finished_detail:
        detail = lot.finished_detail
        profile = profile or GoodsFacts(scope="general" if detail.is_general else "customers",
            customer_ids=[] if detail.is_general else [detail.owner_customer_id],
            product_ids=[detail.product_id] if detail.product_id else [], processing="cut", display_name=detail.product_name_snapshot or "",
            note=lot.remarks or "").model_dump()
        profile.setdefault("source_customer_id", detail.owner_customer_id)
        profile.setdefault("source_customer_name", detail.owner_customer_name_snapshot)
        return dict(lot_id=lot.id, version=lot.version, facts=profile, inventory_type="finished",
            physical=dict(length=detail.length_mm,width=detail.width_mm,flute=detail.flute_type_snapshot,
                name=profile.get("display_name") or detail.product_name_snapshot,material=detail.material_code_snapshot),
            editable=lot.status=="active" and lot.quantity_reserved==0 and lot.quantity_available>0)
    detail = lot.semi_finished_detail
    if profile is None:
        profile = GoodsFacts(scope="customers" if detail.owner_customer_id else "general",
            customer_ids=[detail.owner_customer_id] if detail.owner_customer_id else [],
            processing="raw" if detail.sheet_type == "raw_board" else "creased" if detail.sheet_type == "creased_sheet" else "cut",
            face_paper=lot_face(db, lot), estimated_material=detail.material_code_snapshot or "",
            display_name=detail.internal_name or "", note=lot.remarks or "").model_dump()
    profile["material_code"] = profile.get("material_code") or detail.material_code_snapshot or ""
    profile.setdefault("source_customer_id", detail.owner_customer_id)
    profile.setdefault("source_customer_name", detail.owner_customer_name_snapshot)
    physical = dict(length=detail.board_length_mm, width=detail.board_width_mm, flute=detail.flute_type,
        material=detail.material_code_snapshot, name=profile.get("display_name") or detail.internal_name)
    if can_view_inventory_cost(user):
        from app.services.inventory_valuation import cost_payload
        physical["settlement_unit_price"] = cost_payload(lot, db)["unit_cost"]
    return dict(lot_id=lot.id, version=lot.version, facts=profile, physical=physical,
        editable=lot.status == "active" and lot.quantity_reserved == 0 and lot.quantity_available > 0)


@router.put("/{lot_id}", dependencies=[Depends(PermissionChecker("warehouse.correct"))])
def update_goods(lot_id: int, payload: GoodsUpdate, db: Session = Depends(get_db), user: User = Depends(admin_only)):
    unrestricted(user, db)
    lot = _require_lot_customer_access(db, lot_id, user)
    digest = request_hash(user, f"update:{lot_id}", payload)
    previous = replay(db, payload.idempotency_key, digest)
    if previous is not None:
        return previous
    if not payload.correction_reason.strip():
        raise HTTPException(422, "请填写资料修正原因")
    from app.services.fixed_shelf_staging import staging_owner
    if staging_owner(db, lot.id):
        raise HTTPException(409, "该批次已集货待送，请先解除集货后修正资料")
    if lot.pallet_item and (not lot.pallet_item.pallet or not lot.pallet_item.pallet.is_current
            or lot.pallet_item.pallet.location_id != lot.warehouse_location_id):
        raise HTTPException(409, "栈板位置与库存不一致，请先核对")
    facts = payload.facts.model_copy(deep=True)
    material = validate_references(db, facts)
    detail = lot.semi_finished_detail or lot.finished_detail
    finished_product = None
    if lot.finished_detail:
        if not payload.correction_reason.strip():
            raise HTTPException(422, "请填写资料修正原因")
        if len(facts.customer_ids)>1 or len(facts.product_ids)>1:
            raise HTTPException(422, "成品批次只可对应一款实际产品和一个专用客户")
        pid = facts.product_ids[0] if facts.product_ids else detail.product_id
        finished_product = db.get(Product, pid)
        if not finished_product or (facts.scope=="customers" and finished_product.customer_id not in facts.customer_ids):
            raise HTTPException(422, "请选择适用客户下实际规格相同的产品")
        if pid != detail.product_id:
            from app.services.finished_stock_identity import product_basis
            from app.services.bom_inventory_contract import is_body_lot
            old_basis = json.loads(detail.physical_basis_json or "null")
            new_basis = json.loads(product_basis(finished_product))
            if detail.product_id is None:
                from app.services.bom_inventory_contract import product_has_assembly
                from app.services.unassigned_finished_entry import claim_matches
                if (not claim_matches(detail, finished_product) or product_has_assembly(db, finished_product.id)
                        or payload.correction_quantity not in (None, lot.quantity_available)
                        or lot.quantity_consumed or lot.quantity_scrapped or lot.quantity_damaged):
                    raise HTTPException(409, "待认领成品须整批核实相同尺寸、箱型、单位和材质后关联；不允许将估算实物冒认为不同产品或组合套装")
            elif is_body_lot(lot) or not old_basis or {k:v for k,v in old_basis.items() if k!="product_id"}!={k:v for k,v in new_basis.items() if k!="product_id"}:
                raise HTTPException(409, "客户产品变更必须有完全相同的冻结实物规格和工艺依据，请先核实身份")
    else:
        raw = detail.sheet_type == "raw_board"
        if detail.layer_count == 1 and material is None:
            facts.face_paper = payload.facts.face_paper
        if raw != (facts.processing == "raw"):
            raise HTTPException(422, "不能通过用途编辑改变原材料或半成品的入库类型")
        if (facts.processing == "creased") != (detail.sheet_type == "creased_sheet"):
            raise HTTPException(422, "压线状态应按原始入库事实维护，不能通过用途编辑新增或取消压线")
        if material and material.layer_count and material.layer_count != detail.layer_count:
            raise HTTPException(422, "核实材质的层数与原始库存不一致，请先核对")
        if not facts.material_code:
            facts.material_code = detail.material_code_snapshot
    before = get_goods(lot_id, db, user)["facts"]
    if (before or {}).get('processing') == 'dedicated_component' and any(
            facts.model_dump().get(key) != before.get(key) for key in ('processing','scope','customer_ids','product_ids')):
        raise HTTPException(409,'专用盖/底的客户、产品及未完工身份不能通过资料修正改为通用片料')
    if facts.processing == 'dedicated_component' and (before or {}).get('processing') != 'dedicated_component':
        raise HTTPException(409,'不能通过资料编辑伪造已压线开槽的专用部件来源')

    before.setdefault("source_customer_id", detail.owner_customer_id)
    before.setdefault("source_customer_name", detail.owner_customer_name_snapshot)
    before["display_name"] = before.get("display_name") or (detail.product_name_snapshot if lot.finished_detail else detail.internal_name) or ""
    before.setdefault("source_display_name", before["display_name"])
    if "display_name" not in payload.facts.model_fields_set:
        facts.display_name = (before or {}).get("display_name", "")
    if facts.display_name != (before or {}).get("display_name", "") and not payload.correction_reason.strip():
        raise HTTPException(422, "请填写资料修正原因")
    try:
        if payload.sync_product_id:
            impact = correction_impact(db, user, lot, payload)
            if not payload.correction_reason.strip() or payload.impact_fingerprint != impact["fingerprint"]:
                raise HTTPException(409, "请预览常用箱同步影响并填写修正原因，资料变化后须重新预览")
            changed_product = db.execute(update(Product).where(Product.id == payload.sync_product_id,
                Product.version == impact["product_version"]).values(product_name=facts.display_name.strip(), version=Product.version+1))
            if changed_product.rowcount != 1:
                raise HTTPException(409, "常用箱已变化，请重新预览")
            db.add(OperationLog(user_id=user.id,username=user.username,role=user.role,action="UPDATE",
                resource=f"products/{payload.sync_product_id}",entity_type="product",entity_id=payload.sync_product_id,
                description="盘点资料修正：管理员明确同步常用箱名称",details=json.dumps(dict(impact=impact,reason=payload.correction_reason),ensure_ascii=False)))
        changed = db.execute(update(InventoryLot).where(InventoryLot.id == lot_id,
            InventoryLot.version == payload.expected_version, InventoryLot.status == "active",
            InventoryLot.quantity_reserved == 0, InventoryLot.quantity_available > 0).values(version=InventoryLot.version + 1),
            execution_options={"synchronize_session": False})
        if changed.rowcount != 1:
            raise HTTPException(409, "批次已变化、已预占或不可用，请刷新后核对")
        db.refresh(lot)
        take = payload.correction_quantity or lot.quantity_available
        if take > lot.quantity_available:
            raise HTTPException(409, "修正数量超过可用库存，请刷新")
        if take < lot.quantity_available or lot.quantity_consumed or lot.quantity_scrapped or lot.quantity_damaged:
            if not payload.correction_reason.strip():
                raise HTTPException(422, "拆分修正请填写原因")
            from app.services.warehouse_goods_correction import split_for_correction
            lot = split_for_correction(db, lot, take, user, payload.idempotency_key)
        if finished_product:
            detail = lot.finished_detail
            claiming = detail.product_id is None
            if detail.product_id != finished_product.id:
                from app.services.finished_stock_identity import product_basis
                detail.physical_basis_json = product_basis(finished_product)
                detail.product_id = finished_product.id
                detail.inventory_code_snapshot = finished_product.product_code
            detail.is_general = facts.scope=="general"
            detail.owner_customer_id = None if detail.is_general else facts.customer_ids[0]
            customer = db.get(Customer, detail.owner_customer_id) if detail.owner_customer_id else None
            detail.owner_customer_name_snapshot = customer.name if customer else None
            detail.product_name_snapshot = facts.display_name or detail.product_name_snapshot
            if lot.pallet_item:
                from app.models.warehouse_inventory import InventoryPallet
                pallet = lot.pallet_item.pallet
                changed_pallet = db.execute(update(InventoryPallet).where(InventoryPallet.id==pallet.id,
                    InventoryPallet.version==pallet.version).values(version=InventoryPallet.version+1,updated_by=user.id))
                if changed_pallet.rowcount != 1:
                    raise HTTPException(409, "栈板已变化，请刷新")
                lot.pallet_item.customer_id=detail.owner_customer_id
                lot.pallet_item.customer_name_snapshot=detail.owner_customer_name_snapshot
                lot.pallet_item.product_id=detail.product_id
                lot.pallet_item.inventory_code=detail.inventory_code_snapshot
                lot.pallet_item.product_name=detail.product_name_snapshot
                if claiming:
                    lot.pallet_item.match_status = 'matched'
                    from app.models.warehouse_inventory import WarehouseGroundOccupancy
                    db.execute(update(WarehouseGroundOccupancy).where(
                        WarehouseGroundOccupancy.pallet_id==pallet.id,
                        WarehouseGroundOccupancy.status=='active',
                        WarehouseGroundOccupancy.product_id.is_(None)).values(
                            product_id=finished_product.id, customer_id=finished_product.customer_id,
                            version=WarehouseGroundOccupancy.version+1))
        elif lot.pallet_item:
            from app.models.warehouse_inventory import InventoryPallet
            pallet = lot.pallet_item.pallet
            if not pallet or not pallet.is_current or pallet.location_id != lot.warehouse_location_id:
                raise HTTPException(409, "栈板位置与库存不一致，请先核对")
            changed_pallet = db.execute(update(InventoryPallet).where(InventoryPallet.id==pallet.id,
                InventoryPallet.version==pallet.version).values(version=InventoryPallet.version+1,updated_by=user.id))
            if changed_pallet.rowcount != 1:
                raise HTTPException(409, "栈板已变化，请刷新")
            lot.pallet_item.product_name=facts.display_name or lot.pallet_item.product_name
        if lot.semi_finished_detail:
            lot.semi_finished_detail.internal_name = facts.display_name or lot.semi_finished_detail.internal_name
        lot.remarks = facts.note
        return record(db, user, lot, facts, payload.idempotency_key, digest, before, "UPDATE", reason=payload.correction_reason.strip())
    except WarehouseInventoryError as error:
        db.rollback()
        raise HTTPException(error.status_code, str(error)) from error
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
    facts = payload.facts.model_copy(deep=True)
    if facts.processing == 'dedicated_component':
        raise HTTPException(422,'专用盖/底请从采购来料的实际余片确认入库，保留冻结来源')
    movement_key = "goods:" + payload.idempotency_key
    if db.scalar(select(InventoryMovement.id).where(InventoryMovement.idempotency_key == movement_key)):
        raise HTTPException(409, "入库流水已存在但用途回执不完整，请刷新核对，不重复入库")
    material = validate_references(db, facts)
    if not facts.material_code:
        raise HTTPException(422, "请输入材质代码，或选择供应商对应材质")
    if material and material.layer_count and material.layer_count != payload.layer_count:
        raise HTTPException(422, "层数与材质主数据不一致")
    supplier = None
    direct_estimate = None
    if payload.layer_count == 1 and payload.sheet_unit_cost is not None:
        supplier = db.get(Supplier, payload.supplier_id)
        if supplier is None or not supplier.is_active:
            raise HTTPException(422, "原纸供应商不存在或已停用，请重新选择")
        facts.face_paper = payload.facts.face_paper
        direct_estimate = InventoryCostEstimate(payload.sheet_unit_cost, Decimal(0),
            Decimal(payload.board_length_mm * payload.board_width_mm) / Decimal(1000000),
            "manual_sheet_unit_cost", dict(currency="CNY", tax_included=True,
                price_unit="元/张", settlement_unit_price=str(payload.sheet_unit_cost),
                supplier_id=supplier.id, supplier_name=supplier.standard_name, supplier_version=supplier.version,
                operator_id=user.id, stock_date=str(payload.stock_date),
                estimate_basis="administrator_entered_sheet_cost", cost_label="原纸入库单价",
                note="管理员登记含税到库每张成本；不生成采购应付"))
    estimate = estimate_semi_finished_cost(db, material_id=material.id, material_code=material.code,
        supplier_name=material.supplier_name, layer_count=payload.layer_count, flute_type=payload.flute_type,
        board_length_mm=payload.board_length_mm, board_width_mm=payload.board_width_mm) if material else None
    if material and estimate is None:
        raise HTTPException(422, "所选供应商材质没有可用的当前平方价，请先在材质维护中保存报价")
    if estimate:
        estimate.detail.update(estimate_basis="manual_selected_material_settlement", material_version=material.version,
            currency=material.purchase_currency, tax_included=material.purchase_tax_included,
            tax_rate=str(material.purchase_tax_rate) if material.purchase_tax_rate is not None else None,
            settlement_square_price=str(estimate.square_price), settlement_unit_price=str(estimate.unit_cost))
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
            material_code=facts.material_code, material_id=material.id if material else None,
            capture_material_cost=material is not None,
            supplier_name=supplier.standard_name if supplier else material.supplier_name if material else None, layer_count=payload.layer_count,
            entry_cost_estimate=direct_estimate,
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
        if payload.source_kind != "existing_stocktake" and direct_estimate is None:
            freeze_entry_cost(db, lot)
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


class UnassignedFinishedEntry(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    location_id: int = Field(gt=0)
    expected_layout_version: int = Field(gt=0)
    quantity: int = Field(gt=0, le=10000000)
    stock_date: date
    name: str = Field(min_length=1, max_length=200)
    length_mm: int = Field(gt=0, le=20000)
    width_mm: int = Field(gt=0, le=20000)
    height_mm: int = Field(gt=0, le=20000)
    box_style: Literal['A1', '其他'] = 'A1'
    report_length_mm: int | None = Field(default=None, gt=0, le=20000)
    report_width_mm: int | None = Field(default=None, gt=0, le=20000)
    splice_mode: Literal['single', 'double'] = 'single'
    flap_mm: int = Field(default=30, gt=0, le=200)
    material_id: int = Field(gt=0)
    material_confidence: Literal['estimated','confirmed'] = 'estimated'
    dimension_source: Literal['tape','label'] = 'tape'
    note: str = Field(default='', max_length=1000)
    fingerprint: str = ''
    idempotency_key: str = Field(min_length=10, max_length=100)


@router.post('/finished-unassigned/preview', dependencies=[Depends(PermissionChecker('warehouse.correct'))])
def preview_unassigned(payload: UnassignedFinishedEntry, db: Session = Depends(get_db), user: User = Depends(admin_only)):
    unrestricted(user, db)
    from app.services.unassigned_finished_entry import preview
    try:
        return preview(db, payload)[1]
    except WarehouseInventoryError as error:
        raise HTTPException(error.status_code, str(error)) from error


@router.post('/finished-unassigned', dependencies=[Depends(PermissionChecker('warehouse.correct'))])
def create_unassigned(payload: UnassignedFinishedEntry, db: Session = Depends(get_db), user: User = Depends(admin_only)):
    unrestricted(user, db)
    digest = request_hash(user, 'finished-unassigned', payload)
    previous = replay(db, payload.idempotency_key, digest)
    if previous:
        return previous
    from app.services.unassigned_finished_entry import create
    try:
        lot = create(db, payload, user)
        facts = GoodsFacts(scope='general', display_name=payload.name, processing='cut',
            material_code=lot.finished_detail.material_code_snapshot,
            verified_material_id=payload.material_id, material_confidence=payload.material_confidence,
            dimension_source=payload.dimension_source, note=payload.note)
        return record(db, user, lot, facts, payload.idempotency_key, digest, None, 'CREATE')
    except (WarehouseInventoryError, WarehouseStocktakeBatchError) as error:
        db.rollback()
        raise HTTPException(error.status_code, str(error)) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(409, '请求或货位已变化，请核对原请求结果后重试') from error
    except Exception:
        db.rollback()
        raise
