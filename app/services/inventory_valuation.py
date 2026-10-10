"""RMB inventory material valuation. Never creates a purchase/payable fact."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import json
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.material import Material
from app.models.material_mapping import MaterialCodeMappingCandidate
from app.models.product import Product
from app.models.product_bom import ProductBomComponent
from app.models.warehouse_inventory import InventoryLot, FinishedGoodsInventoryDetail
from app.services.box_type_rules import recommend_box_type, BoxTypeRuleError
from app.services.inventory_cost_snapshot import (
    InventoryCostEstimate, apply_cost_snapshot, estimate_finished_product_cost,
    estimate_semi_finished_cost, _find_material,
)

ALGORITHM = "inventory-confirmed-material-cny-v1"
CONFIRMED_SOURCE = "inventory_confirmed_material"
Q = Decimal("0.0001")
OWNER_SOURCES = {"owner_current_reference_backfill", "owner_current_external_reference"}


def positive(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number > 0 else None
    except (ValueError, ArithmeticError):
        return None


def can_view_inventory_cost(user) -> bool:
    return bool(user and user.is_active and user.role in {"admin", "boss"})


@dataclass
class CostResolution:
    estimate: InventoryCostEstimate | None
    missing: list[str]


def _material_for_product(db, product):
    material = _find_material(db, material_id=product.material_id,
        material_code=product.default_material_code or product.legacy_material_text,
        supplier_name=product.material.supplier_name if product.material else None,
        layer_count=product.layer_count)
    if material or product.material_id:
        return material, {}
    # Only an explicitly approved, unambiguous dictionary mapping is evidence.
    text = (product.legacy_material_text or "").strip()
    codes = {text, product.default_material_code or ""}
    if text:
        codes.update(text.split(maxsplit=1))
    mappings = list(db.scalars(select(MaterialCodeMappingCandidate).where(
        MaterialCodeMappingCandidate.old_code.in_(codes),
        MaterialCodeMappingCandidate.review_status == "approved")))
    targets = {(m.new_code, m.new_supplier) for m in mappings if m.new_code and m.new_supplier}
    if len(targets) != 1:
        return None, {}
    code, supplier = next(iter(targets))
    matches = list(db.scalars(select(Material).where(Material.code == code,
        Material.supplier_name == supplier, Material.is_active.is_(True))))
    return (matches[0], {"approved_mapping_ids": [m.id for m in mappings]}) if len(matches) == 1 else (None, {})


def _external_cost(db, product):
    from app.models.external_packaging_purchase import (
        ExternalPackagingPurchaseItem as Item, ExternalPackagingPurchaseCancellation as Cancel,
    )
    rows = list(db.scalars(select(Item).where(
        Item.customer_product_id_snapshot == product.id,
        ~Item.purchase_order_id.in_(select(Cancel.purchase_order_id)),
    ).order_by(Item.confirmed_at.desc(), Item.id.desc()).limit(1)))
    if not rows:
        return CostResolution(None, ["请维护本产品的供应商采购单价及采购片数/成品数量关系"])
    row = rows[0]
    unit, numerator, denominator = (positive(row.unit_price),
        positive(row.purchase_quantity_basis_snapshot), positive(row.order_quantity_basis_snapshot))
    if row.currency != "CNY" or not all((unit, numerator, denominator)):
        return CostResolution(None, ["本产品缺少有效人民币采购价或成品换算关系"])
    if row.tax_mode == "tax_exclusive":
        if row.tax_rate is None:
            return CostResolution(None, ["本产品未税采购价缺少税率"])
        unit *= 1 + Decimal(str(row.tax_rate))
    elif row.tax_mode != "tax_inclusive":
        return CostResolution(None, ["本产品采购价税口径不完整"])
    unit = (unit * numerator / denominator).quantize(Q, rounding=ROUND_HALF_UP)
    return CostResolution(InventoryCostEstimate(unit, Decimal(0), Decimal(0), CONFIRMED_SOURCE, {
        "algorithm_version": ALGORITHM, "currency": "CNY", "tax_included": True,
        "estimate_basis": "current_product_purchase_reference", "product_id": product.id,
        "product_version": product.version, "purchase_item_id": row.id,
        "price_version_id": row.price_version_id, "purchase_unit_price": str(row.unit_price),
        "purchase_quantity_basis": str(numerator), "order_quantity_basis": str(denominator),
        "formula": "含税采购单价 × 采购数量基数 / 成品数量基数",
    }), [])


def _authorized_product_recipe(db, product):
    """Reuse this exact product's approved recipe, but price a new entry today.

    Approval is not transferred to similarly named products or a changed master.
    Historical reference prices themselves remain immutable.
    """
    if product.is_composite:
        return None
    lots = db.scalars(select(InventoryLot).join(FinishedGoodsInventoryDetail).where(
        FinishedGoodsInventoryDetail.product_id == product.id,
        InventoryLot.cost_snapshot_source == "owner_current_reference_backfill",
    ).order_by(InventoryLot.cost_snapshot_at.desc(), InventoryLot.id.desc()))
    for lot in lots:
        try:
            detail = json.loads(lot.cost_snapshot_detail_json or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(detail, dict) or not detail.get("authorization") or detail.get("basis") != "current_reference_cost_not_historical_purchase_fact":
            continue
        if detail.get("product_version") != product.version or detail.get("currency") != "CNY":
            continue
        parts = detail.get("components")
        if not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict):
            continue
        part = parts[0]
        if part.get("component") != "whole" or not positive(part.get("length_mm")) or not positive(part.get("width_mm")):
            continue
        material = db.get(Material, detail.get("material_id"))
        if not material or not material.is_active or material.purchase_currency != "CNY" or material.purchase_tax_included is None:
            continue
        view = SimpleNamespace(material=material, material_id=material.id, default_material_code=None,
            legacy_material_text=None, default_cardboard_length=None, default_cardboard_width=None,
            layer_count=material.layer_count, flute_type=detail.get("flute_type"), box_style="异形箱",
            report_length_mm=positive(part["length_mm"]), report_width_mm=positive(part["width_mm"]),
            base_report_length_mm=None, base_report_width_mm=None, pieces_per_box=positive(part.get("pieces_per_box")) or 1)
        estimate = estimate_finished_product_cost(db, product=view)
        if not estimate or not positive(estimate.unit_cost):
            continue
        unit = estimate.unit_cost
        if not material.purchase_tax_included:
            if material.purchase_tax_rate is None:
                continue
            unit = (unit * (1 + material.purchase_tax_rate)).quantize(Q, rounding=ROUND_HALF_UP)
        return CostResolution(InventoryCostEstimate(unit, estimate.square_price, estimate.area_m2, CONFIRMED_SOURCE,
            {**estimate.detail, "algorithm_version": ALGORITHM, "currency": "CNY", "tax_included": True,
             "product_id": product.id, "product_version": product.version, "material_version": material.version,
             "reference_recipe_lot_id": lot.id, "reference_recipe": detail,
             "formula_version": "owner-approved-product-recipe-current-quote-v1",
             "source_tax_included": material.purchase_tax_included, "source_tax_rate": str(material.purchase_tax_rate)}), [])
    return None


def resolve_product_cost(db: Session, product: Product, visited=None, *, main_only=False,
                         physical_yield=None, assembled_body_only=False, stock_stage='complete', for_entry=False) -> CostResolution:
    from app.services.inventory_cost_rules import resolve_rule, estimate_rule
    if for_entry:
        if not str(product.unit or '').strip():
            return CostResolution(None, ['请在常用箱补销售/实物单位'])
        if not product.is_composite and product.supply_mode != 'external_purchase' and not (
                (positive(product.length_mm) and positive(product.width_mm)) or
                (positive(product.report_length_mm) and positive(product.report_width_mm))):
            return CostResolution(None, ['请在常用箱补实物尺寸或报料长宽（毫米）'])
    from app.services.bom_inventory_contract import product_has_assembly
    # A whole-kit reference override is not evidence of the bare body's cost.
    bare_body = (stock_stage == 'body' or assembled_body_only) and product_has_assembly(db, product.id)
    explicit = None if bare_body else resolve_rule(db, product)
    # Historical fixed/sale references remain readable, but new physical entries
    # must use the material recipe (external purchases keep their own contract).
    if for_entry and explicit is not None and explicit.estimate and (explicit.estimate.detail.get('cost_rule') or {}).get('mode') in {'sale', 'fixed'}:
        explicit = None
    if explicit is not None:
        return explicit
    from app.models.multilevel_bom import ProductBomProfile
    profile = db.get(ProductBomProfile, product.id)
    from app.services.bom_inventory_contract import product_has_assembly
    if profile and not assembled_body_only and stock_stage == 'complete' and (
            profile.source == 'assembled' or product_has_assembly(db, product.id)):
        from app.services.bom_entry_cost import assembled_entry_cost
        return assembled_entry_cost(db, product, for_entry=for_entry)
    result = _resolve_product_cost(db, product, visited, main_only=main_only or stock_stage == 'body', physical_yield=physical_yield, for_entry=for_entry)
    # A kit sale price cannot be copied to a separately stored physical component.
    # Kits need an explicit allocation rule; a zero/unknown sale is never a price.
    if not for_entry and not result.estimate and not product.is_composite and positive(product.sale_unit_price):
        fallback = estimate_rule(db, product, dict(mode="sale", temporary=True,
            basis="老板确认：材料/采购成本资料不足时，以本产品有效售价暂作成本",
            evidence={"missing_material_inputs": result.missing}))
        return fallback
    return result


def _resolve_product_cost(db: Session, product: Product, visited=None, *, main_only=False, physical_yield=None, for_entry=False) -> CostResolution:
    visited = set(visited or ())
    if product.id in visited:
        return CostResolution(None, ["组合产品存在循环关系"])
    visited.add(product.id)
    if product.supply_mode == "external_purchase":
        return _external_cost(db, product)
    components = list(db.scalars(select(ProductBomComponent).where(
        ProductBomComponent.parent_product_id == product.id))) if product.is_composite and not main_only else []
    if product.is_composite and not main_only:
        if not components:
            return CostResolution(None, ["组合产品未维护完整部件清单"])
        main = resolve_product_cost(db, product, visited - {product.id}, main_only=True, for_entry=for_entry)
        parts, missing, total = [], ["主片：" + m for m in main.missing], Decimal(0)
        if main.estimate:
            total += main.estimate.unit_cost
            parts.append({"product_id": product.id, "quantity_per_set": "1",
                "unit_cost": str(main.estimate.unit_cost), "evidence": main.estimate.detail})
        for component in components:
            child = db.get(Product, component.component_product_id)
            result = resolve_product_cost(db, child, visited, for_entry=for_entry) if child else CostResolution(None, ["部件产品不存在"])
            if not result.estimate:
                missing.extend(f"部件{component.internal_component_code}：{m}" for m in result.missing)
                continue
            total += result.estimate.unit_cost * component.quantity_per_set
            parts.append({"component_id": component.id, "product_id": child.id,
                "quantity_per_set": str(component.quantity_per_set), "unit_cost": str(result.estimate.unit_cost),
                "evidence": result.estimate.detail})
        if missing:
            return CostResolution(None, missing)
        return CostResolution(InventoryCostEstimate(total.quantize(Q, rounding=ROUND_HALF_UP), Decimal(0), Decimal(0),
            CONFIRMED_SOURCE, {"algorithm_version": ALGORITHM, "currency": "CNY", "tax_included": True,
                "product_id": product.id, "product_version": product.version,
                "formula": "主片材料成本 + 各部件材料单价 × 每套部件数量", "components": parts}), [])
    material, mapping_evidence = _material_for_product(db, product)
    missing = []
    if material is None or not material.is_active:
        missing.append("请在常用箱绑定已报价的供应商材质")
    elif material.purchase_currency != "CNY":
        missing.append("供应商材质必须维护人民币价格")
    # A detached projection prevents read-only costing from editing product facts.
    fields = ("box_style", "layer_count", "flute_type", "report_length_mm", "report_width_mm",
              "base_report_length_mm", "base_report_width_mm", "pieces_per_box")
    view = SimpleNamespace(**{f: getattr(product, f) for f in fields}, material=material,
        material_id=material.id if material else None, default_material_code=None,
        legacy_material_text=None, default_cardboard_length=None, default_cardboard_width=None)
    formula_version = "saved-report-mm-v1"
    if not positive(view.report_length_mm) or not positive(view.report_width_mm):
        try:
            recommendation = recommend_box_type(box_style=product.box_style,
                length_mm=int(product.length_mm) if product.length_mm else None,
                width_mm=int(product.width_mm) if product.width_mm else None,
                height_mm=int(product.height_mm) if product.height_mm else None,
                splice_mode=product.splice_mode or ("double" if product.pieces_per_box == 2 else "single"),
                flap_mm=product.flap_mm, crease_type=product.crease_type)
        except BoxTypeRuleError as error:
            recommendation = {"auto_calculated": False, "message": str(error)}
        if recommendation["auto_calculated"]:
            for field in ("report_length_mm", "report_width_mm", "base_report_length_mm", "base_report_width_mm", "pieces_per_box"):
                if field in recommendation:
                    setattr(view, field, recommendation[field])
            formula_version = recommendation["formula_version"]
        else:
            missing.append("请补实际展开纸板长宽（毫米）；" + str(recommendation.get("message", "该箱型需展开尺寸")))
    if view.report_length_mm == 1 and view.report_width_mm == 1:
        missing.append("纸板尺寸1×1为占位资料，请填写实际展开尺寸")
    if missing:
        return (None if for_entry else _authorized_product_recipe(db, product)) or CostResolution(None, missing)
    view.sheet_cutting_settings = getattr(product, "sheet_cutting_settings", None)
    estimate = estimate_finished_product_cost(db, product=view)
    if estimate is None or estimate.unit_cost <= 0:
        return CostResolution(None, ["供应商平方价、价格单位或天地盖底片尺寸不完整"])
    from app.services.requisition_quantities import CUTTING_MODE_BOX_STYLES, cutting_factor, normalize_cutting_mode
    try:
        mode = normalize_cutting_mode(product.default_cutting_mode, strict=True) if product.box_style in CUTTING_MODE_BOX_STYLES else '一开一'
    except ValueError as error:
        return CostResolution(None, [str(error)])
    output = physical_yield if physical_yield is not None else cutting_factor(mode)
    if view.sheet_cutting_settings is not None:
        from app.services.sheet_cutting_settings import component_settings
        setting = component_settings(view.sheet_cutting_settings)
        # The estimate already divides actual supplier area by physical output,
        # including any explicit trim allowance; do not divide by cutting twice.
        output = 1 if physical_yield is None else physical_yield
        if physical_yield is not None:
            estimate = InventoryCostEstimate(estimate.unit_cost * setting.output_per_sheet,
                estimate.square_price, estimate.area_m2, estimate.source, estimate.detail)
    if type(output) is not int or output <= 0:
        return CostResolution(None, ['开料每张产出必须为正整数'])
    unit = estimate.unit_cost / output
    if material.purchase_tax_included is False:
        if material.purchase_tax_rate is None:
            return CostResolution(None, ["材质未税报价缺少税率"])
        unit = (unit * (1 + material.purchase_tax_rate)).quantize(Q, rounding=ROUND_HALF_UP)
    elif material.purchase_tax_included is not True:
        return CostResolution(None, ["材质报价未明确含税口径"])
    unit = unit.quantize(Q, rounding=ROUND_HALF_UP)
    if unit <= 0:
        return CostResolution(None, ['每片成本低于有效精度，请核对每张开料产出和单价'])
    detail = {**estimate.detail, **mapping_evidence, "algorithm_version": ALGORITHM,
        "cutting_mode": mode, "yield_per_sheet": output,
        "sheet_basis_cost": str(estimate.unit_cost),
        "formula": "纸板面积 × 平方价 × 每件用片数 / 每张开料产出（再按税口径换算）",
        "formula_version": formula_version, "currency": "CNY", "tax_included": True,
        "source_tax_included": material.purchase_tax_included, "source_tax_rate": str(material.purchase_tax_rate),
        "material_version": material.version, "product_id": product.id, "product_version": product.version,
        "estimate_basis": "stocktake_confirmed_current_material"}
    return CostResolution(InventoryCostEstimate(unit, estimate.square_price,
        (estimate.area_m2 / output).quantize(Decimal('.000001'), rounding=ROUND_HALF_UP), CONFIRMED_SOURCE, detail), [])


def resolve_lot_cost(db, lot) -> CostResolution:
    if lot.finished_detail:
        product = db.get(Product, lot.finished_detail.product_id)
        from app.services.bom_inventory_contract import is_body_lot
        return resolve_product_cost(db, product, main_only=True,
            stock_stage='body' if is_body_lot(lot) else 'complete') if product else CostResolution(None, ["产品不存在"])
    detail = lot.semi_finished_detail
    if detail is None:
        return CostResolution(None, ["批次缺少产品或片料资料"])
    estimate = estimate_semi_finished_cost(db, material_id=detail.material_id,
        material_code=detail.material_code_snapshot, supplier_name=detail.supplier_name,
        layer_count=detail.layer_count, flute_type=detail.flute_type,
        board_length_mm=detail.board_length_mm, board_width_mm=detail.board_width_mm)
    if estimate is None:
        return CostResolution(None, ["请补供应商材质报价和片料长宽"])
    material = db.get(Material, estimate.detail["material_id"])
    if material.purchase_currency != "CNY" or material.purchase_tax_included is None:
        return CostResolution(None, ["请维护人民币报价及税口径"])
    unit = estimate.unit_cost
    if not material.purchase_tax_included:
        if material.purchase_tax_rate is None:
            return CostResolution(None, ["未税报价缺少税率"])
        unit = (unit * (1 + material.purchase_tax_rate)).quantize(Q, rounding=ROUND_HALF_UP)
    return CostResolution(InventoryCostEstimate(unit, estimate.square_price, estimate.area_m2, CONFIRMED_SOURCE,
        {**estimate.detail, "algorithm_version": ALGORITHM, "currency": "CNY", "tax_included": True,
         "material_version": material.version, "source_tax_rate": str(material.purchase_tax_rate),
         "source_tax_included": material.purchase_tax_included}), [])


def frozen_cost(lot, db=None, visited=None):
    """Read only the entry snapshot, never today's price or a currency conversion."""
    if db is not None and lot.cost_snapshot_source in {'stock_preparation','bom_assembly','subkit_conversion','stock_preparation_assembly','sheet_cut_production'}:
        from app.services.derived_inventory_cost import derived_cost
        return derived_cost(db,lot,set(visited or ()))
    unit = positive(lot.estimated_unit_cost_snapshot)
    try:
        detail = json.loads(lot.cost_snapshot_detail_json or "{}")
    except (ValueError, TypeError):
        return None, {}
    if not isinstance(detail, dict):
        return None, {}
    from app.services.entry_source_valuation import SOURCES, source_entry_cost
    if lot.cost_snapshot_source in SOURCES:
        return source_entry_cost(db, lot, detail, set(visited or ()))
    currency = detail.get("currency")
    if not currency and db is not None and lot.cost_snapshot_source == 'purchase_receipt_actual' and detail.get('purchase_receipt_fact_id'):
        from app.models.purchase_receipt import PurchaseReceiptFact
        fact = db.get(PurchaseReceiptFact, detail['purchase_receipt_fact_id'])
        if fact:
            currency = fact.currency
            detail = dict(detail, currency=currency)
    if not currency and detail.get("price_unit") in {"元/㎡", "元/平方米", "元/m2", "元/平方"}:
        currency = "CNY"
    visited = set(visited or ())
    visited.add(lot.id)
    origin_id = detail.get("source_semi_inventory_lot_id")
    if not currency and db is not None and origin_id and origin_id not in visited:
        origin = db.get(InventoryLot, origin_id)
        origin_unit, origin_detail = frozen_cost(origin, db, visited) if origin else (None, {})
        if origin_unit:
            currency = "CNY"
            detail = {**detail, "origin_currency_evidence": origin_detail}
    if currency != "CNY" or lot.cost_snapshot_source not in {
        CONFIRMED_SOURCE, "material_quote_area", "purchase_receipt_actual", "manual_sheet_unit_cost", *OWNER_SOURCES
    }:
        return None, detail
    if lot.cost_snapshot_source in OWNER_SOURCES and (
        not detail.get("authorization") or detail.get("basis") != "current_reference_cost_not_historical_purchase_fact"
    ):
        return None, detail
    # Some historical estimates mislabeled centimetres as millimetres. Do not
    # promote a clearly undersized sheet snapshot into a confirmed outbound cost.
    physical = lot.finished_detail
    components = detail.get("components") or []
    if lot.cost_snapshot_source == "material_quote_area" and physical and isinstance(components, list) and len(components) == 1 and isinstance(components[0], dict):
        part = components[0]
        sheet_max = max(positive(part.get("length_mm")) or 0, positive(part.get("width_mm")) or 0)
        physical_max = max(positive(getattr(physical, f, None)) or 0 for f in ("length_mm", "width_mm", "height_mm"))
        if part.get("component") == "whole" and (positive(part.get("pieces_per_box")) or 1) <= 2 and 0 < sheet_max * 2 < physical_max:
            return None, {**detail, "validation_issue": "旧报料尺寸小于实物，疑似厘米误当毫米，须核对展开尺寸"}
    return unit, detail


def cost_payload(lot, db=None):
    from app.services.warehouse_display_units import lot_display_unit
    unit, detail = frozen_cost(lot, db)
    quantity = lot.quantity_available + lot.quantity_reserved + lot.quantity_damaged
    labour = detail.get('standard_labour_unit_cost') if unit is not None else None
    total_unit = unit + Decimal(labour) if labour is not None else None
    display_unit = detail.get('product_unit') or lot_display_unit(lot)
    return {"lot_id": lot.id, "unit_cost": str(unit) if unit else None,
        "display_unit": {'sheets': '张', 'boxes': '只', 'pieces': '片'}.get(display_unit, display_unit),
        "standard_labour_unit_cost": labour,
        "standard_total_unit_cost": str(total_unit) if total_unit is not None else None,
        "standard_total_value": str((total_unit * quantity).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)) if total_unit is not None else None,
        "standard_labour_missing": detail.get('standard_labour_missing'),
        "inventory_value": str((unit * quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if unit else None,
        "quantity": quantity, "unit": lot.unit, "currency": "CNY",
        "source": lot.cost_snapshot_source, "captured_at": str(lot.cost_snapshot_at) if lot.cost_snapshot_at else None,
        "validation_issue": detail.get("validation_issue"),
        "label": detail.get("cost_label") or ("采购入库成本" if lot.cost_snapshot_source == "purchase_receipt_actual" else (
            "老板确认参考成本" if lot.cost_snapshot_source in OWNER_SOURCES else (
                "参考配方材料成本" if detail.get("reference_recipe_lot_id") else "批次材料成本"))),
        "cost_basis": (detail.get("cost_rule") or {}).get("basis"), "temporary": bool(detail.get("temporary"))}


def freeze_entry_cost(db, lot, product=None, *, stock_stage='complete'):
    result = resolve_product_cost(db, product, main_only=(stock_stage=='body'), stock_stage=stock_stage, for_entry=True) if product else resolve_lot_cost(db, lot)
    if not result.estimate:
        from app.services.warehouse_inventory import WarehouseInventoryError
        raise WarehouseInventoryError("未能确定入库成本：" + "；".join(result.missing), 422)
    if product:
        result.estimate.detail['product_unit'] = product.unit
    apply_cost_snapshot(lot, result.estimate)
    return result.estimate


def require_inherited_entry_cost(db, lot):
    """A conversion cannot turn unknown source cost into a new priced batch."""
    unit, detail = frozen_cost(lot, db)
    stored = positive(lot.estimated_unit_cost_snapshot)
    if unit is None or stored is None or abs(unit - stored) > Q:
        from app.services.warehouse_inventory import WarehouseInventoryError
        issue = detail.get('validation_issue') or '请先补齐并确认该来源批次的入库成本'
        raise WarehouseInventoryError(f'批次 {lot.lot_number} 成本依据不完整：{issue}', 409)
    return stored
