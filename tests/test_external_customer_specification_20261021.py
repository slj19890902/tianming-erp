"""External customer wording survives the real versioned common-box save API."""
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_p1_40a_packaging_masterdata import (
    p1_40a_app, _login, _external_payload, _honeycomb_payload, _other_packaging_payload,
)


def test_external_customer_specification_editor_and_save_payload():
    root = Path(__file__).resolve().parents[1]
    node = shutil.which('node')
    assert node
    result = subprocess.run([node, str(root / 'tests/external_customer_specification.cjs'), str(root)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('payload_builder', [_external_payload, _honeycomb_payload, _other_packaging_payload])
def test_external_customer_spec_save_reopen_blank_follow_and_stale_version(p1_40a_app, payload_builder):
    from app.models.product import Product
    from app.services.product_specification import delivery_specification
    with TestClient(p1_40a_app) as client:
        _login(client)
        payload = payload_builder(p1_40a_app.state.fixture)
        created = client.post('/api/master/products', json=payload)
        assert created.status_code == 201, created.text
        before = created.json()
        url = f"/api/master/products/{before['id']}"
        version = before['version']
        for wording in ('客户规格 0800×0180×60mm / 10片一包', '', None):
            update = dict(payload, expected_version=version, customer_specification=wording)
            preview = client.post(url + '/update-preview', json=update)
            assert preview.status_code == 200, preview.text
            update['confirmation_token'] = preview.json()['confirmation_token']
            saved = client.put(url, json=update)
            assert saved.status_code == 200, saved.text
            reopened = client.get(url).json()
            assert reopened['customer_specification'] == wording
            assert reopened['external_supply']['specification'] == before['external_supply']['specification']
            assert reopened['external_packaging_default_purchase_quantity_basis'] == before['external_packaging_default_purchase_quantity_basis']
            with p1_40a_app.state.factory() as db:
                product = db.get(Product, before['id'])
                assert delivery_specification(product) == (wording if wording is not None else product.external_packaging_specification_summary)
            # An old editor cannot overwrite this save.
            assert client.put(url, json=update).status_code == 409
            version = reopened['version']
