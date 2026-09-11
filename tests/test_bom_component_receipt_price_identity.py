from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.test_p1_13c_bom_a3_physical_sources import (
    a3_surround_app, _login, _source_payload, _mark_material_requisition_items_as_legacy,
)


@pytest.mark.parametrize("missing_child_material", [False, True])
def test_component_receipt_never_borrows_parent_material_price(a3_surround_app, missing_child_material):
    from app.models.material import Material
    from app.models.order import OrderItem
    from app.models.product_bom import SalesOrderItemBomComponent
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact
    from app.models.incoming_receipt import IncomingReceiptItem
    app, factory = a3_surround_app
    with factory() as db:
        parent_material = Material(code="PARENT-ONLY", supplier_name="匿名供应商",
            quote_price=Decimal("9"), price_unit="元/平方米", purchase_currency="CNY",
            purchase_tax_included=True, purchase_tax_rate=Decimal("0.13"), is_active=True)
        db.add(parent_material)
        db.flush()
        db.get(OrderItem, 1).material_id = parent_material.id
        child = db.get(SalesOrderItemBomComponent, 1)
        expected_material_id = child.snapshot_component_material_id
        if missing_child_material:
            child.snapshot_component_material_id = None
        db.commit()
    with TestClient(app) as client:
        _login(client)
        created = client.post("/api/requisition/batches", json={"supplier_name": "匿名供应商",
            "items": [_source_payload(1, "cover"), _source_payload(1, "base"),
                      _source_payload(2, "whole")]})
        assert created.status_code == 201, created.text
        _mark_material_requisition_items_as_legacy(factory)
        received = client.put("/api/incoming/receive/r1")
        if missing_child_material:
            assert received.status_code == 422, received.text
            assert received.json()["detail"]["code"] == "SUPPLIER_RECEIPT_STABLE_MATERIAL_REQUIRED"
        else:
            assert received.status_code == 200, received.text
    with factory() as db:
        if missing_child_material:
            assert db.scalar(select(func.count()).select_from(IncomingReceiptItem)) == 0
            assert db.scalar(select(func.count()).select_from(SupplierReceiptSettlementPriceFact)) == 0
        else:
            price = db.scalar(select(SupplierReceiptSettlementPriceFact))
            assert price.material_id == expected_material_id
            assert price.unit_price == Decimal("1")
