from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest


ROOT = Path(__file__).resolve().parents[1]
PARENT = "ea09v8x9z98"
TARGET = "eb10v8x9z99"
MIGRATION = (
    ROOT
    / "alembic"
    / "versions"
    / "eb10v8x9z99_external_packaging_product_profile.py"
)


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _seed_legacy_product(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        customer_id = connection.execute(
            """
            INSERT INTO customers (
                name,payment_term_days,statement_cycle_start_day,credit_limit,
                delivery_method,default_tax_rate,status,is_active,version
            ) VALUES ('P1-40A迁移客户',0,20,0,'配送',0.13,'active',1,1)
            RETURNING id
            """
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO products (
                customer_id,product_code,customer_material_code,product_name,
                box_category,printing_plate_mode,unit,default_cutting_mode,
                production_label_enabled,is_composite,combination_mode,
                is_internal_component,is_active,manual_modified,version
            ) VALUES (
                ?,'P1-40A-OLD','P1-40A-OLD','迁移前纸板常用箱',
                'normal','no_plate','只','一开一',
                0,0,'parent_priced_set',0,1,0,1
            )
            """,
            (customer_id,),
        )
        connection.commit()


def test_p1_40a_migration_is_linear() -> None:
    spec = importlib.util.spec_from_file_location("p1_40a_migration", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == TARGET
    assert module.down_revision == PARENT


def test_p1_40a_round_trip_defaults_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "p1-40a-migration.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, PARENT)
    _seed_legacy_product(path)

    command.upgrade(config, TARGET)
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            """
            SELECT supply_mode,external_packaging_category_code,
                   external_packaging_candidate_snapshot_json
            FROM products WHERE product_code='P1-40A-OLD'
            """
        ).fetchone()
        assert row == ("corrugated_production", None, None)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE products SET supply_mode='unexpected' "
                "WHERE product_code='P1-40A-OLD'"
            )

    command.downgrade(config, PARENT)
    with sqlite3.connect(path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(products)")
        }
        assert "supply_mode" not in columns
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    command.upgrade(config, TARGET)
    with sqlite3.connect(path) as connection:
        supplier_id = connection.execute(
            """
            INSERT INTO supplier_master_records (
                standard_name,normalized_name,is_active,sort_order,version
            ) VALUES ('P1-40A中空板供应商','p140a-hollow',1,1,1)
            RETURNING id
            """
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO supplier_supply_categories "+
            "(supplier_id,category_code,is_active) VALUES (?,'hollow_board',1)",
            (supplier_id,),
        )
        connection.execute(
            """
            INSERT INTO external_packaging_products (
                supplier_id,category_code,supplier_product_code,
                normalized_supplier_product_code,product_name,purchase_unit,
                specification_summary,specification_json,is_active,version
            ) VALUES (
                ?,'hollow_board','HB-TEST','HB-TEST','迁移中空板','张',
                '蓝色 1200×800×5mm','{"color":"蓝色"}',1,1
            )
            """,
            (supplier_id,),
        )
        connection.execute(
            """
            UPDATE products
            SET supply_mode='external_purchase',
                box_style='其他',
                external_packaging_category_code='paper_corner_guard',
                external_packaging_specification_json='{"length_mm":870}',
                external_packaging_specification_summary='L型50×50×5mm，长870mm',
                external_packaging_purchase_unit='根',
                external_packaging_candidate_snapshot_json=
                    '[{"external_product_id":1,"is_default":true}]'
            WHERE product_code='P1-40A-OLD'
            """
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="禁止破坏性降级"):
        command.downgrade(config, PARENT)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []