from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.test_mold_tool_workflow import _login, _protected_business_state, mold_app


ROOT = Path(__file__).resolve().parents[1]


def _seed_bound_mold(factory) -> tuple[int, int]:
    from app.models.mold_tool import MoldTool
    from app.models.product import Product

    with factory() as db:
        mold = MoldTool(
            mold_code="P163-M-001",
            mold_name="模具联动测试客户 P1-63 模具",
            rack_location="1F-M-R01-L1-G01",
        )
        db.add(mold)
        db.flush()
        product = Product(
            customer_id=1,
            product_code="P163-P-001",
            customer_material_code="P163-P-001",
            product_name="P1-63 五层加强纸箱",
            length_mm=520,
            width_mm=350,
            height_mm=300,
            mold_tool_id=mold.id,
        )
        db.add(product)
        db.commit()
        return mold.id, product.id


def _order_payload(product_id: int, token: str | None = None) -> dict:
    payload = {
        "customer_id": 1,
        "customer_po": "P1-63-PO",
        "order_date": "2026-08-17",
        "delivery_date": "2026-08-20",
        "items": [{"product_id": product_id, "quantity": 20, "unit_price": "2.5"}],
    }
    if token:
        payload["mold_repair_confirmation_token"] = token
    return payload


def test_repair_lifecycle_is_independent_idempotent_and_audited(mold_app) -> None:
    from app.models.audit import OperationLog
    from app.models.mold_tool import MoldRepairEvent, MoldTool

    app, factory = mold_app
    mold_id, _product_id = _seed_bound_mold(factory)
    protected_before = _protected_business_state(factory)
    with TestClient(app) as client:
        _login(client, "workshop")
        payload = {
            "target_status": "needs_repair",
            "expected_version": 1,
            "idempotency_key": "p1-63-repair-start-0001",
            "confirmed": True,
        }
        started = client.post(f"/api/warehouse/molds/{mold_id}/repair-status", json=payload)
        assert started.status_code == 200, started.text
        assert started.json()["mold"]["repair_status"] == "needs_repair"
        assert started.json()["mold"]["repair_version"] == 2
        replay = client.post(f"/api/warehouse/molds/{mold_id}/repair-status", json=payload)
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True

        live = client.get(f"/api/warehouse/molds/live/{mold_id}")
        assert live.status_code == 200, live.text
        assert live.json()["mold"]["repair_status"] == "needs_repair"
        assert live.json()["mold"]["repair_status_label"] == "待维修"

        completed = client.post(
            f"/api/warehouse/molds/{mold_id}/repair-status",
            json={
                "target_status": "normal",
                "expected_version": 2,
                "idempotency_key": "p1-63-repair-complete-0001",
                "confirmed": True,
            },
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["mold"]["repair_status"] == "normal"
        detail = client.get(f"/api/warehouse/molds/{mold_id}/detail")
        assert detail.status_code == 200, detail.text
        repair_timeline = [row for row in detail.json()["timeline"] if row["event_type"] == "mold_repair_status"]
        assert [row["label"] for row in repair_timeline] == ["标记待维修", "维修完毕"]
        client.post("/api/auth/logout")
        _login(client, "sales")
        denied = client.post(
            f"/api/warehouse/molds/{mold_id}/repair-status",
            json={
                "target_status": "needs_repair",
                "expected_version": 3,
                "idempotency_key": "p1-63-sales-denied-0001",
                "confirmed": True,
            },
        )
        assert denied.status_code == 403, denied.text

    assert _protected_business_state(factory) == protected_before
    with factory() as db:
        mold = db.get(MoldTool, mold_id)
        assert mold is not None
        assert (mold.repair_status, mold.repair_version) == ("normal", 3)
        assert (mold.rack_location, mold.location_version, mold.is_active) == (
            "1F-M-R01-L1-G01",
            1,
            True,
        )
        assert db.scalar(select(func.count(MoldRepairEvent.id))) == 2
        assert db.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.action_code.in_(("mold.repair.start", "mold.repair.complete"))
            )
        ) == 2


def test_order_save_requires_one_current_signed_repair_acknowledgement(mold_app) -> None:
    from app.models.mold_tool import MoldTool
    from app.models.order import Order

    app, factory = mold_app
    mold_id, product_id = _seed_bound_mold(factory)
    with factory() as db:
        mold = db.get(MoldTool, mold_id)
        assert mold is not None
        mold.repair_status = "needs_repair"
        mold.repair_version = 2
        db.commit()

    with TestClient(app) as client:
        _login(client, "admin")
        rejected = client.post("/api/orders", json=_order_payload(product_id))
        assert rejected.status_code == 409, rejected.text
        detail = rejected.json()["detail"]
        assert detail["code"] == "MOLD_REPAIR_CONFIRMATION_REQUIRED"
        assert detail["warnings"][0]["mold_code"] == "P163-M-001"

        preview = client.post(
            "/api/orders/mold-repair-preview",
            json={"product_ids": [product_id]},
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["required"] is True
        token = preview.json()["confirmation_token"]
        created = client.post("/api/orders", json=_order_payload(product_id, token))
        assert created.status_code == 201, created.text
        order_id = created.json()["id"]
        order_item = created.json()["items"][0]

        item_payload = {
            "quantity": order_item["quantity"],
            "unit_price": str(order_item["unit_price"]),
            "product_code": order_item["snapshot_product_code"],
            "product_name": order_item["snapshot_product_name"],
            "material": order_item.get("snapshot_material"),
            "specification": order_item.get("snapshot_spec"),
        }
        item_rejected = client.put(
            f"/api/orders/items/{order_item['id']}", json=item_payload
        )
        assert item_rejected.status_code == 409, item_rejected.text
        item_saved = client.put(
            f"/api/orders/items/{order_item['id']}",
            json={**item_payload, "mold_repair_confirmation_token": token},
        )
        assert item_saved.status_code == 200, item_saved.text

        group_rejected = client.put(
            f"/api/orders/{order_id}",
            json={"customer_po": "P1-63-PO-EDIT"},
        )
        assert group_rejected.status_code == 409, group_rejected.text
        group_saved = client.put(
            f"/api/orders/{order_id}",
            json={
                "customer_po": "P1-63-PO-EDIT",
                "mold_repair_confirmation_token": token,
            },
        )
        assert group_saved.status_code == 200, group_saved.text

        with factory() as db:
            mold = db.get(MoldTool, mold_id)
            assert mold is not None
            mold.repair_version = 3
            db.commit()
        stale = client.post("/api/orders", json=_order_payload(product_id, token))
        assert stale.status_code == 409, stale.text

    with factory() as db:
        assert db.scalar(select(func.count(Order.id))) == 1


def test_repair_frontends_and_all_order_save_entries_use_current_warning_contract() -> None:
    warehouse = (ROOT / "static/warehouse.html").read_text(encoding="utf-8")
    mobile = (ROOT / "static/mobile_mold_live.html").read_text(encoding="utf-8")
    index = (ROOT / "static/index.html").read_text(encoding="utf-8")
    for marker in (
        "changeMoldRepairStatus",
        "/repair-status",
        "维修完毕",
        "待维修",
        "moldRepairAttempt",
        "repair_version",
    ):
        assert marker in warehouse
    assert "repair-alert" in mobile
    assert 'mold.repair_status==="needs_repair"' in mobile
    assert "待维修｜请先联系维修人员核对" in mobile
    for marker in (
        "prepareMoldRepairConfirmation",
        "/api/orders/mold-repair-preview",
        "mold_repair_confirmation_token",
        "saveConfirmedImportDrafts",
        "saveNewOrder",
        "saveCurrentOrderItem",
        "saveOrderGroup",
    ):
        assert marker in index
    assert index.count("window.confirm(this.moldRepairWarningText") == 1
