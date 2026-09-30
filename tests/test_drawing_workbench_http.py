"""HTTP contract checks: real router dependencies, disposable SQLite only."""
from contextlib import contextmanager
from decimal import Decimal
import json
import subprocess

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import get_current_user, get_db
from app.api.products import router as products_router
from app.models import Base
from app.models.access_control import UserCustomerScope
from app.models.customer import Customer
from app.models.product import Product
from app.models.user import User


def _fixture(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'drawing-http.sqlite3'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as db:
        first, other = Customer(name='范围客户'), Customer(name='另一客户')
        admin = User(username='admin', password_hash='x', role='admin', real_name='A')
        sales = User(username='sales', password_hash='x', role='sales', real_name='S', customer_access_mode='selected')
        finance = User(username='finance', password_hash='x', role='finance', real_name='F')
        workshop = User(username='workshop', password_hash='x', role='workshop', real_name='W', customer_access_mode='selected')
        db.add_all([first, other, admin, sales, finance, workshop]); db.flush()
        db.add(UserCustomerScope(user_id=sales.id, customer_id=first.id))
        db.add(UserCustomerScope(user_id=workshop.id, customer_id=first.id))
        products = [Product(customer_id=first.id, product_code='HTTP-1', customer_material_code='HTTP-1',
                            product_name='工作台衬板', length_mm=Decimal('100'), width_mm=Decimal('50'), height_mm=Decimal('3'), flute_type='B', version=1),
                    Product(customer_id=other.id, product_code='HTTP-2', customer_material_code='HTTP-2',
                            product_name='他客衬板', length_mm=Decimal('100'), width_mm=Decimal('50'), height_mm=Decimal('3'), flute_type='B', version=1)]
        db.add_all(products); db.commit()
        ids = {'admin': admin.id, 'sales': sales.id, 'finance': finance.id, 'workshop': workshop.id, 'one': products[0].id, 'two': products[1].id}
    return engine, factory, ids


def _app(factory, actor_id):
    app = FastAPI()
    app.include_router(products_router, prefix='/api/master/products')
    def override_db():
        with factory() as db:
            yield db
    def override_user(db: Session = Depends(get_db)):
        return db.get(User, actor_id[0])
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    return app


def test_workbench_http_roundtrip_and_frozen_exports(tmp_path, monkeypatch):
    monkeypatch.setenv('ERP_FILE_STORAGE_DIR', str(tmp_path / 'files'))
    engine, factory, ids = _fixture(tmp_path)
    actor = [ids['admin']]
    try:
        with TestClient(_app(factory, actor)) as client:
            root = f"/api/master/products/{ids['one']}/managed-drawing"
            assert client.get('/api/master/products/drawing-workbench/catalog').status_code == 200
            assert client.get(root + '/workbench-context').status_code == 200
            before = engine.connect().execute(__import__('sqlalchemy').text('select count(*) from drawing_designs')).scalar_one()
            preview = client.post(root + '/workbench-preview', json={'template_key': 'liner_v1', 'parameters': {}, 'editor_state': {'dimension_basis': 'dieline'}})
            assert preview.status_code == 200 and preview.json()['geometry']['width_mm'] == '50'
            assert engine.connect().execute(__import__('sqlalchemy').text('select count(*) from drawing_designs')).scalar_one() == before
            draft = {'expected_product_version': 1, 'template_key': 'liner_v1', 'parameters': {},
                     'editor_state': {'dimension_basis': 'dieline'}, 'idempotency_key': 'http-workbench-save-001'}
            saved = client.put(root, json=draft); assert saved.status_code == 200, saved.text
            assert client.get(root).json()['draft']['editor_state']['dimension_basis'] == 'dieline'
            published = client.post(root + '/releases', json={'expected_product_version': 1, 'expected_design_version': 1,
                                      'idempotency_key': 'http-workbench-release-001'}); assert published.status_code == 200, published.text
            release = published.json()['id']
            for fmt in ('svg', 'pdf_1to1', 'dxf'):
                response = client.get(f'{root}/releases/{release}/export', params={'format': fmt})
                assert response.status_code == 200 and response.headers['content-disposition'].startswith('attachment;')
            assert client.put(root, json=draft).status_code == 200
            changed = {**draft, 'parameters': {'length_mm': '99'}}
            assert client.put(root, json=changed).status_code == 409
            current = {**draft, 'idempotency_key': 'http-workbench-save-002', 'expected_design_version': 1}
            assert client.put(root, json=current).status_code == 200
            stale = {**draft, 'idempotency_key': 'http-workbench-save-003', 'expected_design_version': 1}
            assert client.put(root, json=stale).status_code == 409
    finally:
        engine.dispose()


def test_frontend_requests_reach_production_router_without_aliases(tmp_path, monkeypatch):
    """Use URLs emitted by the real workbench, with only production's mount."""
    monkeypatch.setenv('ERP_FILE_STORAGE_DIR', str(tmp_path / 'files'))
    engine, factory, ids = _fixture(tmp_path)
    script = r"""
const {Workbench}=require('./static/drawing-workbench.js');
const requests=[];
global.axios={get:async url=>{requests.push({method:'GET',url});return {data:{}}},
  post:async(url,payload)=>{requests.push({method:'POST',url,payload});throw {code:'ERR_CANCELED'}}};
const w=new Workbench({productId:Number(process.argv[1])});
w.mount=()=>{w.root={}};w.setStatus=()=>{};w.initState=()=>{};w.render=()=>{};w.requestPreview=()=>{};
(async()=>{await w.open();w.state={templateKey:'liner_v1',parameters:{},editorState:{dimension_basis:'dieline'}};
await w.previewNow();process.stdout.write(JSON.stringify(requests))})().catch(e=>{console.error(e);process.exitCode=1});
"""
    try:
        requests = json.loads(subprocess.check_output(['node', '-e', script, str(ids['one'])], text=True))
        assert len(requests) == 4
        with TestClient(_app(factory, [ids['admin']])) as client:
            for request in requests:
                response = client.request(request['method'], request['url'], json=request.get('payload'))
                assert response.status_code == 200, (request['url'], response.text)
    finally:
        engine.dispose()


def test_workbench_http_permission_and_customer_scope(tmp_path, monkeypatch):
    monkeypatch.setenv('ERP_FILE_STORAGE_DIR', str(tmp_path / 'files'))
    engine, factory, ids = _fixture(tmp_path); actor = [ids['finance']]
    try:
        with TestClient(_app(factory, actor)) as client:
            root = f"/api/master/products/{ids['one']}/managed-drawing"
            assert client.get(root).status_code == 403
        actor[0] = ids['workshop']
        with TestClient(_app(factory, actor)) as client:
            root = f"/api/master/products/{ids['one']}/managed-drawing"
            assert client.get(root).status_code == 200
            assert client.put(root, json={'expected_product_version': 1, 'template_key': 'liner_v1', 'parameters': {}}).status_code == 403
        actor[0] = ids['sales']
        with TestClient(_app(factory, actor)) as client:
            assert client.get(f"/api/master/products/{ids['two']}/managed-drawing").status_code == 403
            root = f"/api/master/products/{ids['one']}/managed-drawing"
            assert client.get(root).status_code == 200
    finally:
        engine.dispose()
