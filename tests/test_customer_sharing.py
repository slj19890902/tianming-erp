import json
from unittest.mock import patch
import pytest
from sqlalchemy import select
from fastapi import HTTPException
from tests.test_shared_finished_stock import reservation_db, setup_pair, activate
from app.models.customer import Customer
from app.models.product import Product
from app.models.customer_sharing import CustomerSharingMember,CustomerSharingGroup
from app.models.shared_finished_stock import SharedFinishedMember
from app.services import customer_sharing as service
from tests.test_yg_gy_shared_master import edit


def args(data,**kw):
    return dict(customer_id=data['other_customer'].id,source_customer_id=data['customer'].id,
        product_ids=[data['product'].id],new_products_shared=True,delivery_heading_customer_id=None,
        delivery_address_customer_id=None,**kw)


def save(db,data,values):
    plan=service.preview(db,actor=data['admin'],**values)
    assert not plan['blocked'],plan['blocked']
    result=service.confirm(db,actor=data['admin'],preview_hash=plan['preview_hash'],**values)
    db.commit();return result


def test_customer_catalog_reuses_one_sku_prices_and_new_private_products(reservation_db):
    db,data=reservation_db
    result=save(db,data,args(data))
    peer=db.scalar(select(Product).where(Product.customer_id==data['other_customer'].id,Product.product_code==data['product'].product_code))
    assert peer and peer.sale_unit_price is None
    assert db.get(SharedFinishedMember,peer.id).group_id==db.get(SharedFinishedMember,data['product'].id).group_id
    edit(db,data,peer,{'report_length_mm':802});db.commit()
    assert data['product'].report_length_mm==peer.report_length_mm==802
    source=data['other_product'];source.product_code='PRIVATE';db.commit()
    service.enroll_new_product(db,source,data['admin'],False);db.commit()
    assert db.get(SharedFinishedMember,source.id) is None
    service.enroll_new_product(db,source,data['admin']);db.commit()
    assert db.get(SharedFinishedMember,source.id)
    assert result['linked'] and db.query(CustomerSharingMember).count()==2


def test_third_customer_and_separate_delivery_defaults_leave_existing_customers_unchanged(reservation_db):
    db,data=reservation_db;save(db,data,args(data))
    third=Customer(customer_number=7003,customer_code='SUB-C',name='子公司C',address='C地址',contact_person='C联系人',phone='C电话')
    db.add(third);db.commit();before=service.delivery_defaults(db,third)
    values=args(data);values.update(customer_id=third.id,delivery_heading_customer_id=data['customer'].id,delivery_address_customer_id=data['other_customer'].id)
    save(db,data,values)
    assert db.query(CustomerSharingMember).count()==3
    after=service.delivery_defaults(db,third)
    assert after['customer_name_snapshot']==data['customer'].name
    assert before['customer_name_snapshot']=='子公司C' and third.name=='子公司C' and third.address=='C地址'
    peer=db.scalar(select(Product).where(Product.customer_id==third.id,Product.product_code==data['product'].product_code))
    edit(db,data,peer,{'report_width_mm':220});db.commit()
    assert {p.report_width_mm for p in db.scalars(select(Product).where(Product.product_code==peer.product_code))}=={220}


def test_conflicting_master_is_explained_not_overwritten_and_atomic_failure(reservation_db):
    db,data=reservation_db;target,item,lot=setup_pair(db,data)
    target.report_width_mm=777;db.commit()
    values=args(data);plan=service.preview(db,actor=data['admin'],**values)
    assert not plan['actions'] and plan['blocked'] and '不同' in plan['blocked'][0]['reason']
    with patch.object(service,'append_audit_event',side_effect=RuntimeError('audit')):
        with pytest.raises(RuntimeError):service.confirm(db,actor=data['admin'],preview_hash=plan['preview_hash'],**values)
    db.rollback();assert db.query(CustomerSharingGroup).count()==0
    assert target.report_width_mm==777 and lot.quantity_available==50


def test_stale_preview_and_nonadmin_rejected(reservation_db):
    db,data=reservation_db;values=args(data);plan=service.preview(db,actor=data['admin'],**values)
    data['product'].version+=1;db.commit()
    with pytest.raises(HTTPException):
        service.confirm(db,actor=data['admin'],preview_hash=plan['preview_hash'],**values)
    db.rollback()
    with pytest.raises(HTTPException):service.confirm(db,actor=None,preview_hash=plan['preview_hash'],**values)
    assert db.query(CustomerSharingGroup).count()==0

from tests.test_shared_bom_stock import composite_requisition_app,_p181_published_map_identity,seed_pair

def test_bom_catalog_creates_customer_references_and_keeps_recipe(composite_requisition_app,_p181_published_map_identity):
    app,factory=composite_requisition_app;seed_pair(factory)
    from app.models.user import User
    from app.models.product_bom import ProductBomComponent
    with factory() as db:
        third=Customer(customer_number=9003,customer_code='SUB-BOM',name='子公司BOM');db.add(third);db.commit()
        values=dict(customer_id=third.id,source_customer_id=1,product_ids=[1],new_products_shared=True,
            delivery_heading_customer_id=None,delivery_address_customer_id=None)
        data={'admin':db.get(User,1)};result=save(db,data,values)
        assert len(result['linked'])==1
        created=list(db.scalars(select(Product).where(Product.customer_id==third.id)))
        assert len(created)==3 and all(p.sale_unit_price is None for p in created)
        parent=next(p for p in created if p.is_composite)
        recipe=list(db.scalars(select(ProductBomComponent).where(ProductBomComponent.parent_product_id==parent.id)))
        assert [int(e.quantity_per_set) for e in recipe]==[3,4]
        from app.services import shared_finished_stock as shared
        assert shared.preview(db,product_ids=[1,parent.id],lot_ids=[],group_id=result['linked'][0]['group_id'])
        child=next(p for p in created if p.is_internal_component)
        edit(db,data,child,{'report_length_mm':1000});db.commit()
        assert shared.preview(db,product_ids=[1,parent.id],lot_ids=[],group_id=result['linked'][0]['group_id'])
        from app.services.composite_bom import replace_product_bom
        from app.services.shared_catalog_bom import EDGE_FIELDS
        replace_product_bom(db,parent_product_id=parent.id,expected_version=parent.version,user=data['admin'],
            change_reason='明确统一配比',inventory_mode='assembled',material_mode='expand_children',delivery_mode='parent',
            components=[dict(component_product_id=e.component_product_id,inventory_relation='assembly',
                **{k:(5 if k=='quantity_per_set' else getattr(e,k)) for k in EDGE_FIELDS}) for e in recipe])
        db.commit()
        assert [int(e.quantity_per_set) for e in db.scalars(select(ProductBomComponent).where(ProductBomComponent.parent_product_id==1).order_by(ProductBomComponent.display_order))]==[5,5]
        assert shared.preview(db,product_ids=[1,parent.id],lot_ids=[],group_id=result['linked'][0]['group_id'])
        from app.services.shared_customer_access import PHYSICAL_FIELDS
        from app.services.composite_bom import CompositeBOMError
        new_child=Product(customer_id=third.id,product_code='ONLY-NEW-CHILD',customer_material_code='ONLY-NEW-CHILD',
            product_name='仅本客户新子件',is_internal_component=True,**{k:getattr(child,k) for k in PHYSICAL_FIELDS})
        db.add(new_child);db.commit()
        before=parent.version
        with pytest.raises(CompositeBOMError,match='尚未关联'):
            replace_product_bom(db,parent_product_id=parent.id,expected_version=parent.version,user=data['admin'],
                inventory_mode='assembled',material_mode='expand_children',delivery_mode='parent',
                components=[dict(component_product_id=e.component_product_id,inventory_relation='assembly',
                    **{k:getattr(e,k) for k in EDGE_FIELDS}) for e in recipe]+
                    [dict(component_product_id=new_child.id,inventory_relation='assembly',quantity_per_set=1)])
        db.rollback()
        assert parent.version==before
        assert len(list(db.scalars(select(ProductBomComponent).where(ProductBomComponent.parent_product_id==parent.id))))==2


def test_existing_qualified_stock_join_is_audited_without_quantity_or_reservation_change(reservation_db):
    from tests.test_shared_finished_stock import add_lot
    from app.services import shared_finished_stock as shared
    db,data=reservation_db;lot=add_lot(db,data,quantity=50,key='catalog-existing');db.commit()
    before=(lot.quantity_available,lot.quantity_reserved,lot.estimated_unit_cost_snapshot,lot.finished_detail.physical_basis_json)
    result=save(db,data,args(data));peer=db.scalar(select(Product).where(Product.customer_id==data['other_customer'].id,Product.product_code==data['product'].product_code))
    assert result['linked'] and shared.match(db,lot,product_id=peer.id,customer_id=peer.customer_id)
    assert before==(lot.quantity_available,lot.quantity_reserved,lot.estimated_unit_cost_snapshot,lot.finished_detail.physical_basis_json)


def test_http_customer_scope_and_transaction_errors_are_structured(reservation_db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import customers
    from app.api.deps import get_db,get_current_user
    db,data=reservation_db
    app=FastAPI();app.include_router(customers.router,prefix='/api/master/customers')
    def session():yield db
    app.dependency_overrides[get_db]=session
    app.dependency_overrides[get_current_user]=lambda:data['admin']
    client=TestClient(app);values=args(data);cid=values.pop('customer_id')
    preview=client.post(f'/api/master/customers/{cid}/product-sharing/preview',json=values)
    assert preview.status_code==200,preview.text
    result=client.post(f'/api/master/customers/{cid}/product-sharing/confirm',json={**values,'preview_hash':preview.json()['preview_hash']})
    assert result.status_code==200,result.text
    assert client.get(f'/api/master/customers/{cid}/product-sharing').json()['new_products_shared']
    stale=client.post(f'/api/master/customers/{cid}/product-sharing/confirm',json={**values,'preview_hash':preview.json()['preview_hash']})
    assert stale.status_code==409 and isinstance(stale.json()['detail'],str)


def test_create_product_api_enrolls_default_and_keeps_private_choice(reservation_db):
    from app.api.products import ProductPayload,_create_product
    db,data=reservation_db;save(db,data,args(data));source=data['product']
    fields={k:getattr(source,k) for k in ProductPayload.model_fields if hasattr(source,k)}
    fields.update(product_code='NEW-GROUP-1',customer_material_code='NEW-GROUP-1',product_name='共用新品')
    result=_create_product(ProductPayload(**fields),db,data['admin'])
    assert db.get(SharedFinishedMember,result['id'])
    fields.update(product_code='PRIVATE-2',customer_material_code='PRIVATE-2',product_name='独有新品',share_with_customer_group=False)
    result=_create_product(ProductPayload(**fields),db,data['admin'])
    assert db.get(SharedFinishedMember,result['id']) is None


def test_new_shared_product_conflict_returns_json_and_keeps_both_customers_unchanged(reservation_db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import products
    from app.api.deps import get_db,get_current_user
    db,data=reservation_db;save(db,data,args(data));source=data['product']
    fields={k:getattr(source,k) for k in products.ProductPayload.model_fields if hasattr(source,k)}
    fields.update(product_code='CONFLICT-NEW',customer_material_code='CONFLICT-NEW',product_name='新品',
        customer_id=data['other_customer'].id,report_width_mm=777,share_with_customer_group=False)
    existing=products._create_product(products.ProductPayload(**fields),db,data['admin'])
    count=db.query(Product).count()
    app=FastAPI();app.include_router(products.router,prefix='/api/master/products')
    def session():yield db
    app.dependency_overrides[get_db]=session
    app.dependency_overrides[get_current_user]=lambda:data['admin']
    fields.update(customer_id=source.customer_id,report_width_mm=source.report_width_mm,share_with_customer_group=None)
    response=TestClient(app).post('/api/master/products',json=products.ProductPayload(**fields).model_dump(mode='json'))
    assert response.status_code==409,response.text
    assert '不同' in response.json()['detail']
    assert db.query(Product).count()==count
    assert db.get(Product,existing['id']).report_width_mm==777


def test_individual_component_count_alias_is_not_an_invented_unit_conversion():
    from types import SimpleNamespace
    from app.services.shared_product_master import _same_role
    attrs=dict(supply_mode='corrugated_production',is_composite=False,is_virtual_composite_parent=False,
        is_internal_component=True,pieces_per_box=1,splice_mode='single')
    assert _same_role([SimpleNamespace(**attrs,unit='只'),SimpleNamespace(**attrs,unit='片')])
    assert not _same_role([SimpleNamespace(**attrs,unit='张'),SimpleNamespace(**attrs,unit='片')])


def test_existing_sku_customer_outside_catalog_is_not_silently_given_master_sync(reservation_db):
    db,data=reservation_db;target,item,lot=setup_pair(db,data);activate(db,data,target,lot)
    third=Customer(customer_number=7023,customer_code='NEW-SUB',name='新子公司');db.add(third);db.commit()
    values=args(data);values['customer_id']=third.id
    plan=service.preview(db,actor=data['admin'],**values)
    assert not plan['actions'] and '范围外' in plan['blocked'][0]['reason']
    assert not db.get(CustomerSharingMember,third.id)


def test_shared_bom_master_change_keeps_frozen_reservations_and_consumption(composite_requisition_app,_p181_published_map_identity):
    from tests.test_shared_bom_stock import source_lots,activate as activate_bom
    from app.models.user import User
    from app.models.warehouse_inventory import InventoryReservation
    from app.models.shared_finished_stock import SharedFinishedReservation
    from app.services.bom_auto_reservation import reserve_new_order_stock
    from app.services import shared_bom_stock as bom,shared_finished_stock as shared
    app,factory=composite_requisition_app;seed_pair(factory)
    with factory() as db:
        admin=db.get(User,1);lots=source_lots(db)
        gids=[activate_bom(db,[2,5],[lots[0].id]),activate_bom(db,[3,6],[lots[1].id]),activate_bom(db,[1,4])]
        for gid in gids:service.activate_master(db,gid,admin)
        reserve_new_order_stock(db,order_item_id=1,operator_id=1);db.commit()
        reservation=db.scalar(select(InventoryReservation).where(InventoryReservation.inventory_lot_id==lots[0].id))
        fact=db.get(SharedFinishedReservation,reservation.id);expected=fact.product_basis_json
        evidence=bom.consumed_evidence(db,lots[0],2,1,expected)
        edit(db,{'admin':admin},db.get(Product,2),{'production_notes':'新批生产说明'});db.commit()
        assert bom.reservation_matches(db,reservation,lots[0],2,1,expected)
        assert not bom.reservation_matches(db,reservation,lots[0],2,1,shared.product_basis(db.get(Product,2)))
        assert bom.verify_consumed(db,lots[0],evidence,2,1)
        assert shared.match(db,lots[0],product_id=2,customer_id=1) is None
