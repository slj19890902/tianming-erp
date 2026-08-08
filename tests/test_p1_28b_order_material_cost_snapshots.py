from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models.order_material_cost_snapshot import (
    SalesOrderItemMaterialCostSnapshot,
)
from app.services.order_material_cost_snapshot import (
    freeze_order_item_material_cost,
    serialize_order_item_material_cost_snapshot,
)
from tests.test_phase5_orders import _login, _payload, order_api_app


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static/index.html").read_text(encoding="utf-8")
ORDERS_SOURCE = (ROOT / "app/api/orders.py").read_text(encoding="utf-8")


def test_snapshot_is_idempotent_and_a_material_change_appends_a_version(
    order_api_app, monkeypatch
) -> None:
    from app.models.material import Material
    from app.models.order import OrderItem
    from app.services import order_material_cost

    def fixed_price(_db, *, material, supplier_name, layer_count, flute_type):
        return {
            "base_price": "2.0000",
            "flute_delta": "0",
            "effective_price": "2.0000",
            "rule_id": None,
            "supplier_name": supplier_name,
            "layer_count": layer_count,
            "flute_type": flute_type,
        }

    monkeypatch.setattr(order_material_cost, "get_effective_material_price", fixed_price)
    app, session_factory = order_api_app
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/orders", json=_payload())
    assert response.status_code == 201, response.text

    with session_factory() as session:
        item = session.scalar(select(OrderItem).order_by(OrderItem.id))
        material = session.get(Material, item.material_id)
        material.is_active = True
        item.snapshot_report_length_mm = 500
        item.snapshot_report_width_mm = 400
        first, created = freeze_order_item_material_cost(
            session, item, actor_id=None
        )
        same, repeated = freeze_order_item_material_cost(
            session, item, actor_id=None
        )
        assert created is True
        assert repeated is False
        assert same.id == first.id
        item.quantity += 1
        second, changed = freeze_order_item_material_cost(
            session, item, actor_id=None
        )
        assert changed is True
        assert second.snapshot_version == first.snapshot_version + 1
        assert first.order_quantity_snapshot + 1 == second.order_quantity_snapshot
        frozen = serialize_order_item_material_cost_snapshot(first)
        assert frozen["material_cost_is_current_estimate"] is False
        assert frozen["material_cost_scope_label"].startswith("下单时材料成本")


def test_new_order_freezes_every_item_and_cost_permission_controls_response(
    order_api_app,
) -> None:
    app, session_factory = order_api_app
    with TestClient(app) as client:
        _login(client)
        created = client.post("/api/orders", json=_payload())
        assert created.status_code == 201, created.text
        body = created.json()
        assert all(
            item["material_cost_is_current_estimate"] is False
            for item in body["items"]
        )
        assert all(
            item["material_cost_history_label"] == "历史冻结版本 V1"
            for item in body["items"]
        )
        order_id = body["id"]

        _login(client, "sales")
        sales_detail = client.get(f"/api/orders/{order_id}")
        assert sales_detail.status_code == 200, sales_detail.text
        assert all(
            "material_cost_status" not in item
            for item in sales_detail.json()["items"]
        )

    with session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(SalesOrderItemMaterialCostSnapshot)
        ) == 2


def test_all_formal_order_writers_freeze_before_commit_and_ui_labels_history() -> None:
    assert ORDERS_SOURCE.count("freeze_order_item_material_cost(") >= 3
    assert "freeze_order_item_material_cost(db, item, actor_id=user.id)" in ORDERS_SOURCE
    assert "get_latest_order_item_material_cost_snapshots_by_items" in ORDERS_SOURCE
    assert "当前规则估算，非历史成本事实" in (
        ROOT / "app/services/order_material_cost_snapshot.py"
    ).read_text(encoding="utf-8")
    assert "item.material_cost_history_label" in INDEX
    assert "历史订单为当前规则估算" not in INDEX
    assert "未计生产损耗和加工费" in INDEX
