"""Real 000205 common-box save regression, on disposable factory copies only."""
from copy import deepcopy
from sqlalchemy import select, func
from app.models.warehouse_inventory import InventoryLot, InventoryReservation
from app.models.order import Order, OrderItem
from app.services.multilevel_bom_requirements import read_graph_requirements
from tests.test_multilevel_bom_factory_compile import factory_copy
from tests.test_bom_other_products_acceptance import factory_http


def payload(db, quantity=600):
    lot = db.get(InventoryLot, 918)
    return {"customer_id":136, "customer_po":"ISOLATED-YL205-600",
        "idempotency_key":"isolated-yl205-save-600", "order_date":"2026-09-28",
        "items":[{"client_line_id":"yl205", "product_id":3799,
            "quantity":quantity, "unit_price":"1", "reservation_plan":{
                "finished":[{"lot_id":lot.id,"expected_version":lot.version,
                    "requested_qty":quantity,"confirmed":True,"recommendation_source":"dedicated"}],
                "semi":[]}}]}


def test_real_yl205_common_box_save_600_preserves_360_without_child_demand(factory_http):
    client, db = factory_http
    request = payload(db)
    result = client.post('/api/orders', json=request)
    assert result.status_code == 201, result.text
    iid = result.json()['items'][0]['id']
    db.expire_all()
    lot = db.get(InventoryLot,918)
    assert (lot.quantity_available, lot.quantity_reserved) == (360,600)
    rows = list(db.scalars(select(InventoryReservation).where(InventoryReservation.order_item_id==iid)))
    assert len(rows)==1 and rows[0].sales_order_item_bom_component_id is not None
    graph = read_graph_requirements(db,iid)
    assert all(p.make_units == 0 for p in graph.plan.products)
    assert db.get(OrderItem,iid).quantity == 600
    replay = client.post('/api/orders', json=request)
    assert replay.status_code in (200,201) and replay.json()['id']==result.json()['id'], replay.text
    changed = {**request,'items':[{**request['items'][0],'quantity':601}]}
    assert client.post('/api/orders',json=changed).status_code==409


def test_real_yl205_draft_preview_is_read_only_and_full(factory_http):
    client, db = factory_http
    request = payload(db)
    result = client.post('/api/orders/inventory-draft-preview',json={
        'customer_id':136,'items':request['items']})
    assert result.status_code==200, result.text
    row=result.json()['items'][0]
    assert row['finished_planned_quantity']==600 and row['production_required_quantity']==0
    assert row['coverage_state']=='full' and row['requisition_components']==[]
    db.expire_all()
    assert db.get(InventoryLot,918).quantity_available==960


def test_shared_lot_two_lines_and_shortage_keep_exact_set_ratio(factory_http):
    client, db = factory_http
    request = payload(db)
    request['items'].append({**deepcopy(request['items'][0]), 'client_line_id':'second-205'})
    preview = client.post('/api/orders/inventory-draft-preview', json={
        'customer_id':136,'items':request['items']})
    assert preview.status_code==200, preview.text
    assert [r['finished_planned_quantity'] for r in preview.json()['items']]==[600,360]
    result = client.post('/api/orders',json=request)
    assert result.status_code==201, result.text
    db.expire_all()
    assert db.get(InventoryLot,918).quantity_available==0
    assert db.get(InventoryLot,918).quantity_reserved==960
    first,second = [read_graph_requirements(db,r['id']) for r in result.json()['items']]
    assert next(p.make_units for p in first.plan.products if p.product_id==3799)==0
    demands={p.product_id:p for p in second.plan.products}
    assert demands[3799].make_units==240
    assert demands[3771].required_units==720 and demands[3783].required_units==960


def test_parent_plan_rejects_stale_cross_customer_wrong_identity_and_phantom(factory_http):
    from app.models.product import Product
    from app.models.multilevel_bom import ProductBomProfile
    from app.services.warehouse_inventory import finished_inventory_candidates_for_product
    client, db = factory_http
    request = payload(db)
    before = db.scalar(select(func.count()).select_from(Order))
    stale=deepcopy(request)
    stale['items'][0]['reservation_plan']['finished'][0]['expected_version']+=1
    assert client.post('/api/orders',json=stale).status_code==409
    unconfirmed=deepcopy(request)
    unconfirmed['items'][0]['reservation_plan']['finished'][0]['confirmed']=False
    assert client.post('/api/orders',json=unconfirmed).status_code==409
    lot=db.get(InventoryLot,918)
    owner=lot.finished_detail.owner_customer_id
    lot.finished_detail.owner_customer_id=next(x for x in db.scalars(select(Product.customer_id)) if x!=136)
    db.commit()
    assert client.post('/api/orders',json=request).status_code==409
    lot.finished_detail.owner_customer_id=owner
    identity=lot.finished_detail.physical_basis_json
    lot.finished_detail.physical_basis_json='{}'
    db.commit()
    assert client.post('/api/orders',json=request).status_code==409
    assert finished_inventory_candidates_for_product(db,customer_id=136,product_id=3799)==[]
    lot.finished_detail.physical_basis_json=identity
    profile=db.get(ProductBomProfile,3799)
    profile.source='separate'
    profile.material_mode='expand_children'
    profile.delivery_mode='components'
    db.get(Product,3799).is_virtual_composite_parent=True
    db.commit()
    assert client.post('/api/orders',json=request).status_code==409
    assert finished_inventory_candidates_for_product(db,customer_id=136,product_id=3799)==[]
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Order))==before
    assert (lot.quantity_available,lot.quantity_reserved)==(960,0)
    client.cookies.clear()
    assert client.post('/api/orders',json=request).status_code==401


def test_failure_after_bom_reservation_rolls_back_order_stock_and_audit(factory_http,monkeypatch):
    from app.api import orders
    from app.models.warehouse_inventory import InventoryMovement
    from app.models.audit import OperationLog
    client,db=factory_http
    models=(Order,OrderItem,InventoryReservation,InventoryMovement,OperationLog)
    before={m:db.scalar(select(func.count()).select_from(m)) for m in models}
    def fail(*args,**kwargs):
        raise RuntimeError('isolated late order cost failure')
    monkeypatch.setattr(orders,'freeze_order_item_material_cost',fail)
    assert client.post('/api/orders',json=payload(db)).status_code==500
    db.expire_all()
    assert {m:db.scalar(select(func.count()).select_from(m)) for m in models}==before
    lot=db.get(InventoryLot,918)
    assert (lot.quantity_available,lot.quantity_reserved,lot.version)==(960,0,2)
