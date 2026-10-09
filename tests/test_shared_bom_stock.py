"""Fictional equivalent BOMs keep one physical balance across two customers."""
import json
from decimal import Decimal
import pytest
from sqlalchemy import select
from fastapi.testclient import TestClient
from app.models.product import Product
from app.models.customer import Customer
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot
from app.models.shared_finished_stock import SharedBomMember, SharedFinishedGroup
from app.services import shared_finished_stock as shared
from app.services.warehouse_inventory import WarehouseInventoryError
from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity
from tests.test_multilevel_bom_receipt_flow import seed_graph, save
from tests.test_bom_confirmation396 import command


def seed_pair(factory):
    seed_graph(factory)
    with factory() as db:
        db.add(Customer(id=2,customer_number=2,customer_code='FICTION-BOM-B',name='虚构共用乙客户',payment_term_days=0,credit_limit=0))
        db.flush()
        for pid in (1,2,3):
            source=db.get(Product,pid)
            values={c.name:getattr(source,c.name) for c in Product.__table__.columns if c.name not in {'id','created_at','updated_at','customer_id'}}
            db.add(Product(id=pid+3,customer_id=2,**values))
        db.flush()
        save(db,db.get(User,1),4,'assembled',[(5,3,'assembly'),(6,4,'assembly')])
        from app.models.multilevel_bom import ProductBomProfile
        for pid in (1,4):
            profile=db.get(ProductBomProfile,pid)
            profile.material_mode='expand_children';profile.delivery_mode='parent'
        from app.services.multilevel_bom_orders import freeze_master_order_bom
        freeze_master_order_bom(db,order_item_id=1,actor=db.get(User,1))
        material=db.get(Product,2).material
        for pid in (1,2,3,4,5,6):db.get(Product,pid).flute_type=material.flute_type
        material.quote_price=2;material.price_unit='元/㎡';material.purchase_currency='CNY';material.purchase_tax_included=True
        db.commit()


def activate(db,pids,lids=()):
    value=shared.preview(db,product_ids=pids,lot_ids=lids)
    result=shared.confirm(db,product_ids=pids,lot_ids=lids,preview_hash=value['preview_hash'],
        operation_key='fiction-bom-'+str(pids),evidence='隔离虚构实物对应确认',actor=db.get(User,1))
    db.flush()
    return result['group_id']


def source_lots(db):
    from app.services.production_workflow import _receipt_auto_finished_ground_target
    from app.services.warehouse_inventory import manual_finished_in
    from app.services.inventory_valuation import CONFIRMED_SOURCE
    from app.core.time_contract import beijing_today
    lots=[]
    for pid,qty in ((5,32),(6,38)):
        place=_receipt_auto_finished_ground_target(db,claim=True,customer_id=2,product_id=pid)
        lot=manual_finished_in(db,customer_id=2,product_id=pid,location_id=place.location.id,
            quantity=qty,stock_date=beijing_today(),source_type='manual',remarks='虚构已加工子件',operator_id=1,
            idempotency_key=f'shared-bom-source-{pid}',expected_layout_version=place.layout_version)
        lot.estimated_unit_cost_snapshot=Decimal('0.1234');lot.cost_snapshot_source=CONFIRMED_SOURCE
        lot.cost_snapshot_detail_json='{"currency":"CNY","basis":"isolated_test_reviewed_cost"}'
        lots.append(lot)
    return lots


def test_role_recipe_and_drift(composite_requisition_app,_p181_published_map_identity):
    app,factory=composite_requisition_app;seed_pair(factory)
    with factory() as db:
        with pytest.raises(WarehouseInventoryError,match='先把'):activate(db,[1,4])
        with pytest.raises(WarehouseInventoryError):activate(db,[2,4])
        db.get(Product,5).unit='只';db.flush()
        activate(db,[2,5]);activate(db,[3,6]);parent=activate(db,[1,4])
        assert len(list(db.scalars(select(SharedBomMember))))==6
        assert list(db.scalars(select(InventoryLot)))==[]
        db.commit()
        db.get(Product,6).print_content='乙专用';db.flush()
        with pytest.raises(WarehouseInventoryError):shared.preview(db,product_ids=[1,4],lot_ids=[],group_id=parent)


@pytest.mark.parametrize('processed',[False,True])
def test_cross_customer_partial_assembly_reverse(composite_requisition_app,_p181_published_map_identity,processed):
    app,factory=composite_requisition_app;seed_pair(factory)
    from app.api.deliveries import router
    app.include_router(router,prefix='/api/deliveries')
    if processed:
        from tests.test_order_inventory_reliability import processed_lots
        ids=processed_lots(factory)
        from app.models.warehouse_goods import WarehouseGoodsProfile
        from app.services.shared_bom_stock import compiled_bases
        with factory() as db:
            bases=compiled_bases(db,4)
            for lid,pid in zip(ids,(5,6)):
                lot=db.get(InventoryLot,lid);lot.semi_finished_detail.owner_customer_id=2
                profile=db.get(WarehouseGoodsProfile,lid);value=json.loads(profile.data_json)
                value.update(customer_ids=[2],product_ids=[pid],physical_basis=bases[pid]);profile.data_json=json.dumps(value)
            db.commit()
    with factory() as db:
        lots=[db.get(InventoryLot,lid) for lid in ids] if processed else source_lots(db)
        activate(db,[2,5],[lots[0].id]);activate(db,[3,6],[lots[1].id]);activate(db,[1,4])
        before=[(l.id,shared.lot_identity(l)) for l in lots]
        from app.services.bom_auto_reservation import reserve_new_order_stock
        reserve_new_order_stock(db,order_item_id=1,operator_id=1)
        assert [(l.quantity_available,l.quantity_reserved) for l in lots]==[(2,30),(0,38)]
        assert reserve_new_order_stock(db,order_item_id=1,operator_id=1)==[]
        db.commit()
    with TestClient(app) as client:
        _login(client)
        rows=client.get('/api/production/pending-assemblies');assert rows.status_code==200,rows.text
        row=next(r for r in rows.json()['items'] if r.get('order_item_id')==1)
        assert row.get('expected_outputs')=={'1':9},row
        payload=command(row,'shared-bom-partial');payload['expected_outputs']={'1':5}
        result=client.post('/api/production/assemblies/1/confirm',json=payload)
        assert result.status_code==200,result.text
        assert client.post('/api/production/assemblies/1/confirm',json=payload).json()==result.json()
        with factory() as db:
            for lid,basis in before:
                lot=db.get(InventoryLot,lid)
                assert (lot.finished_detail or lot.semi_finished_detail).owner_customer_id==2 and shared.lot_identity(lot)==basis
            for group in db.scalars(select(SharedFinishedGroup)):group.enabled=False
            db.commit()
        from app.core.time_contract import beijing_today
        delivery=client.post('/api/deliveries',json=dict(customer_id=1,delivery_date=beijing_today().isoformat(),items=[dict(order_item_id=1,delivered_quantity=2)]))
        assert delivery.status_code==201,delivery.text
        did=delivery.json()['id'];sent=client.put(f'/api/deliveries/{did}/dispatch')
        assert sent.status_code==200,sent.text
        assert client.post('/api/production/assemblies/'+str(result.json()['assembly_ids'][0])+'/reverse',json={'confirm_reverse':True}).status_code==409
        cancelled=client.put(f'/api/deliveries/{did}/cancel');assert cancelled.status_code==200,cancelled.text
        result2=client.post('/api/production/assemblies/'+str(result.json()['assembly_ids'][0])+'/reverse',json={'confirm_reverse':True})
        assert result2.status_code==200,result2.text
        row=next(r for r in client.get('/api/production/pending-assemblies').json()['items'] if r.get('order_item_id')==1)
        assert row['expected_outputs']=={'1':9}
        with factory() as db:
            db.get(Product,2).report_length_mm=1300;db.commit()
        frozen=client.post('/api/production/assemblies/1/confirm',json=command(row,'shared-frozen-after-pause'))
        assert frozen.status_code==200,frozen.text


def test_scoped_bom_candidates_and_reservation(composite_requisition_app,_p181_published_map_identity):
    from app.models.access_control import UserCustomerScope,UserPermissionOverride
    from app.models.product_bom import SalesOrderItemBomComponent
    from app.models.order import OrderItem
    app,factory=composite_requisition_app;seed_pair(factory)
    with factory() as db:
        lots=source_lots(db);activate(db,[2,5],[lots[0].id]);activate(db,[3,6],[lots[1].id]);activate(db,[1,4])
        lid=lots[0].id;version=lots[0].version
        sid=db.scalar(select(SalesOrderItemBomComponent.id).where(SalesOrderItemBomComponent.sales_order_item_id==1,SalesOrderItemBomComponent.component_product_id==2))
        user=db.get(User,1);user.role='sales';user.customer_access_mode='selected'
        db.add(UserCustomerScope(user_id=1,customer_id=1))
        for code in ('warehouse.reserve','warehouse.view','orders.view'):
            db.add(UserPermissionOverride(user_id=1,permission_code=code,is_allowed=True))
        db.commit()
    with TestClient(app) as client:
        _login(client)
        path=f'/api/warehouse/finished/bom-components/{sid}/candidates?order_item_id=1'
        candidates=client.get(path);assert candidates.status_code==200,candidates.text
        assert any(r['lot_id']==lid for r in candidates.json()['items'])
        assert '虚构共用乙客户' not in candidates.text
        body=dict(order_item_id=1,bom_snapshot_id=sid,inventory_lot_id=lid,quantity=10,expected_version=version,idempotency_key='scoped-bom-reserve')
        result=client.post('/api/warehouse/finished/bom-components/reservations',json=body)
        assert result.status_code==200,result.text
        assert client.get('/api/warehouse/finished/shared-stock/groups').status_code==403


def test_additive_migration_and_immutable_proof(composite_requisition_app,_p181_published_map_identity):
    import importlib.util,hashlib
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from desktop_assistant.migration import facts
    app,factory=composite_requisition_app;seed_pair(factory)
    engine=factory.kw['bind'];SharedBomMember.__table__.drop(engine)
    path=Path(engine.url.database);before=facts(path)
    spec=importlib.util.spec_from_file_location('shared_bom_migration',Path(__file__).parents[1]/'alembic/versions/em1009bs_shared_bom.py')
    migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade();migration.downgrade();migration.upgrade()
    assert facts(path,columns=before['columns'])==before
    with factory() as db:
        activate(db,[2,5]);activate(db,[3,6]);activate(db,[1,4]);db.commit()
    with engine.begin() as connection:
        for query in ('UPDATE shared_bom_members SET role=\'assembled\'','DELETE FROM shared_bom_members'):
            from sqlalchemy.exc import IntegrityError
            with pytest.raises(IntegrityError):connection.exec_driver_sql(query)
        with Operations.context(MigrationContext.configure(connection)):
            with pytest.raises(RuntimeError,match='禁止有损降级'):migration.downgrade()
        assert connection.exec_driver_sql('PRAGMA integrity_check').scalar()=='ok'
        assert connection.exec_driver_sql('PRAGMA foreign_key_check').all()==[]


from stock_preparation_legacy_fixture import stock_replenishment_app,base_stock_replenishment_app


def test_real_preparation_auto_enrollment_cross_customer_stock_assembly(stock_replenishment_app):
    from tests.test_stock_preparation_groups import prepare,plan,action_body
    from app.api.stock_preparation import router
    from app.models.product_bom import ProductBomComponent
    from app.models.stock_preparation import StockPreparationJob as Job
    from app.models.shared_finished_stock import SharedFinishedPolicy,SharedFinishedLot
    from app.services.composite_bom import replace_product_bom
    from app.services.stock_preparation_assembly import preview
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    from app.api.deliveries import router as deliveries
    app.include_router(deliveries,prefix='/api/deliveries')
    with TestClient(app) as client:
        parent=prepare(app,factory,client)
        with factory() as db:
            root=db.get(Product,parent);actor=db.scalar(select(User).where(User.role=='admin'))
            actor_id=actor.id
            edges=list(db.scalars(select(ProductBomComponent).where(ProductBomComponent.parent_product_id==parent)))
            children=[e.component_product_id for e in edges];ratios={e.component_product_id:int(e.quantity_per_set) for e in edges}
            root.is_virtual_composite_parent=False
            for pid in children:
                p=db.get(Product,pid);p.is_internal_component=True;p.unit='片'
            db.flush()
            replace_product_bom(db,parent_product_id=parent,expected_version=root.version,user=actor,inventory_mode='assembled',material_mode='expand_children',delivery_mode='parent',components=[dict(component_product_id=pid,quantity_per_set=ratios[pid],inventory_relation='assembly') for pid in children])
            customer=Customer(customer_number=9876,customer_code='BOM-TEST-B',name='虚构BOM乙客户',payment_term_days=0,credit_limit=0)
            db.add(customer);db.flush();mapping={}
            for pid in [*children,parent]:
                source=db.get(Product,pid)
                values={c.name:getattr(source,c.name) for c in Product.__table__.columns if c.name not in {'id','customer_id','created_at','updated_at'}}
                target=Product(customer_id=customer.id,**values);db.add(target);db.flush();mapping[pid]=target.id
            target_parent=db.get(Product,mapping[parent])
            replace_product_bom(db,parent_product_id=target_parent.id,expected_version=target_parent.version,user=actor,inventory_mode='assembled',material_mode='expand_children',delivery_mode='parent',components=[dict(component_product_id=mapping[pid],quantity_per_set=ratios[pid],inventory_relation='assembly') for pid in children])
            for pid in [*children,parent]:
                value=shared.preview(db,product_ids=[pid,mapping[pid]],lot_ids=[])
                result=shared.confirm(db,product_ids=[pid,mapping[pid]],lot_ids=[],preview_hash=value['preview_hash'],operation_key=f'stock-pair-{pid}',evidence='虚构对应实物已核对',actor=actor)
                db.add(SharedFinishedPolicy(group_id=result['group_id'],auto_enroll=True))
            db.commit()
        rows=client.get('/api/production/stock-preparation').json()['items']
        from app.services.stock_preparation import source
        for row in rows:
            rid=row['receipt_item_id']
            with factory() as db:
                _,item,lot=source(db,rid)
                pid=item.reference_product_id or item.product_id
                if pid not in children:continue
                actual=ratios[pid]*5
                quantity=actual*item.pieces_per_box//item.stock_yield_per_sheet
                version=lot.version
            planned=client.post(f'/api/production/stock-preparation/{rid}/actions',json=dict(action='plan',quantity=quantity,lot_version=version,operation_key=f'shared-plan-{rid}'))
            assert planned.status_code==200,planned.text
            with factory() as db:
                job=db.get(Job,planned.json()['job_id']);_,_,lot=source(db,rid)
                body=dict(action='complete',job_id=job.id,job_version=job.version,actual_output=actual,
                    lot_version=lot.version,operation_key=f'shared-complete-{rid}',output_kind='semi',location_id=7,layout_version=1)
            completed=client.post(f'/api/production/stock-preparation/{rid}/actions',json=body)
            assert completed.status_code==200,completed.text
        with factory() as db:
            jobs=list(db.scalars(select(Job)))
            assert all(db.get(SharedFinishedLot,j.output_lot_id) for j in jobs)
            value=preview(db,mapping[parent],3)
            assert value['available_sets']==5,value
            assert all(r['shared_stock'] for r in value['sources'])
        payload=dict(action='assemble_stock',operation_key='shared-actual-stock-assembly',parent_id=mapping[parent],sets=3,
            basis_hash=value['basis_hash'],location_id=7,layout_version=1,jobs=[{k:r[k] for k in ('job_id','job_version','lot_id','lot_version','output_version')} for r in value['sources']])
        result=client.post('/api/production/stock-preparation/group-actions',json=payload)
        assert result.status_code==200,result.text
        assert client.post('/api/production/stock-preparation/group-actions',json=payload).json()==result.json()
        with factory() as db:
            output=db.get(InventoryLot,result.json()['output_lot_id'])
            assert output.quantity_available==3 and db.get(SharedFinishedLot,output.id)
            assert shared.match(db,output,product_id=parent,customer_id=db.get(Product,parent).customer_id)
            assert sorted(db.get(InventoryLot,j.output_lot_id).quantity_available for j in jobs)==[6,8]
            from app.models.order import Order,OrderItem
            from app.core.time_contract import beijing_today
            from app.services.multilevel_bom_orders import freeze_master_order_bom
            from app.services.bom_auto_reservation import reserve_new_order_stock
            p=db.get(Product,parent);customer_id=p.customer_id
            order=Order(order_number='FICTION-SHARED-KIT-DEL',customer_id=customer_id,order_date=beijing_today(),status='pending_production',payment_status='unpaid',total_amount=20)
            db.add(order);db.flush()
            item=OrderItem(order_id=order.id,product_id=parent,quantity=2,unit_price=10,subtotal=20,
                material_status='pending',requisition_status='未报料',snapshot_product_code=p.product_code,
                snapshot_product_name=p.product_name,composite_fulfillment_mode_snapshot='parent_delivery',sales_unit_snapshot='套')
            db.add(item);db.flush();iid=item.id
            freeze_master_order_bom(db,order_item_id=iid,actor=db.get(User,actor_id))
            reserve_new_order_stock(db,order_item_id=iid,operator_id=actor_id)
            assert (output.quantity_available,output.quantity_reserved)==(1,2)
            db.commit()
        sent=client.post('/api/deliveries',json=dict(customer_id=customer_id,delivery_date=beijing_today().isoformat(),items=[dict(order_item_id=iid,delivered_quantity=2)]))
        assert sent.status_code==201,sent.text
        did=sent.json()['id'];dispatch=client.put(f'/api/deliveries/{did}/dispatch')
        assert dispatch.status_code==200,dispatch.text
        cancelled=client.put(f'/api/deliveries/{did}/cancel');assert cancelled.status_code==200,cancelled.text
        with factory() as db:
            assert sorted(db.get(InventoryLot,j.output_lot_id).quantity_available for j in jobs)==[6,8]
