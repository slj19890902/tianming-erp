from types import SimpleNamespace as NS
import pytest
from fastapi import HTTPException
from app.services.order_create_readback import capture_source_lines, attach_frozen_proof, attach_replay_evidence
from tests.test_phase5_orders import order_api_app, _login


@pytest.fixture(autouse=True)
def fictional_document_evidence(tmp_path, monkeypatch):
    # Let conftest establish its disposable application settings before binding
    # the separate document fixture. This does not change database settings.
    from app.api import orders
    from app.services.customer_document_fields import review_entries
    (tmp_path/'customer-document-review.json').write_text('{"entries":[]}', encoding='utf-8')
    monkeypatch.setenv('ERP_UAT_ROOT', str(tmp_path))
    review_entries.cache_clear()
    yield
    review_entries.cache_clear()


def fixture():
    product = NS(id=1, customer_id=2, version=3, supply_mode="corrugated_production", production_notes=" default ")
    line = NS(client_line_id="line-1", product_id=1, product_expected_version=3, production_notes="")
    payload = NS(readback_contract="a01-v1", idempotency_key="test-key-1", items=[line])
    response = dict(id=9, customer_id=2, items=[dict(id=10, product_id=1, item_sequence=1, client_line_id="line-1", quantity=4)])
    return payload, {1: product}, response


def test_proof_freezes_default_version_and_preserves_reordered_identities():
    payload, products, response = fixture()
    source = capture_source_lines(payload, products)
    products[1].production_notes = "changed"
    products[1].version = 4
    attach_frozen_proof(response, source)
    assert response["create_readback"]["lines"][0]["default_production_notes"] == "default"
    assert response["create_readback"]["lines"][0]["product_version"] == 3
    current = dict(id=9, customer_id=2, items=[dict(id=10)])
    attach_replay_evidence(current, response)
    assert current["items"][0]["client_line_id"] == "line-1"
    assert current["create_readback"] == response["create_readback"]


@pytest.mark.parametrize("mutation,status", [("version",409),("missing_version",422),("missing_key",422),("foreign_product",422),("missing_line",422)])
def test_invalid_source_fails_before_persist(mutation, status):
    payload, products, _ = fixture()
    if mutation == "version": products[1].version = 4
    elif mutation == "missing_version": payload.items[0].product_expected_version = None
    elif mutation == "missing_key": payload.idempotency_key = None
    elif mutation == "foreign_product": payload.items[0].product_id = 99
    else: payload.items[0].client_line_id = ""
    with pytest.raises(HTTPException) as error: capture_source_lines(payload, products)
    assert error.value.status_code == status


def test_old_callers_keep_existing_contract():
    payload, _, _ = fixture()
    payload.readback_contract = None
    assert capture_source_lines(payload, {}) is None


def test_history_without_proof_is_never_backfilled():
    with pytest.raises(HTTPException) as error:
        attach_replay_evidence(dict(id=9, customer_id=2), dict(id=9))
    assert error.value.status_code == 409


def test_external_snapshot_has_no_production_notes():
    payload, products, response = fixture()
    products[1].supply_mode = "external_purchase"
    attach_frozen_proof(response, capture_source_lines(payload, products))
    assert response["create_readback"]["lines"][0]["expected_production_notes"] is None


def test_replay_cannot_bind_another_order():
    payload, products, response = fixture()
    attach_frozen_proof(response, capture_source_lines(payload, products))
    with pytest.raises(HTTPException):
        attach_replay_evidence(dict(id=99, customer_id=2, items=[]), response)


def api_payload(factory):
    from app.models.product import Product
    with factory() as db:
        product = db.get(Product, 1)
        product.unit = "只"
        db.commit()
        version = product.version
    return dict(idempotency_key="test-create-proof-transaction", readback_contract="a01-v1",
        customer_id=1, customer_po="FICTIONAL-PROOF-TRANSACTION", order_date="2026-10-06",
        items=[dict(product_id=1, product_expected_version=version, client_line_id="stable-line", quantity=4, unit_price="3.60")])


def test_real_transaction_proof_failure_rolls_back_order_and_replay(order_api_app, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy import select, func
    from app.models.order import Order, OrderItem
    from app.models.audit import OperationLog
    import app.services.order_create_readback as service
    app, factory = order_api_app
    from app.core.config import settings
    app.state.erp_settings = settings
    payload = api_payload(factory)
    with TestClient(app) as client:
        _login(client)
        def fail(*args): raise HTTPException(409, "虚构测试注入保存证明失败")
        with monkeypatch.context() as patch:
            patch.setattr(service, "attach_frozen_proof", fail)
            assert client.post('/api/orders', json=payload).status_code == 409
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(Order)) == 0
            assert db.scalar(select(func.count()).select_from(OrderItem)) == 0
            assert db.scalar(select(func.count()).select_from(OperationLog).where(OperationLog.action == "order_create_replay")) == 0
        created = client.post('/api/orders', json=payload)
        assert created.status_code == 201, created.text
        assert client.post('/api/orders', json=payload).json()['id'] == created.json()['id']


def test_real_get_replay_keeps_original_default_after_master_changes(order_api_app):
    from fastapi.testclient import TestClient
    from app.models.product import Product
    app, factory = order_api_app
    from app.core.config import settings
    app.state.erp_settings = settings
    payload = api_payload(factory)
    with TestClient(app) as client:
        _login(client)
        created = client.post('/api/orders', json=payload)
        assert created.status_code == 201, created.text
        original = created.json()['create_readback']
        with factory() as db:
            product = db.get(Product, 1)
            product.production_notes = '虚构测试变更主档，不覆盖订单冻结说明'
            product.version += 1
            db.commit()
        saved = client.get('/api/orders/'+str(created.json()['id']), params={'create_key':payload['idempotency_key']})
        assert saved.status_code == 200, saved.text
        assert saved.json()['create_readback'] == original
        assert client.post('/api/orders', json=payload).json()['create_readback'] == original


def test_legacy_digest_matches_v548_body_without_new_fields():
    import hashlib,json
    from app.api.orders import OrderCreate, _order_create_identity
    payload = OrderCreate(idempotency_key='legacy-request-key',customer_id=1,items=[dict(product_id=1,quantity=4,unit_price='3.60')])
    historical = payload.model_dump(mode='json',exclude={'idempotency_key','mold_repair_confirmation_token','readback_contract'})
    for row in historical['items']: row.pop('product_expected_version')
    digest = hashlib.sha256(json.dumps(historical,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    assert _order_create_identity(payload,1)[1] == digest
