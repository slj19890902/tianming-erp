"""Anonymous equivalents of the exported completed 3+4-piece assembly."""
import copy
import hashlib
import json
from datetime import datetime
from types import SimpleNamespace as N
import pytest
from app.models.incoming_receipt import IncomingReceiptItem
from app.models.master_data_object_version import MasterDataObjectVersion
from app.models.stock_preparation import StockPreparationJob, StockPreparationCommand
from app.models.stock_replenishment import StockReplenishmentOrderItem
from app.models.warehouse_inventory import InventoryLot
from app.models.warehouse_goods import WarehouseGoodsProfile
from app.services import frozen_assembly_mold_proof as proof
from app.services.finished_stock_identity import matches_lot_stock_identity, _product_basis
from app.services.stock_purchase_identity import _signature, _view


def enc(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class ReadOnlyDb:
    def __init__(self):
        self.rows = {}; self.entries = []; self.movements = {}; self.info = {}
    def get(self, model, key):
        return self.rows.get((model, key))
    def scalars(self, statement):
        return self.entries
    def scalar(self, statement):
        return self.movements.get(next(iter(statement.compile().params.values())))


@pytest.fixture(params=['finished', 'processed'])
def frozen_assembly(monkeypatch, request):
    db = ReadOnlyDb(); old_children = []; new_children = []; inputs = []
    for pid, ratio, mold in ((2, 3, 428), (3, 4, 429)):
        basis = {'schema': 1, 'product_id': pid, 'unit': '只', 'spec': '100×20mm',
                 'material': 'BOARD-A', 'production_process': '模切', 'mold_tool_id': None, 'assembly': []}
        fields = {'customer_id': 1, 'mold_tool_id': mold, 'unit': '只'}
        item = N(customer_id=1, reference_product_id=pid, product_id=None,
                 created_at=datetime(2026, 10, 8), production_snapshot_json=None)
        purchase_names = ('material_code_snapshot', 'layer_count', 'flute_type', 'report_length_mm',
                          'report_width_mm', 'crease_type', 'crease_left_mm', 'crease_middle_mm',
                          'crease_right_mm', 'component_type', 'pieces_per_box', 'stock_yield_per_sheet')
        for name in purchase_names:
            setattr(item, name, None)
        identity = {'schema': 1, 'product_id': pid, 'customer_id': 1, 'product_version': 8,
                    'fields': fields, 'physical_basis': enc(basis), 'source': 'master_history',
                    'history_id': pid, 'purchase_signature': _signature(item)}
        snapshot = {'product_id': pid, 'product_version': 8, 'physical_basis': enc(basis),
                    'purchase_identity': identity, 'sheet_cutting_snapshot': {'is_die_cut': True}}
        history_text = enc(fields)
        history = N(object_type='product', object_id=pid, version=8, snapshot_json=history_text,
                    snapshot_sha256=hashlib.sha256(history_text.encode()).hexdigest(),
                    created_at=datetime(2026, 10, 7))
        job = N(product_id=pid, status='completed', output_lot_id=pid, receipt_item_id=pid,
                actual_output=1440 * ratio, product_snapshot=enc(snapshot))
        child = N(id=pid, inventory_type='finished', source_ref_type='stock_preparation', source_ref_id=pid,
                  finished_detail=N(product_id=pid, owner_customer_id=1, is_general=False,
                                    physical_basis_json=enc(basis)), semi_finished_detail=None)
        if request.param == 'processed':
            child.inventory_type='semi_finished'; child.finished_detail=None
            child.semi_finished_detail=N(owner_customer_id=1,component_type='whole',stock_yield_per_sheet=1,pieces_per_box=1)
            db.rows[(WarehouseGoodsProfile,pid)]=N(data_json=enc({'output_piece':True,'processing':'die_cut',
                'remaining_processes':[], 'scope':'customers','customer_ids':[1],'product_ids':[pid],
                'physical_basis':enc(basis)}))
        db.rows.update({(IncomingReceiptItem, pid): N(status='posted', stock_replenishment_item_id=pid),
                        (StockReplenishmentOrderItem, pid): item, (MasterDataObjectVersion, pid): history,
                        (StockPreparationJob, pid): job, (InventoryLot, pid): child})
        key = f'fiction-consume-{pid}'
        db.movements[key] = N(inventory_lot_id=pid, movement_type='consume', quantity=1440 * ratio, operator_id=1)
        inputs.append({'product_id': pid, 'lot_id': pid, 'quantity': 1440 * ratio, 'movement_key': key})
        old_children.append({'product_id': pid, 'quantity': ratio, 'basis_hash': hashlib.sha256(enc(basis).encode()).hexdigest()})
        basis['mold_tool_id'] = str(mold)
        new_children.append({'product_id': pid, 'quantity': ratio, 'basis_hash': hashlib.sha256(enc(basis).encode()).hexdigest()})
    actual = {'schema': 1, 'product_id': 1, 'unit': '套', 'box_style': '刀卡', 'assembly': old_children}
    wanted = {**actual, 'assembly': new_children}
    lot = N(id=100, inventory_type='finished', source_ref_type='preparation_assembly', source_ref_id=100,
            quantity_available=1440, quantity_reserved=0, quantity_consumed=0,
            _sa_instance_state=True, finished_detail=N(product_id=1, owner_customer_id=1,
                is_general=False, physical_basis_json=enc(actual)))
    db.entries = [N(idempotency_key='prep-kit:fiction', quantity=1440, operator_id=1)]
    db.rows[(StockPreparationCommand, 'fiction')] = N(actor_id=1,
        result_json=enc({'action': 'assemble', 'output_lot_id':100, 'sets':1440,
            'recipe': {'parent_id':1,'customer_id':1,'parent_basis':enc(actual)}, 'inputs':inputs}))
    monkeypatch.setattr(proof, 'object_session', lambda _: db)
    return db, lot, enc(wanted)


def test_original_frozen_molds_qualify_completed_assembly_without_confirmation_or_writes(frozen_assembly):
    db, lot, wanted = frozen_assembly
    before = copy.deepcopy(lot.__dict__)
    assert matches_lot_stock_identity(lot, wanted)
    assert lot.__dict__ == before
    # Qualification stays the same after the existing allocator moves quantities.
    lot.quantity_available = 940; lot.quantity_reserved = 500
    assert matches_lot_stock_identity(lot, wanted)


@pytest.mark.parametrize('breakage', ['history_hash','history_customer','receipt','job','mold','child_owner',
                                    'child_basis','movement','assembly_sets','different_style','different_recipe'])
def test_unproven_or_physically_different_stock_remains_rejected(frozen_assembly, breakage):
    db, lot, wanted = frozen_assembly
    if breakage == 'history_hash': db.rows[(MasterDataObjectVersion,2)].snapshot_sha256 = '0'*64
    elif breakage == 'history_customer':
        h=db.rows[(MasterDataObjectVersion,2)]; h.snapshot_json=enc({'customer_id':2,'mold_tool_id':428,'unit':'只'});h.snapshot_sha256=hashlib.sha256(h.snapshot_json.encode()).hexdigest()
    elif breakage == 'receipt': db.rows[(IncomingReceiptItem,2)].status = 'voided'
    elif breakage == 'job': db.rows[(StockPreparationJob,2)].status = 'pending'
    elif breakage == 'mold':
        j=db.rows[(StockPreparationJob,2)];s=json.loads(j.product_snapshot);s['purchase_identity']['fields']['mold_tool_id']=999;j.product_snapshot=enc(s)
    elif breakage == 'child_owner':
        c=db.rows[(InventoryLot,2)];(c.finished_detail or c.semi_finished_detail).owner_customer_id=2
    elif breakage == 'child_basis':
        c=db.rows[(InventoryLot,2)]
        if c.finished_detail: c.finished_detail.physical_basis_json='{}'
        else:
            r=db.rows[(WarehouseGoodsProfile,2)];p=json.loads(r.data_json);p['physical_basis']='{}';r.data_json=enc(p)
    elif breakage == 'movement': db.movements['fiction-consume-2'].quantity -= 1
    elif breakage == 'assembly_sets': db.entries[0].quantity -= 1
    else:
        value=json.loads(wanted)
        if breakage == 'different_style': value['box_style']='模切内盒'
        else: value['assembly'][0]['quantity']=4
        wanted=enc(value)
    assert not proof.matches_proven_assembly(lot,wanted)


def test_future_purchase_projection_retains_independent_die_cut_settings():
    fields = dict(customer_id=1, unit='只', box_category='normal', box_style='隔板',
        supply_mode='corrugated_production', length_mm=100, width_mm=20, height_mm=None,
        mold_tool_id=428, flute_type='AB')
    contract = {'schema_version':2, 'theoretical_length_mm':'100','theoretical_width_mm':'20',
        'length_parts':1,'width_parts':1,'is_die_cut':True,'mold_count':4,
        'supplier_length_mm':'100','supplier_width_mm':'20','cutting_factor':1,'yield_per_supplier_sheet':4}
    item=N(material_code_snapshot='BOARD-A',layer_count=5,flute_type='AB',sheet_cutting_snapshot=contract)
    projected=_view({'fields':fields,'product_id':2},item)
    assert json.loads(_product_basis(projected))['mold_tool_id']=='428'
