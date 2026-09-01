from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.orm import Session


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "ja62v8x9z51"
TARGET_REVISION = "jb63v8x9z52"
CURRENT_HEAD = "jf65v8x9z54"
EXPECTED_ORDER_INDEXES = {
    "ix_sales_orders_customer_po": ("customer_po",),
    "ix_sales_orders_customer_po_group": ("customer_id", "customer_po"),
}
EXPECTED_ITEM_INDEXES = {
    "ux_sales_order_items_item_order_number": ("item_order_number",),
    "ix_sales_order_items_snapshot_product_code": ("snapshot_product_code",),
    "ix_sales_order_items_snapshot_product_name": ("snapshot_product_name",),
}


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "p1-136-order-number-migration-test")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return config


def _index_contract(
    connection: sqlite3.Connection, table: str
) -> dict[str, tuple[bool, tuple[str, ...]]]:
    result: dict[str, tuple[bool, tuple[str, ...]]] = {}
    for row in connection.execute(f"PRAGMA index_list({table})"):
        name = str(row[1])
        columns = tuple(
            str(item[2])
            for item in connection.execute(f'PRAGMA index_info("{name}")')
        )
        result[name] = (bool(row[2]), columns)
    return result


def _assert_schema(connection: sqlite3.Connection) -> None:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "order_item_number_sequences" in tables
    item_columns = {
        str(row[1]): row
        for row in connection.execute("PRAGMA table_info(sales_order_items)")
    }
    assert {"item_order_number", "item_sequence"} <= item_columns.keys()
    sequence_columns = {
        str(row[1]): row
        for row in connection.execute(
            "PRAGMA table_info(order_item_number_sequences)"
        )
    }
    assert {
        "order_id",
        "last_item_sequence",
        "updated_at",
    } <= sequence_columns.keys()
    assert int(sequence_columns["order_id"][5]) == 1
    assert int(sequence_columns["last_item_sequence"][3]) == 1
    assert int(sequence_columns["updated_at"][3]) == 1

    order_indexes = _index_contract(connection, "sales_orders")
    for name, columns in EXPECTED_ORDER_INDEXES.items():
        assert order_indexes[name] == (False, columns)
    item_indexes = _index_contract(connection, "sales_order_items")
    for name, columns in EXPECTED_ITEM_INDEXES.items():
        assert item_indexes[name] == (
            name == "ux_sales_order_items_item_order_number",
            columns,
        )


def _assert_health(connection: sqlite3.Connection, revision: str) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    assert connection.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchone() == (revision,)
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _prepare_metadata_parent(
    monkeypatch: pytest.MonkeyPatch,
    database: Path,
    *,
    structure: str,
) -> Config:
    from app.core.database import create_sqlite_engine
    from app.models import Base

    engine = create_sqlite_engine(database)
    Base.metadata.create_all(engine)
    engine.dispose()
    with sqlite3.connect(database) as connection:
        if structure in {"missing", "partial", "legacy"}:
            for name in (*EXPECTED_ORDER_INDEXES, *EXPECTED_ITEM_INDEXES):
                connection.execute(f'DROP INDEX IF EXISTS "{name}"')
            connection.execute("DROP TABLE order_item_number_sequences")
        if structure in {"missing", "legacy"}:
            connection.execute(
                "ALTER TABLE sales_order_items DROP COLUMN item_order_number"
            )
            connection.execute("ALTER TABLE sales_order_items DROP COLUMN item_sequence")
        if structure == "legacy":
            from scripts.admin.apply_order_number_structure import migrate_schema

            connection.row_factory = sqlite3.Row
            migrate_schema(connection)
        connection.commit()
    config = _config(monkeypatch, database)
    command.stamp(config, PARENT_REVISION)
    return config


def _seed_numbered_order(
    database: Path,
    *,
    item_numbers: tuple[str, ...] = ("TM20260901001-003",),
    item_sequences: tuple[int, ...] = (3,),
    add_sequence_row: bool,
) -> tuple[int, list[int]]:
    from app.core.database import create_sqlite_engine
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem, OrderItemNumberSequence
    from app.models.product import Product

    engine = create_sqlite_engine(database)
    try:
        with Session(engine, expire_on_commit=False) as session:
            customer = Customer(
                customer_number=9136,
                customer_code="P1136",
                name="P1-136迁移验收客户",
                payment_term_days=30,
                credit_limit=Decimal("0"),
            )
            session.add(customer)
            session.flush()
            product = Product(
                customer_id=customer.id,
                product_code="P1136-BOX",
                customer_material_code="P1136-BOX",
                product_name="P1-136迁移验收纸箱",
                box_category="normal",
            )
            session.add(product)
            session.flush()
            order = Order(
                order_number="TM20260901001",
                customer_id=customer.id,
                customer_po="P1-136-PO",
                order_date=date(2026, 9, 1),
                delivery_date=date(2026, 9, 2),
                status="pending_delivery",
                payment_status="unpaid",
                total_amount=Decimal(str(len(item_numbers))),
            )
            session.add(order)
            session.flush()
            items: list[OrderItem] = []
            for item_number, item_sequence in zip(
                item_numbers, item_sequences, strict=True
            ):
                item = OrderItem(
                    order_id=order.id,
                    product_id=product.id,
                    item_order_number=item_number,
                    item_sequence=item_sequence,
                    quantity=1,
                    unit_price=Decimal("1"),
                    subtotal=Decimal("1"),
                    material_status="received",
                    snapshot_product_code=product.product_code,
                    snapshot_product_name=product.product_name,
                    snapshot_spec="400×300×200mm",
                )
                session.add(item)
                items.append(item)
            if add_sequence_row:
                session.add(
                    OrderItemNumberSequence(
                        order_id=order.id,
                        last_item_sequence=max(item_sequences),
                    )
                )
            session.commit()
            return int(order.id), [int(item.id) for item in items]
    finally:
        engine.dispose()


def _business_snapshot(database: Path) -> tuple:
    with sqlite3.connect(database) as connection:
        return (
            connection.execute("SELECT COUNT(*) FROM sales_orders").fetchone()[0],
            connection.execute(
                "SELECT COUNT(*) FROM sales_order_items"
            ).fetchone()[0],
            connection.execute(
                "SELECT id, order_id, item_order_number, item_sequence "
                "FROM sales_order_items ORDER BY id"
            ).fetchall(),
            connection.execute(
                "SELECT order_id, last_item_sequence "
                "FROM order_item_number_sequences ORDER BY order_id"
            ).fetchall(),
        )


def test_empty_database_full_chain_creates_order_number_contract_and_accepts_orm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p1-136-empty-full-chain.sqlite3"
    config = _config(monkeypatch, database)
    script = ScriptDirectory.from_config(config)
    assert script.get_revision(TARGET_REVISION).down_revision == PARENT_REVISION

    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        _assert_health(connection, CURRENT_HEAD)
        _assert_schema(connection)

    _seed_numbered_order(database, add_sequence_row=True)
    with sqlite3.connect(database) as connection:
        _assert_health(connection, CURRENT_HEAD)
        assert connection.execute(
            "SELECT item_order_number, item_sequence FROM sales_order_items"
        ).fetchone() == ("TM20260901001-003", 3)


def test_existing_complete_legacy_structure_is_adopted_without_business_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p1-136-existing-complete.sqlite3"
    config = _prepare_metadata_parent(
        monkeypatch, database, structure="legacy"
    )
    _seed_numbered_order(database, add_sequence_row=True)
    before = _business_snapshot(database)

    command.upgrade(config, TARGET_REVISION)

    with sqlite3.connect(database) as connection:
        _assert_health(connection, TARGET_REVISION)
        _assert_schema(connection)
    assert _business_snapshot(database) == before


def test_partial_structure_is_completed_and_sequence_floor_is_backfilled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p1-136-partial.sqlite3"
    config = _prepare_metadata_parent(monkeypatch, database, structure="partial")
    order_id, _ = _seed_numbered_order(database, add_sequence_row=False)
    command.upgrade(config, TARGET_REVISION)

    with sqlite3.connect(database) as connection:
        _assert_health(connection, TARGET_REVISION)
        _assert_schema(connection)
        assert connection.execute(
            "SELECT last_item_sequence FROM order_item_number_sequences "
            "WHERE order_id=?",
            (order_id,),
        ).fetchone() == (3,)
        assert connection.execute(
            "SELECT item_order_number, item_sequence FROM sales_order_items"
        ).fetchone() == ("TM20260901001-003", 3)


def test_duplicate_non_null_item_numbers_fail_before_schema_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p1-136-duplicate.sqlite3"
    config = _prepare_metadata_parent(monkeypatch, database, structure="partial")
    _seed_numbered_order(
        database,
        item_numbers=("TM20260901001-001", "TM20260901001-001"),
        item_sequences=(1, 2),
        add_sequence_row=False,
    )

    with pytest.raises(RuntimeError, match="duplicate item_order_number"):
        command.upgrade(config, TARGET_REVISION)

    with sqlite3.connect(database) as connection:
        _assert_health(connection, PARENT_REVISION)
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "order_item_number_sequences" not in tables
        assert not (
            EXPECTED_ORDER_INDEXES.keys()
            & _index_contract(connection, "sales_orders").keys()
        )
        assert not (
            EXPECTED_ITEM_INDEXES.keys()
            & _index_contract(connection, "sales_order_items").keys()
        )


def test_incompatible_named_index_fails_before_schema_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p1-136-incompatible-index.sqlite3"
    config = _prepare_metadata_parent(monkeypatch, database, structure="partial")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE INDEX ix_sales_orders_customer_po "
            "ON sales_orders (customer_id)"
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="incompatible contract"):
        command.upgrade(config, TARGET_REVISION)

    with sqlite3.connect(database) as connection:
        _assert_health(connection, PARENT_REVISION)
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "order_item_number_sequences" not in tables
        assert _index_contract(connection, "sales_orders")[
            "ix_sales_orders_customer_po"
        ] == (False, ("customer_id",))


def test_existing_sequence_floor_below_numbered_items_fails_before_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p1-136-low-sequence-floor.sqlite3"
    config = _prepare_metadata_parent(monkeypatch, database, structure="complete")
    order_id, _ = _seed_numbered_order(database, add_sequence_row=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE order_item_number_sequences SET last_item_sequence=2 "
            "WHERE order_id=?",
            (order_id,),
        )
        connection.commit()
    before = _business_snapshot(database)

    with pytest.raises(RuntimeError, match="sequence floor is below"):
        command.upgrade(config, TARGET_REVISION)

    with sqlite3.connect(database) as connection:
        _assert_health(connection, PARENT_REVISION)
    assert _business_snapshot(database) == before


def test_empty_database_can_round_trip_but_order_facts_block_downgrade(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    empty_database = tmp_path / "p1-136-empty-round-trip.sqlite3"
    empty_config = _prepare_metadata_parent(
        monkeypatch, empty_database, structure="missing"
    )
    command.upgrade(empty_config, TARGET_REVISION)
    command.downgrade(empty_config, PARENT_REVISION)
    with sqlite3.connect(empty_database) as connection:
        _assert_health(connection, PARENT_REVISION)
        item_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(sales_order_items)")
        }
        assert "item_order_number" not in item_columns
        assert "item_sequence" not in item_columns
    command.upgrade(empty_config, TARGET_REVISION)
    with sqlite3.connect(empty_database) as connection:
        _assert_health(connection, TARGET_REVISION)
        _assert_schema(connection)

    facts_database = tmp_path / "p1-136-facts-downgrade.sqlite3"
    facts_config = _prepare_metadata_parent(
        monkeypatch, facts_database, structure="complete"
    )
    _seed_numbered_order(facts_database, add_sequence_row=True)
    command.upgrade(facts_config, TARGET_REVISION)
    before = _business_snapshot(facts_database)
    with pytest.raises(RuntimeError, match="order numbering facts would be lost"):
        command.downgrade(facts_config, PARENT_REVISION)
    with sqlite3.connect(facts_database) as connection:
        _assert_health(connection, TARGET_REVISION)
    assert _business_snapshot(facts_database) == before


def test_downgrade_preserves_empty_structure_that_predated_the_migration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p1-136-preexisting-empty.sqlite3"
    config = _prepare_metadata_parent(monkeypatch, database, structure="legacy")

    command.upgrade(config, TARGET_REVISION)
    command.downgrade(config, PARENT_REVISION)

    with sqlite3.connect(database) as connection:
        _assert_health(connection, PARENT_REVISION)
        _assert_schema(connection)
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "p1_136_order_number_schema_adoption_state" not in tables

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        _assert_health(connection, TARGET_REVISION)
        _assert_schema(connection)
