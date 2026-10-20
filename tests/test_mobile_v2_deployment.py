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
