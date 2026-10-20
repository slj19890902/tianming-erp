"""Signed SPA hosting and camera permission must match deployed mobile routes."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.main import app, HSTSMiddleware


def test_mobile_entry_keeps_legacy_identity_context_and_new_default():
    with TestClient(app, follow_redirects=False) as client:
        for path in ('/mobile', '/mobile/'):
            response = client.get(path)
            assert response.status_code == 307
            assert response.headers['location'] == '/frontend-v2/mobile'
        old = client.get('/mobile/?mobile_page=production&task_id=7')
        assert old.status_code == 200
        assert 'productionPage' in old.text
        assert client.get('/mobile/erp.html').status_code == 200
        for path in ('/frontend-v2/mobile', '/frontend-v2/mobile/query', '/frontend-v2/mobile/warehouse'):
            response = client.get(path)
            assert response.status_code == 200
            assert '/frontend-v2/assets/' in response.text
        assert client.get('/frontend-v2/assets/missing.js').status_code == 404


def test_camera_is_self_only_on_spa_and_scan():
    web = FastAPI()
    web.add_middleware(HSTSMiddleware)
    @web.get('/{path:path}')
    def document(path: str):
        return {'ok': True}
    with TestClient(web) as client:
        for path in ('/frontend-v2/', '/frontend-v2/mobile/query', '/mobile/scan'):
            assert client.get(path).headers['permissions-policy'].startswith('camera=(self)')
        assert client.get('/api/mobile/erp/shell').headers['permissions-policy'].startswith('camera=()')


def test_supplier_order_pending_detail_uses_exact_visible_source(monkeypatch):
    from fastapi import Response, HTTPException
    from app.api import mobile_erp
    from app.models.user import User
    import pytest
    user = User(id=77, username="mobile-detail-test", role="admin", real_name="test", is_active=True)
    row = {"item_id": "so642", "order_item_id": None, "supplier_order_item_id": 642}
    def visible_routes(db, principal):
        assert principal is user
        return [row]
    def rows(db, *, user, selected_pending_routes):
        assert selected_pending_routes == [row]
        return [row]
    monkeypatch.setattr(mobile_erp, "_pending_incoming_route_rows", visible_routes)
    monkeypatch.setattr(mobile_erp, "_incoming_rows", rows)
    result = mobile_erp.pending_incoming_production_detail("so642", Response(), db=None, user=user)
    assert result["incoming_item"]["supplier_order_item_id"] == 642
    assert result["read_only"] is True
    with pytest.raises(HTTPException) as denied:
        mobile_erp.pending_incoming_production_detail("so643", Response(), db=None, user=user)
    assert denied.value.status_code == 404
    with pytest.raises(HTTPException) as invalid:
        mobile_erp.pending_incoming_production_detail("so642/other", Response(), db=None, user=user)
    assert invalid.value.status_code == 422
