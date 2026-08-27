from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from test_p1_06_reported_documents_pagination import (
    _login,
    reported_documents_app,
)


__all__ = ["reported_documents_app"]


def test_supplier_reported_list_and_detail_keep_each_frozen_line_crease(
    reported_documents_app,
) -> None:
    from app.api.deps import get_db
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    override = reported_documents_app.dependency_overrides[get_db]
    session_generator = override()
    db = next(session_generator)
    try:
        customer = Customer(
            customer_number=28,
            customer_code="P028",
            name="P028 匿名客户",
            chinese_short_name="匿名客户",
        )
        db.add(customer)
        db.flush()
        product = Product(
            customer_id=customer.id,
            product_code="P028-SAME-PRODUCT",
            customer_material_code="P028-SAME-PRODUCT",
            product_name="P028 同款多压线纸箱",
            box_category="normal",
            crease_type="压线",
            crease_left_mm=999,
            crease_middle_mm=999,
            crease_right_mm=999,
        )
        order = Order(
            order_number="TM-P028-001",
            customer_id=customer.id,
            order_date=date(2026, 8, 27),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("20"),
        )
        db.add_all([product, order])
        db.flush()

        pressed = OrderItem(
            order_id=order.id,
            product_id=product.id,
            item_order_number="TM-P028-001-001",
            item_sequence=1,
            quantity=10,
            unit_price=Decimal("1"),
            subtotal=Decimal("10"),
            material_status="pending",
            requisition_status="已报料",
            snapshot_product_code="P028-PRESSED",
            snapshot_product_name="P028 压线款",
            snapshot_report_length_mm=2030,
            snapshot_report_width_mm=705,
            snapshot_crease_type="压线",
            snapshot_crease_left_mm=202,
            snapshot_crease_middle_mm=301,
            snapshot_crease_right_mm=202,
        )
        net_sheet = OrderItem(
            order_id=order.id,
            product_id=product.id,
            item_order_number="TM-P028-001-002",
            item_sequence=2,
            quantity=10,
            unit_price=Decimal("1"),
            subtotal=Decimal("10"),
            material_status="pending",
            requisition_status="已报料",
            snapshot_product_code="P028-NET",
            snapshot_product_name="P028 净料款",
            snapshot_report_length_mm=580,
            snapshot_report_width_mm=835,
            snapshot_crease_type="净料",
        )
        db.add_all([pressed, net_sheet])
        db.flush()

        supplier_order = SupplierRequisitionOrder(
            order_number="SRO-P028-MULTI-CREASE",
            supplier_name="P028 供应商",
            report_length_mm=2030,
            report_width_mm=705,
            crease_type="压线",
            crease_left_mm=202,
            crease_middle_mm=301,
            crease_right_mm=202,
            total_quantity=20,
            requisition_qty=20,
            status="confirmed",
            created_at=datetime(2026, 8, 27, 9, 0),
        )
        db.add(supplier_order)
        db.flush()
        db.add_all(
            [
                SupplierRequisitionOrderItem(
                    supplier_order_id=supplier_order.id,
                    order_item_id=pressed.id,
                    product_id=product.id,
                    order_number=order.order_number,
                    product_code="P028-PRESSED",
                    product_name="P028 压线款",
                    customer_name=customer.name,
                    report_length_mm=2030,
                    report_width_mm=705,
                    quantity=10,
                    requisition_qty=10,
                ),
                SupplierRequisitionOrderItem(
                    supplier_order_id=supplier_order.id,
                    order_item_id=net_sheet.id,
                    product_id=product.id,
                    order_number=order.order_number,
                    product_code="P028-NET",
                    product_name="P028 净料款",
                    customer_name=customer.name,
                    report_length_mm=580,
                    report_width_mm=835,
                    quantity=10,
                    requisition_qty=10,
                ),
            ]
        )
        db.commit()
        supplier_order_id = int(supplier_order.id)
    finally:
        session_generator.close()

    with TestClient(reported_documents_app) as client:
        _login(client, "admin", "AdminPass123!")
        purchase = client.get(f"/api/requisition/supplier-orders/{supplier_order_id}")
        documents = client.get(
            "/api/requisition/reported-documents",
            params={"document_number": "SRO-P028-MULTI-CREASE"},
        )
        items = client.get(
            "/api/requisition/reported-items",
            params={"document_number": "SRO-P028-MULTI-CREASE"},
        )

    assert purchase.status_code == documents.status_code == items.status_code == 200
    purchase_by_code = {
        source["product_code"]: line["crease_display"]
        for line in purchase.json()["lines"]
        for source in line["source_items"]
    }
    document_by_code = {
        line["product_code"]: line["crease_display"]
        for line in documents.json()["items"][0]["line_items"]
    }
    item_by_code = {
        line["product_code"]: line["crease_display"]
        for line in items.json()["items"]
    }
    expected = {"P028-PRESSED": "202+301+202", "P028-NET": "净料"}
    assert purchase_by_code == expected
    assert document_by_code == expected
    assert item_by_code == expected
