from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.core import inventory_entry_guard  # noqa: F401 - installs commit guard
from app.core.receipt_price_guard import ReceiptPriceGuardSession
from app.models.product import Product
from app.models.warehouse_inventory import InventoryLot, InventoryMovement, WarehouseLocation
from app.services.product_readiness import product_readiness
from app.services.warehouse_inventory import manual_finished_in
from tests.test_inventory_cost_snapshot import db, _customer, _material


def test_82021188_class_report_ready_but_finished_entry_requires_finished_dimensions(db):
    customer = _customer(db)
    material = _material(db, code="K82021188", price="2")
    product = Product(
        customer_id=customer.id,
        product_code="82021188",
        customer_material_code="82021188",
        product_name="隔离复现：报料规格不能替代成品规格",
        unit="只",
        box_category="normal",
        box_style="衬板",
        material_id=material.id,
        layer_count=3,
        flute_type="B",
        report_length_mm=1000,
        report_width_mm=600,
        crease_type="毛片",
        pieces_per_box=1,
    )
    location = WarehouseLocation(
        location_code="ENTRY-82021188", location_name="隔离正式成品位",
        warehouse_type="finished",
    )
    db.add_all([product, location]); db.commit()
    readiness = product_readiness(product)
    assert readiness["ready"] is True
    assert readiness["readiness_scope"] == "requisition"

    args = dict(customer_id=customer.id, product_id=product.id,
        location_id=location.id, quantity=8, stock_date=date(2026, 10, 15),
        source_type="stocktake", remarks="保留原盘点表单", operator_id=None,
        idempotency_key="entry-82021188-once")
    with ReceiptPriceGuardSession(bind=db.get_bind()) as session:
        manual_finished_in(session, **args)
        with pytest.raises(HTTPException) as error:
            session.commit()
        detail = error.value.detail
        assert detail["code"] == "INVENTORY_ENTRY_INCOMPLETE"
        assert [row["field"] for row in detail["issues"] if row["repairable"]] == ["length_mm", "width_mm"]
        assert all("报料" in row["reason"] for row in detail["issues"] if row["repairable"])
        session.rollback()
        assert session.scalar(select(func.count()).select_from(InventoryLot)) == 0
        assert session.scalar(select(func.count()).select_from(InventoryMovement)) == 0

    with ReceiptPriceGuardSession(bind=db.get_bind()) as session:
        current = session.get(Product, product.id)
        current.length_mm, current.width_mm = 300, 200
        session.commit()
        first = manual_finished_in(session, **args)
        session.commit()
        first_id = first.id
        replay = manual_finished_in(session, **args)
        session.commit()
        assert replay.id == first_id
        assert session.scalar(select(func.count()).select_from(InventoryLot)) == 1
        assert session.scalar(select(func.count()).select_from(InventoryMovement)) == 1
