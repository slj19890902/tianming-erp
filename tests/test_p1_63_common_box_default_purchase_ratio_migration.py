from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest


ROOT = Path(__file__).resolve().parents[1]
PARENT = "zz34v8x9z23"
TARGET = "aaa35v8x9z24"
MIGRATION = (
    ROOT
    / "alembic"
    / "versions"
    / "aaa35v8x9z24_common_box_default_purchase_ratio.py"
)


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _seed_external_product(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        customer_id = connection.execute(
            "INSERT INTO customers(name) VALUES ('P1-63 migration customer')"
        ).lastrowid
        product_id = connection.execute(
            """
            INSERT INTO products(
                customer_id,product_code,customer_material_code,product_name,
                box_category,box_style,supply_mode,printing_plate_mode,unit,
                default_cutting_mode,production_label_enabled,is_composite,
                combination_mode,is_internal_component,is_active,manual_modified,version,
                external_packaging_category_code,external_packaging_specification_json,
                external_packaging_specification_summary,external_packaging_purchase_unit,
                external_packaging_candidate_snapshot_json
            ) VALUES (?,?,?,?,'normal','其他','external_purchase','no_plate','片',
                      '一开一',0,0,'parent_priced_set',0,1,0,1,
                      'honeycomb_board','{}','迁移测试蜂窝板','片','[]')
            """,
            (customer_id, "P1-63-EXT", "P1-63-EXT", "迁移测试蜂窝板"),
        ).lastrowid
        connection.execute(
            """
            CREATE TRIGGER p1_63_unrelated_product_guard
            BEFORE DELETE ON products
            BEGIN SELECT RAISE(ABORT, 'keep unrelated product trigger'); END
            """
        )
        connection.commit()
        return int(product_id)


def test_p1_63_migration_is_linear() -> None:
    spec = importlib.util.spec_from_file_location("p1_63_migration", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == TARGET
    assert module.down_revision == PARENT


def test_p1_63_round_trip_guards_history_and_preserves_unrelated_trigger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "p1-63.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, PARENT)
    product_id = _seed_external_product(path)
    command.upgrade(config, TARGET)

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            """
            SELECT external_packaging_default_order_quantity_basis,
                   external_packaging_default_purchase_quantity_basis
            FROM products WHERE id=?
            """,
            (product_id,),
        ).fetchone() == (None, None)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE products
                SET external_packaging_default_order_quantity_basis=1
                WHERE id=?
                """,
                (product_id,),
            )
        connection.rollback()
        connection.execute(
            """
            UPDATE products
            SET external_packaging_default_order_quantity_basis=1,
                external_packaging_default_purchase_quantity_basis=2
            WHERE id=?
            """,
            (product_id,),
        )
        connection.commit()
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name=?",
            ("p1_63_unrelated_product_guard",),
        ).fetchone() is not None
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    with pytest.raises(RuntimeError, match="default purchase ratios"):
        command.downgrade(config, PARENT)

    clean_path = tmp_path / "p1-63-clean.sqlite3"
    clean_config = _config(monkeypatch, clean_path)
    command.upgrade(clean_config, PARENT)
    _seed_external_product(clean_path)
    command.upgrade(clean_config, TARGET)
    command.downgrade(clean_config, PARENT)
    command.upgrade(clean_config, TARGET)
    with sqlite3.connect(clean_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name=?",
            ("p1_63_unrelated_product_guard",),
        ).fetchone() is not None
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == TARGET
