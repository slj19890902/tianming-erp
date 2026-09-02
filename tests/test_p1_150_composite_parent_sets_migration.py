from __future__ import annotations

import importlib.util
import io
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory


ROOT = Path(__file__).resolve().parents[1]
PARENT = "jo73v8x9z62"
TARGET = "jp74v8x9z63"
PLAN_TABLE = "stock_replenishment_bom_component_plans"
EXTERNAL_TRIGGER = "trg_sales_order_item_bom_components_material_correction_guard"
INTERNAL_TRIGGER = "trg_p1_150_test_inventory_reservation_internal"


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "p1-150-composite-parent-migration-test")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def _load_migration_module():
    path = (
        ROOT
        / "alembic"
        / "versions"
        / f"{TARGET}_p1_150_composite_parent_set_plans.py"
    )
    spec = importlib.util.spec_from_file_location("p1_150_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _health(connection: sqlite3.Connection, revision: str) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    assert connection.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchone() == (revision,)
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _seed_parent_facts(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(
        """
        INSERT INTO customers(
            id, name, payment_term_days, statement_cycle_start_day,
            credit_limit, delivery_method, default_tax_rate, status, is_active
        ) VALUES (9150, 'P1-150迁移客户', 0, 20, 0, '配送', 0.13, 'active', 1)
        """
    )
    connection.executemany(
        """
        INSERT INTO products(
            id, customer_id, product_code, customer_material_code,
            product_name, is_composite, is_internal_component,
            is_virtual_composite_parent, version
        ) VALUES (?, 9150, ?, ?, ?, ?, ?, ?, 1)
        """,
        [
            (9150, "PARENT-150", "PARENT-150", "组合父件", 1, 0, 1),
            (9151, "COMP-150", "COMP-150", "实体组件", 0, 1, 0),
        ],
    )
    connection.execute(
        """
        INSERT INTO product_bom_components(
            id, parent_product_id, component_product_id,
            quantity_per_set, display_order, internal_component_code
        ) VALUES (9150, 9150, 9151, 3, 0, 'COMP-150')
        """
    )
    connection.execute(
        """
        INSERT INTO stock_replenishment_orders(
            id, order_number, source_type, status
        ) VALUES (9150, 'SR-P1-150', 'customer_request', 'confirmed')
        """
    )
    location = connection.execute(
        "SELECT id FROM warehouse_locations "
        "WHERE is_active=1 AND placement_status='placed' ORDER BY id LIMIT 1"
    ).fetchone()
    assert location is not None
    connection.execute(
        """
        INSERT INTO inventory_lots(
            id, lot_number, inventory_type, warehouse_location_id,
            quantity_available, quantity_reserved, quantity_consumed,
            quantity_damaged, quantity_scrapped, unit, status, source_type,
            stock_date, last_movement_at, version
        ) VALUES (
            9150, 'LOT-P1-150', 'semi_finished', ?,
            200, 0, 0, 0, 0, 'sheets', 'active', 'manual',
            '2026-09-03', '2026-09-03 08:00:00', 1
        )
        """,
        (int(location[0]),),
    )
    connection.execute(
        """
        INSERT INTO inventory_reservations(
            id, reservation_number, inventory_lot_id, reservation_type,
            reserved_stock_quantity, status
        ) VALUES (9150, 'RS-LEGACY-P1-150', 9150, 'finished_order', 1, 'active')
        """
    )
    connection.execute(
        f"""
        CREATE TRIGGER {INTERNAL_TRIGGER}
        AFTER UPDATE ON inventory_reservations
        FOR EACH ROW BEGIN SELECT 1; END
        """
    )
    connection.commit()


def _insert_fully_offset_plan(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"""
        INSERT INTO {PLAN_TABLE}(
            id, replenishment_order_id, replenishment_item_id, stock_policy_id,
            parent_product_id, parent_product_version,
            parent_product_code_snapshot, parent_product_name_snapshot,
            product_bom_component_id, component_product_id,
            component_product_version, component_product_code_snapshot,
            component_product_name_snapshot, display_order,
            parent_set_quantity, quantity_per_set, required_piece_quantity,
            incoming_covered_piece_quantity, reserved_piece_quantity,
            net_required_piece_quantity, yield_per_sheet,
            cutting_mode_snapshot, is_die_cut_snapshot,
            mold_max_yield_per_sheet_snapshot,
            purchase_sheet_quantity, cutting_remainder_piece_quantity,
            sheet_type, component_type, internal_component_code_snapshot,
            bom_fingerprint, request_fingerprint
        ) VALUES (
            9150, 9150, NULL, NULL,
            9150, 1, 'PARENT-150', '组合父件',
            9150, 9151, 1, 'COMP-150', '实体组件', 0,
            100, 3, 300, 100, 200, 0, 4,
            '一开四', 0, NULL, 0, 0,
            'raw_board', 'whole', 'COMP-150',
            ?, ?
        )
        """,
        ("b" * 64, "r" * 64),
    )


def test_p1_150_migration_is_linear_preserves_rows_and_round_trips(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p1-150-round-trip.sqlite3"
    config = _config(monkeypatch, database)
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == [TARGET]
    assert script.get_revision(TARGET).down_revision == PARENT

    command.upgrade(config, PARENT)
    with sqlite3.connect(database) as connection:
        _health(connection, PARENT)
        _seed_parent_facts(connection)
        assert "stock_replenishment_bom_component_plan_id" not in _columns(
            connection, "inventory_reservations"
        )

    command.upgrade(config, TARGET)
    with sqlite3.connect(database) as connection:
        _health(connection, TARGET)
        assert PLAN_TABLE in {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "stock_replenishment_bom_component_plan_id" in _columns(
            connection, "inventory_reservations"
        )
        assert {
            "cutting_mode_snapshot",
            "is_die_cut_snapshot",
            "mold_max_yield_per_sheet_snapshot",
        }.issubset(_columns(connection, PLAN_TABLE))
        assert connection.execute(
            "SELECT reservation_number FROM inventory_reservations WHERE id=9150"
        ).fetchone() == ("RS-LEGACY-P1-150",)
        assert connection.execute(
            "SELECT COUNT(1) FROM sqlite_master WHERE type='trigger' AND name=?",
            (EXTERNAL_TRIGGER,),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(1) FROM sqlite_master WHERE type='trigger' AND name=?",
            (INTERNAL_TRIGGER,),
        ).fetchone() == (1,)

    command.downgrade(config, PARENT)
    with sqlite3.connect(database) as connection:
        _health(connection, PARENT)
        assert PLAN_TABLE not in {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert connection.execute(
            "SELECT reservation_number FROM inventory_reservations WHERE id=9150"
        ).fetchone() == ("RS-LEGACY-P1-150",)
        assert connection.execute(
            "SELECT COUNT(1) FROM sqlite_master WHERE type='trigger' AND name=?",
            (EXTERNAL_TRIGGER,),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(1) FROM sqlite_master WHERE type='trigger' AND name=?",
            (INTERNAL_TRIGGER,),
        ).fetchone() == (1,)

    command.upgrade(config, TARGET)
    with sqlite3.connect(database) as connection:
        _health(connection, TARGET)


def test_p1_150_database_accepts_full_offset_and_rejects_invalid_targets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p1-150-constraints.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, PARENT)
    with sqlite3.connect(database) as connection:
        _seed_parent_facts(connection)
    command.upgrade(config, TARGET)

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert_fully_offset_plan(connection)
        connection.execute(
            """
            INSERT INTO inventory_reservations(
                reservation_number, inventory_lot_id, reservation_type,
                stock_replenishment_bom_component_plan_id,
                reserved_stock_quantity, status
            ) VALUES ('SRS-P1-150', 9150, 'semi_requisition', 9150, 200, 'active')
            """
        )
        connection.commit()
        assert connection.execute(
            f"SELECT replenishment_item_id, purchase_sheet_quantity "
            f"FROM {PLAN_TABLE} WHERE id=9150"
        ).fetchone() == (None, 0)

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO inventory_reservations(
                    reservation_number, inventory_lot_id, reservation_type,
                    reserved_stock_quantity, status
                ) VALUES ('SRS-P1-150-NO-TARGET', 9150, 'semi_requisition', 1, 'active')
                """
            )
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(f"DELETE FROM {PLAN_TABLE} WHERE id=9150")
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM stock_replenishment_orders WHERE id=9150"
            )
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO inventory_reservations(
                    reservation_number, inventory_lot_id, reservation_type,
                    stock_replenishment_bom_component_plan_id,
                    reserved_stock_quantity, status
                ) VALUES ('SRS-P1-150-WRONG-TYPE', 9150, 'finished_order', 9150, 1, 'active')
                """
            )
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"""
                UPDATE {PLAN_TABLE}
                SET required_piece_quantity=301
                WHERE id=9150
                """
            )
        connection.rollback()
        _health(connection, TARGET)


def test_p1_150_downgrade_fails_closed_after_plan_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p1-150-facts.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, PARENT)
    with sqlite3.connect(database) as connection:
        _seed_parent_facts(connection)
    command.upgrade(config, TARGET)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert_fully_offset_plan(connection)
        connection.commit()

    with pytest.raises(RuntimeError, match="P1-150 downgrade blocked"):
        command.downgrade(config, PARENT)
    with sqlite3.connect(database) as connection:
        _health(connection, TARGET)
        assert connection.execute(
            f"SELECT COUNT(1) FROM {PLAN_TABLE}"
        ).fetchone() == (1,)


def test_p1_150_upgrade_fails_closed_for_orphan_legacy_semi_requisition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p1-150-orphan-legacy-reservation.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, PARENT)
    with sqlite3.connect(database) as connection:
        _seed_parent_facts(connection)
        connection.execute(
            """
            INSERT INTO inventory_reservations(
                reservation_number, inventory_lot_id, reservation_type,
                reserved_stock_quantity, status
            ) VALUES ('SRS-P1-150-ORPHAN', 9150, 'semi_requisition', 1, 'active')
            """
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="legacy semi_requisition reservation"):
        command.upgrade(config, TARGET)
    with sqlite3.connect(database) as connection:
        _health(connection, PARENT)
        assert PLAN_TABLE not in {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }


def test_p1_150_postgresql_offline_ddl_is_linear_and_portable() -> None:
    migration = _load_migration_module()
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    migration.op = Operations(context)
    migration._assert_legacy_semi_requisition_targets_are_valid = lambda: None
    migration._drop_sqlite_reservation_triggers = lambda: []
    migration.upgrade()
    sql = output.getvalue()

    assert f"CREATE TABLE {PLAN_TABLE}" in sql
    assert "stock_replenishment_bom_component_plan_id INTEGER" in sql
    assert "ck_inventory_reservations_semi_requisition_target" in sql
    assert "fk_inventory_reservations_stock_replenishment_bom_plan" in sql
    assert "AUTOINCREMENT" not in sql
