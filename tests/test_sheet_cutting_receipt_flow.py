from copy import deepcopy
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_phase11_requisition import requisition_app, _login, _save_supplier_order_draft
from tests.test_sheet_cutting_supplier_flow import _source, _preview
from tests.test_p1_81_receipt_purpose_flow import (
    FrozenSource, _seed_material_and_staging, _use_p181_published_map_identity,
    _freeze_receipt_fact, _receive,
)


@pytest.mark.parametrize('trim', [False, True])
def test_v2_receipt_uses_supplier_sheet_yield_and_frozen_cost(requisition_app, monkeypatch, trim):
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot, SupplierRequisitionOrderItem
    from app.models.warehouse_inventory import InventoryLot, FinishedGoodsInventoryDetail
    app, sessions = requisition_app
    _use_p181_published_map_identity(monkeypatch)
    material_id = _seed_material_and_staging(sessions)
    if trim:
        from tests.test_supplier_sheet_defaults import seed_trim
        item_id, product_id = seed_trim(sessions, quantity=600)
    else:
        item_id, product_id = _source(sessions, quantity=400)
    with sessions() as db:
        item, product = db.get(OrderItem, item_id), db.get(Product, product_id)
        item.material_id = product.material_id = material_id
        item.snapshot_material = 'KAKAK'
        db.commit()
    with TestClient(app) as client:
        _login(client, 'admin')
        if trim:
            from tests.test_phase11_requisition import _preview_supplier_order_draft
            draft = _preview_supplier_order_draft(client, [{'type':'order_item','order_item_id':item_id}])
        else:
            draft = _preview(client, item_id, length_parts=2, width_parts=2)
        saved = _save_supplier_order_draft(client, draft)
        assert saved.status_code == 201, saved.text
        with sessions() as db:
            line = db.scalar(select(SupplierRequisitionOrderItem).where(SupplierRequisitionOrderItem.order_item_id == item_id))
            snap = db.scalar(select(PurchasePurposeSourceSnapshot).where(PurchasePurposeSourceSnapshot.supplier_requisition_order_item_id == line.id))
            source = FrozenSource(source_key=snap.source_key, route_key=f'so{line.id}', supplier_item_id=line.id,
                source_version=line.version, purpose_snapshot_id=snap.id, purpose_snapshot_version=snap.snapshot_version,
                receipt_plan_fingerprint=snap.preview_fingerprint, component_type=snap.component_type,
                material_id=line.material_id, order_purpose_sheet_qty=50, reserve_purpose_sheet_qty=0)
            if trim:
                assert (line.report_length_mm,line.report_width_mm)==(750,700)
                # Later common-box edits must not change this purchase/receipt.
                product=db.get(Product,product_id);product.report_length_mm=999;product.report_width_mm=888
                product.sheet_cutting_settings=None;product.crease_type='净料';db.commit()
        facts = _freeze_receipt_fact(client, source, idempotency_key='cutting-receipt-fact',
            unit_price='2.80' if trim else '2.5000', price_unit='per_square_meter' if trim else 'per_sheet')
        assert facts.status_code == 200, facts.text
        facts = facts.json()
        received = _receive(client, source, facts, quantity=50, idempotency_key='cutting-receipt-50')
        assert received.status_code == 200, received.text
        replay = _receive(client, source, facts, quantity=50, idempotency_key='cutting-receipt-50')
        assert replay.status_code == 200, replay.text
    with sessions() as db:
        lots = db.scalars(select(InventoryLot).join(FinishedGoodsInventoryDetail).where(FinishedGoodsInventoryDetail.product_id == product_id)).all()
        assert sum(row.quantity_available + row.quantity_reserved for row in lots) == (600 if trim else 400)
        assert all(row.estimated_unit_cost_snapshot == Decimal('0.1225' if trim else '0.3125') for row in lots)
