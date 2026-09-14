"""Explicit RMB reference rules and atomic, previewed revaluation of physical stock."""
import json
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, update
from app.core.time_contract import utc_now_naive
from app.models.inventory_cost_rule import InventoryCostRule, InventoryCostMutation
from app.models.material import Material
from app.models.product import Product
from app.models.warehouse_inventory import InventoryLot, FinishedGoodsInventoryDetail, InventoryLotTransfer
from app.services.audit_log import append_audit_event
from app.services.inventory_cost_snapshot import InventoryCostEstimate, apply_cost_snapshot, _effective_square_price
from app.services.material_cost_supplement import canonical, fingerprint

Q = Decimal("0.0001")
SOURCE = "inventory_confirmed_material"
ALGORITHM = "inventory-cost-rule-cny-v1"


class CostRuleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)
    mode: Literal["auto", "material", "fixed", "sale"] = "auto"
    basis: str = Field(min_length=1, max_length=1000)
    purchase_channel: str = Field(default="", max_length=120)
    material_id: int | None = Field(default=None, gt=0)
    flute_type: Literal["NONE", "A", "B", "C", "E", "F", "AB", "BC", "BE", "AE", "AC"] | None = None
    length_mm: Decimal | None = Field(default=None, gt=0, le=100000)
    width_mm: Decimal | None = Field(default=None, gt=0, le=100000)
    sheets_per_product: int = Field(default=1, ge=1, le=100)
    products_per_sheet: int = Field(default=1, ge=1, le=1000)
    unit_cost: Decimal | None = Field(default=None, ge=Decimal("0.0001"), le=10000000, decimal_places=4)
    temporary: bool = False
    evidence: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def complete(self):
        if self.mode == "material" and not all((self.material_id, self.length_mm, self.width_mm)):
            raise ValueError("材料计价需要材质和纸板长宽（毫米）")
        if self.mode == "fixed" and self.unit_cost is None:
            raise ValueError("外购/参考单价必须大于零")
        if len(canonical(self.evidence)) > 20000:
            raise ValueError("成本来源说明过长")
        return self


def config_dict(config):
    return CostRuleConfig.model_validate(config).model_dump(mode="json")


def rule_payload(db, product):
    row = db.get(InventoryCostRule, product.id)
    return dict(product_id=product.id, product_version=product.version, product_code=product.product_code,
        product_name=product.product_name, version=row.version if row else 0,
        config=json.loads(row.config_json) if row else config_dict(dict(mode="auto", basis="材料优先，缺价时采用有效售价参考")))


def estimate_rule(db, product, config, *, version=0):
    """The price is per physical product, not per entire BOM set."""
    from app.services.inventory_valuation import CostResolution, positive
    cfg = CostRuleConfig.model_validate(config)
    detail = dict(algorithm_version=ALGORITHM, currency="CNY", tax_included=True,
        product_id=product.id, product_version=product.version, cost_rule_version=version,
        cost_rule=cfg.model_dump(mode="json"), estimate_basis="approved_stocktake_cost_rule",
        cost_label={"material":"材料成本", "sale":"售价参考成本", "fixed":"外购/分摊参考成本", "auto":"自动成本"}[cfg.mode],
        basis=cfg.basis, temporary=cfg.temporary)
    area, square = Decimal(0), Decimal(0)
    if cfg.mode == "auto":
        return None
    if cfg.mode in {"fixed", "sale"}:
        unit = cfg.unit_cost if cfg.mode == "fixed" else positive(product.sale_unit_price)
        if not unit:
            return CostResolution(None, ["尚无有效售价；请补材料资料或外购单价，不能用零元入库"])
        detail["formula"] = "已确认人民币含税单价" if cfg.mode == "fixed" else "本产品当前售价作参考成本（非历史采购价）"
        detail["sale_unit_price"] = str(product.sale_unit_price) if cfg.mode == "sale" else None
    else:
        material = db.get(Material, cfg.material_id)
        if not material or not material.is_active or material.purchase_currency != "CNY" or material.purchase_tax_included is None:
            return CostResolution(None, ["成本材质必须为有效人民币报价且含税口径完整"])
        result = _effective_square_price(db, material=material, supplier_name=material.supplier_name,
            layer_count=material.layer_count, flute_type=cfg.flute_type or product.flute_type)
        if not result:
            return CostResolution(None, ["成本材质缺有效平方价"])
        square, quote = result
        if not material.purchase_tax_included:
            if material.purchase_tax_rate is None:
                return CostResolution(None, ["未税材质报价缺少税率"])
            square *= 1 + material.purchase_tax_rate
        area = cfg.length_mm * cfg.width_mm / Decimal(1000000) * cfg.sheets_per_product / cfg.products_per_sheet
        unit = area * square
        detail.update(material_id=material.id, material_code=material.code, material_version=material.version,
            supplier_name=material.supplier_name, material_quote=quote,
            formula="纸板长mm × 宽mm ÷ 1000000 × 每件用纸张数 ÷ 每张出数 × 含税平方价")
    unit = unit.quantize(Q, rounding=ROUND_HALF_UP)
    if unit <= 0:
        return CostResolution(None, ["计算成本低于有效精度，请核对单位"])
    return CostResolution(InventoryCostEstimate(unit, square.quantize(Q, rounding=ROUND_HALF_UP),
        area.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP), SOURCE, detail), [])


def resolve_rule(db, product):
    row = db.get(InventoryCostRule, product.id)
    return estimate_rule(db, product, json.loads(row.config_json), version=row.version) if row else None


def is_revaluable(db, lot, visited=None):
    visited = set(visited or ())
    if lot.id in visited or lot.source_ref_type is not None or lot.cost_snapshot_source == "purchase_receipt_actual":
        return False
    if lot.source_type in {"manual", "stocktake"}:
        return True
    if lot.source_type != "transfer":
        return False
    visited.add(lot.id)
    origins = list(db.scalars(select(InventoryLotTransfer.source_lot_id).where(InventoryLotTransfer.target_lot_id == lot.id).distinct()))
    return bool(origins) and all((source := db.get(InventoryLot, pid)) is not None and is_revaluable(db, source, visited) for pid in origins)


def save_rule(db, product, config, *, user, expected_version, expected_product_version):
    if not user or not user.is_active or user.role != "admin":
        raise PermissionError("仅管理员可维护成本依据")
    if product.deleted_at is not None or product.version != expected_product_version:
        raise ValueError("产品资料已变化，请刷新")
    value = config_dict(config)
    current = db.get(InventoryCostRule, product.id)
    before = rule_payload(db, product)
    if before["version"] != expected_version:
        raise ValueError("成本依据已被修改，请刷新")
    if current and canonical(value) == current.config_json:
        return before
    result = estimate_rule(db, product, value, version=expected_version + 1)
    if result is not None and not result.estimate:
        raise ValueError("；".join(result.missing))
    if current:
        changed = db.execute(update(InventoryCostRule).where(InventoryCostRule.product_id == product.id,
            InventoryCostRule.version == expected_version).values(config_json=canonical(value),
            version=expected_version + 1, updated_by=user.id, updated_at=utc_now_naive()),
            execution_options={"synchronize_session": False})
        if changed.rowcount != 1:
            raise ValueError("成本依据已被修改，请刷新")
        db.expire(current)
    else:
        db.add(InventoryCostRule(product_id=product.id, config_json=canonical(value), version=1, updated_by=user.id))
    db.add(InventoryCostMutation(batch_id=f"rule:{product.id}:{expected_version+1}", product_id=product.id,
        request_json=canonical(before), response_json=canonical(dict(before=before, after=value)), created_by=user.id))
    append_audit_event(db, actor=user, event_category="business", result="success", source="api",
        module_code="warehouse", action_code="inventory_cost_rule_save", resource="inventory_cost_rule",
        entity_type="product", entity_id=product.id, description="维护盘点入库成本依据；不改生产资料或历史成本",
        details=dict(before=before, after=value))
    db.flush()
    return rule_payload(db, product)


def preview_revalue(db, product, lot_ids):
    """Only explicitly selected manual/stocktake stock; actual purchases stay intact."""
    from app.services.inventory_valuation import resolve_product_cost
    from app.services.inventory_cost_backfill import COST_FIELDS
    ids = sorted(set(lot_ids))
    lots = list(db.scalars(select(InventoryLot).join(FinishedGoodsInventoryDetail).where(
        InventoryLot.id.in_(ids), FinishedGoodsInventoryDetail.product_id == product.id).order_by(InventoryLot.id)))
    if not ids or len(lots) != len(ids):
        raise ValueError("请选择同一产品的有效在库批次")
    resolution = resolve_product_cost(db, product, main_only=True)
    if not resolution.estimate:
        raise ValueError("；".join(resolution.missing))
    estimate = resolution.estimate
    rows = []
    for lot in lots:
        quantity = lot.quantity_available + lot.quantity_reserved + lot.quantity_damaged
        if quantity <= 0 or not is_revaluable(db, lot):
            raise ValueError(f"批次{lot.id}不属于可调整的盘点/手工在库批次；采购和生产批次保持原价")
        rows.append(dict(lot_id=lot.id, version=lot.version, quantity=quantity, location_id=lot.warehouse_location_id,
            before={f: str(getattr(lot, f)) if getattr(lot, f) is not None else None for f in COST_FIELDS},
            unit_cost=str(estimate.unit_cost)))
    plan = dict(product_id=product.id, rule=rule_payload(db, product), rows=rows,
        estimate=dict(unit_cost=str(estimate.unit_cost), square_price=str(estimate.square_price), area_m2=str(estimate.area_m2),
            source=estimate.source, evidence=estimate.detail))
    return {**plan, "fingerprint": fingerprint(plan)}


def revalue(db, product, lot_ids, *, user, expected, batch_id):
    if not user or not user.is_active or user.role != "admin":
        raise PermissionError("仅管理员可确认在库成本调整")
    if not batch_id.strip() or len(batch_id) > 64:
        raise ValueError("调整批次标记无效")
    request = canonical(dict(product_id=product.id, lot_ids=sorted(set(lot_ids)), fingerprint=expected))
    prior = db.get(InventoryCostMutation, batch_id)
    if prior:
        result = json.loads(prior.response_json)
        if prior.request_json != request:
            raise ValueError("同一调整批次不能更换请求")
        return {**result, "replayed": True}
    plan = preview_revalue(db, product, lot_ids)
    if plan["fingerprint"] != expected:
        raise ValueError("库存或报价已变化，请重新预览；未调整成本")
    e = plan["estimate"]
    for p in plan["rows"]:
        lot = db.get(InventoryLot, p["lot_id"])
        changed = db.execute(update(InventoryLot).where(InventoryLot.id == lot.id, InventoryLot.version == p["version"])
            .values(version=InventoryLot.version + 1), execution_options={"synchronize_session":False})
        if changed.rowcount != 1:
            raise ValueError("库存已变化，整批取消")
        db.refresh(lot)
        apply_cost_snapshot(lot, InventoryCostEstimate(Decimal(e["unit_cost"]), Decimal(e["square_price"]),
            Decimal(e["area_m2"]), e["source"], {**e["evidence"], "original_cost":p["before"],
                "adoption_batch":batch_id, "scope":"仅当前在库成本；不改已冻结出库、数量、位置或应付"}))
    result = dict(product_id=product.id, lot_ids=sorted(set(lot_ids)), count=len(plan["rows"]), fingerprint=expected, replayed=False, plan=plan)
    db.add(InventoryCostMutation(batch_id=batch_id, product_id=product.id, request_json=request,
        response_json=canonical(result), created_by=user.id))
    append_audit_event(db, actor=user, event_category="business", result="success", source="api",
        module_code="warehouse", action_code="inventory_cost_revalue", resource="inventory_cost_revalue", batch_id=batch_id,
        entity_type="product", entity_id=product.id, description="管理员确认指定在库批次成本调整", details={k:v for k,v in result.items() if k != "plan"})
    # The immutable mutation stores the full before/after evidence without audit truncation.
    db.flush()
    return result
