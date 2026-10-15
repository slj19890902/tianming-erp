"""The SQLite table rebuild must retain the command audit guards."""
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _migration():
    path = Path(__file__).resolve().parents[1] / "alembic/versions/eq1015sa_inventory_first_assembly.py"
    spec = importlib.util.spec_from_file_location("eq1015sa_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _triggers(connection):
    return dict(connection.exec_driver_sql(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
        "AND tbl_name = 'stock_preparation_commands'"
    ).all())


def _nullable(connection):
    return next(row[3] == 0 for row in connection.exec_driver_sql(
        "PRAGMA table_info(stock_preparation_commands)"
    ) if row[1] == "receipt_item_id")


def _setup(connection):
    connection.exec_driver_sql(
        "CREATE TABLE stock_preparation_commands (operation_key VARCHAR(80) PRIMARY KEY, "
        "receipt_item_id INTEGER NOT NULL, request_json TEXT NOT NULL)"
    )
    connection.exec_driver_sql("CREATE INDEX ix_stock_preparation_commands_receipt_item_id "
                               "ON stock_preparation_commands (receipt_item_id)")
    for action in ("UPDATE", "DELETE"):
        connection.exec_driver_sql(
            f"CREATE TRIGGER stock_preparation_commands_no_{action.lower()} "
            f"BEFORE {action} ON stock_preparation_commands "
            "BEGIN SELECT RAISE(ABORT, '备库安排流水不可修改或删除'); END"
        )
    connection.exec_driver_sql("INSERT INTO stock_preparation_commands VALUES ('original', 1, '{}')")


def test_roundtrip_preserves_immutable_command_guards(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'commands.sqlite3'}")
    with engine.begin() as connection:
        _setup(connection)
        migration = _migration()
        migration.op = Operations(MigrationContext.configure(connection))
        original = _triggers(connection)

        migration.upgrade()
        assert _nullable(connection)
        assert _triggers(connection) == original
        with pytest.raises(sa.exc.IntegrityError):
            connection.exec_driver_sql("UPDATE stock_preparation_commands SET request_json = '{}' ")
        with pytest.raises(sa.exc.IntegrityError):
            connection.exec_driver_sql("DELETE FROM stock_preparation_commands")

        migration.downgrade()
        assert not _nullable(connection)
        assert _triggers(connection) == original
        assert connection.exec_driver_sql(
            "SELECT operation_key, receipt_item_id FROM stock_preparation_commands"
        ).all() == [("original", 1)]


def test_upgrade_refuses_missing_immutable_guard(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'commands.sqlite3'}")
    with engine.begin() as connection:
        _setup(connection)
        connection.exec_driver_sql("DROP TRIGGER stock_preparation_commands_no_delete")
        migration = _migration()
        migration.op = Operations(MigrationContext.configure(connection))
        with pytest.raises(RuntimeError, match="protection triggers changed"):
            migration.upgrade()
        assert not _nullable(connection)


def test_inventory_first_command_blocks_lossy_downgrade(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'commands.sqlite3'}")
    with engine.begin() as connection:
        _setup(connection)
        migration = _migration()
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        guarded = _triggers(connection)
        connection.exec_driver_sql(
            "INSERT INTO stock_preparation_commands VALUES ('inventory-first', NULL, '{}')"
        )
        with pytest.raises(RuntimeError, match="not lossless"):
            migration.downgrade()
        assert _nullable(connection)
        assert _triggers(connection) == guarded
        assert connection.exec_driver_sql(
            "SELECT count(*) FROM stock_preparation_commands WHERE receipt_item_id IS NULL"
        ).scalar() == 1
