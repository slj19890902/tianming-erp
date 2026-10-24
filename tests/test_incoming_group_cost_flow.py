"""Anonymous reproduction of the 2,000-set / 509-sheet receipt failure."""
import json
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select, delete

from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login, _component_payload
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity, _seed_material_and_staging, _freeze_receipt_fact, _receive
from tests.test_multilevel_bom_receipt_flow import read_purchase_sources
from tests.test_multilevel_bom_master import save


def test_509_sheet_receipt_frozen_cost_output_and_reversal(composite_requisition_app, _p181_published_map_identity):
    from app.models.product import Product
    from app.models.order import OrderItem
    from app.models.user import User
    from app.models.product_bom import SalesOrderItemBomComponent
    from app.models.warehouse_inventory import WarehouseLocation, InventoryLot
    from app.models.production import ProductionCompletion
    from app.models.multilevel_bom import BomAssembly, BomAssemblyInput
    from app.models.purchase_receipt import IncomingReceiptPurposeAllocation
    from app.services.multilevel_bom_orders import freeze_master_order_bom
    from app.services.inventory_valuation import frozen_cost
    from app.core.inventory_entry_guard import validate_entries
    app, factory = composite_requisition_app
    with factory() as db:
        db.get(WarehouseLocation, 1).location_code='ANONYMOUS-OLD'
        db.commit()
    material_id=_seed_material_and_staging(factory,finished_slot_count=8)
    with factory() as db:
        from app.models.supplier import Supplier
        from app.services.supplier_master import normalize_supplier_identity
        db.add(Supplier(standard_name='苏州纸板供应商', normalized_name=normalize_supplier_identity('苏州纸板供应商'),
            display_name='苏州纸板供应商',sort_order=20,is_active=True,version=1))
        db.execute(delete(SalesOrderItemBomComponent))
        third=Product(customer_id=1,product_code='ANON-C',customer_material_code='ANON-C',product_name='匿名底件',unit='片')
        db.add(third);db.flush();third_id=third.id
        modes={2:80,3:135,third_id:8}
        for pid,y in modes.items():
            p=db.get(Product,pid)
            p.material_id=material_id;p.box_style='隔板';p.unit='片'
            p.report_length_mm=700;p.report_width_mm=950
            p.length_mm=700;p.width_mm=950;p.pieces_per_box=1
            p.default_cutting_mode=f'一开{y}'
        item=db.get(OrderItem,1);item.quantity=2000;item.composite_fulfillment_mode_snapshot='parent_delivery'
        save(db,db.get(User,1),1,'assembled',[(2,5,'assembly'),(3,9,'assembly'),(third_id,1,'assembly')])
        compiled=freeze_master_order_bom(db,order_item_id=1,actor=db.get(User,1))
        snapshots=[(s.id,s.component_product_id) for s in compiled.snapshots if s.component_product_id in modes]
        db.commit()
    with TestClient(app) as client:
        _login(client)
        request={'request_key':'anonymous-509-requisition','supplier_name':'苏州纸板供应商','items':[
            {**_component_payload(sid),'special_process':f'一开{modes[pid]}'} for sid,pid in snapshots]}
        r=client.post('/api/requisition/batches',json=request)
        assert r.status_code==201,r.text
        sources=read_purchase_sources(factory,material_id)
        assert sorted(s.order_purpose_sheet_qty for s in sources)==[125,134,250]
        pending=client.get('/api/incoming/pending',params={'grouped':True,'page_size':1}).json()
        assert pending['total']==1,pending
        assert len(pending['items'])==3
        assert sum(x['remaining_quantity'] for x in pending['items'])==509
        reported=client.get('/api/requisition/reported-items',params={'grouped':True,'page_size':1}).json()
        assert reported['total']==1,reported
        assert len(reported['items'])==3
        assert sum(x['requisition_qty'] for x in reported['items'])==509
        payload=[]
        from types import SimpleNamespace
        collector=SimpleNamespace(put=lambda url,json:dict(item_id=url.rsplit('/',1)[-1],**json))
        for i,source in enumerate(sources):
            fact=_freeze_receipt_fact(client,source,idempotency_key=f'price-509-{i}',unit_price='1.1970')
            assert fact.status_code==200,fact.text
            payload.append(_receive(collector,source,fact.json(),quantity=source.order_purpose_sheet_qty,idempotency_key=f'receive-509-{i}'))
        request={'atomic':True,'idempotency_key':'anonymous-509-receipt','items':payload}
        r=client.put('/api/incoming/batch-receive',json=request)
        assert r.status_code==200,r.text
        assert r.json()['succeeded']==3
        receipt_ids=[row['item']['receipt_item_id'] for row in r.json()['results']]
        replay=client.put('/api/incoming/batch-receive',json=request)
        assert replay.status_code==200,replay.text
        assert [row['item']['receipt_item_id'] for row in replay.json()['results']]==receipt_ids
        with factory() as db:
            completed=list(db.scalars(select(ProductionCompletion).where(ProductionCompletion.status=='posted')))
            quantities={db.get(InventoryLot,c.inventory_lot_id).finished_detail.product_id:c.actual_output_quantity for c in completed}
            assert quantities=={2:10000,3:18090,third_id:2000}
            assemblies=list(db.scalars(select(BomAssembly).where(BomAssembly.status=='posted')))
            assert sum(a.quantity for a in assemblies)==2000
            assert sum(a.total_cost for a in assemblies)==Decimal('608.4750')
            inputs=list(db.scalars(select(BomAssemblyInput)))
            old_comparison_errors=[abs(db.get(InventoryLot,x.lot_id).estimated_unit_cost_snapshot*x.quantity-x.total_cost) for x in inputs]
            assert sorted(old_comparison_errors)==[Decimal('.0500'),Decimal('.3750'),Decimal('.6000')]
            for a in assemblies:
                lot=db.get(InventoryLot,a.output_lot_id)
                unit,basis=frozen_cost(lot,db)
                assert unit is not None,basis
                assert basis.get('consumed_assembly_spec')
            b=next(c for c in completed if db.get(InventoryLot,c.inventory_lot_id).finished_detail.product_id==3)
            b_lot=db.get(InventoryLot,b.inventory_lot_id)
            assert b_lot.quantity_available+b_lot.quantity_reserved==90
            # Real transaction guard, including assembled physical evidence.
            db.info['new_inventory_entry_ids']={lot.id for lot in db.scalars(select(InventoryLot))}
            validate_entries(db)
            allocation=db.scalar(select(IncomingReceiptPurposeAllocation).where(IncomingReceiptPurposeAllocation.production_completion_id==b.id))
            allocation.status='reversed'
            with db.no_autoflush:
                assert frozen_cost(b_lot,db)[0] is None
            db.rollback()
        # Reversing the receipt unwinds automatic assembly and restores the
        # other component batches; it must not manufacture historical facts.
        r=client.put(f'/api/incoming/receipt-items/{receipt_ids[-1]}/revert',json={})
        assert r.status_code==200,r.text
        with factory() as db:
            assert not list(db.scalars(select(BomAssembly).where(BomAssembly.status=='posted')))
