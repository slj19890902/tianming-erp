from fastapi.testclient import TestClient

from tests.test_master_data_versioning_p4_writers import writer_app, _create_customer, _create_product, _product_payload
from app.services.sheet_cutting_settings import SheetCuttingSettings


def test_product_api_keeps_theoretical_size_and_independent_cutting_for_a1(writer_app):
    settings = {'schema_version': 2, 'whole': SheetCuttingSettings(2, 2, 1, False).to_dict()}
    with TestClient(writer_app) as client:
        customer = _create_customer(client, 'CUT', 181)
        product = _create_product(client, customer['id'], 'CUT', box_style='A1普通箱',
            report_length_mm=340, report_width_mm=200, sheet_cutting_settings=settings)
        assert product['sheet_cutting_settings'] == settings
        assert product['default_cutting_mode'] == '一开四'
        assert float(product['report_length_mm']) == 340
        payload = _product_payload(customer['id'], 'CUT', box_style='A1普通箱',
            report_length_mm=340, report_width_mm=200, expected_version=product['version'],
            change_reason='只修改备注，保留独立开料设置', remark='保留开料')
        response = client.put(f"/api/master/products/{product['id']}", json=payload)
        assert response.status_code == 200, response.text
        assert response.json()['sheet_cutting_settings'] == settings
        assert response.json()['default_cutting_mode'] == '一开四'
        payload.update(expected_version=response.json()['version'], sheet_cutting_settings=None)
        refused = client.put(f"/api/master/products/{product['id']}", json=payload)
        assert refused.status_code == 409, refused.text
