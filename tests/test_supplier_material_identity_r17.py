from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from tests.test_phase11_requisition import _login, requisition_app
from tests.test_p1_81_receipt_purpose_flow import (
    _create_frozen_sources,
    _p181_published_map_identity,
    _receive,
    _seed_material_and_staging,
)


@pytest.mark.parametrize("material_code", ["A+A", "KAKAK-AB", "UAT-KA-AB"])
def test_new_purchase_keeps_master_material_identity_for_normal_receipt(
    requisition_app, material_code
):
    from app.models.material import Material
    from app.models.supplier_requisition_order import SupplierRequisitionOrderItem

    app, session_factory = requisition_app
    material_id = _seed_material_and_staging(session_factory)
    with session_factory() as session:
        session.get(Material, material_id).code = material_code
        session.commit()

    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client, session_factory, order_quantity=10, purchase_total=10,
            order_purpose=10, stock_purpose=0,
        )[0]
        with session_factory() as session:
            saved = session.get(SupplierRequisitionOrderItem, source.supplier_item_id)
            assert saved.material_id == material_id
            assert saved.material_code_snapshot == material_code

        _login(client, "workshop")
        response = client.put(
            "/api/requisition/purchase-sources/"
            + quote(source.source_key, safe="") + "/receipt-facts/auto",
            json={
                "actual_material_id": material_id,
                "purchase_purpose_source_snapshot_id": source.purpose_snapshot_id,
                "purpose_snapshot_version": source.purpose_snapshot_version,
                "receipt_plan_fingerprint": source.receipt_plan_fingerprint,
                "expected_source_version": source.source_version,
                "expected_latest_receipt_fact_version": 0,
                "idempotency_key": "r17-master-code-normal-receipt",
            },
        )
        assert response.status_code == 200, response.text
        fact = response.json()
        assert fact["actual_material_id"] == material_id
        assert fact["unit_price"] == "99.9900"
        received = _receive(client, source, fact, quantity=10,
                            idempotency_key="r17-material-identity-receive")
        assert received.status_code == 200, received.text
        replay = _receive(client, source, fact, quantity=10,
                          idempotency_key="r17-material-identity-receive")
        assert replay.status_code == 200, replay.text
        assert replay.json()["material_status"] == "received"


def test_snapshot_cleanup_remains_only_for_unbound_legacy_source(requisition_app):
    from app.api.requisition import _supplier_item_snapshot_values
    from app.models.order import OrderItem

    _, session_factory = requisition_app
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        item.material_id = None
        item.snapshot_material = "KAKAK / AB"
        item.layer_count = 5
        result = _supplier_item_snapshot_values(
            session, item, fallback_material_id=None,
            fallback_supplier_name=None, fallback_layer_count=5,
            fallback_flute_type="AB",
        )
        assert result["material_code_snapshot"] == "KAKAK"
        assert result["material_id"] is None
