from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "aq44v7w8x9m34"
TARGET_REVISION = "ar45v7w8x9n35"
INTERACTIVE_REVISION = "as46v7w8x9o36"
MIGRATION_PATH = (
    PROJECT_ROOT
    / "alembic"
    / "versions"
    / "ar45v7w8x9n35_floor3_warehouse_locations.py"
)
SEED_DATA_PATH = PROJECT_ROOT / "alembic" / "seed_data" / "floor3_locations_v11.json"
NEW_LOCATION_COLUMNS = {
    "warehouse_floor",
    "area_code",
    "storage_type",
    "level_no",
    "side_code",
    "sort_order",
    "is_temporary",
    "source_version",
}
EXCLUDED_LOCATION_CODES = {"LA1", "LA2", "LCD1"}
EXPECTED_AREA_COUNTS = {
    "A1": 10,
    "A2": 25,
    "AB1": 6,
    "AB2": 2,
    "B1": 14,
    "B2": 23,
    "C1": 22,
    "C2": 33,
    "CD1": 14,
    "D1": 28,
    "D2": 19,
    "DE1": 5,
    "E1": 27,
    "E2": 33,
    "E3": 6,
    "E4": 16,
    "F1": 12,
    "F12": 8,
    "F2": 30,
    "F3": 30,
    "F34": 3,
    "F4": 30,
}
LOCATION_REFERENCE_FKS = {
    ("inventory_lots", "warehouse_location_id", "id", "RESTRICT"),
    ("inventory_stock_policies", "default_location_id", "id", "SET NULL"),
    ("stock_replenishment_order_items", "location_id", "id", "SET NULL"),
}
STORAGE_TYPE_TRIGGER_NAMES = {
    "trg_warehouse_locations_storage_type_insert",
    "trg_warehouse_locations_storage_type_update",
}
EXPECTED_DOWNGRADE_BLOCKED_MESSAGE = (
    "三楼 Phase A 已产生栈板业务数据或被正式库存/补库业务引用，"
    "禁止破坏性降级；请停止服务并恢复 ar45 升级前的完整数据库备份。"
)
COMMON_MOJIBAKE_FRAGMENTS = ("涓夋ゼ", "鏍堟澘", "绂佹", "锛岃")


def _alembic_config(monkeypatch: pytest.MonkeyPatch, database_path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database_path.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "floor3-location-migration-test")
    return Config(str(PROJECT_ROOT / "alembic.ini"))


def _upgrade(
    monkeypatch: pytest.MonkeyPatch, database_path: Path, revision: str
) -> None:
    command.upgrade(_alembic_config(monkeypatch, database_path), revision)


def _downgrade(
    monkeypatch: pytest.MonkeyPatch, database_path: Path, revision: str
) -> None:
    command.downgrade(_alembic_config(monkeypatch, database_path), revision)


def _columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table_name})")}


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _trigger_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        )
    }


def _location_index_signature(
    connection: sqlite3.Connection,
) -> dict[str, tuple[bool, tuple[str, ...]]]:
    signature: dict[str, tuple[bool, tuple[str, ...]]] = {}
    for row in connection.execute("PRAGMA index_list(warehouse_locations)"):
        index_name = str(row[1])
        escaped_name = index_name.replace('"', '""')
        columns = tuple(
            str(index_row[2])
            for index_row in connection.execute(
                f'PRAGMA index_info("{escaped_name}")'
            )
        )
        signature[index_name] = (bool(row[2]), columns)
    return signature


def _location_reference_fks(
    connection: sqlite3.Connection,
) -> set[tuple[str, str, str, str]]:
    references: set[tuple[str, str, str, str]] = set()
    for table_name in (
        "inventory_lots",
        "inventory_stock_policies",
        "stock_replenishment_order_items",
    ):
        for row in connection.execute(f"PRAGMA foreign_key_list({table_name})"):
            if row[2] == "warehouse_locations":
                references.add((table_name, row[3], row[4], row[6]))
    return references


def _schema_guard(
    connection: sqlite3.Connection,
) -> tuple[
    dict[str, tuple[bool, tuple[str, ...]]],
    set[tuple[str, str, str, str]],
]:
    indexes = _location_index_signature(connection)
    references = _location_reference_fks(connection)
    assert indexes["ix_warehouse_locations_type_active"] == (
        False,
        ("warehouse_type", "is_active"),
    )
    assert any(
        unique and columns == ("location_code",)
        for unique, columns in indexes.values()
    )
    assert references == LOCATION_REFERENCE_FKS
    return indexes, references


def _assert_schema_guard(
    connection: sqlite3.Connection,
    expected: tuple[
        dict[str, tuple[bool, tuple[str, ...]]],
        set[tuple[str, str, str, str]],
    ],
) -> None:
    assert (_location_index_signature(connection), _location_reference_fks(connection)) == expected


def _assert_database_health(connection: sqlite3.Connection, revision: str) -> None:
    assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
        revision,
    )
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert not any(name.startswith("_alembic_tmp") for name in _tables(connection))


def test_ar45_uses_in_place_location_alters_and_unique_revision() -> None:
    raw_source = MIGRATION_PATH.read_bytes()
    source = raw_source.decode("utf-8", errors="strict")
    assert "\ufffd" not in source
    assert not any(fragment in source for fragment in COMMON_MOJIBAKE_FRAGMENTS)
    assert "三楼 Phase A 已产生栈板业务数据或被正式库存/补库业务引用" in source
    assert "禁止破坏性降级；请停止服务并恢复 ar45 升级前的完整数据库备份" in source
    assert 'revision = "ar45v7w8x9n35"' in source
    assert 'down_revision = "aq44v7w8x9m34"' in source
    assert "op.add_column(" in source
    assert source.index("_assert_seed_codes_available(seed_rows)") < source.index(
        "op.add_column("
    )
    downgrade_body = source.split("def downgrade() -> None:", 1)[1]
    assert downgrade_body.index("_assert_safe_downgrade()") < downgrade_body.index(
        "op.drop_index("
    )
    assert "ALTER TABLE warehouse_locations DROP COLUMN" in source
    assert "batch_alter_table" not in source
    assert "recreate=" not in source
    assert "am40v7w8x9i30" not in source
    assert "structural_fields" not in source
    assert [
        path.name
        for path in (PROJECT_ROOT / "alembic" / "versions").glob(
            "*floor3_warehouse_locations.py"
        )
    ] == ["ar45v7w8x9n35_floor3_warehouse_locations.py"]


def test_floor3_orm_defaults_and_partial_indexes_match_migrations(
    tmp_path: Path,
) -> None:
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.warehouse_inventory import Floor3LocationLayout

    database_path = tmp_path / "floor3-orm-schema.sqlite3"
    engine = create_sqlite_engine(database_path)
    Base.metadata.create_all(engine)
    engine.dispose()

    assert Floor3LocationLayout.__table__.c.source_type.default.arg == "manual"

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        location_id = connection.execute(
            """
            INSERT INTO warehouse_locations (
                location_code, location_name, warehouse_type, is_active
            ) VALUES ('ORM-FLOOR3-01', 'ORM floor3 location', 'finished', 1)
            RETURNING id
            """
        ).fetchone()[0]
        assert connection.execute(
            "SELECT sort_order, is_temporary FROM warehouse_locations WHERE id = ?",
            (location_id,),
        ).fetchone() == (0, 0)

        connection.execute(
            """
            INSERT INTO floor3_location_layouts (
                location_id, left_pct, top_pct, width_pct, height_pct
            ) VALUES (?, 0, 0, 10, 10)
            """,
            (location_id,),
        )
        assert connection.execute(
            "SELECT z_index, version, source_type FROM floor3_location_layouts"
        ).fetchone() == (0, 1, "manual")

        pallet_id = connection.execute(
            "INSERT INTO inventory_pallets (pallet_code, location_id) "
            "VALUES ('ORM-PALLET-01', ?) RETURNING id",
            (location_id,),
        ).fetchone()[0]
        assert connection.execute(
            "SELECT status, is_current, needs_relocation, version "
            "FROM inventory_pallets WHERE id = ?",
            (pallet_id,),
        ).fetchone() == ("active", 1, 0, 1)

        connection.execute(
            """
            INSERT INTO inventory_pallet_items (
                pallet_id, item_type, quantity, unit
            ) VALUES (?, 'finished', 1, 'boxes')
            """,
            (pallet_id,),
        )
        assert connection.execute(
            "SELECT match_status FROM inventory_pallet_items"
        ).fetchone() == ("pending",)

        connection.executemany(
            "INSERT INTO inventory_location_movements "
            "(pallet_id, movement_type, idempotency_key) VALUES (?, 'move', ?)",
            [
                (pallet_id, None),
                (pallet_id, None),
                (pallet_id, "orm-movement-1"),
            ],
        )
        connection.commit()

        movement_indexes = {
            row[1]: bool(row[2])
            for row in connection.execute(
                "PRAGMA index_list(inventory_location_movements)"
            )
        }
        assert movement_indexes["uq_inventory_location_movements_idempotency_key"]
        index_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            ("uq_inventory_location_movements_idempotency_key",),
        ).fetchone()[0]
        assert "WHERE idempotency_key IS NOT NULL" in index_sql
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO inventory_location_movements "
                "(pallet_id, movement_type, idempotency_key) "
                "VALUES (?, 'move', 'orm-movement-1')",
                (pallet_id,),
            )
        connection.rollback()


def test_floor3_location_upgrade_preserves_schema_and_seeds_v11(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_payload = json.loads(SEED_DATA_PATH.read_text(encoding="utf-8"))
    assert seed_payload["source_version"] == "V11"
    assert set(seed_payload["excluded_codes"]) == EXCLUDED_LOCATION_CODES
    assert len(EXPECTED_AREA_COUNTS) == 22

    database_path = tmp_path / "floor3-locations.sqlite3"
    _upgrade(monkeypatch, database_path, PREVIOUS_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        schema_before = _schema_guard(connection)
        legacy_location_id = connection.execute(
            """
            INSERT INTO warehouse_locations (
                location_code, location_name, warehouse_type, remarks
            ) VALUES ('LEGACY-FG-01', 'Existing business name', 'finished', 'Keep this note')
            """
        ).lastrowid
        legacy_lot_id = connection.execute(
            """
            INSERT INTO inventory_lots (
                lot_number, inventory_type, warehouse_location_id, unit,
                source_type, stock_date, last_movement_at
            ) VALUES ('LEGACY-LOT', 'finished', ?, 'boxes', 'manual',
                      '2026-07-15', '2026-07-15 12:00:00')
            """,
            (legacy_location_id,),
        ).lastrowid
        connection.commit()

    _upgrade(monkeypatch, database_path, TARGET_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _assert_database_health(connection, TARGET_REVISION)
        _assert_schema_guard(connection, schema_before)
        assert NEW_LOCATION_COLUMNS <= _columns(connection, "warehouse_locations")
        assert STORAGE_TYPE_TRIGGER_NAMES <= _trigger_names(connection)
        assert connection.execute("SELECT COUNT(*) FROM warehouse_locations").fetchone() == (
            397,
        )
        assert connection.execute(
            "SELECT COUNT(DISTINCT location_code) FROM warehouse_locations"
        ).fetchone() == (397,)
        assert dict(
            connection.execute(
                "SELECT area_code, COUNT(*) FROM warehouse_locations "
                "WHERE source_version = 'V11' "
                "GROUP BY area_code ORDER BY area_code"
            ).fetchall()
        ) == EXPECTED_AREA_COUNTS
        assert connection.execute(
            "SELECT COUNT(DISTINCT area_code) FROM warehouse_locations "
            "WHERE source_version = 'V11'"
        ).fetchone() == (22,)
        assert connection.execute(
            "SELECT source_version, COUNT(*) FROM warehouse_locations "
            "GROUP BY source_version ORDER BY source_version"
        ).fetchall() == [(None, 1), ("V11", 396)]
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_locations "
            "WHERE location_code IN ('LA1', 'LA2', 'LCD1')"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT area_code, storage_type, is_temporary, COUNT(*) "
            "FROM warehouse_locations WHERE area_code IN ('F12', 'F34') "
            "GROUP BY area_code, storage_type, is_temporary ORDER BY area_code"
        ).fetchall() == [
            ("F12", "temporary_aisle", 1, 8),
            ("F34", "temporary_aisle", 1, 3),
        ]
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_locations "
            "WHERE source_version = 'V11' AND is_temporary = 1"
        ).fetchone() == (11,)
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_locations "
            "WHERE area_code = 'CD1' "
            "AND remarks LIKE '%CD1容量待现场复核%'"
        ).fetchone() == (14,)
        assert connection.execute(
            "SELECT id, location_name, remarks, warehouse_type, warehouse_floor, area_code "
            "FROM warehouse_locations WHERE location_code = 'LEGACY-FG-01'"
        ).fetchone() == (
            legacy_location_id,
            "Existing business name",
            "Keep this note",
            "finished",
            None,
            None,
        )
        assert connection.execute(
            "SELECT id, warehouse_location_id FROM inventory_lots "
            "WHERE lot_number = 'LEGACY-LOT'"
        ).fetchone() == (legacy_lot_id, legacy_location_id)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE warehouse_locations SET storage_type = 'invalid' "
                "WHERE location_code = 'A1-L01'"
            )
        connection.rollback()
        assert connection.execute("SELECT COUNT(*) FROM inventory_pallets").fetchone() == (
            0,
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM inventory_pallet_items"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM inventory_location_movements"
        ).fetchone() == (0,)


def test_floor3_upgrade_aborts_before_writes_on_any_seed_code_collision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "floor3-seed-collision.sqlite3"
    _upgrade(monkeypatch, database_path, PREVIOUS_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        schema_before = _schema_guard(connection)
        first_id = connection.execute(
            "INSERT INTO warehouse_locations "
            "(location_code, location_name, warehouse_type) "
            "VALUES ('A1-L01', 'Existing A1', 'finished')"
        ).lastrowid
        second_id = connection.execute(
            "INSERT INTO warehouse_locations "
            "(location_code, location_name, warehouse_type) "
            "VALUES ('F12-P01', 'Existing F12', 'shared')"
        ).lastrowid
        connection.commit()

    with pytest.raises(RuntimeError) as error_info:
        _upgrade(monkeypatch, database_path, TARGET_REVISION)
    message = str(error_info.value)
    assert "seed 编码与既有 warehouse_locations 冲突" in message
    assert "A1-L01" in message
    assert "F12-P01" in message

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _assert_database_health(connection, PREVIOUS_REVISION)
        _assert_schema_guard(connection, schema_before)
        assert not NEW_LOCATION_COLUMNS & _columns(connection, "warehouse_locations")
        assert STORAGE_TYPE_TRIGGER_NAMES.isdisjoint(_trigger_names(connection))
        assert {
            "inventory_pallets",
            "inventory_pallet_items",
            "inventory_location_movements",
        }.isdisjoint(_tables(connection))
        assert connection.execute(
            "SELECT id, location_code FROM warehouse_locations ORDER BY id"
        ).fetchall() == [(first_id, "A1-L01"), (second_id, "F12-P01")]


def test_floor3_clean_round_trip_preserves_indexes_and_inbound_fks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "floor3-clean-round-trip.sqlite3"
    _upgrade(monkeypatch, database_path, PREVIOUS_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        schema_before = _schema_guard(connection)

    _upgrade(monkeypatch, database_path, TARGET_REVISION)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _assert_database_health(connection, TARGET_REVISION)
        _assert_schema_guard(connection, schema_before)

    _downgrade(monkeypatch, database_path, PREVIOUS_REVISION)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _assert_database_health(connection, PREVIOUS_REVISION)
        _assert_schema_guard(connection, schema_before)
        assert not NEW_LOCATION_COLUMNS & _columns(connection, "warehouse_locations")
        assert STORAGE_TYPE_TRIGGER_NAMES.isdisjoint(_trigger_names(connection))
        assert {
            "inventory_pallets",
            "inventory_pallet_items",
            "inventory_location_movements",
        }.isdisjoint(_tables(connection))
        assert connection.execute("SELECT COUNT(*) FROM warehouse_locations").fetchone() == (
            0,
        )

    _upgrade(monkeypatch, database_path, TARGET_REVISION)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _assert_database_health(connection, TARGET_REVISION)
        _assert_schema_guard(connection, schema_before)
        assert connection.execute("SELECT COUNT(*) FROM warehouse_locations").fetchone() == (
            396,
        )


def test_floor3_downgrade_blocks_any_seed_structure_drift_before_drop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "floor3-seed-drift.sqlite3"
    _upgrade(monkeypatch, database_path, TARGET_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "UPDATE warehouse_locations SET sort_order = 9999 "
            "WHERE location_code = 'CD1-U01'"
        )
        connection.commit()

    with pytest.raises(RuntimeError) as error_info:
        _downgrade(monkeypatch, database_path, PREVIOUS_REVISION)
    assert "396 个 V11 seed 结构已漂移" in str(error_info.value)
    assert "CD1-U01" in str(error_info.value)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _assert_database_health(connection, TARGET_REVISION)
        assert NEW_LOCATION_COLUMNS <= _columns(connection, "warehouse_locations")
        assert {
            "inventory_pallets",
            "inventory_pallet_items",
            "inventory_location_movements",
        } <= _tables(connection)
        assert connection.execute(
            "SELECT sort_order FROM warehouse_locations "
            "WHERE location_code = 'CD1-U01'"
        ).fetchone() == (9999,)


def test_floor3_downgrade_blocks_formal_business_references(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "floor3-formal-references.sqlite3"
    _upgrade(monkeypatch, database_path, TARGET_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        location_ids = {
            code: connection.execute(
                "SELECT id FROM warehouse_locations WHERE location_code = ?", (code,)
            ).fetchone()[0]
            for code in ("F12-P01", "F12-P02", "F12-P03")
        }
        connection.execute(
            """
            INSERT INTO inventory_lots (
                lot_number, inventory_type, warehouse_location_id, unit,
                source_type, stock_date, last_movement_at
            ) VALUES ('LOT-FLOOR3', 'finished', ?, 'boxes', 'manual',
                      '2026-07-15', '2026-07-15 12:00:00')
            """,
            (location_ids["F12-P01"],),
        )
        connection.execute(
            """
            INSERT INTO inventory_stock_policies (
                policy_name, target_inventory_type, target_quantity,
                default_location_id
            ) VALUES ('Floor3 policy', 'finished', 10, ?)
            """,
            (location_ids["F12-P02"],),
        )
        replenishment_order_id = connection.execute(
            """
            INSERT INTO stock_replenishment_orders (
                order_number, source_type
            ) VALUES ('SR-FLOOR3', 'manual_history')
            """
        ).lastrowid
        connection.execute(
            """
            INSERT INTO stock_replenishment_order_items (
                replenishment_order_id, target_inventory_type,
                product_name_snapshot, quantity, location_id
            ) VALUES (?, 'finished', 'Floor3 item', 10, ?)
            """,
            (replenishment_order_id, location_ids["F12-P03"]),
        )
        connection.commit()
        _assert_database_health(connection, TARGET_REVISION)

    with pytest.raises(RuntimeError) as error_info:
        _downgrade(monkeypatch, database_path, PREVIOUS_REVISION)
    assert str(error_info.value) == EXPECTED_DOWNGRADE_BLOCKED_MESSAGE

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _assert_database_health(connection, TARGET_REVISION)
        assert NEW_LOCATION_COLUMNS <= _columns(connection, "warehouse_locations")
        assert connection.execute(
            "SELECT warehouse_location_id FROM inventory_lots "
            "WHERE lot_number = 'LOT-FLOOR3'"
        ).fetchone() == (location_ids["F12-P01"],)
        assert connection.execute(
            "SELECT default_location_id FROM inventory_stock_policies "
            "WHERE policy_name = 'Floor3 policy'"
        ).fetchone() == (location_ids["F12-P02"],)
        assert connection.execute(
            "SELECT location_id FROM stock_replenishment_order_items "
            "WHERE replenishment_order_id = ?",
            (replenishment_order_id,),
        ).fetchone() == (location_ids["F12-P03"],)


def test_floor3_downgrade_blocks_unexpected_v11_reference_without_declassification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "floor3-unexpected-reference.sqlite3"
    _upgrade(monkeypatch, database_path, TARGET_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            CREATE TABLE future_floor3_assignments (
                id INTEGER PRIMARY KEY,
                location_code TEXT,
                FOREIGN KEY (location_code)
                    REFERENCES warehouse_locations(location_code)
                    ON DELETE SET NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO future_floor3_assignments (location_code) VALUES ('F2-S2-L03')"
        )
        connection.commit()

    with pytest.raises(RuntimeError) as error_info:
        _downgrade(monkeypatch, database_path, PREVIOUS_REVISION)
    assert str(error_info.value) == EXPECTED_DOWNGRADE_BLOCKED_MESSAGE

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _assert_database_health(connection, TARGET_REVISION)
        assert NEW_LOCATION_COLUMNS <= _columns(connection, "warehouse_locations")
        assert connection.execute(
            "SELECT source_version FROM warehouse_locations "
            "WHERE location_code = 'F2-S2-L03'"
        ).fetchone() == ("V11",)
        assert connection.execute(
            "SELECT location_code FROM future_floor3_assignments"
        ).fetchone() == ("F2-S2-L03",)
        assert {
            "inventory_pallets",
            "inventory_pallet_items",
            "inventory_location_movements",
        } <= _tables(connection)


def test_floor3_downgrade_blocks_pallet_business_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "floor3-pallet-data.sqlite3"
    _upgrade(monkeypatch, database_path, TARGET_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        location_id = connection.execute(
            "SELECT id FROM warehouse_locations WHERE location_code = 'F12-P01'"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO inventory_pallets (pallet_code, location_id) "
            "VALUES ('PALLET-1', ?)",
            (location_id,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO inventory_pallets (pallet_code, location_id) "
                "VALUES ('PALLET-2', ?)",
                (location_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO inventory_pallets (pallet_code, location_id, is_current) "
                "VALUES ('PALLET-BAD', NULL, 1)"
            )
        pallet_id = connection.execute(
            "SELECT id FROM inventory_pallets WHERE pallet_code = 'PALLET-1'"
        ).fetchone()[0]
        connection.executemany(
            """
            INSERT INTO inventory_pallet_items (
                pallet_id, inventory_code, product_name, item_type,
                quantity, unit, match_status
            ) VALUES (?, ?, ?, 'finished', 1.000, 'boxes', 'pending')
            """,
            [
                (pallet_id, f"INV-{number}", f"Snapshot {number}")
                for number in range(1, 6)
            ],
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO inventory_location_movements (pallet_id, movement_type) "
                "VALUES (?, 'invalid')",
                (pallet_id,),
            )
        connection.execute(
            "INSERT INTO inventory_location_movements "
            "(pallet_id, from_location_id, movement_type) VALUES (?, ?, 'clear')",
            (pallet_id, location_id),
        )
        connection.commit()
        _assert_database_health(connection, TARGET_REVISION)

    with pytest.raises(RuntimeError) as error_info:
        _downgrade(monkeypatch, database_path, PREVIOUS_REVISION)
    assert str(error_info.value) == EXPECTED_DOWNGRADE_BLOCKED_MESSAGE

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _assert_database_health(connection, TARGET_REVISION)
        assert connection.execute("SELECT COUNT(*) FROM inventory_pallets").fetchone() == (
            1,
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM inventory_pallet_items WHERE pallet_id = ?",
            (pallet_id,),
        ).fetchone() == (5,)
        assert connection.execute(
            "SELECT COUNT(*) FROM inventory_location_movements WHERE pallet_id = ?",
            (pallet_id,),
        ).fetchone() == (1,)


def test_floor3_interactive_layouts_seed_constraints_and_round_trip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "floor3-interactive-layout.sqlite3"
    _upgrade(monkeypatch, database_path, PREVIOUS_REVISION)
    _upgrade(monkeypatch, database_path, INTERACTIVE_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _assert_database_health(connection, INTERACTIVE_REVISION)
        assert {"floor3_location_layouts", "inventory_location_movements"} <= _tables(
            connection
        )
        assert {
            "location_id",
            "left_pct",
            "top_pct",
            "width_pct",
            "height_pct",
            "z_index",
            "version",
            "source_type",
            "created_by",
            "updated_by",
            "created_at",
            "updated_at",
        } <= _columns(connection, "floor3_location_layouts")
        assert _columns(connection, "inventory_location_movements") >= {
            "idempotency_key",
            "confirmed_at",
            "pallet_version_before",
            "pallet_version_after",
        }
        assert connection.execute(
            "SELECT COUNT(*), COUNT(DISTINCT location_id) "
            "FROM floor3_location_layouts"
        ).fetchone() == (396, 396)
        assert connection.execute(
            """
            SELECT
                location.area_code,
                location.level_no,
                printf('%.4f', MIN(layout.top_pct)),
                printf('%.4f', MAX(layout.top_pct)),
                printf('%.4f', MIN(layout.height_pct)),
                printf('%.4f', MAX(layout.height_pct)),
                COUNT(DISTINCT layout.top_pct),
                COUNT(*)
            FROM floor3_location_layouts AS layout
            JOIN warehouse_locations AS location ON location.id = layout.location_id
            WHERE location.area_code IN ('F2', 'F3', 'F4')
            GROUP BY location.area_code, location.level_no
            ORDER BY location.area_code, location.level_no
            """
        ).fetchall() == [
            (area_code, level_no, top, top, height, height, 1, 10)
            for area_code in ("F2", "F3", "F4")
            for level_no, top, height in (
                (1, "0.0000", "33.3333"),
                (2, "33.3333", "33.3334"),
                (3, "66.6667", "33.3333"),
            )
        ]
        assert connection.execute(
            """
            SELECT
                location.storage_type,
                COALESCE(location.side_code, ''),
                printf('%.4f', MIN(100 - layout.left_pct - layout.width_pct)),
                printf('%.4f', MAX(100 - layout.left_pct - layout.width_pct)),
                COUNT(*)
            FROM floor3_location_layouts AS layout
            JOIN warehouse_locations AS location ON location.id = layout.location_id
            WHERE location.area_code = 'D1'
            GROUP BY location.storage_type, location.side_code
            ORDER BY location.storage_type, location.side_code
            """
        ).fetchall() == [
            ("ground", "L", "41.0000", "41.0000", 10),
            ("ground", "R", "70.0000", "70.0000", 10),
            ("rack", "", "4.0000", "4.0000", 8),
        ]
        assert connection.execute(
            """
            SELECT
                location.area_code,
                location.storage_type,
                printf('%.4f', MIN(layout.top_pct)),
                printf('%.4f', MAX(layout.top_pct)),
                printf('%.4f', MIN(100 - layout.left_pct - layout.width_pct)),
                printf('%.4f', MAX(100 - layout.left_pct - layout.width_pct)),
                COUNT(*)
            FROM floor3_location_layouts AS layout
            JOIN warehouse_locations AS location ON location.id = layout.location_id
            WHERE location.area_code = 'DE1'
            GROUP BY location.area_code, location.storage_type
            ORDER BY location.area_code, location.storage_type
            """
        ).fetchall() == [
            ("DE1", "ground", "12.0000", "12.0000", "3.0000", "79.0000", 5),
        ]
        assert connection.execute(
            """
            SELECT
                location.storage_type,
                COALESCE(location.side_code, ''),
                location.location_code,
                printf('%.4f', layout.top_pct),
                printf('%.4f', 100 - layout.left_pct - layout.width_pct)
            FROM floor3_location_layouts AS layout
            JOIN warehouse_locations AS location ON location.id = layout.location_id
            WHERE location.area_code = 'E4'
            ORDER BY
                CASE location.storage_type WHEN 'rack' THEN 0 ELSE 1 END,
                location.side_code,
                location.sort_order,
                location.location_code
            """
        ).fetchall() == [
            *[
                (
                    "rack",
                    "",
                    f"E4-S2-{index + 1:02d}",
                    "2.0000",
                    f"{2 + index * 24:.4f}",
                )
                for index in range(4)
            ],
            *[
                (
                    "ground",
                    side,
                    f"E4-{side}{index + 1:02d}",
                    f"{left:.4f}",
                    f"{2 + index * 16:.4f}",
                )
                for side, left in (("L", 34), ("R", 67))
                for index in range(6)
            ],
        ]
        assert connection.execute(
            """
            SELECT COUNT(*)
            FROM floor3_location_layouts AS layout
            JOIN warehouse_locations AS location ON location.id = layout.location_id
            WHERE location.source_version = 'V11'
              AND (
                layout.left_pct < 0 OR layout.top_pct < 0
                OR layout.width_pct <= 0 OR layout.height_pct <= 0
                OR layout.left_pct + layout.width_pct > 100
                OR layout.top_pct + layout.height_pct > 100
              )
            """
        ).fetchone() == (0,)
        assert connection.execute(
            """
            SELECT location.area_code, COUNT(*)
            FROM floor3_location_layouts AS layout
            JOIN warehouse_locations AS location ON location.id = layout.location_id
            WHERE location.area_code IN ('F12', 'F34')
            GROUP BY location.area_code ORDER BY location.area_code
            """
        ).fetchall() == [("F12", 8), ("F34", 3)]
        assert connection.execute(
            "SELECT COUNT(*) FROM floor3_location_layouts "
            "WHERE source_type = 'seeded' AND version = 1"
        ).fetchone() == (396,)
        assert {
            "trg_inventory_location_movements_immutable_update",
            "trg_inventory_location_movements_immutable_delete",
        } <= _trigger_names(connection)
        movement_indexes = {
            row[1]: bool(row[2])
            for row in connection.execute(
                "PRAGMA index_list(inventory_location_movements)"
            )
        }
        assert movement_indexes["uq_inventory_location_movements_idempotency_key"]
        assert connection.execute(
            "PRAGMA foreign_key_list(floor3_location_layouts)"
        ).fetchall()
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    _downgrade(monkeypatch, database_path, PREVIOUS_REVISION)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _assert_database_health(connection, PREVIOUS_REVISION)
        assert "floor3_location_layouts" not in _tables(connection)
        assert not {
            "idempotency_key",
            "confirmed_at",
            "pallet_version_before",
            "pallet_version_after",
        } & _columns(connection, "inventory_location_movements")
        assert {
            "trg_inventory_location_movements_immutable_update",
            "trg_inventory_location_movements_immutable_delete",
        }.isdisjoint(_trigger_names(connection))
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    _upgrade(monkeypatch, database_path, INTERACTIVE_REVISION)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _assert_database_health(connection, INTERACTIVE_REVISION)
        assert connection.execute(
            "SELECT COUNT(*) FROM floor3_location_layouts"
        ).fetchone() == (396,)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_floor3_interactive_manual_default_preserves_downgrade_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "floor3-interactive-manual-default.sqlite3"
    _upgrade(monkeypatch, database_path, INTERACTIVE_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        location_id = connection.execute(
            "SELECT id FROM warehouse_locations WHERE location_code = 'F2-S2-L03'"
        ).fetchone()[0]
        connection.execute(
            "DELETE FROM floor3_location_layouts WHERE location_id = ?",
            (location_id,),
        )
        connection.execute(
            """
            INSERT INTO floor3_location_layouts (
                location_id, left_pct, top_pct, width_pct, height_pct
            ) VALUES (?, 10, 10, 10, 10)
            """,
            (location_id,),
        )
        connection.commit()
        assert connection.execute(
            "SELECT z_index, version, source_type FROM floor3_location_layouts "
            "WHERE location_id = ?",
            (location_id,),
        ).fetchone() == (0, 1, "manual")

    with pytest.raises(RuntimeError, match="interactive layout downgrade blocked"):
        _downgrade(monkeypatch, database_path, TARGET_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _assert_database_health(connection, INTERACTIVE_REVISION)
        assert connection.execute(
            "SELECT source_type FROM floor3_location_layouts WHERE location_id = ?",
            (location_id,),
        ).fetchone() == ("manual",)


def test_floor3_interactive_movements_are_idempotent_and_immutable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "floor3-interactive-movement-guards.sqlite3"
    _upgrade(monkeypatch, database_path, PREVIOUS_REVISION)
    _upgrade(monkeypatch, database_path, INTERACTIVE_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        location_id = connection.execute(
            "SELECT id FROM warehouse_locations WHERE location_code = 'F12-P01'"
        ).fetchone()[0]
        pallet_id = connection.execute(
            "INSERT INTO inventory_pallets (pallet_code, location_id) VALUES (?, ?) "
            "RETURNING id",
            ("LAYOUT-GUARD-PALLET", location_id),
        ).fetchone()[0]
        movement_id = connection.execute(
            """
            INSERT INTO inventory_location_movements (
                pallet_id, to_location_id, movement_type, idempotency_key,
                pallet_version_before, pallet_version_after
            ) VALUES (?, ?, 'move', ?, 1, 2)
            RETURNING id
            """,
            (pallet_id, location_id, "layout-guard-move-1"),
        ).fetchone()[0]
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE inventory_location_movements SET remarks = 'changed' WHERE id = ?",
                (movement_id,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM inventory_location_movements WHERE id = ?",
                (movement_id,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO inventory_location_movements (
                    pallet_id, to_location_id, movement_type, idempotency_key
                ) VALUES (?, ?, 'move', ?)
                """,
                (pallet_id, location_id, "layout-guard-move-1"),
            )
        connection.rollback()
        connection.commit()

    with pytest.raises(RuntimeError, match="downgrade blocked"):
        _downgrade(monkeypatch, database_path, PREVIOUS_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _assert_database_health(connection, INTERACTIVE_REVISION)
        assert connection.execute(
            "SELECT COUNT(*) FROM inventory_location_movements"
        ).fetchone() == (1,)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


@pytest.mark.parametrize(
    "column_name, value",
    [("source_type", "manual"), ("version", 2)],
)
def test_floor3_interactive_downgrade_blocks_non_seeded_layout_usage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    column_name: str,
    value: str | int,
) -> None:
    database_path = tmp_path / f"floor3-interactive-{column_name}.sqlite3"
    _upgrade(monkeypatch, database_path, PREVIOUS_REVISION)
    _upgrade(monkeypatch, database_path, INTERACTIVE_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            f"UPDATE floor3_location_layouts SET {column_name} = ? WHERE id = 1",
            (value,),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="downgrade blocked"):
        _downgrade(monkeypatch, database_path, PREVIOUS_REVISION)
SEMI_LOCATION_REVISION = "au48v8x9y0q38"


def test_default_semi_finished_location_is_added_and_removed_safely(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "default-semi-location.sqlite3"
    _upgrade(monkeypatch, database_path, SEMI_LOCATION_REVISION)
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT location_name, warehouse_type, is_active, warehouse_floor, source_version
            FROM warehouse_locations WHERE location_code = 'SF-TEMP'
            """
        ).fetchone()
        assert row == ("半成品待定位区", "semi_finished", 1, None, None)

    _downgrade(monkeypatch, database_path, "at47v7w8x9p37")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_locations WHERE location_code = 'SF-TEMP'"
        ).fetchone() == (0,)


def test_default_semi_finished_location_downgrade_blocks_references(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "default-semi-location-referenced.sqlite3"
    _upgrade(monkeypatch, database_path, SEMI_LOCATION_REVISION)
    with sqlite3.connect(database_path) as connection:
        location_id = connection.execute(
            "SELECT id FROM warehouse_locations WHERE location_code = 'SF-TEMP'"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO inventory_pallets (pallet_code, location_id) VALUES (?, ?)",
            ("SF-TEMP-REFERENCE", location_id),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="SF-TEMP"):
        _downgrade(monkeypatch, database_path, "at47v7w8x9p37")
