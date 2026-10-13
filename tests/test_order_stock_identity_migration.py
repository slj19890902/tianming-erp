from pathlib import Path
import importlib.util
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.config import Config
from alembic.script import ScriptDirectory


def test_identity_upgrade_downgrade_upgrade_keeps_old_facts_and_refuses_loss(tmp_path):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('order_identity_migration', root/'alembic/versions/eq1013oi_order_stock_identity.py')
    migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
    assert ScriptDirectory.from_config(Config(str(root/'alembic.ini'))).get_heads() == ['eq1013oi']
    engine = sa.create_engine('sqlite:///' + str(tmp_path/'identity.sqlite3'))
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE sales_order_items(id INTEGER PRIMARY KEY, quantity INTEGER, snapshot_spec TEXT)')
        conn.exec_driver_sql("INSERT INTO sales_order_items VALUES(1,100,'旧规格')")
        conn.exec_driver_sql('CREATE INDEX fixture_identity_idx ON sales_order_items(quantity)')
        conn.exec_driver_sql("CREATE TRIGGER fixture_identity_guard BEFORE DELETE ON sales_order_items BEGIN SELECT RAISE(ABORT,'history'); END")
        migration.op = Operations(MigrationContext.configure(conn))
        before = conn.exec_driver_sql('SELECT * FROM sales_order_items').all()
        migration.upgrade()
        assert conn.exec_driver_sql('SELECT stock_identity_snapshot_json FROM sales_order_items').scalar_one() is None
        migration.downgrade(); migration.upgrade()
        assert conn.exec_driver_sql('SELECT id,quantity,snapshot_spec FROM sales_order_items').all() == before
        assert conn.exec_driver_sql("SELECT count(*) FROM sqlite_master WHERE name IN ('fixture_identity_idx','fixture_identity_guard')").scalar_one() == 2
        conn.exec_driver_sql("UPDATE sales_order_items SET stock_identity_snapshot_json='{}'")
        with pytest.raises(RuntimeError, match='禁止有损降级'): migration.downgrade()
        assert conn.exec_driver_sql('SELECT stock_identity_snapshot_json FROM sales_order_items').scalar_one() == '{}'
        assert conn.exec_driver_sql('PRAGMA integrity_check').scalar_one() == 'ok'
        assert conn.exec_driver_sql('PRAGMA foreign_key_check').all() == []
    engine.dispose()
