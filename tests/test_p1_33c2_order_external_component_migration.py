from __future__ import annotations

from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect

from app.core.database import create_sqlite_engine


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "dp98v8x9z87"
TARGET_REVISION = "dq99v8x9z88"


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _checks(path: Path) -> tuple[str, int]:
    with sqlite3.connect(path) as connection:
        return (
            connection.execute("PRAGMA integrity_check").fetchone()[0],
            len(connection.execute("PRAGMA foreign_key_check").fetchall()),
        )


def test_snapshot_migration_is_linear_and_model_matches() -> None:
    source = (
        ROOT
        / "alembic/versions/dq99v8x9z88_order_external_component_snapshots.py"
    ).read_text(encoding="utf-8")
    assert 'revision = "dq99v8x9z88"' in source
    assert 'down_revision = "dp98v8x9z87"' in source
    assert "禁止破坏性降级" in source
    assert "immutable_update" in source

    from app.models import Base

    assert {
        "sales_order_item_external_components",
        "sales_order_item_external_component_candidates",
    } <= set(Base.metadata.tables)


def test_snapshot_migration_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "external-order-roundtrip.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)
    tables = set(inspect(create_sqlite_engine(path)).get_table_names())
    assert "sales_order_item_external_components" in tables
    assert _checks(path) == ("ok", 0)
    command.downgrade(config, PARENT_REVISION)
    tables = set(inspect(create_sqlite_engine(path)).get_table_names())
    assert "sales_order_item_external_components" not in tables
    assert _checks(path) == ("ok", 0)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == TARGET_REVISION
        )
    assert _checks(path) == ("ok", 0)


def test_snapshot_rows_are_immutable_and_downgrade_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "external-order-fail-closed.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        customer_id = connection.execute(
            "INSERT INTO customers(name) VALUES (?)", ("迁移匿名客户",)
        ).lastrowid
        product_id = connection.execute(
            """
            INSERT INTO products(
                customer_id,product_code,customer_material_code,product_name
            ) VALUES (?,?,?,?)
            """,
            (customer_id, "MIG-BOX", "MIG-BOX", "迁移匿名纸箱"),
        ).lastrowid
        supplier_id = connection.execute(
            """
            INSERT INTO supplier_master_records(
                standard_name,normalized_name,display_name,sort_order
            ) VALUES (?,?,?,?)
            """,
            ("迁移匿名供应商", "迁移匿名供应商", "迁移供应商", 999),
        ).lastrowid
        external_id = connection.execute(
            """
            INSERT INTO external_packaging_products(
                supplier_id,category_code,supplier_product_code,
                normalized_supplier_product_code,product_name,purchase_unit,
                specification_summary,specification_json
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                supplier_id,
                "paper_corner_guard",
                "MIG-CORNER",
                "MIG-CORNER",
                "迁移护角",
                "根",
                "L型",
                "{}",
            ),
        ).lastrowid
        set_id = connection.execute(
            """
            INSERT INTO product_external_component_sets(product_id,version,is_current)
            VALUES (?,1,1)
            """,
            (product_id,),
        ).lastrowid
        component_id = connection.execute(
            """
            INSERT INTO product_external_components(
                component_set_id,display_order,purpose,quantity_per_finished_unit,
                waste_rate,consumption_unit,is_required,category_code,
                specification_json,specification_summary
            ) VALUES (?,1,'四角防护',4,0.02,'根',1,'paper_corner_guard','{}','L型')
            """,
            (set_id,),
        ).lastrowid
        source_candidate_id = connection.execute(
            """
            INSERT INTO product_external_component_candidates(
                component_id,external_product_id,is_default,supplier_id_snapshot,
                supplier_name_snapshot,supplier_product_code_snapshot,
                product_name_snapshot,purchase_unit_snapshot,
                external_product_version_snapshot
            ) VALUES (?,?,1,?,?,?,?,?,1)
            """,
            (
                component_id,
                external_id,
                supplier_id,
                "迁移供应商",
                "MIG-CORNER",
                "迁移护角",
                "根",
            ),
        ).lastrowid
        order_id = connection.execute(
            """
            INSERT INTO sales_orders(order_number,customer_id,order_date,total_amount)
            VALUES ('TM20260809001',?,'2026-08-09',100)
            """,
            (customer_id,),
        ).lastrowid
        item_id = connection.execute(
            """
            INSERT INTO sales_order_items(
                order_id,product_id,quantity,unit_price,subtotal,snapshot_product_name
            ) VALUES (?,?,100,1,100,'迁移匿名纸箱')
            """,
            (order_id, product_id),
        ).lastrowid
        snapshot_id = connection.execute(
            """
            INSERT INTO sales_order_item_external_components(
                sales_order_item_id,source_component_set_id,source_component_id,
                source_component_set_version,display_order,purpose,
                quantity_per_finished_unit,waste_rate,consumption_unit,is_required,
                category_code,specification_json,specification_summary
            ) VALUES (?,?,?,1,1,'四角防护',4,0.02,'根',1,
                      'paper_corner_guard','{}','L型')
            """,
            (item_id, set_id, component_id),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO sales_order_item_external_component_candidates(
                order_component_id,source_candidate_id,external_product_id_snapshot,
                is_default,supplier_id_snapshot,supplier_name_snapshot,
                supplier_product_code_snapshot,product_name_snapshot,
                purchase_unit_snapshot,external_product_version_snapshot
            ) VALUES (?,?,?,1,?,?,?,?,?,1)
            """,
            (
                snapshot_id,
                source_candidate_id,
                external_id,
                supplier_id,
                "迁移供应商",
                "MIG-CORNER",
                "迁移护角",
                "根",
            ),
        )
        connection.commit()
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute(
                "UPDATE sales_order_item_external_components "
                "SET purpose='被篡改' WHERE id=?",
                (snapshot_id,),
            )

    with pytest.raises(RuntimeError, match="禁止破坏性降级"):
        command.downgrade(config, PARENT_REVISION)
    assert _checks(path) == ("ok", 0)
