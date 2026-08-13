from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


ROOT = Path(__file__).resolve().parents[1]
PARENT = "ii17v8x9z06"
TARGET = "jj18v8x9z07"


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "p1-50c-label-migration-test")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    return config


def test_p1_50c_is_single_head_and_clean_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    current_alembic_head: str,
) -> None:
    database = tmp_path / "p1-50c-roundtrip.sqlite3"
    config = _config(monkeypatch, database)
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == [current_alembic_head]
    assert script.get_revision(TARGET).down_revision == PARENT

    command.upgrade(config, PARENT)
    command.upgrade(config, TARGET)
    with sqlite3.connect(database) as connection:
        task_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(production_tasks)")
        }
        assert {
            "production_label_template_version_snapshot",
            "production_label_product_version_snapshot",
        }.issubset(task_columns)
        for table in (
            "production_label_plan_refreshes",
            "production_packaging_label_print_jobs",
            "production_packaging_label_print_job_tasks",
        ):
            assert connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone() == (1,)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    command.downgrade(config, PARENT)
    command.upgrade(config, TARGET)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (TARGET,)


def test_existing_task_gets_legacy_template_metadata_without_print_fact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "p1-50c-existing.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, PARENT)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        customer_id = connection.execute(
            "INSERT INTO customers (customer_number,customer_code,name) VALUES (95001,'P150C','P150C客户')"
        ).lastrowid
        product_id = connection.execute(
            "INSERT INTO products (customer_id,product_code,customer_material_code,product_name,box_category,box_style,production_label_enabled,production_label_units_per_label,version) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (customer_id, "P150C-P", "P150C-P", "迁移标签箱", "normal", "A1", 1, 5, 3),
        ).lastrowid
        order_id = connection.execute(
            "INSERT INTO sales_orders (order_number,customer_id,order_date,status,payment_status,total_amount) VALUES (?,?,?,?,?,?)",
            ("P150C-O", customer_id, "2026-08-13", "pending_production", "unpaid", 23),
        ).lastrowid
        item_id = connection.execute(
            "INSERT INTO sales_order_items (order_id,product_id,quantity,unit_price,subtotal,material_status,special_process,snapshot_product_name) VALUES (?,?,?,?,?,?,?,?)",
            (order_id, product_id, 23, 1, 23, "pending", "一开一", "迁移标签箱"),
        ).lastrowid
        connection.execute(
            "INSERT INTO production_tasks (order_item_id,status,planned_quantity,finished_coverage_snapshot,ordered_quantity_snapshot,material_received_quantity,material_input_quantity,output_factor,production_label_enabled_snapshot,production_label_units_per_label_snapshot,production_label_total_quantity_snapshot,production_label_count_snapshot,version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, "waiting_material", 0, 0, 23, 0, 0, 1, 1, 5, 23, 5, 2),
        )
        connection.commit()

    command.upgrade(config, TARGET)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT production_label_template_version_snapshot,production_label_product_version_snapshot FROM production_tasks"
        ).fetchone() == ("legacy_65x45_v1", None)
        assert connection.execute(
            "SELECT COUNT(*) FROM production_packaging_label_print_jobs"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM production_label_plan_refreshes"
        ).fetchone() == (0,)

    # A pre-migration task carries only legacy metadata, so an otherwise empty
    # database remains safely reversible and can be upgraded again.
    command.downgrade(config, PARENT)
    command.upgrade(config, TARGET)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT production_label_template_version_snapshot,production_label_product_version_snapshot FROM production_tasks"
        ).fetchone() == ("legacy_65x45_v1", None)


def test_downgrade_refuses_to_erase_new_label_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "p1-50c-protected.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO production_packaging_label_print_jobs "
            "(supplier_order_id,idempotency_key,request_hash,template_version,plan_fingerprint,payload_json,payload_hash,status) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (999999, "p150c-downgrade-guard", "a" * 64, "current_40x30_v1", "b" * 64, "{}", "c" * 64, "prepared"),
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="P1-50C"):
        command.downgrade(config, PARENT)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (TARGET,)


def test_current_template_requires_product_version_and_blocks_downgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "p1-50c-current-task-protected.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO production_tasks "
                "(order_item_id,production_label_template_version_snapshot) "
                "VALUES (?,?)",
                (999998, "current_40x30_v1"),
            )
        connection.execute(
            "INSERT INTO production_tasks "
            "(order_item_id,production_label_template_version_snapshot,"
            "production_label_product_version_snapshot) VALUES (?,?,?)",
            (999999, "current_40x30_v1", 1),
        )
        connection.commit()

    # The task snapshot itself is a durable new fact even before any refresh
    # receipt or print job exists.
    with pytest.raises(RuntimeError, match="P1-50C"):
        command.downgrade(config, PARENT)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM production_label_plan_refreshes"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM production_packaging_label_print_jobs"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (TARGET,)
