from __future__ import annotations

from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool


def make_client():
    from phase1_postgres.database import get_session
    from phase1_postgres.main import create_app
    from phase1_postgres.models import Base

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    app = create_app(engine=engine, seed_demo_data=True)

    def override_session():
        from sqlalchemy.orm import Session

        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app), engine


def prepare_received_order_item(client: TestClient, quantity: int = 100) -> dict:
    order = client.post(
        "/api/orders",
        json={
            "customer_id": 1,
            "customer_po": "DELIVERY-UAT-001",
            "order_date": "2026-06-17",
            "delivery_date": "2026-06-20",
            "items": [{"product_id": 1, "quantity": quantity}],
        },
    )
    assert order.status_code == 201
    order_body = order.json()
    order_item_id = order_body["items"][0]["id"]

    requisition = client.post(
        "/api/requisitions",
        json={
            "supplier_name": "佳丰纸板",
            "items": [{"order_item_id": order_item_id, "requisition_qty": quantity}],
        },
    )
    assert requisition.status_code == 201
    requisition_item_id = requisition.json()["items"][0]["id"]

    received = client.put(
        f"/api/wms/receive/{requisition_item_id}",
        json={"actual_receive_qty": quantity, "received_by": "车间收料员"},
    )
    assert received.status_code == 200
    assert received.json()["status"] == "COMPLETED"
    return {"order": order_body, "order_item_id": order_item_id}


def test_delivery_receipt_owner_review_then_statement_uses_signed_quantity():
    client, _engine = make_client()
    prepared = prepare_received_order_item(client, quantity=100)

    pending = client.get("/api/deliveries/pending-items")
    assert pending.status_code == 200
    assert pending.json()["total"] == 1
    assert pending.json()["items"][0]["remaining_qty"] == 100

    delivery = client.post(
        "/api/deliveries",
        json={
            "customer_id": 1,
            "delivery_date": "2026-06-18",
            "vehicle_number": "苏E12345",
            "driver_name": "张师傅",
            "items": [{"order_item_id": prepared["order_item_id"], "delivery_qty": 100}],
        },
    )
    assert delivery.status_code == 201
    delivery_body = delivery.json()
    assert delivery_body["status"] == "DELIVERED_WAIT_RECEIPT"
    assert delivery_body["total_quantity"] == 100
    delivery_item_id = delivery_body["items"][0]["id"]

    blocked_statement = client.post(
        "/api/statements/generate",
        json={"customer_id": 1, "start_date": "2026-06-01", "end_date": "2026-06-20"},
    )
    assert blocked_statement.status_code == 400
    assert "老板已核对" in blocked_statement.json()["detail"]

    receipt = client.post(
        "/api/receipts",
        json={
            "delivery_id": delivery_body["id"],
            "signed_by": "客户仓库",
            "actual_received_date": "2026-06-19",
            "items": [
                {
                    "delivery_item_id": delivery_item_id,
                    "actual_signed_qty": 95,
                    "difference_reason": "客户实际只签收95个",
                }
            ],
        },
    )
    assert receipt.status_code == 201
    receipt_body = receipt.json()
    assert receipt_body["status"] == "SIGNED_WAIT_OWNER_REVIEW"
    assert receipt_body["items"][0]["actual_signed_qty"] == 95
    assert receipt_body["items"][0]["amount"] == "346.75"

    reviewed = client.post(f"/api/receipts/{receipt_body['id']}/owner-confirm", json={"reviewed_by": "老板"})
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "OWNER_REVIEWED"

    statement = client.post(
        "/api/statements/generate",
        json={"customer_id": 1, "start_date": "2026-06-01", "end_date": "2026-06-20"},
    )
    assert statement.status_code == 201
    statement_body = statement.json()
    assert statement_body["statement_month"] == "2026-06"
    assert statement_body["total_quantity"] == 95
    assert statement_body["total_amount"] == "346.75"
    assert statement_body["items"][0]["actual_signed_qty"] == 95

    export = client.get(f"/api/statements/{statement_body['id']}/export")
    assert export.status_code == 200
    assert export.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    workbook = load_workbook(BytesIO(export.content))
    worksheet = workbook.active
    assert worksheet["A1"].value == "天明包装月结对账清单"
    assert worksheet["A4"].value == "送货日期"
    assert worksheet["H5"].value == 95
    assert worksheet["J5"].value == 346.75
