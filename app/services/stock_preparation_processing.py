"""Receipt-owned processing and explicit batches over the existing ledger.

GET projections never call these mutations. Splitting a task records the old
reservation release and fresh batch reservations in the caller's transaction.
"""
import json
from sqlalchemy import select
from app.models.product import Product
from app.models.raw_purchase_plan import RawPurchasePlan
from app.models.stock_preparation import StockPreparationJob as Job, StockPreparationCommand as Command
from app.services import stock_preparation as prep
from app.services.stock_preparation_groups import encode, digest


def processing_block(db, item, lot):
    if getattr(item, 'frozen_order_reserve', False):
        return '通用多送备料沿原用途保留，需要时另行安排'
    if not lot or lot.inventory_type != 'semi_finished':
        return '这批来料不属于待加工原纸'
    from app.services.stock_preparation_read import has_raw_plan
    if has_raw_plan(db, item.id):
        return '统一原片已按订单分配，请使用订单加工入口'
    if not (item.reference_product_id or item.product_id):
        return '通用备料未指定加工产品'
    if lot.quantity_available > 0 and lot.quantity_available*item.stock_yield_per_sheet//item.pieces_per_box<=0:
        return '本批材料不足一个产品的投入，请保留原纸等待继续处理'
    from app.services.warehouse_goods import goods_profile
    profile = goods_profile(db, lot) or {}
    if profile.get('output_piece') or profile.get('dimension_basis') == 'source_board':
        return '这是已加工子件，不能再次作为原纸加工'
    try:
        product = prep.plan_product(db, item, lot)
        from app.services.stock_replenishment_plan import frozen_bom_plan
        contract = frozen_bom_plan(item)
        if contract:
            child = next((c for c in contract['components'] if c['product_id'] == product.id), None)
            parent = db.get(Product, contract['parent_product_id'])
            if (not child or child['product_version'] != product.version or not parent
                    or parent.version != contract['parent_product_version']):
                return '采购后产品或BOM版本已变化，请核对冻结加工身份'
        elif (product.updated_at and item.created_at and product.updated_at > item.created_at):
            return '采购后产品资料已变化，请核对原加工身份'
        if item.sheet_cutting_snapshot:
            from app.services.sheet_cutting_contract import SheetCuttingContract
            cutting = SheetCuttingContract.from_snapshot(item.sheet_cutting_snapshot)
            if cutting.yield_per_supplier_sheet != item.stock_yield_per_sheet:
                return '冻结开料与每张产出不一致，请核对'
    except prep.WarehouseInventoryError as error:
        return str(error)
    except (ValueError, KeyError, TypeError) as error:
        return '冻结加工合同不完整，请核对'
    return None


def freeze_snapshot(db, item, lot):
    block = processing_block(db, item, lot)
    if block:
        prep.fail(block)
    product = db.get(Product, item.reference_product_id or item.product_id)
    from app.services.finished_stock_identity import product_basis
    from app.services.stock_replenishment_plan import frozen_bom_plan
    contract = frozen_bom_plan(item)
    return dict(product_id=product.id, code=item.product_code_snapshot or product.product_code,
        name=item.product_name_snapshot or product.product_name, factor=item.stock_yield_per_sheet,
        pieces_per_box=item.pieces_per_box, physical_basis=product_basis(product),
        product_version=product.version, planned_location=None, preparation_group=None,
        frozen_bom_plan=contract, sheet_cutting_snapshot=item.sheet_cutting_snapshot,
        bom_parent_basis=product_basis(db.get(Product,contract['parent_product_id'])) if contract else None,
        auto_planned=True)


def auto_plan_receipt(db, receipt, lot, actor):
    """A newly posted explicit-product receipt owns one deterministic task."""
    _, item, _ = prep.source(db, receipt.id)
    if processing_block(db, item, lot):
        return None
    key = 'auto-stock-receipt:' + str(receipt.id)
    snapshot = freeze_snapshot(db, item, lot)
    before = max(int(receipt.cumulative_received_quantity)-int(receipt.received_quantity),0)
    planned_input = min(lot.quantity_available,max(int(receipt.planned_quantity)-before,0))
    # A supplier over-receipt proves physical raw stock, not a new processing
    # purpose. Cancelled earlier tasks do not refill this receipt-plan allowance.
    if planned_input*snapshot['factor']//snapshot['pieces_per_box']<=0:
        return None
    return prep.mutate(db, receipt_id=receipt.id, actor=actor, frozen_snapshot=snapshot,
        payload=dict(action='plan', operation_key=key, lot_version=lot.version, quantity=planned_input))


def process(db, receipt_id, payload, actor):
    """One submit records this batch and leaves unprocessed input reserved."""
    request = encode(dict(payload, receipt_id=receipt_id))
    key = payload['operation_key']
    old = db.get(Command, key)
    if old:
        if old.request_json != request or old.actor_id != actor.id:
            prep.fail('操作标识已用于其他内容')
        return json.loads(old.result_json)
    receipt, item, lot = prep.source(db, receipt_id)
    if not lot or lot.version != payload['lot_version']:
        prep.fail('库存已变化，请刷新')
    if payload['action'] == 'process':
        snapshot = freeze_snapshot(db, item, lot)
        amount = payload.get('actual_input_quantity')
        if amount is None:
            prep.fail('请填写本次实际投入张数')
        if amount <= 0 or amount > lot.quantity_available:
            prep.fail('本次投入必须大于0且不能超过可用材料')
        original = None
        remaining = lot.quantity_available - amount
    else:
        original = db.get(Job, payload.get('job_id'))
        if (not original or original.receipt_item_id != receipt_id or original.status != 'pending'
                or original.version != payload.get('job_version')):
            prep.fail('待加工任务已变化，请刷新')
        snapshot = json.loads(original.product_snapshot)
        group = snapshot.get('preparation_group')
        if group and not group.get('independent'):
            prep.fail('原整组任务请从整组入口处理')
        amount = payload.get('actual_input_quantity')
        amount = original.input_quantity if amount is None else amount
        if amount <= 0 or amount > original.input_quantity:
            prep.fail('本次投入必须大于0且不能超过任务未加工张数')
        remaining = original.input_quantity - amount
        # Full completion retains the original task ID and legacy contract.
        if not remaining:
            child_payload = dict(payload)
            child_payload.pop('actual_input_quantity', None)
            child_payload['operation_key'] = digest([key, 'complete'])[:60]
            result = prep.mutate(db, receipt_id=receipt_id, payload=child_payload, actor=actor,
                                 output_kind=payload['output_kind'])
            result.update(completed_job_id=original.id, continuation_job_id=None, remaining_input_quantity=0)
            db.add(Command(operation_key=key,receipt_item_id=receipt_id,request_json=request,result_json=encode(result),actor_id=actor.id))
            db.flush()
            return result
        prep.mutate(db, receipt_id=receipt_id, actor=actor, payload=dict(action='cancel',
            operation_key=digest([key, 'cancel'])[:60], lot_version=lot.version,
            job_id=original.id, job_version=original.version))
        snapshot = dict(snapshot, batch_parent_job_id=original.id)
    planned = prep.mutate(db, receipt_id=receipt_id, actor=actor, frozen_snapshot=snapshot,
        payload=dict(action='plan',operation_key=digest([key,'batch'])[:60],lot_version=lot.version,quantity=amount))
    batch = db.get(Job, planned['job_id'])
    complete = dict(payload,action='complete',operation_key=digest([key,'output'])[:60],
        lot_version=lot.version,job_id=batch.id,job_version=batch.version)
    complete.pop('actual_input_quantity',None)
    prep.mutate(db,receipt_id=receipt_id,payload=complete,actor=actor,output_kind=payload['output_kind'])
    continuation = None
    if remaining and remaining*snapshot['factor']//snapshot['pieces_per_box']>0:
        continuation = prep.mutate(db,receipt_id=receipt_id,actor=actor,frozen_snapshot=snapshot,
            payload=dict(action='plan',operation_key=digest([key,'remaining'])[:60],lot_version=lot.version,quantity=remaining))['job_id']
    result = dict(action='complete',job_id=batch.id,completed_job_id=batch.id,
        continuation_job_id=continuation,remaining_input_quantity=remaining,
        original_job_id=original.id if original else None)
    db.add(Command(operation_key=key,receipt_item_id=receipt_id,request_json=request,result_json=encode(result),actor_id=actor.id))
    db.flush()
    return result
