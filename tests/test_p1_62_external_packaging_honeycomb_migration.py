from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest


ROOT = Path(__file__).resolve().parents[1]
PARENT = "yy33v8x9z22"
TARGET = "zz34v8x9z23"
MIGRATION = (
    ROOT
    / "alembic"
    / "versions"
    / "zz34v8x9z23_external_packaging_honeycomb_history.py"
)


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def test_honeycomb_purchase_migration_is_unique_head(current_alembic_head: str) -> None:
    spec = importlib.util.spec_from_file_location("p1_62_migration", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == TARGET
    assert module.down_revision == PARENT
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    revision_ids = {
        revision.revision
        for revision in script.walk_revisions("base", current_alembic_head)
    }
    assert TARGET in revision_ids


def test_honeycomb_purchase_migration_round_trip_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "p1-62-honeycomb.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, PARENT)
    command.upgrade(config, TARGET)
    with sqlite3.connect(path) as connection:
        order_item_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(sales_order_items)")
        }
        item_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(external_packaging_purchase_items)"
            )
        }
        assert {
            "external_packaging_order_quantity_basis_snapshot",
            "external_packaging_purchase_quantity_basis_snapshot",
            "external_packaging_quantity_per_finished_unit_snapshot",
        } <= order_item_columns
        assert "specification_json_snapshot" in item_columns
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert {
            "trg_external_packaging_purchase_cancellations_immutable_update",
            "trg_external_packaging_purchase_cancellations_immutable_delete",
            "trg_external_packaging_purchase_items_immutable_update",
            "trg_external_packaging_purchase_items_immutable_delete",
        } <= triggers
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    command.downgrade(config, PARENT)
    command.upgrade(config, TARGET)
    with sqlite3.connect(path) as connection:
        supplier_id = connection.execute(
            """
            INSERT INTO supplier_master_records (
                standard_name,normalized_name,is_active,sort_order,version
            ) VALUES ('蜂窝板迁移供应商','honeycomb-migration',1,1,1)
            RETURNING id
            """
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO supplier_supply_categories "
            "(supplier_id,category_code,is_active) VALUES (?,'honeycomb_board',1)",
            (supplier_id,),
        )
        connection.execute(
            """
            INSERT INTO external_packaging_products (
                supplier_id,category_code,supplier_product_code,
                normalized_supplier_product_code,product_name,purchase_unit,
                specification_summary,specification_json,is_active,version
            ) VALUES (
                ?,'honeycomb_board','FWB-TEST','FWB-TEST','蜂窝板','片',
                '材质170*110*170，孔径15mm，800×180×60mm',
                '{"material":"170*110*170","aperture_mm":15,"length_mm":800,"width_mm":180,"thickness_mm":60}',
                1,1
            )
            """,
            (supplier_id,),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="honeycomb"):
        command.downgrade(config, PARENT)
