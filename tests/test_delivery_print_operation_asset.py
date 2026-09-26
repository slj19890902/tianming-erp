from pathlib import Path

from fastapi.testclient import TestClient


def test_print_operation_key_is_served_by_real_application():
    from app.main import create_app
    # No lifespan/database initialization is needed to exercise static routes.
    client = TestClient(create_app())
    try:
        response = client.get('/operation-key.js')
        assert response.status_code == 200
        assert response.content == (Path(__file__).resolve().parents[1] / 'static/operation-key.js').read_bytes()
        assert 'TmOperationKey' in response.text
        for route in ['/delivery-print.html', '/delivery-print-designer.html']:
            page = client.get(route)
            assert page.status_code == 200
            assert 'src="/operation-key.js"' in page.text
    finally:
        client.close()
