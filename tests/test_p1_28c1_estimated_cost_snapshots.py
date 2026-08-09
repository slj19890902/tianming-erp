from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError

from app.models.order_estimated_cost_snapshot import SalesOrderItemEstimatedCostSnapshot
from tests.test_phase5_orders import _login, _payload, order_api_app


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static/index.html").read_text(encoding="utf-8")
ORDERS_SOURCE = (ROOT / "app/api/orders.py").read_text(encoding="utf-8")


def _fixed_price(_db, *, material, supplier_name, layer_count, flute_type):
    return {
        "base_price": "2.0000",
        "flute_delta": "0",
        "effective_price": "2.0000",
        "rule_id": None,
        "supplier_name": supplier_name,
        "layer_count": layer_count,
        "flute_type": flute_type,
    }


def test_new_order_freezes_complete_estimate_and_adjustment_is_versioned(
    order_api_app, monkeypatch
) -> None:
    from app.models.product import Product
    from app.services import order_material_cost

    monkeypatch.setattr(order_material_cost, "get_effective_material_price", _fixed_price)
    app, session_factory = order_api_app
    with session_factory() as session:
        product = session.get(Product, 1)
        product.box_style = "A1"
        product.print_content = "双色印刷"
        product.report_length_mm = 500
        product.report_width_mm = 400
        session.commit()

    payload = _payload()
    payload["items"] = [payload["items"][0]]
    with TestClient(app) as client:
        _login(client)
        created = client.post("/api/orders", json=payload)
        assert created.status_code == 201, created.text
        item = created.json()["items"][0]
        assert item["estimated_total_cost_status"] == "calculated"
        assert Decimal(item["estimated_loss_rate"]) == Decimal("0.030000")
        assert Decimal(item["estimated_order_total_cost"]) == Decimal("132.40")
        breakdown = item["estimated_cost_breakdown"]
        assert breakdown["loss_components"][0]["added_loss_sheet_quantity"] == 6
        assert Decimal(breakdown["processing_total_cost"]) == Decimal("50.00")
        item_id = item["id"]

        changed = client.post(
            f"/api/orders/items/{item_id}/estimated-cost",
            json={
                "expected_snapshot_version": 1,
                "loss_rate": "0.05",
                "die_fee": "10",
                "plate_fee": "5",
                "freight_fee": "0",
                "other_fee": "0",
            },
        )
        assert changed.status_code == 200, changed.text
        changed_body = changed.json()
        assert changed_body["estimated_total_cost_snapshot_version"] == 2
        assert Decimal(changed_body["estimated_order_total_cost"]) == Decimal("149.00")
        assert changed_body["estimated_cost_breakdown"]["loss_components"][0]["added_loss_sheet_quantity"] == 10

        repeated = client.post(
            f"/api/orders/items/{item_id}/estimated-cost",
            json={
                "expected_snapshot_version": 2,
                "loss_rate": "0.05",
                "die_fee": "10",
                "plate_fee": "5",
                "freight_fee": "0",
                "other_fee": "0",
            },
        )
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["estimated_total_cost_snapshot_version"] == 2
        stale = client.post(
            f"/api/orders/items/{item_id}/estimated-cost",
            json={"expected_snapshot_version": 1, "loss_rate": "0.03"},
        )
        assert stale.status_code == 409

        _login(client, "sales")
        forbidden = client.post(
            f"/api/orders/items/{item_id}/estimated-cost",
            json={"expected_snapshot_version": 2, "loss_rate": "0.03"},
        )
        assert forbidden.status_code == 403

    with session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(SalesOrderItemEstimatedCostSnapshot)
        ) == 2


def test_incomplete_material_or_unknown_processing_never_becomes_zero_complete(
    order_api_app,
) -> None:
    app, _session_factory = order_api_app
    payload = _payload()
    payload["items"] = [payload["items"][1]]
    with TestClient(app) as client:
        _login(client)
        created = client.post("/api/orders", json=payload)
    assert created.status_code == 201, created.text
    item = created.json()["items"][0]
    assert item["estimated_total_cost_status"] in {"partial", "missing"}
    assert item["estimated_order_total_cost"] is None
    assert item["estimated_cost"] is None
    assert item["estimated_cost_missing_items"]


def test_processing_defaults_cover_die_cut_and_external_only_assembly() -> None:
    from app.services.order_estimated_cost_snapshot import _processing_rule

    class FakeSession:
        def scalar(self, _statement):
            return 1

    die_item = SimpleNamespace(
        id=1,
        product=SimpleNamespace(box_style="模切内盒"),
    )
    category, batch, unit, missing = _processing_rule(
        FakeSession(), die_item, None
    )
    assert (category, batch, unit, missing) == (
        "die_cut",
        Decimal("30"),
        Decimal("0.15"),
        [],
    )
    external_item = SimpleNamespace(
        id=2,
        product=SimpleNamespace(box_style=None),
    )
    category, batch, unit, missing = _processing_rule(
        FakeSession(), external_item, None
    )
    assert (category, batch, unit, missing) == (
        "external_assembly",
        Decimal("10"),
        Decimal("0.03"),
        [],
    )


def test_all_order_writers_freeze_estimate_and_ui_marks_it_non_actual() -> None:
    assert ORDERS_SOURCE.count("freeze_order_item_estimated_cost(") >= 4
    assert '"estimated_total_cost_scope_label": "预计成本，非实际成本"' in ORDERS_SOURCE
    assert "/items/{item_id}/estimated-cost" in ORDERS_SOURCE
    assert "预计成本，非实际成本" in INDEX
    assert "调整预计成本" in INDEX


def test_estimated_cost_unique_conflict_classifier_is_narrow() -> None:
    from app.services.order_estimated_cost_snapshot import (
        is_estimated_cost_snapshot_unique_conflict,
    )

    collision = IntegrityError(
        "INSERT",
        {},
        sqlite3.IntegrityError(
            "UNIQUE constraint failed: "
            "sales_order_item_estimated_cost_snapshots.order_item_reference_snapshot, "
            "sales_order_item_estimated_cost_snapshots.snapshot_version"
        ),
    )
    unrelated = IntegrityError(
        "INSERT",
        {},
        sqlite3.IntegrityError("CHECK constraint failed: unrelated_business_table"),
    )
    assert is_estimated_cost_snapshot_unique_conflict(collision) is True
    assert is_estimated_cost_snapshot_unique_conflict(unrelated) is False


def test_two_real_sessions_map_snapshot_race_to_409_then_retry_after_rollback(
    order_api_app,
) -> None:
    from app.api.orders import EstimatedCostUpdate, update_order_item_estimated_cost
    from app.models.user import User

    app, session_factory = order_api_app
    payload = _payload()
    payload["items"] = [payload["items"][0]]
    with TestClient(app) as client:
        _login(client)
        created = client.post("/api/orders", json=payload)
    assert created.status_code == 201, created.text
    item_id = int(created.json()["items"][0]["id"])

    engine = session_factory.kw["bind"]

    @event.listens_for(engine, "connect")
    def _enable_wal_for_real_concurrency(dbapi_connection, _record) -> None:
        dbapi_connection.execute("PRAGMA journal_mode = WAL")

    engine.dispose()
    warm_connections = [engine.connect(), engine.connect()]
    try:
        assert all(
            connection.exec_driver_sql("PRAGMA journal_mode").scalar_one().lower()
            == "wal"
            for connection in warm_connections
        )
    finally:
        for connection in warm_connections:
            connection.close()

    ready_to_flush = Barrier(2)
    winner_committed = Event()

    def worker(role: str, fee: str) -> tuple[str, int, int | None]:
        with session_factory() as db:
            user = db.scalar(select(User).where(User.username == "admin"))
            assert user is not None
            original_flush = db.flush
            coordinated = False

            def coordinated_flush(objects=None):
                nonlocal coordinated
                has_estimated_snapshot = any(
                    isinstance(row, SalesOrderItemEstimatedCostSnapshot)
                    for row in db.new
                )
                if not coordinated and has_estimated_snapshot:
                    coordinated = True
                    ready_to_flush.wait(timeout=10)
                    if role == "loser":
                        assert winner_committed.wait(timeout=10)
                return original_flush(objects)

            db.flush = coordinated_flush
            update = EstimatedCostUpdate(
                expected_snapshot_version=1,
                loss_rate=Decimal("0.05"),
                die_fee=Decimal(fee),
            )
            if role == "winner":
                try:
                    result = update_order_item_estimated_cost(
                        item_id,
                        update,
                        request=None,
                        db=db,
                        user=user,
                    )
                    return role, 200, int(result["estimated_total_cost_snapshot_version"])
                finally:
                    winner_committed.set()

            try:
                update_order_item_estimated_cost(
                    item_id,
                    update,
                    request=None,
                    db=db,
                    user=user,
                )
            except HTTPException as error:
                assert error.status_code == 409
                assert "预计成本已被其他操作更新" in str(error.detail)
            else:
                raise AssertionError("并发落后写入应命中唯一键冲突")

            retried = update_order_item_estimated_cost(
                item_id,
                EstimatedCostUpdate(
                    expected_snapshot_version=2,
                    loss_rate=Decimal("0.05"),
                    die_fee=Decimal(fee),
                ),
                request=None,
                db=db,
                user=user,
            )
            return role, 409, int(retried["estimated_total_cost_snapshot_version"])

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda pair: worker(*pair),
                (("winner", "1"), ("loser", "2")),
            )
        )

    assert sorted((role, status, version) for role, status, version in results) == [
        ("loser", 409, 3),
        ("winner", 200, 2),
    ]
    with session_factory() as db:
        versions = list(
            db.scalars(
                select(SalesOrderItemEstimatedCostSnapshot.snapshot_version)
                .where(SalesOrderItemEstimatedCostSnapshot.sales_order_item_id == item_id)
                .order_by(SalesOrderItemEstimatedCostSnapshot.snapshot_version)
            )
        )
    assert versions == [1, 2, 3]


def test_unrelated_integrity_error_is_not_hidden_as_estimated_cost_conflict(
    order_api_app,
    monkeypatch,
) -> None:
    from app.api import orders

    app, _session_factory = order_api_app
    payload = _payload()
    payload["items"] = [payload["items"][0]]
    with TestClient(app) as client:
        _login(client)
        created = client.post("/api/orders", json=payload)
        assert created.status_code == 201, created.text
        item_id = int(created.json()["items"][0]["id"])

        def raise_unrelated(*_args, **_kwargs):
            raise IntegrityError(
                "INSERT INTO unrelated_business_table",
                {},
                sqlite3.IntegrityError(
                    "CHECK constraint failed: unrelated_business_table"
                ),
            )

        monkeypatch.setattr(orders, "freeze_order_item_estimated_cost", raise_unrelated)
        with pytest.raises(IntegrityError, match="unrelated_business_table"):
            client.post(
                f"/api/orders/items/{item_id}/estimated-cost",
                json={"expected_snapshot_version": 1, "loss_rate": "0.05"},
            )
