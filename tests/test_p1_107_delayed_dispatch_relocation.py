from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models.production import ProductionCompletion
from app.models.warehouse_inventory import (
    InventoryLocationMovement,
    InventoryLot,
    InventoryPallet,
    WarehouseLocation,
)
from test_p1_49b_dispatch_pallet_map import (
    MOVE_BATCH_URL,
    _inventory_totals,
    _login,
    _now,
    dispatch_pallet_app,
)
from test_n029_production_service import production_app


def _overview(client: TestClient, idle_days: int = 3) -> dict:
    response = client.get(
        "/api/warehouse/twin-dashboard/overview",
        params={"days": 30, "dispatch_idle_days": idle_days},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_old_direct_dispatch_pallet_is_recommended_without_writing_location(
    dispatch_pallet_app,
) -> None:
    app, factory, ids = dispatch_pallet_app
    with factory() as db:
        first = db.get(ProductionCompletion, ids["first_completion"])
        second = db.get(ProductionCompletion, ids["second_completion"])
        staging = db.get(WarehouseLocation, ids["staging"])
        assert first is not None and second is not None and staging is not None
        first.completed_at = _now() - timedelta(days=5)
        second.completed_at = _now() - timedelta(days=2)
        movement_count = int(db.scalar(select(func.count(InventoryLocationMovement.id))) or 0)
        first_version = int(db.get(InventoryPallet, ids["first_pallet"]).version)
        db.commit()

    with TestClient(app) as client:
        _login(client, "n029-admin")
        projection = _overview(client)

    delayed = projection["delayed_dispatch_relocation"]
    assert delayed["policy"] == {
        "idle_days": 3,
        "minimum_idle_days": 1,
        "maximum_idle_days": 30,
        "source_rule": "已发布地图中的直接完工当前栈板",
        "legacy_source_location_code": "F1-DISPATCH-01",
        "recommended_floor_code": "3F",
        "recommended_area_code": "SEMI-008",
        "writes_inventory": False,
        "notice": "这里只列出可整理的真实待送栈板，不会自动改库存位置；请先完成现场搬运，再使用现有移货确认提交。",
    }
    assert delayed["candidate_count"] == 1
    assert delayed["available_target_count"] == 1
    assert delayed["targets"][0]["location_id"] == ids["delayed_dispatch_target"]
    assert delayed["items"][0]["pallet_id"] == ids["first_pallet"]
    assert delayed["items"][0]["source_floor_code"] == "1F"
    assert delayed["items"][0]["idle_days"] >= 5
    assert delayed["items"][0]["can_plan_move"] is True

    with factory() as db:
        first_pallet = db.get(InventoryPallet, ids["first_pallet"])
        target_pallet = db.scalar(
            select(InventoryPallet).where(
                InventoryPallet.location_id == ids["delayed_dispatch_target"],
                InventoryPallet.is_current.is_(True),
            )
        )
        assert first_pallet is not None
        assert first_pallet.location_id == ids["staging"]
        assert first_pallet.version == first_version
        assert target_pallet is None
        assert int(db.scalar(select(func.count(InventoryLocationMovement.id))) or 0) == movement_count


def test_idle_days_filter_and_customer_scope_fail_closed(
    dispatch_pallet_app,
) -> None:
    app, factory, ids = dispatch_pallet_app
    with factory() as db:
        first = db.get(ProductionCompletion, ids["first_completion"])
        second = db.get(ProductionCompletion, ids["second_completion"])
        assert first is not None and second is not None
        first.completed_at = _now() - timedelta(days=6)
        second.completed_at = _now() - timedelta(days=6)
        db.commit()

    with TestClient(app) as client:
        _login(client, "n029-scoped")
        projection = _overview(client, idle_days=5)
        too_low = client.get(
            "/api/warehouse/twin-dashboard/overview",
            params={"days": 30, "dispatch_idle_days": 0},
        )
        too_high = client.get(
            "/api/warehouse/twin-dashboard/overview",
            params={"days": 30, "dispatch_idle_days": 31},
        )

    delayed = projection["delayed_dispatch_relocation"]
    assert [row["pallet_id"] for row in delayed["items"]] == [ids["first_pallet"]]
    assert delayed["available_target_count"] == 0
    assert delayed["targets"] == []
    assert delayed["items"][0]["can_plan_move"] is False
    assert too_low.status_code == 422
    assert too_high.status_code == 422


def test_current_map_direct_completion_pallet_is_recommended_not_only_legacy_staging(
    dispatch_pallet_app,
) -> None:
    app, factory, ids = dispatch_pallet_app
    with factory() as db:
        completion = db.get(ProductionCompletion, ids["first_completion"])
        pallet = db.get(InventoryPallet, ids["first_pallet"])
        lot = db.get(InventoryLot, ids["first_lot"])
        target = db.get(WarehouseLocation, ids["floor3_target"])
        assert all(row is not None for row in (completion, pallet, lot, target))
        completion.completed_at = _now() - timedelta(days=5)
        # This mirrors current receipt-auto direct completions: the pallet and
        # finished lot are in a published physical map slot, not F1-DISPATCH-01.
        pallet.location_id = target.id
        lot.warehouse_location_id = target.id
        db.commit()

    with TestClient(app) as client:
        _login(client, "n029-admin")
        delayed = _overview(client)["delayed_dispatch_relocation"]

    assert delayed["candidate_count"] == 1
    assert delayed["items"][0]["pallet_id"] == ids["first_pallet"]
    assert delayed["items"][0]["source_location_id"] == ids["floor3_target"]
    assert delayed["items"][0]["source_floor_code"] == "3F"


def test_unmapped_direct_completion_pallet_is_not_offered_as_a_move_candidate(
    dispatch_pallet_app,
) -> None:
    app, factory, ids = dispatch_pallet_app
    with factory() as db:
        completion = db.get(ProductionCompletion, ids["first_completion"])
        staging = db.get(WarehouseLocation, ids["staging"])
        assert completion is not None and staging is not None
        completion.completed_at = _now() - timedelta(days=5)
        staging.placement_status = "unplaced"
        db.commit()

    with TestClient(app) as client:
        _login(client, "n029-admin")
        delayed = _overview(client)["delayed_dispatch_relocation"]

    assert delayed["candidate_count"] == 0


def test_confirmed_physical_move_reuses_audited_pallet_flow(
    dispatch_pallet_app,
) -> None:
    app, factory, ids = dispatch_pallet_app
    with factory() as db:
        completion = db.get(ProductionCompletion, ids["first_completion"])
        assert completion is not None
        completion.completed_at = _now() - timedelta(days=5)
        order_id = int(completion.order_item_id)
        before_totals = _inventory_totals(db)
        before_movements = int(
            db.scalar(select(func.count(InventoryLocationMovement.id))) or 0
        )
        db.commit()

    body = {
        "idempotency_key": "p1-107-confirmed-physical-move",
        "confirmed": True,
        "items": [
            {
                "client_item_id": "p1-107-first-pallet",
                "operation": "pallet_move",
                "pallet_id": ids["first_pallet"],
                "expected_version": 1,
                "target_location_id": ids["delayed_dispatch_target"],
                "expected_target_layout_version": 1,
            }
        ],
    }
    with TestClient(app) as client:
        _login(client, "n029-admin")
        response = client.post(MOVE_BATCH_URL, json=body)
        assert response.status_code == 200, response.text
        projection = _overview(client)

    assert len(response.json()["items"]) == 1
    assert projection["delayed_dispatch_relocation"]["candidate_count"] == 0
    with factory() as db:
        pallet = db.get(InventoryPallet, ids["first_pallet"])
        completion = db.get(ProductionCompletion, ids["first_completion"])
        assert pallet is not None and completion is not None
        assert pallet.location_id == ids["delayed_dispatch_target"]
        assert pallet.location_occupancy_key == "PRIMARY"
        assert completion.order_item_id == order_id
        assert _inventory_totals(db) == before_totals
        assert int(db.scalar(select(func.count(InventoryLocationMovement.id))) or 0) == before_movements + 1


def test_damaged_direct_pallet_is_not_offered_as_a_move_candidate(
    dispatch_pallet_app,
) -> None:
    app, factory, ids = dispatch_pallet_app
    with factory() as db:
        first = db.get(ProductionCompletion, ids["first_completion"])
        second = db.get(ProductionCompletion, ids["second_completion"])
        damaged_lot = db.get(InventoryLot, ids["first_lot"])
        assert first is not None and second is not None and damaged_lot is not None
        first.completed_at = _now() - timedelta(days=5)
        second.completed_at = _now() - timedelta(days=5)
        physical_quantity = (
            int(damaged_lot.quantity_available)
            + int(damaged_lot.quantity_reserved)
            + int(damaged_lot.quantity_damaged)
        )
        damaged_lot.quantity_available = 0
        damaged_lot.quantity_reserved = 0
        damaged_lot.quantity_damaged = physical_quantity
        db.commit()

    with TestClient(app) as client:
        _login(client, "n029-admin")
        projection = _overview(client)

    assert [
        row["pallet_id"]
        for row in projection["delayed_dispatch_relocation"]["items"]
    ] == [ids["second_pallet"]]
