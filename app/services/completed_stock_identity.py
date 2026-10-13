"""Explicit physical confirmation of an existing completed-child lot, not production."""
import hashlib
import json
from app.models.product import Product
from app.models.warehouse_goods import WarehouseGoodsProfile
from app.services.warehouse_goods import goods_profile
from app.services.finished_stock_identity import product_basis
from app.services.warehouse_inventory import WarehouseInventoryError


def preview(db, lot):
    detail = lot.semi_finished_detail
    profile = goods_profile(db, lot) or {}
    if (lot.inventory_type != 'semi_finished' or not detail or lot.status != 'active'
            or lot.quantity_reserved or lot.quantity_consumed or lot.quantity_available <= 0):
        raise WarehouseInventoryError('仅未预占、未消耗的正常半成品批次可确认已完成子件', 409)
    if profile.get('output_piece') or profile.get('physical_basis'):
        raise WarehouseInventoryError('该批次已有完成身份，不能重新覆盖', 409)
    pids = profile.get('product_ids') or []
    if (profile.get('scope') != 'customers' or len(pids) != 1
            or profile.get('customer_ids') != [detail.owner_customer_id]
            or profile.get('processing') not in ('cut', 'die_cut', 'printed')):
        raise WarehouseInventoryError('请先核实并保存唯一客户、产品及已加工类型；原纸不能直接确认为完成子件', 409)
    if detail.component_type != 'whole' or detail.stock_yield_per_sheet != 1 or detail.pieces_per_box != 1:
        raise WarehouseInventoryError('当前批次不是逐片1:1数量；请先核实真实形态，不自动换算数量', 409)
    product = db.get(Product, pids[0])
    if (not product or not product.is_active or product.deleted_at is not None
            or product.customer_id != detail.owner_customer_id or product.is_composite):
        raise WarehouseInventoryError('必须选择本客户有效的实际子件，不能确认组合父件', 409)
    basis = product_basis(product)
    if json.loads(basis).get('assembly'):
        raise WarehouseInventoryError('组套库存不能当单片已加工子件确认', 409)
    result = dict(lot_id=lot.id, lot_number=lot.lot_number, lot_version=lot.version,
        product_id=product.id, product_version=product.version, inventory_type='semi_finished',
        physical_stage='completed_component', quantity_available=lot.quantity_available,
        location_id=lot.warehouse_location_id, physical_basis=json.loads(basis),
        profile_sha256=hashlib.sha256(json.dumps(profile, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        confirmation_message='请逐项核对当前实物确已完成全部工序，当前数量逐片1:1对应所示产品；原数量、成本和来源保持。')
    result['preview_hash'] = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return result


def apply(db, lot, preview):
    profile = db.get(WarehouseGoodsProfile, lot.id)
    facts = json.loads(profile.data_json)
    facts.update(output_piece=True, remaining_processes=[],
        physical_basis=json.dumps(preview['physical_basis'], ensure_ascii=False, sort_keys=True, separators=(',', ':')),
        completion_identity_source='physical_confirmation')
    profile.data_json = json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    db.flush()
    from app.services.processed_component_stock import output_identity
    if output_identity(db, lot) is None:
        raise WarehouseInventoryError('完成子件身份核验失败，未保存', 409)
