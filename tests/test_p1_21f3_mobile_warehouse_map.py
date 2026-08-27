from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import importlib.util
from pathlib import Path
import re
import sqlite3
import subprocess

from alembic import command
from alembic.config import Config
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from test_p1_21b_mobile_admin_product_search import _login, mobile_erp_app


ROOT = Path(__file__).resolve().parents[1]
MOBILE_HTML = (ROOT / "static" / "mobile_erp.html").read_text(encoding="utf-8")
MOBILE_STOCKTAKE_HTML = (ROOT / "static" / "mobile_stocktake.html").read_text(
    encoding="utf-8"
)
PARENT = "ee13v8x9z02"
TARGET = "ff14v8x9z03"
MIGRATION = (
    ROOT
    / "alembic"
    / "versions"
    / "ff14v8x9z03_mobile_warehouse_location_discrepancies.py"
)


def _add_map_target(
    factory, *, code: str = "C1-L02", with_existing: bool = True
) -> tuple[int, int]:
    from app.models.product import Product
    from app.models.warehouse_inventory import (
        FinishedGoodsInventoryDetail,
        Floor3LocationLayout,
        InventoryLot,
        InventoryPalletItem,
        WarehouseGroundLayoutPlan,
        WarehouseGroundLayoutSlot,
        WarehouseLocation,
    )

    with factory() as db:
        source_location = db.scalar(
            select(WarehouseLocation).where(WarehouseLocation.location_code == "C1-L01")
        )
        source_lot = db.scalar(
            select(InventoryLot).where(InventoryLot.lot_number == "FG-MOBILE-001")
        )
        product = db.scalar(select(Product).where(Product.product_code == "MOBILE-BOX-001"))
        assert source_location and source_lot and product and source_lot.finished_detail
        source_lot.quantity_available = 77
        source_lot.quantity_reserved = 3
        source_lot.stock_date = date(2026, 7, 1)
        source_item = db.scalar(
            select(InventoryPalletItem).where(
                InventoryPalletItem.inventory_lot_id == source_lot.id
            )
        )
        assert source_item is not None
        source_item.quantity = Decimal("80")
        target = WarehouseLocation(
            location_code=code,
            location_name="三楼 C1 第二实测位置",
            warehouse_type="finished",
            warehouse_floor=3,
            area_code="C1",
            storage_type="ground",
            placement_status="placed",
            source_version="TWIN_V1",
        )
        db.add(target)
        db.flush()
        db.add(
            Floor3LocationLayout(
                location_id=target.id,
                left_pct=Decimal("35"),
                top_pct=Decimal("10"),
                width_pct=Decimal("10"),
                height_pct=Decimal("10"),
                source_type="manual",
            )
        )
        ground_plan = db.scalar(
            select(WarehouseGroundLayoutPlan).where(
                WarehouseGroundLayoutPlan.status == "published"
            )
        )
        assert ground_plan is not None
        ground_plan.target_slot_count = int(ground_plan.target_slot_count or 0) + 1
        db.add(
            WarehouseGroundLayoutSlot(
                plan_id=ground_plan.id,
                location_id=target.id,
                route_sequence=ground_plan.target_slot_count,
                row_no=1,
                slot_no=ground_plan.target_slot_count,
                x_mm=Decimal(str(1000 + ground_plan.target_slot_count * 1200)),
                y_mm=Decimal("1000"),
                width_mm=1200,
                depth_mm=1000,
            )
        )
        if with_existing:
            existing = InventoryLot(
                lot_number=f"FG-{code}-EXISTING",
                inventory_type="finished",
                warehouse_location_id=target.id,
                quantity_available=20,
                quantity_reserved=0,
                unit="boxes",
                status="active",
                source_type="manual",
                stock_date=date(2026, 7, 2),
                last_movement_at=datetime(2026, 8, 1, 0, 0, 0),
            )
            existing.finished_detail = FinishedGoodsInventoryDetail(
                owner_customer_id=product.customer_id,
                owner_customer_name_snapshot="匿名客户甲",
                is_general=False,
                product_id=product.id,
                inventory_code_snapshot=product.product_code,
                product_name_snapshot=product.product_name,
            )
            db.add(existing)
        db.commit()
        return int(source_lot.id), int(target.id)


def test_mobile_map_uses_published_geometry_without_pallet_identifiers(
    mobile_erp_app,
) -> None:
    from app.models.product import Product
    from app.models.warehouse_inventory import FinishedGoodsInventoryDetail, InventoryLot

    app, _ids, factory = mobile_erp_app
    source_lot_id, target_location_id = _add_map_target(factory)
    with factory() as db:
        other = db.scalar(select(Product).where(Product.product_code == "OTHER-MOBILE-001"))
        assert other is not None
        hidden_lot = InventoryLot(
            lot_number="FG-F3-HIDDEN-CUSTOMER",
            inventory_type="finished",
            warehouse_location_id=target_location_id,
            quantity_available=9,
            unit="boxes",
            status="active",
            source_type="manual",
            stock_date=date(2026, 8, 1),
            last_movement_at=datetime(2026, 8, 1, 0, 0, 0),
        )
        hidden_lot.finished_detail = FinishedGoodsInventoryDetail(
            owner_customer_id=other.customer_id,
            owner_customer_name_snapshot="匿名客户乙",
            is_general=False,
            product_id=other.id,
            inventory_code_snapshot=other.product_code,
            product_name_snapshot=other.product_name,
        )
        db.add(hidden_lot)
        db.commit()
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        floors = client.get("/api/mobile/erp/warehouse/map/floors")
        assert floors.status_code == 200, floors.text
        assert floors.headers["cache-control"] == "private, no-store"
        c1 = next(
            area
            for floor in floors.json()["floors"]
            if floor["floor_code"] == "3F"
            for area in floor["areas"]
            if area["area_code"] == "C1"
        )
        assert c1["map_status"] == "ready"
        area = client.get(
            "/api/mobile/erp/warehouse/map/floors/3F", params={"area_code": "C1"}
        )
        assert area.status_code == 200, area.text
        payload = area.json()
        assert payload["map_status"] == "ready"
        assert payload["guidance"].endswith("到现场后核对相邻位置。")
        target = next(
            row for row in payload["locations"] if row["location_id"] == target_location_id
        )
        assert target["geometry"] == {
            "left_pct": 35.0,
            "top_pct": 10.0,
            "width_pct": 10.0,
            "height_pct": 10.0,
            "z_index": 0,
            "version": 1,
        }
        assert any(
            good["lot_id"] == source_lot_id
            for row in payload["locations"]
            for good in row["goods"]
        )
        assert "pallet" not in area.text.lower()
        pending_area = client.get(
            "/api/mobile/erp/warehouse/map/floors/1F", params={"area_code": "FG"}
        )
        assert pending_area.status_code == 200, pending_area.text
        assert pending_area.json()["map_status"] == "unmeasured"
        assert pending_area.json()["map_status_text"] == "未建立实测地图"
        assert "不会生成假坐标" in pending_area.json()["guidance"]

    with TestClient(app) as client:
        _login(client, "mobile-scoped")
        scoped = client.get(
            "/api/mobile/erp/warehouse/map/floors/3F", params={"area_code": "C1"}
        )
        assert scoped.status_code == 200, scoped.text
        assert "匿名客户乙" not in scoped.text
        assert "OTHER-MOBILE-001" not in scoped.text
        assert any(
            good["lot_id"] == source_lot_id
            for location in scoped.json()["locations"]
            for good in location["goods"]
        )


def test_all_mobile_and_employee_location_lists_share_the_area_projection(
    mobile_erp_app,
) -> None:
    from app.models.product import Product
    from app.models.user import User
    from app.models.warehouse_inventory import WarehouseArea
    from app.api import warehouse as warehouse_api
    from app.services.production_workflow import list_temporary_locations
    from app.services.stocktake import list_locations as list_stocktake_locations

    app, _ids, factory = mobile_erp_app
    _add_map_target(factory)
    with factory() as db:
        area = db.scalar(select(WarehouseArea).where(WarehouseArea.area_code == "C1"))
        product = db.scalar(select(Product).where(Product.product_code == "MOBILE-BOX-001"))
        admin = db.scalar(select(User).where(User.username == "mobile-admin"))
        assert area is not None and product is not None and admin is not None
        area.area_name = "C1 区"
        db.commit()
        customer_id = int(product.customer_id)
        product_id = int(product.id)

        stocktake_location = next(
            row
            for row in list_stocktake_locations(db)
            if row["area_code"] == "C1"
        )
        production_location = next(
            row
            for row in list_temporary_locations(db)
            if row["area_code"] == "C1"
        )
        assert stocktake_location["area_name"] == "右区C1"
        assert stocktake_location["area_master_name"] == "C1 区"
        assert production_location["area_name"] == "右区C1"
        assert production_location["area_master_name"] == "C1 区"
        ground = warehouse_api.list_ground_storage_candidates(
            floor_code="3F",
            area_code="C1",
            customer_id=customer_id,
            product_id=product_id,
            incoming_quantity=1,
            db=db,
            user=admin,
        )
        assert ground["area_name"] == "右区C1"
        assert ground["area_master_name"] == "C1 区"

    with TestClient(app) as client:
        _login(client, "mobile-admin")
        floors = client.get("/api/mobile/erp/warehouse/map/floors")
        assert floors.status_code == 200, floors.text
        mobile_area = next(
            area
            for floor in floors.json()["floors"]
            if floor["floor_code"] == "3F"
            for area in floor["areas"]
            if area["area_code"] == "C1"
        )
        assert mobile_area["area_name"] == "右区C1"
        assert mobile_area["area_master_name"] == "C1 区"

        detail = client.get(
            "/api/mobile/erp/warehouse/map/floors/3F",
            params={"area_code": "C1"},
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["area_name"] == "右区C1"
        assert detail.json()["area_master_name"] == "C1 区"
        assert any(
            feature["feature_kind"] == "zone" and feature["name"] == "右区C1"
            for feature in detail.json()["features"]
        )


def test_confirmed_partial_move_preserves_total_age_reservations_and_idempotency(
    mobile_erp_app,
) -> None:
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryLotTransfer,
        InventoryPalletItem,
        InventoryReservation,
    )

    app, _ids, factory = mobile_erp_app
    source_lot_id, target_location_id = _add_map_target(factory)
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        not_confirmed = client.post(
            f"/api/mobile/erp/warehouse/lots/{source_lot_id}/moves",
            json={
                "expected_version": 1,
                "quantity": 50,
                "target_location_id": target_location_id,
                "expected_target_layout_version": 1,
                "idempotency_key": "f3-move-not-confirmed",
                "physical_move_confirmed": False,
            },
        )
        assert not_confirmed.status_code == 409
        moved = client.post(
            f"/api/mobile/erp/warehouse/lots/{source_lot_id}/moves",
            json={
                "expected_version": 1,
                "quantity": 50,
                "target_location_id": target_location_id,
                "expected_target_layout_version": 1,
                "idempotency_key": "f3-move-001",
                "physical_move_confirmed": True,
            },
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["idempotent_replay"] is False
        replay = client.post(
            f"/api/mobile/erp/warehouse/lots/{source_lot_id}/moves",
            json={
                "expected_version": 1,
                "quantity": 50,
                "target_location_id": target_location_id,
                "expected_target_layout_version": 1,
                "idempotency_key": "f3-move-001",
                "physical_move_confirmed": True,
            },
        )
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True
        stale = client.post(
            f"/api/mobile/erp/warehouse/lots/{source_lot_id}/moves",
            json={
                "expected_version": 1,
                "quantity": 1,
                "target_location_id": target_location_id,
                "expected_target_layout_version": 1,
                "idempotency_key": "f3-move-stale-version",
                "physical_move_confirmed": True,
            },
        )
        assert stale.status_code == 409
        assert "刷新" in stale.text

    with factory() as db:
        lots = list(db.scalars(select(InventoryLot)).all())
        source = db.get(InventoryLot, source_lot_id)
        assert source is not None
        source_total = source.quantity_available + source.quantity_reserved
        target_total = sum(
            lot.quantity_available + lot.quantity_reserved
            for lot in lots
            if lot.warehouse_location_id == target_location_id and lot.status == "active"
        )
        assert (source_total, target_total, source_total + target_total) == (30, 70, 100)
        moved_lot = next(
            lot
            for lot in lots
            if lot.warehouse_location_id == target_location_id
            and lot.lot_number != f"FG-C1-L02-EXISTING"
        )
        assert moved_lot.stock_date == date(2026, 7, 1)
        assert db.scalar(select(func.count(InventoryLotTransfer.id))) == 1
        reservation = db.scalar(
            select(InventoryReservation).where(
                InventoryReservation.inventory_lot_id == source_lot_id
            )
        )
        assert reservation is not None
        assert reservation.reserved_stock_quantity - reservation.released_stock_quantity == 3
        source_item = db.scalar(
            select(InventoryPalletItem).where(
                InventoryPalletItem.inventory_lot_id == source_lot_id
            )
        )
        assert source_item is not None and source_item.quantity == Decimal("30")


def test_employee_report_is_read_only_until_authorized_correction(
    mobile_erp_app,
) -> None:
    from app.models.warehouse_inventory import InventoryLot, WarehouseLocationDiscrepancy

    app, _ids, factory = mobile_erp_app
    source_lot_id, target_location_id = _add_map_target(factory, code="C1-L03")
    with TestClient(app) as client:
        _login(client, "mobile-scoped")
        reported = client.post(
            "/api/mobile/erp/warehouse/location-discrepancies",
            json={
                "inventory_lot_id": source_lot_id,
                "expected_lot_version": 1,
                "reported_quantity": 10,
                "observed_location_id": target_location_id,
                "observed_location_layout_version": 1,
                "reason": "现场货物与系统登记位置不一致",
                "idempotency_key": "f3-report-001",
            },
        )
        assert reported.status_code == 201, reported.text
        report = reported.json()["report"]
        assert report["status"] == "open"
        assert report["observed_location_layout_version"] == 1
        assert report["registered_location"]["location_code"] == "C1-L01"
        assert client.get("/api/mobile/erp/warehouse/location-discrepancies").status_code == 403
        denied = client.post(
            f"/api/mobile/erp/warehouse/location-discrepancies/{report['id']}/resolve",
            json={
                "expected_version": 1,
                "expected_lot_version": 1,
                "idempotency_key": "f3-correct-denied",
                "resolution_note": "无权限不应纠正",
            },
        )
        assert denied.status_code == 403

        _login(client, "mobile-admin")
        listed = client.get("/api/mobile/erp/warehouse/location-discrepancies")
        assert listed.status_code == 200, listed.text
        assert [item["id"] for item in listed.json()["items"]] == [report["id"]]
        assert listed.json()["items"][0]["observed_location"][
            "position_status"
        ] == "mapped"
        corrected = client.post(
            f"/api/mobile/erp/warehouse/location-discrepancies/{report['id']}/resolve",
            json={
                "expected_version": 1,
                "expected_lot_version": 1,
                "idempotency_key": "f3-correct-001",
                "resolution_note": "已核对现场位置，确认纠正系统登记",
            },
        )
        assert corrected.status_code == 200, corrected.text
        assert corrected.json()["report_version"] == 2

    with factory() as db:
        row = db.scalar(select(WarehouseLocationDiscrepancy))
        source = db.get(InventoryLot, source_lot_id)
        assert row is not None and row.status == "resolved"
        assert row.observed_location_layout_version == 1
        assert row.resolution_transfer_id is not None
        assert source is not None
        assert source.quantity_available + source.quantity_reserved == 70
        assert sum(
            lot.quantity_available + lot.quantity_reserved
            for lot in db.scalars(select(InventoryLot)).all()
            if lot.status == "active"
            and lot.warehouse_location_id
            in {row.registered_location_id, row.observed_location_id}
        ) == 100


def test_delayed_discrepancy_correction_rejects_changed_observed_layout(
    mobile_erp_app,
) -> None:
    from app.models.warehouse_inventory import (
        Floor3LocationLayout,
        InventoryLotTransfer,
        WarehouseLocationDiscrepancy,
    )

    app, _ids, factory = mobile_erp_app
    source_lot_id, target_location_id = _add_map_target(
        factory, code="C1-L03-STALE"
    )
    with TestClient(app) as client:
        _login(client, "mobile-scoped")
        reported = client.post(
            "/api/mobile/erp/warehouse/location-discrepancies",
            json={
                "inventory_lot_id": source_lot_id,
                "expected_lot_version": 1,
                "reported_quantity": 10,
                "observed_location_id": target_location_id,
                "observed_location_layout_version": 1,
                "reason": "现场位置待复核",
                "idempotency_key": "f3-report-stale-layout",
            },
        )
        assert reported.status_code == 201, reported.text
        report_id = reported.json()["report"]["id"]

        with factory() as db:
            layout = db.scalar(
                select(Floor3LocationLayout).where(
                    Floor3LocationLayout.location_id == target_location_id
                )
            )
            assert layout is not None
            layout.version = 2
            db.commit()

        _login(client, "mobile-admin")
        corrected = client.post(
            f"/api/mobile/erp/warehouse/location-discrepancies/{report_id}/resolve",
            json={
                "expected_version": 1,
                "expected_lot_version": 1,
                "idempotency_key": "f3-correct-stale-layout",
                "resolution_note": "复核后尝试纠正",
            },
        )
        assert corrected.status_code == 409
        assert "刷新" in corrected.text

    with factory() as db:
        report = db.get(WarehouseLocationDiscrepancy, report_id)
        assert report is not None and report.status == "open"
        assert report.observed_location_layout_version == 1
        assert db.scalar(select(func.count(InventoryLotTransfer.id))) == 0


def test_open_discrepancy_marks_observed_map_until_physical_return_is_confirmed(
    mobile_erp_app,
) -> None:
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryPallet,
        InventoryLotTransfer,
        WarehouseFloor,
        WarehouseLocation,
        WarehouseLocationDiscrepancy,
    )
    from app.services.warehouse_twin_dashboard import build_warehouse_twin_dashboard

    app, _ids, factory = mobile_erp_app
    source_lot_id, observed_location_id = _add_map_target(
        factory, code="C1-L03-RED", with_existing=False
    )
    with TestClient(app) as client:
        _login(client, "mobile-scoped")
        reported = client.post(
            "/api/mobile/erp/warehouse/location-discrepancies",
            json={
                "inventory_lot_id": source_lot_id,
                "expected_lot_version": 1,
                "reported_quantity": 80,
                "observed_location_id": observed_location_id,
                "observed_location_layout_version": 1,
                "reason": "现场临放在另一货位，等待搬回或重新归位",
                "idempotency_key": "f3-report-persistent-red",
            },
        )
        assert reported.status_code == 201, reported.text
        report = reported.json()["report"]
        area = client.get(
            "/api/mobile/erp/warehouse/map/floors/3F",
            params={"area_code": "C1"},
        )
        assert area.status_code == 200, area.text
        observed = next(
            item
            for item in area.json()["locations"]
            if item["location_id"] == observed_location_id
        )
        assert observed["has_location_discrepancy"] is True
        assert observed["location_discrepancy_count"] == 1
        assert observed["discrepant_goods"][0]["report_id"] == report["id"]
        assert observed["discrepant_goods"][0]["lot"]["lot_id"] == source_lot_id
        with factory() as db:
            overview = build_warehouse_twin_dashboard(
                db,
                lots=list(db.scalars(select(InventoryLot)).all()),
                locations=list(db.scalars(select(WarehouseLocation)).all()),
                pallets=list(db.scalars(select(InventoryPallet)).all()),
                floors=list(db.scalars(select(WarehouseFloor)).all()),
                visible_customer_ids=None,
                days=30,
                as_of=date(2026, 8, 27),
            )
            desktop_observed = next(
                item
                for item in overview["locations"]
                if item["location_id"] == observed_location_id
            )
            assert desktop_observed["has_location_discrepancy"] is True
            assert desktop_observed["location_discrepancy_count"] == 1
            assert desktop_observed["location_discrepancies"][0]["id"] == report["id"]

        _login(client, "mobile-admin")
        resolved = client.post(
            f"/api/mobile/erp/warehouse/location-discrepancies/{report['id']}/resolve",
            json={
                "expected_version": 1,
                "expected_lot_version": 1,
                "resolution_action": "physical_returned",
                "idempotency_key": "f3-physical-returned",
                "resolution_note": "实物已搬回系统登记货位",
            },
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["transfer_id"] is None
        replay = client.post(
            f"/api/mobile/erp/warehouse/location-discrepancies/{report['id']}/resolve",
            json={
                "expected_version": 1,
                "expected_lot_version": 1,
                "resolution_action": "physical_returned",
                "idempotency_key": "f3-physical-returned",
                "resolution_note": "实物已搬回系统登记货位",
            },
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True
        after = client.get(
            "/api/mobile/erp/warehouse/map/floors/3F",
            params={"area_code": "C1"},
        )
        after_observed = next(
            item
            for item in after.json()["locations"]
            if item["location_id"] == observed_location_id
        )
        assert after_observed["has_location_discrepancy"] is False

    with factory() as db:
        lot = db.get(InventoryLot, source_lot_id)
        row = db.get(WarehouseLocationDiscrepancy, report["id"])
        assert lot is not None and lot.version == 1
        assert lot.quantity_available + lot.quantity_reserved == 80
        assert row is not None and row.status == "resolved"
        assert row.resolution_transfer_id is None
        assert db.scalar(select(func.count(InventoryLotTransfer.id))) == 0


def test_discrepant_goods_can_move_to_another_position_and_close_report_atomically(
    mobile_erp_app,
) -> None:
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryLotTransfer,
        WarehouseLocationDiscrepancy,
    )

    app, _ids, factory = mobile_erp_app
    source_lot_id, observed_location_id = _add_map_target(
        factory, code="C1-L06-RED", with_existing=False
    )
    _same_lot, target_location_id = _add_map_target(
        factory, code="C1-L07-CORRECT", with_existing=False
    )
    with TestClient(app) as client:
        _login(client, "mobile-scoped")
        reported = client.post(
            "/api/mobile/erp/warehouse/location-discrepancies",
            json={
                "inventory_lot_id": source_lot_id,
                "expected_lot_version": 1,
                "reported_quantity": 80,
                "observed_location_id": observed_location_id,
                "observed_location_layout_version": 1,
                "reason": "现场临放，等待移动到正确空位",
                "idempotency_key": "f3-report-red-move",
            },
        )
        assert reported.status_code == 201, reported.text
        report = reported.json()["report"]

        _login(client, "mobile-admin")
        move_payload = {
            "expected_version": 1,
            "quantity": 80,
            "target_location_id": target_location_id,
            "expected_target_layout_version": 1,
            "location_discrepancy_id": report["id"],
            "expected_discrepancy_version": 1,
            "idempotency_key": "f3-red-move-to-correct",
            "physical_move_confirmed": True,
        }
        moved = client.post(
            f"/api/mobile/erp/warehouse/lots/{source_lot_id}/moves",
            json=move_payload,
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["resolved_location_discrepancy_id"] == report["id"]
        replay = client.post(
            f"/api/mobile/erp/warehouse/lots/{source_lot_id}/moves",
            json=move_payload,
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True

    with factory() as db:
        row = db.get(WarehouseLocationDiscrepancy, report["id"])
        assert row is not None and row.status == "resolved"
        assert row.resolution_transfer_id is not None
        assert db.scalar(select(func.count(InventoryLotTransfer.id))) == 1
        positive = list(
            db.scalars(
                select(InventoryLot).where(
                    InventoryLot.status.in_(("active", "frozen")),
                    InventoryLot.quantity_available
                    + InventoryLot.quantity_reserved
                    + InventoryLot.quantity_damaged
                    > 0,
                )
            ).all()
        )
        assert sum(
            lot.quantity_available + lot.quantity_reserved + lot.quantity_damaged
            for lot in positive
        ) >= 80
        assert any(
            lot.warehouse_location_id == target_location_id
            and lot.quantity_available + lot.quantity_reserved == 80
            for lot in positive
        )


def test_mobile_move_rejects_incompatible_occupied_target_but_allows_red_report(
    mobile_erp_app,
) -> None:
    from app.models.product import Product
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryLotTransfer,
        WarehouseLocationDiscrepancy,
    )

    app, _ids, factory = mobile_erp_app
    source_lot_id, target_location_id = _add_map_target(
        factory, code="C1-L08-OCCUPIED", with_existing=True
    )
    with factory() as db:
        target_lot = db.scalar(
            select(InventoryLot).where(
                InventoryLot.warehouse_location_id == target_location_id
            )
        )
        other_product = db.scalar(
            select(Product).where(Product.product_code == "MOBILE-BOX-002")
        )
        assert target_lot is not None and target_lot.finished_detail is not None
        assert other_product is not None
        target_lot.finished_detail.product_id = other_product.id
        target_lot.finished_detail.inventory_code_snapshot = other_product.product_code
        target_lot.finished_detail.product_name_snapshot = other_product.product_name
        db.commit()

    with TestClient(app) as client:
        _login(client, "mobile-admin")
        blocked = client.post(
            f"/api/mobile/erp/warehouse/lots/{source_lot_id}/moves",
            json={
                "expected_version": 1,
                "quantity": 80,
                "target_location_id": target_location_id,
                "expected_target_layout_version": 1,
                "idempotency_key": "f3-incompatible-target",
                "physical_move_confirmed": True,
            },
        )
        assert blocked.status_code == 409, blocked.text
        assert "不能直接混放" in blocked.text
        reported = client.post(
            "/api/mobile/erp/warehouse/location-discrepancies",
            json={
                "inventory_lot_id": source_lot_id,
                "expected_lot_version": 1,
                "reported_quantity": 80,
                "observed_location_id": target_location_id,
                "observed_location_layout_version": 1,
                "reason": "现场发现该批货物临放在已有其他货物的货位",
                "idempotency_key": "f3-incompatible-target-red",
            },
        )
        assert reported.status_code == 201, reported.text

    with factory() as db:
        assert db.scalar(select(func.count(InventoryLotTransfer.id))) == 0
        assert db.scalar(select(func.count(WarehouseLocationDiscrepancy.id))) == 1


def test_open_discrepancy_stops_rendering_after_lot_has_no_physical_quantity(
    mobile_erp_app,
) -> None:
    from app.models.warehouse_inventory import InventoryLot

    app, _ids, factory = mobile_erp_app
    source_lot_id, observed_location_id = _add_map_target(
        factory, code="C1-L09-ZERO", with_existing=False
    )
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        reported = client.post(
            "/api/mobile/erp/warehouse/location-discrepancies",
            json={
                "inventory_lot_id": source_lot_id,
                "expected_lot_version": 1,
                "reported_quantity": 80,
                "observed_location_id": observed_location_id,
                "observed_location_layout_version": 1,
                "reason": "等待送货或废弃处理归零",
                "idempotency_key": "f3-red-until-zero",
            },
        )
        assert reported.status_code == 201, reported.text
        with factory() as db:
            lot = db.get(InventoryLot, source_lot_id)
            assert lot is not None
            lot.quantity_available = 0
            lot.quantity_reserved = 0
            lot.quantity_damaged = 0
            lot.quantity_consumed = 80
            lot.status = "closed"
            lot.version = 2
            db.commit()
        area = client.get(
            "/api/mobile/erp/warehouse/map/floors/3F",
            params={"area_code": "C1"},
        )
        assert area.status_code == 200, area.text
        observed = next(
            item
            for item in area.json()["locations"]
            if item["location_id"] == observed_location_id
        )
        assert observed["has_location_discrepancy"] is False
        assert observed["discrepant_goods"] == []


def test_whole_move_releases_source_projection_and_binds_target(
    mobile_erp_app,
) -> None:
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryPallet,
        InventoryPalletItem,
    )

    app, _ids, factory = mobile_erp_app
    source_lot_id, target_location_id = _add_map_target(factory, code="C1-L04")
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        moved = client.post(
            f"/api/mobile/erp/warehouse/lots/{source_lot_id}/moves",
            json={
                "expected_version": 1,
                "quantity": 80,
                "target_location_id": target_location_id,
                "expected_target_layout_version": 1,
                "idempotency_key": "f3-whole-move-001",
                "physical_move_confirmed": True,
            },
        )
        assert moved.status_code == 200, moved.text

    with factory() as db:
        source = db.get(InventoryLot, source_lot_id)
        assert source is not None and source.status == "closed"
        assert source.quantity_available + source.quantity_reserved == 0
        assert db.scalar(
            select(InventoryPalletItem).where(
                InventoryPalletItem.inventory_lot_id == source_lot_id
            )
        ) is None
        source_pallet = db.scalar(
            select(InventoryPallet).where(InventoryPallet.pallet_code == "PLT-MOBILE-001")
        )
        assert source_pallet is not None
        assert source_pallet.is_current is True
        assert source_pallet.location_id == target_location_id
        target_lots = list(
            db.scalars(
                select(InventoryLot).where(
                    InventoryLot.warehouse_location_id == target_location_id,
                    InventoryLot.status == "active",
                )
            ).all()
        )
        assert sum(lot.quantity_available + lot.quantity_reserved for lot in target_lots) == 100
        moved_lot = next(lot for lot in target_lots if lot.source_type == "transfer")
        target_item = db.scalar(
            select(InventoryPalletItem).where(
                InventoryPalletItem.inventory_lot_id == moved_lot.id
            )
        )
        assert target_item is not None and target_item.quantity == Decimal("80")


def test_empty_position_searches_all_physical_inventory_and_marks_unmatched(
    mobile_erp_app,
) -> None:
    from app.models.warehouse_inventory import (
        InventoryLot,
        WarehouseUnmatchedInventoryObservation,
    )

    app, _ids, factory = mobile_erp_app
    source_lot_id, target_location_id = _add_map_target(
        factory, code="C1-L05", with_existing=False
    )
    with factory() as db:
        before_lot_count = int(db.scalar(select(func.count(InventoryLot.id))) or 0)
        before_quantity = int(
            db.scalar(
                select(
                    func.sum(
                        InventoryLot.quantity_available
                        + InventoryLot.quantity_reserved
                        + InventoryLot.quantity_damaged
                    )
                )
            )
            or 0
        )

    with TestClient(app) as client:
        _login(client, "mobile-scoped")
        searched = client.get(
            "/api/mobile/erp/warehouse/physical-inventory/search",
            params={
                "customer_keyword": "匿名客户甲",
                "inventory_keyword": "MOBILE-BOX",
            },
        )
        assert searched.status_code == 200, searched.text
        result = next(
            item
            for item in searched.json()["items"]
            if item["lot_id"] == source_lot_id
        )
        assert result["quantity_movable"] == 80
        assert result["registered_location"]["location_code"] == "C1-L01"

        hidden = client.get(
            "/api/mobile/erp/warehouse/physical-inventory/search",
            params={"inventory_keyword": "OTHER-MOBILE-001"},
        )
        assert hidden.status_code == 200
        assert hidden.json()["items"] == []

        reported = client.post(
            "/api/mobile/erp/warehouse/unmatched-inventory-observations",
            json={
                "observed_location_id": target_location_id,
                "observed_location_layout_version": 1,
                "customer_keyword": "匿名客户甲",
                "inventory_keyword": "MOBILE-UNKNOWN-RED",
                "reported_quantity": 7,
                "reported_unit": "只",
                "reason": "现场包装编码未能匹配系统实物库存",
                "idempotency_key": "mobile-unmatched-c1-l05",
            },
        )
        assert reported.status_code == 201, reported.text
        observation = reported.json()["observation"]
        assert observation["status"] == "open"
        replay = client.post(
            "/api/mobile/erp/warehouse/unmatched-inventory-observations",
            json={
                "observed_location_id": target_location_id,
                "observed_location_layout_version": 1,
                "customer_keyword": "匿名客户甲",
                "inventory_keyword": "MOBILE-UNKNOWN-RED",
                "reported_quantity": 7,
                "reported_unit": "只",
                "reason": "现场包装编码未能匹配系统实物库存",
                "idempotency_key": "mobile-unmatched-c1-l05",
            },
        )
        assert replay.status_code == 201
        assert replay.json()["idempotent_replay"] is True
        conflicting_replay = client.post(
            "/api/mobile/erp/warehouse/unmatched-inventory-observations",
            json={
                "observed_location_id": target_location_id,
                "observed_location_layout_version": 1,
                "customer_keyword": "匿名客户甲",
                "inventory_keyword": "MOBILE-UNKNOWN-RED",
                "reported_quantity": 7,
                "reported_unit": "只",
                "reason": "同一请求键但不同现场说明",
                "idempotency_key": "mobile-unmatched-c1-l05",
            },
        )
        assert conflicting_replay.status_code == 409
        assert client.get(
            "/api/mobile/erp/warehouse/unmatched-inventory-observations"
        ).status_code == 403
        area = client.get(
            "/api/mobile/erp/warehouse/map/floors/3F",
            params={"area_code": "C1"},
        )
        target = next(
            item
            for item in area.json()["locations"]
            if item["location_id"] == target_location_id
        )
        assert target["occupancy_state"] == "not_disclosed"
        assert target["goods"] == []
        assert target["has_unmatched_inventory_observation"] is True
        assert target["unmatched_inventory_observation_count"] == 1

        _login(client, "mobile-admin")
        listed = client.get(
            "/api/mobile/erp/warehouse/unmatched-inventory-observations"
        )
        assert listed.status_code == 200, listed.text
        assert [item["id"] for item in listed.json()["items"]] == [
            observation["id"]
        ]
        resolved = client.post(
            f"/api/mobile/erp/warehouse/unmatched-inventory-observations/{observation['id']}/resolve",
            json={
                "expected_version": 1,
                "resolution_note": "已现场核对，后续由正式盘点流程处理",
                "resolved_inventory_lot_id": None,
                "idempotency_key": "mobile-unmatched-c1-l05-resolve",
            },
        )
        assert resolved.status_code == 200, resolved.text
        resolve_replay = client.post(
            f"/api/mobile/erp/warehouse/unmatched-inventory-observations/{observation['id']}/resolve",
            json={
                "expected_version": 1,
                "resolution_note": "已现场核对，后续由正式盘点流程处理",
                "resolved_inventory_lot_id": None,
                "idempotency_key": "mobile-unmatched-c1-l05-resolve",
            },
        )
        assert resolve_replay.status_code == 200
        assert resolve_replay.json()["idempotent_replay"] is True
        conflicting_resolution = client.post(
            f"/api/mobile/erp/warehouse/unmatched-inventory-observations/{observation['id']}/resolve",
            json={
                "expected_version": 1,
                "resolution_note": "同一请求键但不同处理结论",
                "resolved_inventory_lot_id": None,
                "idempotency_key": "mobile-unmatched-c1-l05-resolve",
            },
        )
        assert conflicting_resolution.status_code == 409
        after_area = client.get(
            "/api/mobile/erp/warehouse/map/floors/3F",
            params={"area_code": "C1"},
        )
        after_target = next(
            item
            for item in after_area.json()["locations"]
            if item["location_id"] == target_location_id
        )
        assert after_target["has_unmatched_inventory_observation"] is False

    with factory() as db:
        assert db.scalar(
            select(func.count(WarehouseUnmatchedInventoryObservation.id))
        ) == 1
        assert db.scalar(
            select(WarehouseUnmatchedInventoryObservation.status)
        ) == "resolved"
        assert db.scalar(select(func.count(InventoryLot.id))) == before_lot_count
        assert int(
            db.scalar(
                select(
                    func.sum(
                        InventoryLot.quantity_available
                        + InventoryLot.quantity_reserved
                        + InventoryLot.quantity_damaged
                    )
                )
            )
            or 0
        ) == before_quantity


def _alembic_config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def test_migration_is_linear_round_trip_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spec = importlib.util.spec_from_file_location("f3_migration", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == TARGET
    assert module.down_revision == PARENT
    path = tmp_path / "f3-migration.sqlite3"
    config = _alembic_config(monkeypatch, path)
    command.upgrade(config, PARENT)
    command.upgrade(config, TARGET)
    command.downgrade(config, PARENT)
    command.upgrade(config, TARGET)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO warehouse_location_discrepancies "
            "(inventory_lot_id,registered_location_id,observed_location_id,"
            "reported_lot_version,reported_quantity,reason,status,version,idempotency_key) "
            "VALUES (1,1,2,1,1,'fail closed','open',1,'f3-migration-row')"
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="拒绝破坏性降级"):
        command.downgrade(config, PARENT)


def test_mobile_map_frontend_defers_reads_and_writes_only_after_final_confirm(
    tmp_path: Path,
) -> None:
    for marker in (
        "打开实测仓库地图",
        "/api/mobile/erp/warehouse/map/floors",
        "warehouseMapGeneration",
        "generation !== state.warehouseMapGeneration",
        "createElementNS",
        "现场搬运完成，最终确认",
        "只上报位置不符，不改库存",
        "physical_move_confirmed: true",
        "expected_target_layout_version: target.geometry.version",
        "observed_location_layout_version: target.geometry.version",
        "warehouse/location-discrepancies",
        "待纠正位置报告",
        "has_location_discrepancy",
        "跨楼层、区域选择目标货位",
        "盘点数量",
        "/mobile/stocktake.html?location_id=",
        "location_discrepancy_id",
    ):
        assert marker in MOBILE_HTML
    assert "position.pallet_code" not in MOBILE_HTML
    select_target = re.search(
        r"function selectWarehouseMapTarget\(location\) \{(?P<body>.*?)\n      \}",
        MOBILE_HTML,
        re.S,
    )
    assert select_target
    assert "apiPost(" not in select_target.group("body")
    assert "state.warehouseMapTarget = location" in select_target.group("body")
    load_area = re.search(
        r"async function loadWarehouseMapArea\((?P<signature>.*?)\) \{(?P<body>.*?)\n      \}",
        MOBILE_HTML,
        re.S,
    )
    assert load_area
    assert "preserveMove = false" in load_area.group("signature")
    assert "if (!preserveMove) state.warehouseMapSource = null" in load_area.group(
        "body"
    )
    assert "new URLSearchParams(window.location.search)" in MOBILE_STOCKTAKE_HTML
    assert 'params.get("location_id")' in MOBILE_STOCKTAKE_HTML
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", MOBILE_HTML, re.S)
    inline = "\n".join(script for script in scripts if script.strip())
    target = tmp_path / "mobile-erp-inline.js"
    target.write_text(inline, encoding="utf-8")
    result = subprocess.run(
        ["node", "--check", str(target)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
