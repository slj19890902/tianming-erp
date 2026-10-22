"""Optional customer identifiers must not borrow internal inventory codes."""
from datetime import date
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.models.customer import Customer
from app.models.product import Product
from app.models.order import Order, OrderItem
from app.models.master_data_object_version import MasterDataObjectVersion
from app.services.customer_document_fields import document_snapshot, decode_snapshot
from tests.test_master_data_versioning_p4_writers import writer_app
from tests.test_product_unknown_structure import body, save

URL='/api/master/products'


def customer(app):
    with app.state.session_factory() as db:
        c=Customer(customer_number=998,customer_code='EMPTYCODE',name='合成空料号客户')
        db.add(c);db.commit();return c.id


def payload(cid, **changes):
    return {'customer_id':cid,'product_code':'INTERNAL-57','product_name':'合成纸箱',
        'box_category':'normal',**changes}


@pytest.mark.parametrize('value',['',None,'omit'])
def test_create_blank_code_persists_blank_and_reads_without_internal_fallback(writer_app,value):
    cid=customer(writer_app); data=payload(cid)
    if value!='omit':data['customer_material_code']=value
    with TestClient(writer_app) as client:
        response=client.post(URL,json=data)
        assert response.status_code==201,response.text
        p=response.json();assert p['customer_material_code']==''
        assert p['product_code']=='INTERNAL-57'
        assert p['customer_document']['customer_material_code']==''
        assert client.get(f"{URL}/{p['id']}").json()['customer_material_code']==''
        rows=client.get(URL,params={'customer_id':cid,'keyword':'INTERNAL-57','response_mode':'summary'}).json()['items']
        assert len(rows)==1 and rows[0]['customer_document']['customer_material_code']==''
    with writer_app.state.session_factory() as db:
        assert db.get(Product,p['id']).customer_material_code==''


@pytest.mark.parametrize('value',['',None])
def test_explicit_clear_is_versioned_but_does_not_rewrite_frozen_document(writer_app,value):
    cid=customer(writer_app)
    with TestClient(writer_app) as client:
        p=client.post(URL,json=payload(cid,customer_material_code='000123-X')).json();pid=p['id']
        with writer_app.state.session_factory() as db:
            order=Order(order_number='FROZEN-CUSTOMER-CODE',customer_id=cid,order_date=date(2026,9,1),total_amount=1)
            db.add(order);db.flush()
            item=OrderItem(order_id=order.id,product_id=pid,quantity=1,unit_price=1,subtotal=1,
                snapshot_product_code='INTERNAL-57',snapshot_product_name='合成纸箱')
            db.add(item);db.commit();item_id=item.id;frozen=item.customer_document_snapshot_json
            assert decode_snapshot(frozen)['customer_material_code']=='000123-X'
        original=body(client,pid,customer_material_code=value)
        changed,_=save(client,pid,original)
        assert changed['version']==2 and changed['customer_material_code']==''
        assert changed['product_code']=='INTERNAL-57'
        stale=client.put(f'{URL}/{pid}',json=original)
        assert stale.status_code==409
    with writer_app.state.session_factory() as db:
        assert db.get(OrderItem,item_id).customer_document_snapshot_json==frozen
        assert document_snapshot(db.get(Product,pid))['customer_material_code']==''
        new_item=OrderItem(order_id=order.id,product_id=pid,quantity=1,unit_price=1,subtotal=1,
            snapshot_product_code='INTERNAL-57',snapshot_product_name='新冻结合成纸箱')
        db.add(new_item);db.flush()
        assert decode_snapshot(new_item.customer_document_snapshot_json)['customer_material_code']==''
        versions=db.scalars(select(MasterDataObjectVersion).where(MasterDataObjectVersion.object_type=='product',MasterDataObjectVersion.object_id==pid)).all()
        assert len(versions)==2


def test_omitted_update_keeps_real_identifier_and_internal_code_remains_required(writer_app):
    cid=customer(writer_app)
    with TestClient(writer_app) as client:
        p=client.post(URL,json=payload(cid,customer_material_code=' 000123-X ')).json()
        assert p['customer_material_code']=='000123-X'
        update=body(client,p['id'],remark='仅备注');update.pop('customer_material_code')
        changed,_=save(client,p['id'],update)
        assert changed['customer_material_code']=='000123-X'
        response=client.post(URL,json=payload(cid,product_code='',customer_material_code=''))
        assert response.status_code==422


def test_stocktake_blank_customer_code_checks_internal_identity_not_other_blank_codes(writer_app):
    cid=customer(writer_app)
    with TestClient(writer_app) as client:
        one=client.post(URL+'/stocktake-create',json=payload(cid,customer_material_code=''))
        assert one.status_code==201,one.text
        two=client.post(URL+'/stocktake-create',json=payload(cid,product_code='INTERNAL-2',product_name='另款合成',customer_material_code=''))
        assert two.status_code==201,two.text
        duplicate=client.post(URL+'/stocktake-create',json=payload(cid,product_name='不同品名',customer_material_code=''))
        assert duplicate.status_code==409


@pytest.mark.parametrize('existing',[False,True])
def test_real_editor_save_payload_does_not_invent_customer_code(tmp_path,existing):
    from tests.test_product_unknown_printing_frontend import _editor_runtime
    from tests.test_p1_46_common_box_final_layout import _run_node
    _run_node(tmp_path,_editor_runtime()+f'''
vm.productForm=vm.hydrateProductForm({{id:{'3973' if existing else 'null'},product_code:'INTERNAL-57',customer_material_code:'',print_content:'无印刷'}});
const payload=vm.buildProductWritePayload();
assert.equal(payload.product_code,'INTERNAL-57');
assert.equal(payload.customer_material_code,'');
vm.productForm.customer_material_code='000123-X';
assert.equal(vm.buildProductWritePayload().customer_material_code,'000123-X');
''')
