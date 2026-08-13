from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.core.database import create_sqlite_engine
from app.models.customer import Customer
from app.models.order import Order
from app.models.product import Product


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "dr00v8x9z89"
TARGET_REVISION = "ds01v8x9z90"
TABLE = "sales_order_item_material_cost_snapshots"


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


def test_snapshot_migration_is_linear_and_model_registered() -> None:
    source = (
        ROOT / "alembic/versions/ds01v8x9z90_order_material_cost_snapshots.py"
    ).read_text(encoding="utf-8")
    assert 'revision = "ds01v8x9z90"' in source
    assert 'down_revision = "dr00v8x9z89"' in source
    assert "禁止破坏性降级" in source
    from app.models import Base

    assert TABLE in Base.metadata.tables


def test_snapshot_migration_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "material-cost-snapshot-roundtrip.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)
    assert TABLE in set(inspect(create_sqlite_engine(path)).get_table_names())
    assert _checks(path) == ("ok", 0)
    command.downgrade(config, PARENT_REVISION)
    assert TABLE not in set(inspect(create_sqlite_engine(path)).get_table_names())
    assert _checks(path) == ("ok", 0)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET_REVISION
    assert _checks(path) == ("ok", 0)


def test_snapshot_is_immutable_and_downgrade_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "material-cost-snapshot-fail-closed.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)
    engine = create_sqlite_engine(path)
    with Session(engine) as session:
        customer_id = session.execute(
            text("INSERT INTO customers(name) VALUES ('迁移匿名客户') RETURNING id")
        ).scalar_one()
        product_id = session.execute(
            text(
                """INSERT INTO products(
                customer_id,product_code,customer_material_code,product_name,box_category
                ) VALUES (:customer_id,'P1-28B-MIGRATION','P1-28B-MIGRATION',
                '迁移匿名纸箱','normal') RETURNING id"""
            ),
            {"customer_id": customer_id},
        ).scalar_one()
        order_id = session.execute(
            text(
                """INSERT INTO sales_orders(
                order_number,customer_id,order_date,total_amount
                ) VALUES ('TM20260809028',:customer_id,'2026-08-09',1) RETURNING id"""
            ),
            {"customer_id": customer_id},
        ).scalar_one()
        item_id = session.execute(
            text(
                """INSERT INTO sales_order_items(
                order_id,product_id,quantity,unit_price,subtotal,
                material_status,snapshot_product_name
                ) VALUES (:order_id,:product_id,1,1,1,'pending','迁移匿名纸箱')
                RETURNING id"""
            ),
            {"order_id": order_id, "product_id": product_id},
        ).scalar_one()
        session.execute(
            text(
                f"""INSERT INTO {TABLE}(
                sales_order_item_id,snapshot_version,source_fingerprint,
                order_item_reference_snapshot,
                calculation_status,scope_code,formula_version,precision_version,
                order_quantity_snapshot,components_json,missing_items_json
                ) VALUES (:item_id,1,:fingerprint,'TM20260809028-001','missing','material_only',
                'p1-28a-material-v1','p1-28b-decimal-v1',1,'[]','[]')"""
            ),
            {"item_id": item_id, "fingerprint": "a" * 64},
        )
        session.commit()

    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute(
                f"UPDATE {TABLE} SET snapshot_version=2 WHERE snapshot_version=1"
            )
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute(f"DELETE FROM {TABLE}")
    with pytest.raises(RuntimeError, match="禁止破坏性降级"):
        command.downgrade(config, PARENT_REVISION)
    assert _checks(path) == ("ok", 0)
