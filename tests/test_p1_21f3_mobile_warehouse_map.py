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
PARENT = "ee13v8x9z02"
TARGET = "ff14v8x9z03"
MIGRATION = (
    ROOT
    / "alembic"
    / "versions"
    / "ff14v8x9z03_mobile_warehouse_location_discrepancies.py"
)


def _add_map_target(factory, *, code: str = "C1-L02") -> tuple[int, int]:
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
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", MOBILE_HTML, re.S)
    inline = "\n".join(script for script in scripts if script.strip())
    target = tmp_path / "mobile-erp-inline.js"
    target.write_text(inline, encoding="utf-8")
    result = subprocess.run(
        ["node", "--check", str(target)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
