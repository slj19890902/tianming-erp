from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
import sqlite3

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.requisition import RequisitionHold
from app.models.user import User


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "db84v8x9z73_requisition_holds.py"


@pytest.fixture()
def hold_session_factory(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "q1-04-holds.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        user = User(
            username="q1_hold_user",
            password_hash="not-used-in-model-test",
            role="admin",
            real_name="Q1 暂缓测试",
            display_name="Q1 暂缓测试",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=9904,
            customer_code="Q104",
            name="Q1-04 匿名客户",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        session.add_all([user, customer])
        session.flush()
        product = Product(
            customer_id=customer.id,
            product_code="Q1-HOLD-001",
            customer_material_code="Q1-HOLD-001",
            product_name="Q1 等候报料匿名箱",
            box_category="normal",
        )
        session.add(product)
        session.flush()
        items: list[OrderItem] = []
        for suffix in (1, 2):
            order = Order(
                order_number=f"Q1-HOLD-20260729-{suffix:03d}",
                customer_id=customer.id,
                order_date=date(2026, 7, 29),
                delivery_date=date(2026, 8, 5),
                status="pending_production",
                payment_status="unpaid",
                total_amount=Decimal("100"),
            )
            session.add(order)
            session.flush()
            item = OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=100,
                unit_price=Decimal("1"),
                subtotal=Decimal("100"),
                material_status="pending",
                requisition_status="未报料",
                snapshot_product_code=product.product_code,
                snapshot_product_name=product.product_name,
                snapshot_spec="匿名规格",
                snapshot_material="匿名材质",
            )
            session.add(item)
            items.append(item)
        session.commit()
        return factory, user.id, items[0].id, items[1].id


def _hold_snapshot(
    *,
    order_item_id: int,
    previous_order_item_id: int | None = None,
) -> dict:
    return {
        "order_item_id": order_item_id,
        "order_item_id_snapshot": order_item_id,
        "customer_id_snapshot": 1,
        "customer_name_snapshot": "Q1-04 匿名客户",
        "order_number_snapshot": "Q1-HOLD-20260729-002",
        "order_item_sequence_snapshot": 2,
        "product_code_snapshot": "Q1-HOLD-001",
        "product_name_snapshot": "Q1 等候报料匿名箱",
        "specification_snapshot": "匿名规格",
        "quantity_snapshot": 100,
        "previous_order_item_id": previous_order_item_id,
        "previous_order_item_id_snapshot": previous_order_item_id,
    }


def test_hold_requires_one_mode_and_allows_only_one_active_per_order_item(
    hold_session_factory,
) -> None:
    factory, user_id, previous_item_id, current_item_id = hold_session_factory
    with factory() as session:
        hold = RequisitionHold(
            **_hold_snapshot(
                order_item_id=current_item_id,
                previous_order_item_id=previous_item_id,
            ),
            release_mode="previous_batch_completed",
            created_by=user_id,
        )
        session.add(hold)
        session.commit()
        assert hold.status == "active"
        assert hold.version == 1
        assert hold.created_at is not None

        session.add(
            RequisitionHold(
                **_hold_snapshot(order_item_id=current_item_id),
                release_mode="expected_date",
                expected_requisition_date=date(2026, 8, 1),
                created_by=user_id,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        hold.status = "released"
        hold.version += 1
        hold.released_by = user_id
        session.flush()
        replacement = RequisitionHold(
            **_hold_snapshot(order_item_id=current_item_id),
            release_mode="expected_date",
            expected_requisition_date=date(2026, 8, 1),
            created_by=user_id,
        )
        session.add(replacement)
        session.commit()
        assert replacement.status == "active"
        assert replacement.previous_order_item_id is None


def test_hold_rejects_mixed_conditions_and_self_reference(hold_session_factory) -> None:
    factory, user_id, previous_item_id, current_item_id = hold_session_factory
    with factory() as session:
        session.add(
            RequisitionHold(
                **_hold_snapshot(
                    order_item_id=current_item_id,
                    previous_order_item_id=previous_item_id,
                ),
                release_mode="previous_batch_completed",
                expected_requisition_date=date(2026, 8, 1),
                created_by=user_id,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        session.add(
            RequisitionHold(
                **_hold_snapshot(
                    order_item_id=current_item_id,
                    previous_order_item_id=current_item_id,
                ),
                release_mode="previous_batch_completed",
                created_by=user_id,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def _alembic_config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def test_q1_04_migration_is_linear_round_trips_empty_sqlite_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    compile(source, str(MIGRATION), "exec")
    assert 'revision: str = "db84v8x9z73"' in source
    assert 'down_revision: Union[str, Sequence[str], None] = "da83v8x9z72"' in source
    assert "cx80v8x9z69" not in source
    assert "cv78v8x9z67" not in source
    assert "禁止破坏性降级" in source
    assert "INSERT INTO" not in source and "UPDATE sales_order_items" not in source

    path = tmp_path / "q1-04-migration.sqlite3"
    config = _alembic_config(monkeypatch, path)
    command.upgrade(config, "da83v8x9z72")
    command.upgrade(config, "db84v8x9z73")
    columns = {
        column["name"]
        for column in inspect(create_sqlite_engine(path)).get_columns("requisition_holds")
    }
    assert {
        "order_item_id",
        "order_item_id_snapshot",
        "customer_id_snapshot",
        "customer_name_snapshot",
        "order_number_snapshot",
        "product_code_snapshot",
        "product_name_snapshot",
        "quantity_snapshot",
        "release_mode",
        "previous_order_item_id",
        "expected_requisition_date",
        "status",
        "version",
        "created_by",
        "updated_by",
        "released_by",
        "released_at",
        "release_source",
        "release_note",
    } <= columns
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "db84v8x9z73"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    command.downgrade(config, "da83v8x9z72")
    assert "requisition_holds" not in inspect(create_sqlite_engine(path)).get_table_names()
    command.upgrade(config, "db84v8x9z73")

    # Test the generated migration schema itself, rather than the current ORM:
    # the repository has later model columns beyond the parent revision, while
    # this Q1 candidate must rehearse exactly from da83.
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("INSERT INTO customers (id, name) VALUES (1, '匿名客户')")
        connection.execute(
            "INSERT INTO products "
            "(id, customer_id, product_code, customer_material_code, product_name) "
            "VALUES (1, 1, 'Q1-MIG-001', 'Q1-MIG-001', '匿名箱')"
        )
        for item_id in range(1, 5):
            connection.execute(
                "INSERT INTO sales_orders "
                "(id, order_number, customer_id, order_date, total_amount) "
                "VALUES (?, ?, 1, '2026-07-29', 100)",
                (item_id, f"Q1-MIG-20260729-{item_id:03d}"),
            )
            connection.execute(
                "INSERT INTO sales_order_items "
                "(id, order_id, product_id, quantity, unit_price, subtotal, "
                "snapshot_product_name) VALUES (?, ?, 1, 100, 1, 100, '匿名箱')",
                (item_id, item_id),
            )
        connection.execute(
            "INSERT INTO requisition_holds "
            "(id, order_item_id, order_item_id_snapshot, customer_id_snapshot, "
            "customer_name_snapshot, order_number_snapshot, product_code_snapshot, "
            "product_name_snapshot, quantity_snapshot, release_mode, "
            "previous_order_item_id, previous_order_item_id_snapshot, status, version) "
            "VALUES (1, 2, 2, 1, '匿名客户', 'Q1-MIG-20260729-002', 'Q1-MIG-001', "
            "'匿名箱', 100, 'previous_batch_completed', 1, 1, 'active', 1)"
        )
        connection.execute("DELETE FROM sales_order_items WHERE id = 2")
        target_deleted = connection.execute(
            "SELECT order_item_id, order_item_id_snapshot, status, release_source, "
            "released_at, version FROM requisition_holds WHERE id = 1"
        ).fetchone()
        assert target_deleted[0] is None
        assert target_deleted[1] == 2
        assert target_deleted[2:5] == ("invalidated", "order_item_deleted", target_deleted[4])
        assert target_deleted[4] is not None
        assert target_deleted[5] == 2

        connection.execute(
            "INSERT INTO requisition_holds "
            "(id, order_item_id, order_item_id_snapshot, customer_id_snapshot, "
            "customer_name_snapshot, order_number_snapshot, product_code_snapshot, "
            "product_name_snapshot, quantity_snapshot, release_mode, "
            "previous_order_item_id, previous_order_item_id_snapshot, status, version) "
            "VALUES (2, 4, 4, 1, '匿名客户', 'Q1-MIG-20260729-004', 'Q1-MIG-001', "
            "'匿名箱', 100, 'previous_batch_completed', 3, 3, 'active', 1)"
        )
        connection.execute("DELETE FROM sales_order_items WHERE id = 3")
        previous_deleted = connection.execute(
            "SELECT previous_order_item_id, previous_order_item_id_snapshot, status, "
            "release_source, released_at, version FROM requisition_holds WHERE id = 2"
        ).fetchone()
        assert previous_deleted[0] is None
        assert previous_deleted[1] == 3
        assert previous_deleted[2:] == ("active", None, None, 1)
        connection.commit()
    with pytest.raises(RuntimeError, match="禁止破坏性降级"):
        command.downgrade(config, "da83v8x9z72")
