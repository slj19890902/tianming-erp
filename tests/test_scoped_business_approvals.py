from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from test_n028_customer_scopes import n028_customer_scope_app, _login
from test_stock_replenishment_flow import stock_replenishment_app, _semi_policy_payload
from test_fin001_invoice_tasks import fin001_app, _complete_invoice_profile

def install_request_profile(app,factory,username,customer_id=1):
    from app.api.business_approvals import router
    from app.services.business_visibility import BusinessVisibilityMiddleware
    from app.services.scoped_business_profile import overrides
    from app.models.user import User
    from app.models.access_control import UserPermissionOverride,UserCustomerScope
    from app.core.security import hash_password
    app.include_router(router,prefix='/api/business-approvals');app.add_middleware(BusinessVisibilityMiddleware)
    with factory() as db:
        user=db.scalar(select(User).where(User.username==username))
        if not user:
            user=User(username=username,password_hash=hash_password('RolePass123!'),role='sales',real_name='申请人',must_change_password=False)
            db.add(user);db.flush()
        user.customer_access_mode='selected'
        db.add(UserCustomerScope(user_id=user.id,customer_id=customer_id))
        for code,allowed in overrides().items():db.add(UserPermissionOverride(user_id=user.id,permission_code=code,is_allowed=allowed))
        db.commit()

@pytest.fixture()
def configured(n028_customer_scope_app):
    from app.api.business_approvals import router
    from app.services.business_visibility import BusinessVisibilityMiddleware
    from app.services.scoped_business_profile import overrides
    from app.models.user import User
    from app.models.access_control import UserPermissionOverride
    app,ids,factory=n028_customer_scope_app
    app.include_router(router,prefix="/api/business-approvals")
    app.add_middleware(BusinessVisibilityMiddleware)
    with factory() as db:
        user=db.scalar(select(User).where(User.username=="n028-sales"))
        for row in list(user.permission_overrides):db.delete(row)
        db.flush()
        for code,allowed in overrides().items():db.add(UserPermissionOverride(user_id=user.id,permission_code=code,is_allowed=allowed))
        db.commit()
    with TestClient(app) as client:
        _login(client,"n028-sales","SalesPass123!")
        yield client,ids,factory

def draft(client,ids,**extra):
    body={"action":"customer_update","customer_id":ids['customer'],"target_id":ids['customer'],"payload":{"phone":"12345","expected_version":1},"idempotency_key":"approval-test-0001"}
    body.update(extra)
    return client.post('/api/business-approvals',json=body)

def test_scope_prepare_and_stock_candidates(configured):
    client,ids,_=configured
    response=client.get('/api/business-approvals/options?kind=customers')
    assert [r['id'] for r in response.json()['items']]==[ids['customer']]
    assert client.get(f"/api/business-approvals/prepare?action=customer_update&customer_id={ids['other_customer']}&target_id={ids['other_customer']}").status_code==403
    assert client.get(f"/api/warehouse/finished/products/{ids['product']}/candidates?customer_id={ids['customer']}").status_code==200
    r=client.post(f"/api/warehouse/semi-finished/products/{ids['product']}/candidates",json={"customer_id":ids['customer'],"board_length_mm":500,"board_width_mm":300,"material_code":"ABC","flute_type":"AB","component_type":"whole","pieces_per_box":1,"stock_yield_per_sheet":1})
    assert r.status_code==200,r.text
    assert client.get(f"/api/master/products/{ids['other_product']}").status_code==403
    assert client.put(f"/api/master/customers/{ids['customer']}",json={}).status_code==403

def test_request_approval_and_replay(configured):
    from app.models.customer import Customer
    from app.models.business_approval import BusinessApproval
    client,ids,factory=configured
    r=draft(client,ids);assert r.status_code==201,r.text
    row=r.json()
    assert draft(client,ids).json()['id']==row['id']
    assert draft(client,ids,payload={'phone':'other','expected_version':1}).status_code==409
    with factory() as db:assert db.get(Customer,ids['customer']).phone!= '12345'
    assert client.post(f"/api/business-approvals/{row['id']}/review",json={'approve':True,'expected_version':1}).status_code==403
    _login(client,'n028-admin','AdminPass123!')
    r=client.post(f"/api/business-approvals/{row['id']}/review",json={'approve':True,'expected_version':1})
    assert r.status_code==200,r.text
    assert r.json()['status']=='applied'
    assert client.post(f"/api/business-approvals/{row['id']}/review",json={'approve':True,'expected_version':1}).status_code==200
    with factory() as db:
        assert db.get(Customer,ids['customer']).phone=='12345'
        assert db.scalar(select(func.count()).select_from(BusinessApproval))==1
    _login(client,'n028-sales','SalesPass123!')
    assert draft(client,ids).json()['id']==row['id']


def test_critical_master_approval_preserves_confirmation_gate(configured):
    from app.models.customer import Customer
    from app.models.business_approval import BusinessApproval
    client,ids,factory=configured
    row=draft(client,ids,payload={'name':'审批确认后客户名称','expected_version':1}).json()
    _login(client,'n028-admin','AdminPass123!')
    path=f"/api/business-approvals/{row['id']}/review"
    response=client.post(path,json={'approve':True,'expected_version':1})
    assert response.status_code==409,response.text
    detail=response.json()['detail']
    assert detail['code']=='MASTER_CHANGE_CONFIRMATION_REQUIRED'
    with factory() as db:
        assert db.get(Customer,ids['customer']).name!='审批确认后客户名称'
        request=db.get(BusinessApproval,row['id'])
        assert request.status=='pending' and request.version==1
    response=client.post(path,json={'approve':True,'expected_version':1,'confirmation_token':detail['confirmation_token']})
    assert response.status_code==200,response.text
    with factory() as db:
        assert db.get(Customer,ids['customer']).name=='审批确认后客户名称'
        assert db.get(BusinessApproval,row['id']).status=='applied'

def test_reject_withdraw_stale_and_scope_revocation(configured):
    from app.models.customer import Customer
    from app.models.access_control import UserCustomerScope
    client,ids,factory=configured
    row=draft(client,ids).json()
    _login(client,'n028-admin','AdminPass123!')
    r=client.post(f"/api/business-approvals/{row['id']}/review",json={'approve':False,'expected_version':1,'note':'资料待核对'})
    assert r.status_code==200,r.text
    with factory() as db:assert db.get(Customer,ids['customer']).phone!='12345'
    _login(client,'n028-sales','SalesPass123!')
    row=draft(client,ids,idempotency_key='approval-test-0002').json()
    with factory() as db:
        db.get(Customer,ids['customer']).phone='changed';db.commit()
    _login(client,'n028-admin','AdminPass123!')
    assert client.post(f"/api/business-approvals/{row['id']}/review",json={'approve':True,'expected_version':1}).status_code==409
    with factory() as db:
        for scope in db.scalars(select(UserCustomerScope)).all():db.delete(scope)
        db.commit()
    assert client.post(f"/api/business-approvals/{row['id']}/review",json={'approve':True,'expected_version':1}).status_code==403

def test_approval_audit_failure_rolls_back_business_write(configured,monkeypatch):
    from app.models.customer import Customer
    from app.models.business_approval import BusinessApproval
    from app.services import business_approvals
    client,ids,factory=configured
    row=draft(client,ids).json();_login(client,'n028-admin','AdminPass123!')
    def fail(*a,**kw):raise RuntimeError('injected audit failure')
    monkeypatch.setattr(business_approvals,'audit',fail)
    with pytest.raises(RuntimeError,match='injected'):
        client.post(f"/api/business-approvals/{row['id']}/review",json={'approve':True,'expected_version':1})
    with factory() as db:
        assert db.get(Customer,ids['customer']).phone!='12345'
        approval=db.get(BusinessApproval,row['id']);assert approval.status=='pending' and approval.version==1

def test_cost_redaction_retains_sales_prices():
    from app.services.business_visibility import public_business_payload
    data={'unit_price':'25.589','amount':'767.67','cost_unit_price':10,'snapshot_json':'{"board_price":12,"quantity":5}','nested':{'gross_profit':40,'purchase_price':12}}
    result=public_business_payload(data)
    assert result['unit_price']=='25.589' and result['amount']=='767.67'
    assert 'cost_unit_price' not in result and result['nested']=={}
    assert 'board_price' not in result['snapshot_json']
    assert 'unit_price' not in public_business_payload(data,purchase=True)

def test_concurrent_approval_changes_once_and_boss_cannot_edit_master(configured):
    from concurrent.futures import ThreadPoolExecutor
    from app.models.customer import Customer
    from app.models.business_approval import BusinessApproval
    client,ids,factory=configured
    row=draft(client,ids).json()
    _login(client,'n028-boss','BossPass123!')
    assert client.post(f"/api/business-approvals/{row['id']}/review",json={'approve':True,'expected_version':1}).status_code==403
    _login(client,'n028-admin','AdminPass123!')
    def approve(_):
        return client.post(f"/api/business-approvals/{row['id']}/review",json={'approve':True,'expected_version':1})
    with ThreadPoolExecutor(max_workers=2) as pool:responses=list(pool.map(approve,range(2)))
    assert any(r.status_code==200 for r in responses)
    assert all(r.status_code in {200,409} for r in responses),[r.text for r in responses]
    with factory() as db:
        assert db.get(Customer,ids['customer']).version==2
        assert db.get(BusinessApproval,row['id']).version==2

def test_stock_alert_direct_edit_scoped_and_no_direct_requisition(configured):
    client,ids,_=configured
    r=client.put(f"/api/requisition/stock-policies/finished-products/{ids['product']}",json={'warning_quantity':30,'target_quantity':100})
    assert r.status_code==200,r.text
    assert r.json()['warning_quantity']==30
    assert client.put(f"/api/requisition/stock-policies/finished-products/{ids['other_product']}",json={'warning_quantity':30,'target_quantity':100}).status_code==403
    assert client.post('/api/requisition/stock-replenishment/orders',json={}).status_code in {403,404,405}

def test_product_approval_preserves_hidden_prices(configured):
    from app.models.product import Product
    client,ids,factory=configured
    r=draft(client,ids,action='product_update',target_id=ids['product'],payload={'product_name':'更改名称','expected_version':1})
    assert r.status_code==201,r.text
    assert 'cost_unit_price' not in r.text
    row=r.json();_login(client,'n028-admin','AdminPass123!')
    r=client.post(f"/api/business-approvals/{row['id']}/review",json={'approve':True,'expected_version':1})
    assert r.status_code==200,r.text
    with factory() as db:
        product=db.get(Product,ids['product'])
        assert product.product_name=='更改名称'
        assert product.cost_unit_price==Decimal('2') and product.board_price==Decimal('3') and product.suggested_price==Decimal('4')

def test_scoped_new_order_direct_save_and_delivery_approval(configured):
    from datetime import date
    from test_n028_customer_scopes import _seed_inventory_cost_recipe
    from app.models.warehouse_inventory import WarehouseLocation
    from app.services.warehouse_inventory import manual_finished_in,reserve_finished_inventory
    from app.api.deliveries import router
    from app.models.order import OrderItem
    from app.models.delivery import Delivery
    client,ids,factory=configured
    client.app.include_router(router,prefix='/api/deliveries')
    with factory() as db:
        _seed_inventory_cost_recipe(db);db.commit()
    body={'customer_id':ids['customer'],'customer_po':'NEW-SCOPED-ORDER','items':[{'product_id':ids['product'],'quantity':10,'unit_price':'8'}]}
    r=client.post('/api/orders',json=body);assert r.status_code==201,r.text
    with factory() as db:
        oi=db.scalar(select(OrderItem).order_by(OrderItem.id.desc()));item_id=oi.id
        location=WarehouseLocation(location_code='APPROVAL-STOCK',location_name='审批测试货位',warehouse_type='finished')
        db.add(location);db.flush()
        lot=manual_finished_in(db,customer_id=ids['customer'],product_id=ids['product'],location_id=location.id,
            quantity=10,stock_date=date.today(),source_type='manual',remarks=None,operator_id=None,idempotency_key='approval-stock')
        reserve_finished_inventory(db,order_item_id=item_id,inventory_lot_id=lot.id,quantity=10,expected_version=lot.version,
            operator_id=None,idempotency_key='approval-reserve',warning_acknowledged_codes=[])
        db.commit()
    p={'customer_id':ids['customer'],'items':[{'order_item_id':item_id,'delivered_quantity':5}]}
    assert client.post('/api/deliveries',json=p).status_code==403
    request=client.post('/api/business-approvals',json={'action':'delivery_create','customer_id':ids['customer'],'payload':p,'idempotency_key':'delivery-request-01'})
    assert request.status_code==201,request.text
    with factory() as db:assert db.scalar(select(func.count()).select_from(Delivery))==0
    _login(client,'n028-admin','AdminPass123!')
    reviewed=client.post(f"/api/business-approvals/{request.json()['id']}/review",json={'approve':True,'expected_version':1})
    assert reviewed.status_code==200,reviewed.text
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Delivery))==1
        assert db.get(OrderItem,item_id).delivered_quantity==0

def test_replenishment_request_executes_only_after_admin_review(stock_replenishment_app):
    from app.models.stock_replenishment import StockReplenishmentOrder
    app,factory=stock_replenishment_app
    install_request_profile(app,factory,'approval-sales')
    with TestClient(app) as client:
        _login(client,'admin','RolePass123!')
        p=client.post('/api/requisition/stock-policies',json={'policy_name':'库存补充','target_inventory_type':'finished','product_id':1,'customer_id':1,'warning_quantity':10,'target_quantity':50,'default_location_id':1});assert p.status_code==201,p.text
        _login(client,'approval-sales','RolePass123!')
        d=client.get(f"/api/requisition/stock-policies/{p.json()['id']}/replenishment-draft").json()
        d['idempotency_key']='approval-replenishment-1'
        r=client.post('/api/business-approvals',json={'action':'stock_replenishment','customer_id':1,'payload':d,'idempotency_key':'request-replenishment-1'})
        assert r.status_code==201,r.text
        with factory() as db:assert db.scalar(select(func.count()).select_from(StockReplenishmentOrder))==0
        _login(client,'admin','RolePass123!')
        reviewed=client.post(f"/api/business-approvals/{r.json()['id']}/review",json={'approve':True,'expected_version':1})
        assert reviewed.status_code==200,reviewed.text
        with factory() as db:
            rows=db.scalars(select(StockReplenishmentOrder)).all();assert len(rows)==1 and rows[0].status=='draft'

def test_statement_confirmation_and_invoice_request(fin001_app):
    from app.models.finance import Statement
    from app.models.invoice_task import FinanceInvoiceTask
    app,factory=fin001_app
    install_request_profile(app,factory,'fin001-sales')
    with TestClient(app) as client:
        _login(client,'fin001-admin','RolePass123!');_complete_invoice_profile(client)
        _login(client,'fin001-sales','RolePass123!')
        r=client.post('/api/business-approvals',json={'action':'statement_confirm','customer_id':1,'target_id':1,'payload':{'expected_version':1},'idempotency_key':'statement-confirm-1'})
        assert r.status_code==201,r.text
        assert client.post('/api/finance/statements/1/confirm',json={'expected_version':1}).status_code==403
        _login(client,'fin001-admin','RolePass123!')
        result=client.post(f"/api/business-approvals/{r.json()['id']}/review",json={'approve':True,'expected_version':1})
        assert result.status_code==200,result.text
        with factory() as db:assert db.get(Statement,1).confirmation_status=='confirmed'
        _login(client,'fin001-sales','RolePass123!')
        r=client.post('/api/business-approvals',json={'action':'invoice_task_create','customer_id':1,'target_id':1,'payload':{'expected_version':2,'idempotency_key':'invoice-task-approval-1'},'idempotency_key':'invoice-request-1'})
        assert r.status_code==201,r.text
        with factory() as db:assert db.scalar(select(func.count()).select_from(FinanceInvoiceTask))==0
        _login(client,'fin001-admin','RolePass123!')
        result=client.post(f"/api/business-approvals/{r.json()['id']}/review",json={'approve':True,'expected_version':1})
        assert result.status_code==200,result.text
        with factory() as db:assert db.scalar(select(func.count()).select_from(FinanceInvoiceTask))==1

def test_statement_create_request(fin001_app):
    from app.models.finance import Statement,StatementItem
    from sqlalchemy import delete
    app,factory=fin001_app
    # Leave the isolated fixture's delivery/receipt unbilled.
    with factory() as db:
        db.execute(delete(StatementItem));db.execute(delete(Statement));db.commit()
    install_request_profile(app,factory,'fin001-sales')
    with TestClient(app) as client:
        _login(client,'fin001-sales','RolePass123!')
        r=client.post('/api/business-approvals',json={'action':'statement_create','customer_id':1,'payload':{'customer_id':1,'statement_month':'2026-08','delivery_ids':[1],'idempotency_key':'statement-build-001'},'idempotency_key':'statement-request-001'})
        assert r.status_code==201,r.text
        with factory() as db:assert db.scalar(select(func.count()).select_from(Statement))==0
        _login(client,'fin001-admin','RolePass123!')
        reviewed=client.post(f"/api/business-approvals/{r.json()['id']}/review",json={'approve':True,'expected_version':1})
        assert reviewed.status_code==200,reviewed.text
        with factory() as db:assert db.scalar(select(func.count()).select_from(Statement))==1

