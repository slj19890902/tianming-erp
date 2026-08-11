from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest


ROOT = Path(__file__).resolve().parents[1]
PARENT = "eb10v8x9z99"
TARGET = "ec11v8x9z00"
MIGRATION = ROOT / "alembic" / "versions" / "ec11v8x9z00_order_external_packaging_routing.py"


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _seed_order(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        customer_id = connection.execute(
            "INSERT INTO customers(name) VALUES ('P1-40B migration customer')"
        ).lastrowid
        product_id = connection.execute(
            """
            INSERT INTO products(
                customer_id,product_code,customer_material_code,product_name,
                box_category,printing_plate_mode,unit,default_cutting_mode,
                production_label_enabled,is_composite,combination_mode,
                is_internal_component,is_active,manual_modified,version
            ) VALUES (?,?,?,?,'normal','no_plate','只','一开一',0,0,
                      'parent_priced_set',0,1,0,1)
            """,
            (customer_id, "P1-40B-OLD", "P1-40B-OLD", "legacy corrugated product"),
        ).lastrowid
        order_id = connection.execute(
            """
            INSERT INTO sales_orders(order_number,customer_id,order_date,total_amount)
            VALUES ('P1-40B-ORDER',?,'2026-08-11',100)
            """,
            (customer_id,),
        ).lastrowid
        item_id = connection.execute(
            """
            INSERT INTO sales_order_items(
                order_id,product_id,quantity,unit_price,subtotal,snapshot_product_name
            ) VALUES (?,?,100,1,100,'legacy corrugated product')
            """,
            (order_id, product_id),
        ).lastrowid
        connection.commit()
        return int(item_id)


def test_p1_40b_migration_is_linear() -> None:
    spec = importlib.util.spec_from_file_location("p1_40b_migration", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == TARGET
    assert module.down_revision == PARENT


def test_p1_40b_round_trip_defaults_constraints_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "p1-40b.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, PARENT)
    item_id = _seed_order(path)
    command.upgrade(config, TARGET)

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            """
            SELECT supply_mode_snapshot,external_packaging_category_code_snapshot,
                   external_packaging_candidate_snapshot_json
            FROM sales_order_items WHERE id=?
            """,
            (item_id,),
        ).fetchone() == ("corrugated_production", None, None)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE sales_order_items SET supply_mode_snapshot='invalid' WHERE id=?",
                (item_id,),
            )
        direct_id = connection.execute(
            """
            INSERT INTO sales_order_item_external_components(
                sales_order_item_id,source_component_set_id,source_component_id,
                source_component_set_version,source_kind,display_order,purpose,
                quantity_per_finished_unit,waste_rate,consumption_unit,is_required,
                category_code,specification_json,specification_summary
            ) VALUES (?,NULL,NULL,1,'direct_product',1,'direct external',1,0,
                      '根',1,'paper_corner_guard','{}','870x50x50x5mm')
            """,
            (item_id,),
        ).lastrowid
        assert direct_id
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO sales_order_item_external_components(
                    sales_order_item_id,source_component_set_id,source_component_id,
                    source_component_set_version,source_kind,display_order,purpose,
                    quantity_per_finished_unit,waste_rate,consumption_unit,is_required,
                    category_code,specification_json,specification_summary
                ) VALUES (?,NULL,NULL,1,'bound_component',2,'invalid bound',1,0,
                          '根',1,'paper_corner_guard','{}','invalid')
                """,
                (item_id,),
            )
        connection.rollback()
        connection.execute(
            """
            UPDATE sales_order_items
            SET supply_mode_snapshot='external_purchase',
                external_packaging_category_code_snapshot='paper_corner_guard',
                external_packaging_specification_json_snapshot='{"length_mm":870}',
                external_packaging_specification_summary_snapshot='870x50x50x5mm',
                external_packaging_purchase_unit_snapshot='根',
                external_packaging_candidate_snapshot_json='[{"external_product_id":1,"is_default":true}]',
                external_packaging_product_version_snapshot=1
            WHERE id=?
            """,
            (item_id,),
        )
        connection.commit()
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    with pytest.raises(RuntimeError, match="P1-40B"):
        command.downgrade(config, PARENT)

    clean_path = tmp_path / "p1-40b-clean.sqlite3"
    clean_config = _config(monkeypatch, clean_path)
    command.upgrade(clean_config, PARENT)
    _seed_order(clean_path)
    command.upgrade(clean_config, TARGET)
    command.downgrade(clean_config, PARENT)
    command.upgrade(clean_config, TARGET)
    with sqlite3.connect(clean_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == TARGET