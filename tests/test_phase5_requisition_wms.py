from __future__ import annotations

from fastapi.testclient import TestClient
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


def create_demo_order(client: TestClient, quantity: int = 100) -> dict:
    response = client.post(
        "/api/orders",
        json={
            "customer_id": 1,
            "customer_po": "REQ-UAT-001",
            "order_date": "2026-06-17",
            "delivery_date": "2026-06-24",
            "items": [{"product_id": 1, "quantity": quantity}],
        },
    )
    assert response.status_code == 201
    return response.json()


def test_pending_requisition_excludes_stock_material_items():
    client, engine = make_client()
    order = create_demo_order(client)
    order_item_id = order["items"][0]["id"]

    from phase1_postgres.models import OrderItem
    from sqlalchemy.orm import Session

    with Session(engine) as session:
        item = session.get(OrderItem, order_item_id)
        item.production_source = "STOCK_MATERIAL"
        session.commit()

    response = client.get("/api/requisitions/pending-items")
    assert response.status_code == 200
    assert response.json()["total"] == 0


def test_requisition_and_wms_partial_receive_keeps_remaining_pending():
    client, _engine = make_client()
    order = create_demo_order(client, quantity=100)
    order_item_id = order["items"][0]["id"]

    pending = client.get("/api/requisitions/pending-items")
    assert pending.status_code == 200
    assert pending.json()["total"] == 1
    assert pending.json()["items"][0]["order_item_id"] == order_item_id

    created = client.post(
        "/api/requisitions",
        json={
            "supplier_name": "佳丰纸板",
            "items": [{"order_item_id": order_item_id, "requisition_qty": 100}],
        },
    )
    assert created.status_code == 201
    req_item = created.json()["items"][0]
    assert req_item["remaining_qty"] == 100

    wms = client.get("/api/wms/pending", params={"supplier_name": "佳丰纸板"})
    assert wms.status_code == 200
    assert wms.json()["summary"][0]["supplier_name"] == "佳丰纸板"
    assert wms.json()["summary"][0]["remaining_qty"] == 100

    partial = client.put(
        f"/api/wms/receive/{req_item['id']}",
        json={"actual_receive_qty": 40, "received_by": "车间张师傅"},
    )
    assert partial.status_code == 200
    assert partial.json()["status"] == "PARTIAL"
    assert partial.json()["received_qty"] == 40
    assert partial.json()["remaining_qty"] == 60

    still_pending = client.get("/api/wms/pending", params={"supplier_name": "佳丰纸板"}).json()
    assert still_pending["items"][0]["remaining_qty"] == 60

    done = client.put(
        f"/api/wms/receive/{req_item['id']}",
        json={"actual_receive_qty": 60, "received_by": "车间张师傅"},
    )
    assert done.status_code == 200
    assert done.json()["status"] == "COMPLETED"
    assert done.json()["remaining_qty"] == 0

    empty = client.get("/api/wms/pending", params={"supplier_name": "佳丰纸板"}).json()
    assert empty["total"] == 0


def test_wms_process_panel_after_receive_contains_order_route_and_drawing_placeholder():
    client, _engine = make_client()
    order = create_demo_order(client, quantity=30)
    order_item_id = order["items"][0]["id"]
    req = client.post(
        "/api/requisitions",
        json={
            "supplier_name": "佳丰纸板",
            "items": [{"order_item_id": order_item_id, "requisition_qty": 30}],
        },
    ).json()
    req_item_id = req["items"][0]["id"]
    client.put(f"/api/wms/receive/{req_item_id}", json={"actual_receive_qty": 30})

    detail = client.get(f"/api/wms/requisition-items/{req_item_id}/process-panel")
    assert detail.status_code == 200
    body = detail.json()
    assert body["order_number"].startswith("PO-")
    assert body["customer_name"] == "天华超净"
    assert body["product_name"] == "001A外箱"
    assert body["process_route"] == ["印刷", "开槽/模切", "打钉/粘箱", "待送货"]
    assert body["drawing_status"] == "暂无图纸，按工艺说明生产"
