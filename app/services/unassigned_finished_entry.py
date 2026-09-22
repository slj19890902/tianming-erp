"""Physical stock with an honestly unknown owner; never a customer/product master."""
import hashlib
import json
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import update

from app.models.material import Material
from app.models.warehouse_inventory import InventoryLot, FinishedGoodsInventoryDetail
from app.services.box_type_rules import recommend_box_type
from app.services.inventory_valuation import resolve_lot_cost
from app.services.inventory_cost_snapshot import apply_cost_snapshot
from app.services.warehouse_inventory import (
    WarehouseInventoryError, _claim_inventory_destination, _location, _number,
    _movement, _balances, _ensure_finished_projection_postcondition, utc_now_naive,
)


def preview(db, payload):
    material = db.get(Material, payload.material_id)
    if not material or not material.is_active:
        raise WarehouseInventoryError('请选择有效的供应商材质；可先打开材质维护补齐报价', 422)
    if not material.supplier_name or not material.layer_count or not material.flute_type:
        raise WarehouseInventoryError('供应商材质缺供应商、层数或楞型，请完善所选材质', 422)
    if payload.box_style == 'A1':
        dims = recommend_box_type(box_style='A1', length_mm=payload.length_mm,
            width_mm=payload.width_mm, height_mm=payload.height_mm,
            splice_mode=payload.splice_mode, flap_mm=payload.flap_mm, crease_type='净料')
    else:
        if not payload.report_length_mm or not payload.report_width_mm:
            raise WarehouseInventoryError('非 A1 箱请填写实际报料长宽，不能仅凭外尺寸猜算', 422)
        dims = dict(report_length_mm=payload.report_length_mm, report_width_mm=payload.report_width_mm,
            pieces_per_box=2 if payload.splice_mode == 'double' else 1, formula_version='measured-sheet-mm-v1')
    detail = SimpleNamespace(material_id=material.id, material_code_snapshot=material.code,
        supplier_name=material.supplier_name, layer_count=material.layer_count, flute_type=material.flute_type,
        board_length_mm=dims['report_length_mm'], board_width_mm=dims['report_width_mm'])
    resolved = resolve_lot_cost(db, SimpleNamespace(finished_detail=None, semi_finished_detail=detail))
    if not resolved.estimate or resolved.estimate.unit_cost <= 0:
        raise WarehouseInventoryError('入库成本待完善：'+'；'.join(resolved.missing), 422)
    e = resolved.estimate
    pieces = dims['pieces_per_box']
    from app.services.inventory_cost_snapshot import InventoryCostEstimate
    estimate = InventoryCostEstimate(e.unit_cost * pieces, e.square_price, e.area_m2 * pieces, e.source,
        {**e.detail, **dims, 'product_unit':'只', 'material_confidence':payload.material_confidence,
         'dimension_source':payload.dimension_source, 'identity_status':'unassigned',
         'formula':'报料长 × 报料宽 / 1000000 × 每箱片数 × 当前供应商平方价（按税口径折为含税）',
         'temporary':True, 'cost_label':'客户待认领材料参考成本',
         'estimate_basis':'physical_stocktake_selected_material_not_purchase_fact',
         'physical_dimensions_mm':[payload.length_mm,payload.width_mm,payload.height_mm]})
    data = dict(unit_cost=str(estimate.unit_cost), display_unit='只', evidence=estimate.detail,
        material_version=material.version)
    fingerprint = hashlib.sha256(json.dumps([payload.model_dump(mode='json', exclude={'idempotency_key','fingerprint'}),data],
        ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    return estimate, {**data, 'fingerprint':fingerprint}


def create(db, payload, user):
    estimate, current = preview(db, payload)
    if current['fingerprint'] != payload.fingerprint:
        raise WarehouseInventoryError('资料或报价已变化，请重新计算并核对参考成本', 409)
    changed = db.execute(update(Material).where(Material.id==payload.material_id,
        Material.version==current['material_version']).values(version=Material.version))
    if changed.rowcount != 1:
        raise WarehouseInventoryError('供应商报价已变化，请重新计算', 409)
    _claim_inventory_destination(db, payload.location_id, expected_layout_version=payload.expected_layout_version)
    _location(db, payload.location_id, 'finished')
    from app.services.warehouse_stocktake_batch import _location_live_lots
    if _location_live_lots(db, payload.location_id):
        raise WarehouseInventoryError('此货位已有登记库存；请先核对是否同一批货，待认领新增请选择空库位', 409)
    now = utc_now_naive()
    lot = InventoryLot(lot_number=_number('UF'), inventory_type='finished',
        warehouse_location_id=payload.location_id, quantity_available=payload.quantity, unit='boxes',
        status='active', source_type='stocktake', stock_date=payload.stock_date,
        last_movement_at=now, remarks=payload.note, created_by=user.id)
    estimate.detail['operator_id'] = user.id
    apply_cost_snapshot(lot, estimate, captured_at=now)
    basis = dict(estimate.detail, schema=1, product_id=None, unit='只', identity_status='unassigned',
        box_style=payload.box_style, spec=f'{payload.length_mm}×{payload.width_mm}×{payload.height_mm}',
        material_id=payload.material_id)
    lot.finished_detail = FinishedGoodsInventoryDetail(is_general=True, product_id=None,
        owner_customer_id=None, owner_customer_name_snapshot='客户待认领',
        inventory_code_snapshot=lot.lot_number, product_name_snapshot=payload.name.strip(),
        box_type_snapshot=payload.box_style, length_mm=payload.length_mm, width_mm=payload.width_mm,
        height_mm=payload.height_mm, material_code_snapshot=estimate.detail['material_code'],
        flute_type_snapshot=estimate.detail['flute_type'], physical_basis_json=json.dumps(basis,ensure_ascii=False))
    db.add(lot); db.flush()
    _movement(db, lot=lot, movement_type='manual_in', quantity=payload.quantity,
        before={key:0 for key in _balances(lot)}, operator_id=user.id,
        reason='现场盘点客户待认领成品', remarks=payload.note, idempotency_key='goods:'+payload.idempotency_key)
    _ensure_finished_projection_postcondition(db, lot=lot, operator_id=user.id, create_missing=True)
    db.flush()
    return lot


def claim_matches(detail, product):
    """An estimate becomes an owner link only after the operator matches real facts."""
    from app.services.box_type_rules import get_box_type_rule
    basis = json.loads(detail.physical_basis_json or '{}')
    old_rule, new_rule = get_box_type_rule(detail.box_type_snapshot), get_box_type_rule(product.box_style)
    report = {'report_length_mm':product.report_length_mm, 'report_width_mm':product.report_width_mm}
    if new_rule and new_rule.code == 'a1_0201' and not all(report.values()):
        report = recommend_box_type(box_style='A1', length_mm=int(product.length_mm or 0),
            width_mm=int(product.width_mm or 0),height_mm=int(product.height_mm or 0),
            splice_mode=product.splice_mode or 'single',flap_mm=product.flap_mm or 30,crease_type=product.crease_type)
    return bool(all(report.get(k)==basis.get(k) for k in ('report_length_mm','report_width_mm'))
        and basis.get('identity_status') == 'unassigned' and product.unit == basis.get('unit')
        and product.material_id == basis.get('material_id')
        and old_rule and new_rule and old_rule.code == new_rule.code
        and all(getattr(detail, field) == getattr(product, field) for field in ('length_mm','width_mm','height_mm'))
        and (product.splice_mode or 'single') == ('double' if basis.get('pieces_per_box') == 2 else 'single'))
