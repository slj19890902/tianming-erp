from fastapi.testclient import TestClient
from tests.test_p1_40a_packaging_masterdata import p1_40a_app, _login, _other_packaging_payload


def test_external_finished_stale_factory_process_does_not_require_mold(p1_40a_app):
    payload = _other_packaging_payload(p1_40a_app.state.fixture)
    payload.update(production_process="模切,粘合", mold_tool_id=None,
                   production_label_enabled=True, production_label_units_per_label=50,
                   printing_plate_mode="plate", printing_plate_1_id=None)
    with TestClient(p1_40a_app) as client:
        _login(client)
        response = client.post('/api/master/products', json=payload)
        assert response.status_code == 201, response.text
        saved = response.json()
        assert saved['supply_mode'] == 'external_purchase'
        assert saved['mold_tool_id'] is None
        assert not saved['production_process']
        assert saved['production_label_units_per_label'] == 50
        response = client.get(f"/api/master/products/{saved['id']}")
        assert response.status_code == 200, response.text
        assert response.json()['supply_mode'] == 'external_purchase'
