from decimal import Decimal
import json
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.test_p0_45_receipt_price_gate import receipt_app, incoming_api_app, _login, _counts
from tests.test_p1_36l_incoming_pending_pagination import _fixture, _read


def cost_chain(*, amount='200', take=600, missing_source=False):
    from app.models.warehouse_inventory import InventoryLot, InventoryMovement
    from app.models.multilevel_bom import BomAssembly
    from app.models.production import ProductionCompletion
    from app.models.purchase_receipt import IncomingReceiptPurposeAllocation, PurchaseReceiptFact
    origin = NS(id=1, source_ref_type='production_completion', source_ref_id=11,
        cost_snapshot_source='component_processing_actual', estimated_unit_cost_snapshot=Decimal('.3333'),
        finished_detail=NS(product_id=10), cost_snapshot_detail_json=json.dumps({
            'bom_material_product_id':10, 'finished_quantity':600, 'capitalized_material_cost':'200',
            'bom_material_inputs':[{'kind':'allocation','id':12,'total_cost':'200'}], 'actual':True, 'currency':'CNY'}))
    output = NS(id=2, source_ref_type='bom_assembly', source_ref_id=13,
        cost_snapshot_source='bom_assembly', estimated_unit_cost_snapshot=Decimal(amount)/100,
        finished_detail=NS(product_id=20),cost_snapshot_detail_json='{}')
    input_row=NS(id=14,lot_id=1,product_id=10,quantity=take,total_cost=Decimal(amount),consume_movement_id=15)
    records={(InventoryLot,1):origin, (BomAssembly,13):NS(id=13,status='posted',quantity=100,
        output_product_id=20,total_cost=Decimal(amount)),
        (ProductionCompletion,11):NS(inventory_lot_id=1,actual_output_quantity=600),
        (IncomingReceiptPurposeAllocation,12):NS(purchase_receipt_fact_id=16),
        (PurchaseReceiptFact,16):None if missing_source else NS(currency='CNY'),
        (InventoryMovement,15):NS(inventory_lot_id=1,quantity=take,movement_type='consume')}
    return NS(get=lambda model,key:records.get((model,key)),scalars=lambda query:[input_row]), output


def test_exact_total_receipt_cost_not_rounded_unit_times_600():
    from app.services.inventory_valuation import frozen_cost
    db,output=cost_chain()
    unit,basis=frozen_cost(output,db)
    assert unit == Decimal('2'), basis
    assert basis['total_cost']=='200'
    assert Decimal('.3333')*600 == Decimal('199.9800')


@pytest.mark.parametrize('amount,missing', [('199.98',False),('200.01',False),('200',True)])
def test_real_cost_mismatch_and_missing_purchase_still_rejected(amount,missing):
    from app.services.inventory_valuation import frozen_cost
    db,output=cost_chain(amount=amount,missing_source=missing)
    assert frozen_cost(output,db)[0] is None


@pytest.mark.parametrize('amount', ['66.6666','66.6667'])
def test_partial_cumulative_cost_slices_preserve_four_decimal_total(amount):
    from app.services.inventory_valuation import frozen_cost
    db,output=cost_chain(amount=amount,take=200)
    assert frozen_cost(output,db)[0] is not None


def test_group_paging_keeps_all_members_and_scope(tmp_path):
    from app.models.requisition import RequisitionItem
    _,factory,uid,ordinary=_fixture(tmp_path)
    with factory() as db:
        for row in db.scalars(select(RequisitionItem)):
            row.material_snapshot='SAME-FROZEN-MATERIAL'
            row.cardboard_len=500;row.cardboard_width=300
        db.commit()
    pages=[_read(factory,uid,page=n,page_size=1,grouped=True) for n in range(1,len(ordinary)+3)]
    assert all(p['total']==len(ordinary)+2 for p in pages)
    group=next(p['items'] for p in pages if len(p['items'])==2)
    assert sum(r['remaining_quantity'] for r in group)==450
    assert len({r['receipt_group_key'] for r in group})==1
    assert all('隐藏' not in r['customer_name'] for p in pages for r in p['items'])
    assert _read(factory,uid,page=1,page_size=200)['total']==len(ordinary)+3


def test_atomic_group_failure_rolls_back_successful_receipt_and_can_retry(receipt_app):
    from app.models.material import Material
    from app.models.order import OrderItem
    app,factory=receipt_app
    with factory() as db:
        original=db.get(OrderItem,2).material_id
        bad=Material(code='GROUP-NO-PRICE',supplier_name='P045纸板',is_active=True)
        db.add(bad);db.flush();db.get(OrderItem,2).material_id=bad.id;db.commit()
    payload={'atomic':True,'idempotency_key':'group-receipt-once','items':[
        {'item_id':'r1','received_quantity':100,'idempotency_key':'group-a'},
        {'item_id':'r2','received_quantity':50,'idempotency_key':'group-b'}]}
    with TestClient(app) as client:
        _login(client,'admin');before=_counts(factory)
        r=client.put('/api/incoming/batch-receive',json=payload)
        assert r.status_code==409,r.text
        assert r.json()['detail']['code']=='INCOMING_GROUP_NOT_SAVED'
        assert _counts(factory)==before
        with factory() as db:
            db.get(OrderItem,2).material_id=original;db.commit()
        r=client.put('/api/incoming/batch-receive',json=payload)
        assert r.status_code==200,r.text
        assert r.json()['succeeded']==2
        saved=_counts(factory)
        assert client.put('/api/incoming/batch-receive',json=payload).status_code==200
        assert _counts(factory)==saved
        assert client.put('/api/incoming/batch-receive',json={**payload,'atomic':False}).status_code==409
