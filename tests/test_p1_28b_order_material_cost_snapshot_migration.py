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
        customer = Customer(name="迁移匿名客户")
        session.add(customer)
        session.flush()
        product = Product(
            customer_id=customer.id,
            product_code="P1-28B-MIGRATION",
            customer_material_code="P1-28B-MIGRATION",
            product_name="迁移匿名纸箱",
            box_category="normal",
        )
        session.add(product)
        session.flush()
        order = Order(
            order_number="TM20260809028",
            customer_id=customer.id,
            order_date=date(2026, 8, 9),
            total_amount=Decimal("1"),
        )
        session.add(order)
        session.flush()
        item_id = session.execute(
            text(
                """INSERT INTO sales_order_items(
                order_id,product_id,quantity,unit_price,subtotal,
                material_status,snapshot_product_name
                ) VALUES (:order_id,:product_id,1,1,1,'pending','迁移匿名纸箱')
                RETURNING id"""
            ),
            {"order_id": order.id, "product_id": product.id},
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
