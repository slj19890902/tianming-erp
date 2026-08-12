from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "hh16v8x9z05"
TARGET_REVISION = "hi17v8x9z06"


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "floor1-a1-transitional-staging-test")
    return Config(str(PROJECT_ROOT / "alembic.ini"))


def _upgrade(
    monkeypatch: pytest.MonkeyPatch, database: Path, revision: str
) -> None:
    command.upgrade(_config(monkeypatch, database), revision)


def _downgrade(
    monkeypatch: pytest.MonkeyPatch, database: Path, revision: str
) -> None:
    command.downgrade(_config(monkeypatch, database), revision)


def _seed_transition_and_receipt(
    connection: sqlite3.Connection,
) -> tuple[int, int, int]:
    connection.execute("PRAGMA foreign_keys = ON")
    floor = connection.execute(
        "SELECT id FROM warehouse_floors WHERE floor_number=1"
    ).fetchone()
    if floor is None:
        cursor = connection.execute(
            """
            INSERT INTO warehouse_floors (
                floor_code, floor_name, floor_number, construction_status
            ) VALUES ('F1-UAT', '一楼', 1, 'enabled')
            """
        )
        floor_id = int(cursor.lastrowid)
    else:
        floor_id = int(floor[0])
        connection.execute(
            "UPDATE warehouse_floors SET construction_status='enabled' WHERE id=?",
            (floor_id,),
        )

    area = connection.execute(
        "SELECT id FROM warehouse_areas WHERE floor_id=? AND area_code='A1'",
        (floor_id,),
    ).fetchone()
    if area is None:
        connection.execute(
            """
            INSERT INTO warehouse_areas (
                floor_id, area_code, area_name, construction_status
            ) VALUES (?, 'A1', 'A1原料暂存区', 'ledger_building')
            """,
            (floor_id,),
        )
    else:
        connection.execute(
            "UPDATE warehouse_areas SET construction_status='ledger_building' WHERE id=?",
            (int(area[0]),),
        )

    location = connection.execute(
        "SELECT id FROM warehouse_locations WHERE location_code='1FA'"
    ).fetchone()
    if location is None:
        cursor = connection.execute(
            """
            INSERT INTO warehouse_locations (
                location_code, location_name, warehouse_type, is_active,
                warehouse_floor, area_code, storage_type, placement_status
            ) VALUES (
                '1FA', '一楼A1原料暂存区', 'shared', 1,
                1, 'A1', 'ground', 'unplaced'
            )
            """
        )
        location_id = int(cursor.lastrowid)
    else:
        location_id = int(location[0])
        connection.execute(
            """
            UPDATE warehouse_locations
            SET location_name='一楼A1原料暂存区', warehouse_type='shared',
                is_active=1, warehouse_floor=1, area_code='A1',
                storage_type='ground', placement_status='unplaced',
                source_version=NULL
            WHERE id=?
            """,
            (location_id,),
        )

    other_cursor = connection.execute(
        """
        INSERT INTO warehouse_locations (
            location_code, location_name, warehouse_type, is_active,
            warehouse_floor, area_code, storage_type, placement_status
        ) VALUES (
            'A1-OTHER-UAT', '其他未落位标记', 'shared', 1,
            1, 'A1', 'ground', 'unplaced'
        )
        """
    )
    other_location_id = int(other_cursor.lastrowid)

    order_cursor = connection.execute(
        """
        INSERT INTO stock_replenishment_orders (
            order_number, source_type, status
        ) VALUES ('SR-A1-UAT', 'customer_request', 'confirmed')
        """
    )
    item_cursor = connection.execute(
        """
        INSERT INTO stock_replenishment_order_items (
            replenishment_order_id, target_inventory_type,
            product_name_snapshot, material_code_snapshot,
            normalized_material_code, layer_count, flute_type,
            report_length_mm, report_width_mm, quantity, stocked_quantity
        ) VALUES (
            ?, 'semi_finished', 'YL测试纸箱', 'VINIV',
            'VINIV', 5, 'AB', 1335, 1080, 100, 0
        )
        """,
        (int(order_cursor.lastrowid),),
    )
    receipt_cursor = connection.execute(
        """
        INSERT INTO incoming_receipts (
            receipt_number, status, received_at, idempotency_key
        ) VALUES (
            'IR-A1-UAT', 'posted', CURRENT_TIMESTAMP, 'ir-a1-uat'
        )
        """
    )
    receipt_item_cursor = connection.execute(
        """
        INSERT INTO incoming_receipt_items (
            receipt_id, stock_replenishment_item_id,
            planned_quantity, received_quantity, cumulative_received_quantity,
            variance_quantity, variance_type, resolution_status, status
        ) VALUES (
            ?, ?, 100, 100, 100, 0, 'matched', 'not_required', 'posted'
        )
        """,
        (int(receipt_cursor.lastrowid), int(item_cursor.lastrowid)),
    )
    connection.commit()
    return location_id, other_location_id, int(receipt_item_cursor.lastrowid)


def _insert_lot(
    connection: sqlite3.Connection,
    *,
    lot_number: str,
    location_id: int,
    source_type: str,
    source_ref_type: str | None,
    source_ref_id: int | None,
) -> None:
    connection.execute(
        """
        INSERT INTO inventory_lots (
            lot_number, inventory_type, warehouse_location_id,
            quantity_available, unit, status, source_type,
            source_ref_type, source_ref_id, stock_date, last_movement_at
        ) VALUES (
            ?, 'semi_finished', ?, 100, 'sheets', 'active', ?, ?, ?,
            '2026-08-12', CURRENT_TIMESTAMP
        )
        """,
        (
            lot_number,
            location_id,
            source_type,
            source_ref_type,
            source_ref_id,
        ),
    )


def test_migration_is_the_unique_head_and_round_trips_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "a1-transition-roundtrip.sqlite3"
    config = _config(monkeypatch, database)
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == [TARGET_REVISION]
    assert script.get_revision(TARGET_REVISION).down_revision == PREVIOUS_REVISION

    command.upgrade(config, TARGET_REVISION)
    command.downgrade(config, PREVIOUS_REVISION)
    command.upgrade(config, TARGET_REVISION)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET_REVISION


def test_only_posted_replenishment_receipt_can_use_transitional_1fa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "a1-transition-guard.sqlite3"
    _upgrade(monkeypatch, database, PREVIOUS_REVISION)
    with sqlite3.connect(database) as connection:
        location_id, other_location_id, receipt_item_id = (
            _seed_transition_and_receipt(connection)
        )

    _upgrade(monkeypatch, database, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _insert_lot(
            connection,
            lot_number="SI-A1-ALLOWED",
            location_id=location_id,
            source_type="replenishment",
            source_ref_type="stock_replenishment_receipt",
            source_ref_id=receipt_item_id,
        )
        connection.commit()

        with pytest.raises(
            sqlite3.IntegrityError,
            match="inventory lot requires an active placed location",
        ):
            _insert_lot(
                connection,
                lot_number="SI-A1-MANUAL-BLOCKED",
                location_id=location_id,
                source_type="manual",
                source_ref_type=None,
                source_ref_id=None,
            )
        connection.rollback()

        with pytest.raises(
            sqlite3.IntegrityError,
            match="inventory lot requires an active placed location",
        ):
            _insert_lot(
                connection,
                lot_number="SI-A1-OTHER-BLOCKED",
                location_id=other_location_id,
                source_type="replenishment",
                source_ref_type="stock_replenishment_receipt",
                source_ref_id=receipt_item_id,
            )
        connection.rollback()

    with pytest.raises(RuntimeError, match="1FA 已承载有效库存"):
        _downgrade(monkeypatch, database, PREVIOUS_REVISION)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET_REVISION
