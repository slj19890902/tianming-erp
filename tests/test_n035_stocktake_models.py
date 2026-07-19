from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest
import sqlalchemy as sa


MIGRATION_PATH = Path("alembic/versions/be58v8x9z49_n035_mobile_stocktake.py")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _load_migration():
    spec = importlib.util.spec_from_file_location("n035_stocktake", MIGRATION_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stocktake_models_define_required_schema_and_constraints() -> None:
    from app.models.stocktake import StocktakeItem, StocktakeOrder, StocktakeReview

    assert StocktakeOrder.__tablename__ == "stocktake_orders"
    assert StocktakeItem.__tablename__ == "stocktake_items"
    assert StocktakeReview.__tablename__ == "stocktake_reviews"
    for model, fields in (
        (StocktakeOrder, ("order_number", "location_id", "status", "version", "submitted_by", "submitted_at", "reviewed_by", "reviewed_at", "review_note", "idempotency_key", "created_at", "updated_at")),
        (StocktakeItem, ("order_id", "inventory_lot_id", "lot_version_snapshot", "available_quantity_snapshot", "reserved_quantity_snapshot", "on_hand_quantity_snapshot", "counted_quantity", "difference_quantity", "specification_snapshot", "adjustment_movement_id")),
        (StocktakeReview, ("order_id", "sequence", "action", "from_status", "to_status", "reason", "idempotency_key", "details_json", "reviewed_by", "reviewed_at")),
    ):
        assert set(fields).issubset(model.__table__.columns.keys())

    item_checks = {constraint.name for constraint in StocktakeItem.__table__.constraints}
    assert {
        "ck_stocktake_items_quantities_nonnegative",
        "ck_stocktake_items_on_hand_snapshot",
        "ck_stocktake_items_difference",
    }.issubset(item_checks)
    assert any(
        foreign_key.target_fullname == "inventory_movements.id"
        for foreign_key in StocktakeItem.__table__.foreign_keys
    )
    assert StocktakeItem.__table__.c.specification_snapshot.nullable is True


def test_stocktake_difference_quantity_is_signed_counted_minus_on_hand() -> None:
    from app.models.stocktake import StocktakeItem

    check_sql = {
        str(constraint.sqltext)
        for constraint in StocktakeItem.__table__.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert "difference_quantity = counted_quantity - on_hand_quantity_snapshot" in check_sql
    assert all("difference_quantity >= 0" not in sql for sql in check_sql)

    migration_content = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "difference_quantity = counted_quantity - on_hand_quantity_snapshot" in migration_content
    assert "difference_quantity >= 0" not in migration_content


def test_n035_migration_is_additive_and_uat_only() -> None:
    content = MIGRATION_PATH.read_text(encoding="utf-8").lower()
    assert 'down_revision: union[str, sequence[str], none] = "bd57v8x9z47"' in content
    assert "bd57v8x9z47" in content
    for table in ("stocktake_orders", "stocktake_items", "stocktake_reviews"):
        assert f'"{table}"' in content
    upgrade_content = content.split("def downgrade", 1)[0]
    assert "batch_alter_table" not in upgrade_content
    assert "alter table inventory_movements" not in upgrade_content
    assert "update inventory_movements" not in upgrade_content


def test_n035_downgrade_refuses_to_discard_business_rows() -> None:
    migration = _load_migration()

    class ScalarResult:
        def __init__(self, value: int) -> None:
            self.value = value

        def scalar_one(self) -> int:
            return self.value

    class Connection:
        counts = iter((1, 0, 0))

        def execute(self, statement):  # noqa: ANN001
            return ScalarResult(next(self.counts))

    with pytest.raises(RuntimeError, match="fail-closed"):
        migration._assert_stocktake_tables_empty(Connection())


def _create_sqlite_guard_schema(connection: sa.Connection) -> None:
    connection.execute(
        sa.text(
            """
            CREATE TABLE stocktake_orders (
                id INTEGER PRIMARY KEY,
                order_number TEXT NOT NULL,
                location_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                version INTEGER NOT NULL,
                submitted_by INTEGER NOT NULL,
                submitted_at TEXT NOT NULL,
                reviewed_by INTEGER,
                reviewed_at TEXT,
                review_note TEXT,
                idempotency_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE TABLE inventory_movements (
                id INTEGER PRIMARY KEY,
                inventory_lot_id INTEGER NOT NULL,
                movement_type TEXT NOT NULL
            )
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE TABLE stocktake_items (
                id INTEGER PRIMARY KEY,
                order_id INTEGER NOT NULL,
                inventory_lot_id INTEGER NOT NULL,
                lot_version_snapshot INTEGER NOT NULL,
                available_quantity_snapshot INTEGER NOT NULL,
                reserved_quantity_snapshot INTEGER NOT NULL,
                on_hand_quantity_snapshot INTEGER NOT NULL,
                counted_quantity INTEGER NOT NULL,
                difference_quantity INTEGER NOT NULL,
                lot_number_snapshot TEXT NOT NULL,
                customer_name_snapshot TEXT,
                product_name_snapshot TEXT,
                specification_snapshot TEXT,
                inventory_code_snapshot TEXT,
                location_code_snapshot TEXT NOT NULL,
                unit_snapshot TEXT NOT NULL,
                adjustment_movement_id INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE TABLE stocktake_reviews (
                id INTEGER PRIMARY KEY,
                order_id INTEGER NOT NULL,
                sequence INTEGER NOT NULL,
                action TEXT NOT NULL,
                from_status TEXT NOT NULL,
                to_status TEXT NOT NULL,
                reason TEXT,
                idempotency_key TEXT NOT NULL,
                details_json TEXT,
                reviewed_by INTEGER NOT NULL,
                reviewed_at TEXT NOT NULL
            )
            """
        )
    )


def _insert_draft_order(connection: sa.Connection, order_id: int) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO stocktake_orders
                (id, order_number, location_id, status, version, submitted_by,
                 submitted_at, reviewed_by, reviewed_at, review_note,
                 idempotency_key, created_at, updated_at)
            VALUES
                (:id, :number, 1, 'draft', 1, 7, '2026-07-18 00:00:00',
                 NULL, NULL, NULL, :key, '2026-07-18 00:00:00',
                 '2026-07-18 00:00:00')
            """
        ),
        {"id": order_id, "number": f"ST-{order_id}", "key": f"order-{order_id}"},
    )


def _insert_item(
    connection: sa.Connection,
    *,
    item_id: int,
    order_id: int,
    lot_id: int = 10,
) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO stocktake_items VALUES
                (:id, :order_id, :lot_id, 2, 3, 1, 4, 4, 0, 'LOT-1',
                 NULL, 'Product', '100x200', 'CODE-1', 'A-01', 'boxes',
                 NULL, '2026-07-18 00:00:00')
            """
        ),
        {"id": item_id, "order_id": order_id, "lot_id": lot_id},
    )


def _submit_order(connection: sa.Connection, order_id: int) -> None:
    connection.execute(
        sa.text(
            "UPDATE stocktake_orders SET status = 'submitted', version = version + 1 "
            "WHERE id = :id"
        ),
        {"id": order_id},
    )


def _review_order(connection: sa.Connection, order_id: int, status: str) -> None:
    connection.execute(
        sa.text(
            "UPDATE stocktake_orders SET status = :status, version = version + 1, "
            "reviewed_by = 8, reviewed_at = '2026-07-18 01:00:00', "
            "review_note = 'checked' WHERE id = :id"
        ),
        {"id": order_id, "status": status},
    )


def _guarded_sqlite_connection():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    connection = engine.connect()
    transaction = connection.begin()
    _create_sqlite_guard_schema(connection)
    migration._create_sqlite_guards(connection)
    return engine, connection, transaction


def test_sqlite_only_draft_orders_accept_item_inserts() -> None:
    engine, connection, transaction = _guarded_sqlite_connection()
    try:
        with pytest.raises(sa.exc.IntegrityError, match="created as draft"):
            connection.execute(
                sa.text(
                    "INSERT INTO stocktake_orders VALUES "
                    "(99, 'ST-99', 1, 'submitted', 1, 7, 'now', NULL, NULL, "
                    "NULL, 'order-99', 'now', 'now')"
                )
            )

        for order_id in range(1, 5):
            _insert_draft_order(connection, order_id)
        _insert_item(connection, item_id=1, order_id=1)
        _submit_order(connection, 2)
        _submit_order(connection, 3)
        _review_order(connection, 3, "approved")
        _submit_order(connection, 4)
        _review_order(connection, 4, "rejected")

        for item_id, order_id in ((2, 2), (3, 3), (4, 4)):
            with pytest.raises(sa.exc.IntegrityError, match="only be inserted"):
                _insert_item(connection, item_id=item_id, order_id=order_id)

        for order_id in (2, 3, 4):
            with pytest.raises(sa.exc.IntegrityError, match="immutable update"):
                connection.execute(
                    sa.text(
                        "UPDATE stocktake_orders SET order_number = 'changed' WHERE id = :id"
                    ),
                    {"id": order_id},
                )
            with pytest.raises(sa.exc.IntegrityError, match="order is immutable"):
                connection.execute(
                    sa.text("DELETE FROM stocktake_orders WHERE id = :id"),
                    {"id": order_id},
                )

        _insert_draft_order(connection, 5)
        with pytest.raises(sa.exc.IntegrityError, match="invalid stocktake order transition"):
            _review_order(connection, 5, "approved")
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_sqlite_rejected_order_and_snapshot_are_immutable() -> None:
    engine, connection, transaction = _guarded_sqlite_connection()
    try:
        _insert_draft_order(connection, 1)
        _insert_item(connection, item_id=1, order_id=1)
        _submit_order(connection, 1)
        _review_order(connection, 1, "rejected")

        for statement in (
            "UPDATE stocktake_orders SET review_note = 'changed' WHERE id = 1",
            "DELETE FROM stocktake_orders WHERE id = 1",
            "UPDATE stocktake_items SET counted_quantity = 5 WHERE id = 1",
            "UPDATE stocktake_items SET specification_snapshot = 'changed' WHERE id = 1",
            "DELETE FROM stocktake_items WHERE id = 1",
        ):
            with pytest.raises(sa.exc.IntegrityError, match="immutable"):
                connection.execute(sa.text(statement))
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_sqlite_approved_binding_requires_same_lot_adjust_movement_once() -> None:
    engine, connection, transaction = _guarded_sqlite_connection()
    try:
        _insert_draft_order(connection, 1)
        _insert_item(connection, item_id=1, order_id=1, lot_id=10)
        connection.execute(
            sa.text(
                "INSERT INTO inventory_movements VALUES "
                "(11, 20, 'adjust'), (12, 10, 'consume'), (13, 10, 'adjust')"
            )
        )
        _submit_order(connection, 1)

        with pytest.raises(sa.exc.IntegrityError, match="immutable"):
            connection.execute(
                sa.text("UPDATE stocktake_items SET adjustment_movement_id = 13 WHERE id = 1")
            )

        _review_order(connection, 1, "approved")
        for movement_id in (11, 12):
            with pytest.raises(sa.exc.IntegrityError, match="movement is invalid"):
                connection.execute(
                    sa.text(
                        "UPDATE stocktake_items SET adjustment_movement_id = :movement_id "
                        "WHERE id = 1"
                    ),
                    {"movement_id": movement_id},
                )

        connection.execute(
            sa.text("UPDATE stocktake_items SET adjustment_movement_id = 13 WHERE id = 1")
        )
        assert connection.execute(
            sa.text("SELECT adjustment_movement_id FROM stocktake_items WHERE id = 1")
        ).scalar_one() == 13

        for statement in (
            "UPDATE stocktake_items SET adjustment_movement_id = 11 WHERE id = 1",
            "UPDATE stocktake_items SET adjustment_movement_id = NULL WHERE id = 1",
            "UPDATE stocktake_items SET counted_quantity = 5 WHERE id = 1",
        ):
            with pytest.raises(sa.exc.IntegrityError, match="immutable"):
                connection.execute(sa.text(statement))
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_sqlite_reviews_are_immutable() -> None:
    engine, connection, transaction = _guarded_sqlite_connection()
    try:
        _insert_draft_order(connection, 1)
        connection.execute(
            sa.text(
                "INSERT INTO stocktake_reviews VALUES "
                "(1, 1, 1, 'reject', 'submitted', 'rejected', 'reason', "
                "'review-1', NULL, 8, '2026-07-18 01:00:00')"
            )
        )
        for statement in (
            "UPDATE stocktake_reviews SET reason = 'changed' WHERE id = 1",
            "DELETE FROM stocktake_reviews WHERE id = 1",
        ):
            with pytest.raises(sa.exc.IntegrityError, match="history is immutable"):
                connection.execute(sa.text(statement))
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_postgresql_guards_match_sqlite_audit_contract() -> None:
    content = MIGRATION_PATH.read_text(encoding="utf-8")
    immutable_fields = (
        "id",
        "order_id",
        "inventory_lot_id",
        "lot_version_snapshot",
        "available_quantity_snapshot",
        "reserved_quantity_snapshot",
        "on_hand_quantity_snapshot",
        "counted_quantity",
        "difference_quantity",
        "lot_number_snapshot",
        "customer_name_snapshot",
        "product_name_snapshot",
        "specification_snapshot",
        "inventory_code_snapshot",
        "location_code_snapshot",
        "unit_snapshot",
        "created_at",
    )
    sqlite_guard, postgresql_guard = content.split("def _create_postgresql_guards", 1)
    item_guard = postgresql_guard.split(
        "CREATE FUNCTION stocktake_items_guard()", 1
    )[1].split("CREATE FUNCTION stocktake_items_delete_guard()", 1)[0]
    for field in immutable_fields:
        assert f"NEW.{field} IS OLD.{field}" in sqlite_guard
        assert f"NEW.{field} IS NOT DISTINCT FROM OLD.{field}" in item_guard
    assert "inventory_lot_id = OLD.inventory_lot_id" in item_guard
    assert "movement_type = 'adjust'" in item_guard
    assert "RETURN NEW;" in item_guard
    assert "RETURN OLD;" not in item_guard
    assert "status = 'draft'" in postgresql_guard
    assert "OLD.status = 'submitted'" in postgresql_guard
    assert "NEW.status IN ('approved', 'rejected')" in postgresql_guard


def test_n035_upgrade_and_empty_downgrade_on_isolated_sqlite_copy() -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        for table_name in (
            "warehouse_locations",
            "users",
            "inventory_lots",
        ):
            connection.execute(
                sa.text(f"CREATE TABLE {table_name} (id INTEGER PRIMARY KEY)")
            )
        connection.execute(
            sa.text(
                "CREATE TABLE inventory_movements ("
                "id INTEGER PRIMARY KEY, inventory_lot_id INTEGER NOT NULL, "
                "movement_type TEXT NOT NULL)"
            )
        )
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        columns = {
            column["name"]: column
            for column in sa.inspect(connection).get_columns("stocktake_items")
        }
        assert columns["specification_snapshot"]["nullable"] is True
        triggers = {
            row[0]
            for row in connection.execute(
                sa.text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            )
        }
        assert {
            "trg_stocktake_orders_insert_guard",
            "trg_stocktake_items_insert_guard",
            "trg_stocktake_items_update_guard",
            "trg_stocktake_reviews_immutable_update",
        }.issubset(triggers)

        migration.downgrade()
        remaining = set(sa.inspect(connection).get_table_names())
        assert not {"stocktake_orders", "stocktake_items", "stocktake_reviews"} & remaining
        assert {"warehouse_locations", "users", "inventory_lots", "inventory_movements"} <= remaining
