"""P1-133 mobile stocktake and warehouse-map closure contracts."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models.audit import OperationLog
from app.models.stocktake import StocktakeItem, StocktakeOrder, StocktakeReview
from app.models.warehouse_inventory import (
    FinishedGoodsInventoryDetail,
    InventoryLot,
    InventoryMovement,
    WarehouseLocation,
)
from app.services.warehouse_inventory import _lot_location_transfer_hash
from tests.test_n035_stocktake_api import _login, _logout, stocktake_api


ROOT = Path(__file__).resolve().parents[1]
MOBILE_STOCKTAKE = (ROOT / "static" / "mobile_stocktake.html").read_text(
    encoding="utf-8"
)
MOBILE_ERP = (ROOT / "static" / "mobile_erp.html").read_text(encoding="utf-8")
STOCKTAKE_SERVICE = (ROOT / "app" / "services" / "stocktake.py").read_text(
    encoding="utf-8"
)
WAREHOUSE_SERVICE = (
    ROOT / "app" / "services" / "warehouse_inventory.py"
).read_text(encoding="utf-8")


def test_mobile_map_uses_area_bounds_and_readable_location_colors() -> None:
    from app.api.mobile_erp import _mobile_area_display_bounds

    features = [
        {
            "id": "zone-d1",
            "feature_kind": "zone",
            "erp_area_code": "D1",
            "points": [[16679, -5042], [19229, -5042], [19229, 9958], [16679, 9958]],
        },
        {
            "id": "aisle-d1",
            "feature_kind": "aisle",
            "points": [[15000, -6000], [21000, -6000]],
        },
    ]
    assert _mobile_area_display_bounds(features, area_code="D1") == {
        "min_x": 16679.0,
        "min_y": -5042.0,
        "max_x": 19229.0,
        "max_y": 9958.0,
    }
    assert "compactWarehouseLocation" in MOBILE_ERP
    assert "short_location_label" in MOBILE_ERP
    assert "warehouse-map-location.has-goods { background: #dbeafe" in MOBILE_ERP
    assert "warehouse-map-location.not-disclosed" in MOBILE_ERP
    assert "warehouse-map-location.deep-link { outline: 4px solid #7e22ce" in MOBILE_ERP
    assert "stage.style.height" in MOBILE_ERP
    assert "document.addEventListener(\"visibilitychange\"" in MOBILE_ERP
    assert "window.addEventListener(\"pageshow\"" in MOBILE_ERP


def test_mobile_short_location_label_keeps_row_and_slot_unique() -> None:
    from app.api.mobile_erp import _mobile_short_location_label

    first = type("Location", (), {"ground_row_no": 1, "slot_no": 3, "location_code": "3F-D01-P01-03"})()
    second = type("Location", (), {"ground_row_no": 2, "slot_no": 3, "location_code": "3F-D01-P02-03"})()
    canonical = {"employee_location_name": "三楼 D1区·第1排·3号位"}
    first_label = _mobile_short_location_label(first, canonical=canonical, area_code="D01")
    second_label = _mobile_short_location_label(second, canonical=canonical, area_code="D01")
    assert first_label == "D1·1排·3号位"
    assert second_label == "D1·2排·3号位"
    assert first_label != second_label

    rack_location = type(
        "RackLocation",
        (),
        {"ground_row_no": None, "slot_no": 1, "location_code": "RACK-CELL"},
    )()
    rack_one = _mobile_short_location_label(
        rack_location,
        canonical={"rack_display_name": "R01", "level_no": 1, "slot_no": 1},
        area_code="M01",
    )
    rack_two = _mobile_short_location_label(
        rack_location,
        canonical={"rack_display_name": "R02", "level_no": 1, "slot_no": 1},
        area_code="M01",
    )
    assert rack_one == "R01·1层·1格"
    assert rack_two == "R02·1层·1格"
    assert rack_one != rack_two


def _inventory_state(factory) -> list[tuple[object, ...]]:
    with factory() as db:
        return list(
            db.execute(
                select(
                    InventoryLot.id,
                    InventoryLot.warehouse_location_id,
                    InventoryLot.quantity_available,
                    InventoryLot.quantity_reserved,
                    InventoryLot.quantity_consumed,
                    InventoryLot.quantity_damaged,
                    InventoryLot.quantity_scrapped,
                    InventoryLot.status,
                    InventoryLot.version,
                ).order_by(InventoryLot.id)
            ).all()
        )


def _create_empty_formal_location(factory, *, code: str) -> int:
    with factory() as db:
        location = WarehouseLocation(
            location_code=code,
            location_name=f"{code} 空货位",
            warehouse_type="finished",
            placement_status="placed",
            is_active=True,
        )
        db.add(location)
        db.commit()
        return int(location.id)


def _empty_location_submission(location: dict, *, key: str) -> dict:
    return {
        "location_id": int(location["id"]),
        "location_layout_version": location["layout_version"],
        "location_address_version": location["address_version"],
        "location_position_status": location["position_status"],
        "published_map_revision": location["published_map_revision"],
        "items": [],
        "idempotency_key": key,
    }


def test_mobile_stocktake_rejects_a_stale_location_address_token(stocktake_api) -> None:
    application, factory, _ids = stocktake_api
    location_id = _create_empty_formal_location(factory, code="P1133-STALE-ADDRESS")
    with TestClient(application) as client:
        _login(client, "n035-workshop")
        detail = client.get(
            f"/api/warehouse/stocktake/locations/{location_id}"
        ).json()
        with factory() as db:
            row = db.get(WarehouseLocation, location_id)
            row.address_version = int(row.address_version or 1) + 1
            db.commit()
        rejected = client.post(
            "/api/warehouse/stocktakes",
            json=_empty_location_submission(detail, key="p1133-stale-address"),
        )
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["detail"]["code"] == "STOCKTAKE_LOCATION_CHANGED"


def test_stocktake_approval_rechecks_the_location_identity_snapshot(stocktake_api) -> None:
    application, factory, _ids = stocktake_api
    location_id = _create_empty_formal_location(factory, code="P1133-APPROVE-ADDRESS")
    with TestClient(application) as client:
        _login(client, "n035-workshop")
        detail = client.get(
            f"/api/warehouse/stocktake/locations/{location_id}"
        ).json()
        submitted = client.post(
            "/api/warehouse/stocktakes",
            json=_empty_location_submission(detail, key="p1133-approve-address-submit"),
        )
        assert submitted.status_code == 201, submitted.text
        order_id = int(submitted.json()["id"])
        with factory() as db:
            row = db.get(WarehouseLocation, location_id)
            row.address_version = int(row.address_version or 1) + 1
            db.commit()
        _logout(client)
        _login(client, "n035-admin")
        rejected = client.post(
            f"/api/warehouse/stocktakes/{order_id}/approve",
            json={"idempotency_key": "p1133-approve-address", "reason": "复核"},
        )
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["detail"]["code"] == "STOCKTAKE_LOCATION_CHANGED"
    with factory() as db:
        assert db.get(StocktakeOrder, order_id).status == "submitted"


def test_empty_formal_location_can_submit_and_approve_without_inventory_change(
    stocktake_api,
) -> None:
    application, factory, _ids = stocktake_api
    location_id = _create_empty_formal_location(factory, code="P1133-EMPTY-APPROVE")
    before = _inventory_state(factory)

    with TestClient(application) as client:
        _login(client, "n035-workshop")
        location = client.get(
            f"/api/warehouse/stocktake/locations/{location_id}"
        )
        assert location.status_code == 200, location.text
        assert location.json()["lots"] == []

        submitted = client.post(
            "/api/warehouse/stocktakes",
            json={
                "location_id": location_id,
                "location_layout_version": location.json()["layout_version"],
                "location_address_version": location.json()["address_version"],
                "location_position_status": location.json()["position_status"],
                "published_map_revision": location.json()["published_map_revision"],
                "items": [],
                "idempotency_key": "p1133-empty-submit",
            },
        )
        assert submitted.status_code == 201, submitted.text
        assert submitted.json()["status"] == "submitted"
        assert submitted.json()["items"] == []
        order_id = int(submitted.json()["id"])

        _logout(client)
        _login(client, "n035-admin")
        approved = client.post(
            f"/api/warehouse/stocktakes/{order_id}/approve",
            json={
                "idempotency_key": "p1133-empty-approve",
                "reason": "现场复核为空",
            },
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "approved"
        assert approved.json()["items"] == []

    assert _inventory_state(factory) == before
    with factory() as db:
        order = db.get(StocktakeOrder, order_id)
        assert order is not None and order.status == "approved"
        assert db.scalar(
            select(func.count(StocktakeItem.id)).where(
                StocktakeItem.order_id == order_id
            )
        ) == 0
        review = db.scalar(
            select(StocktakeReview).where(StocktakeReview.order_id == order_id)
        )
        assert review is not None
        assert review.action == "approve"
        assert review.details_json == {"adjustments": []}
        assert db.scalar(select(func.count(InventoryMovement.id))) == 0
        assert list(
            db.scalars(
                select(OperationLog.action)
                .where(
                    OperationLog.entity_id == order_id,
                    OperationLog.action.in_(
                        ("STOCKTAKE_SUBMIT", "STOCKTAKE_APPROVE")
                    ),
                )
                .order_by(OperationLog.id)
            ).all()
        ) == ["STOCKTAKE_SUBMIT", "STOCKTAKE_APPROVE"]


def test_stale_empty_submission_rejects_lot_added_after_location_read(
    stocktake_api,
) -> None:
    application, factory, ids = stocktake_api
    location_id = _create_empty_formal_location(factory, code="P1133-EMPTY-RACE")

    with TestClient(application) as client:
        _login(client, "n035-workshop")
        location = client.get(
            f"/api/warehouse/stocktake/locations/{location_id}"
        )
        assert location.status_code == 200, location.text
        assert location.json()["lots"] == []

        with factory() as db:
            template = db.get(InventoryLot, ids["lot1"])
            assert template is not None and template.finished_detail is not None
            detail = template.finished_detail
            added = InventoryLot(
                lot_number="P1133-LOT-ADDED-DURING-SUBMIT",
                inventory_type="finished",
                warehouse_location_id=location_id,
                quantity_available=5,
                quantity_reserved=0,
                quantity_consumed=0,
                quantity_damaged=0,
                quantity_scrapped=0,
                unit="boxes",
                status="active",
                source_type="manual",
                stock_date=date(2026, 9, 1),
                last_movement_at=datetime(2026, 9, 1, 9, 0),
                version=1,
                created_by=ids["workshop"],
            )
            added.finished_detail = FinishedGoodsInventoryDetail(
                owner_customer_id=detail.owner_customer_id,
                owner_customer_name_snapshot=detail.owner_customer_name_snapshot,
                is_general=detail.is_general,
                product_id=detail.product_id,
                inventory_code_snapshot=detail.inventory_code_snapshot,
                product_name_snapshot=detail.product_name_snapshot,
                length_mm=detail.length_mm,
                width_mm=detail.width_mm,
                height_mm=detail.height_mm,
            )
            db.add(added)
            db.commit()
            added_lot_id = int(added.id)

        rejected = client.post(
            "/api/warehouse/stocktakes",
            json={
                "location_id": location_id,
                "location_layout_version": location.json()["layout_version"],
                "location_address_version": location.json()["address_version"],
                "location_position_status": location.json()["position_status"],
                "published_map_revision": location.json()["published_map_revision"],
                "items": [],
                "idempotency_key": "p1133-stale-empty-submit",
            },
        )
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["detail"]["code"] == "STOCKTAKE_DRIFT"
        assert "完整覆盖库位全部批次" in rejected.json()["detail"]["message"]
        assert str(added_lot_id) in rejected.json()["detail"]["message"]

    with factory() as db:
        added = db.get(InventoryLot, added_lot_id)
        assert added is not None
        assert (
            added.quantity_available,
            added.quantity_reserved,
            added.warehouse_location_id,
            added.version,
        ) == (5, 0, location_id, 1)
        assert db.scalar(
            select(func.count(StocktakeOrder.id)).where(
                StocktakeOrder.idempotency_key == "p1133-stale-empty-submit"
            )
        ) == 0
        assert db.scalar(select(func.count(InventoryMovement.id))) == 0
        assert db.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.action == "STOCKTAKE_SUBMIT",
                OperationLog.details.contains("p1133-stale-empty-submit"),
            )
        ) == 0


def test_mobile_stocktake_has_short_actions_safe_area_and_rack_identity() -> None:
    for label in ("数量正确", "位置不对", "确认现场为空", "看地图"):
        assert label in MOBILE_STOCKTAKE
    assert "viewport-fit=cover" in MOBILE_STOCKTAKE
    assert "safe-area-inset-top" in MOBILE_STOCKTAKE
    assert "safe-area-inset-bottom" in MOBILE_STOCKTAKE
    assert "@media(max-width:420px)" in MOBILE_STOCKTAKE

    assert 'params=new URLSearchParams({warehouse_map:"1"})' in MOBILE_STOCKTAKE
    assert 'params.set("floor_code",floorCode)' in MOBILE_STOCKTAKE
    assert 'params.set("area_code",areaCode)' in MOBILE_STOCKTAKE
    assert 'params.set("location_id",String(locationId))' in MOBILE_STOCKTAKE
    assert 'params.set("lot_id",String(lotId))' in MOBILE_STOCKTAKE
    assert "#warehouse" in MOBILE_STOCKTAKE

    assert '"map_rack_id": payload["map_rack_id"]' in STOCKTAKE_SERVICE
    assert 'pick(location,["rack_display_name"]' in MOBILE_STOCKTAKE
    assert 'pick(location,["level_no"]' in MOBILE_STOCKTAKE
    assert 'pick(location,["slot_no"]' in MOBILE_STOCKTAKE
    assert "第 ${level} 层 · 第 ${slot} 格" in MOBILE_STOCKTAKE


def test_mobile_erp_deep_link_opens_area_and_lot_with_target_guidance() -> None:
    assert 'launchParams.get("warehouse_map") === "1"' in MOBILE_ERP
    assert 'floorCode: launchParams.get("floor_code") || undefined' in MOBILE_ERP
    assert 'areaCode: launchParams.get("area_code") || undefined' in MOBILE_ERP
    assert 'locationId: Number(launchParams.get("location_id") || 0) || undefined' in MOBILE_ERP
    assert 'lotId: Number(launchParams.get("lot_id") || 0) || undefined' in MOBILE_ERP
    assert "preferredLocationId: state.warehouseMapFocusLocationId" in MOBILE_ERP
    assert "Number(item.location_id) === Number(state.warehouseMapFocusLocationId)" in MOBILE_ERP
    assert "renderWarehouseLocationGoods(focusedLocation, preferredLotId)" in MOBILE_ERP
    assert "Number(good.lot_id) === Number(preferredLotId)" in MOBILE_ERP

    assert "已有同一产品；可以共用货位，系统会保留每个批次" in MOBILE_ERP
    assert "已有不同货物，不能直接混放" in MOBILE_ERP
    assert 'String(target.status || "active") !== "active"' in MOBILE_ERP
    assert "Number(target.quantity_damaged || 0) > 0" in MOBILE_ERP
    assert "质量冻结/损坏，仅供核对" in MOBILE_ERP
    assert "目标货位已有冻结、损坏或待核对货物" in WAREHOUSE_SERVICE
    assert "同品可共位保留批次，异品请换空位" in WAREHOUSE_SERVICE


def test_mobile_move_request_hash_includes_both_location_snapshots() -> None:
    request = {
        "lot_id": 17,
        "expected_version": 3,
        "quantity": 8,
        "location_id": 29,
        "expected_source_location_id": 11,
        "expected_source_address_version": 2,
        "expected_source_layout_version": 7,
        "expected_source_map_revision": "map-r1",
        "expected_target_address_version": 3,
        "expected_target_map_revision": "map-r1",
        "ground_secondary_location_id": None,
        "ground_capacity_quantity": None,
    }
    first = _lot_location_transfer_hash(
        **request,
        expected_target_layout_version=4,
    )
    replay = _lot_location_transfer_hash(
        **request,
        expected_target_layout_version=4,
    )
    changed_layout = _lot_location_transfer_hash(
        **request,
        expected_target_layout_version=5,
    )
    missing_layout = _lot_location_transfer_hash(
        **request,
        expected_target_layout_version=None,
    )
    changed_source = _lot_location_transfer_hash(
        **{**request, "expected_source_location_id": 12},
        expected_target_layout_version=4,
    )
    changed_source_map = _lot_location_transfer_hash(
        **{**request, "expected_source_map_revision": "map-r2"},
        expected_target_layout_version=4,
    )

    assert first == replay
    assert len({first, changed_layout, missing_layout, changed_source, changed_source_map}) == 5
    for marker in (
        "expected_source_location_id: ledgerSource.location_id",
        "expected_source_address_version: ledgerSource.address_version",
        "expected_source_layout_version: sourceLayoutVersion",
        "expected_source_map_revision: ledgerSource.published_map_revision",
        "expected_target_layout_version: targetLayoutVersion",
        "expected_target_address_version: target.address_version",
        "expected_target_map_revision: target.published_map_revision",
    ):
        assert marker in MOBILE_ERP
    assert 'state.warehouseMapMoveRequestKey ||= warehouseRequestKey("mobile-map-move")' in MOBILE_ERP
