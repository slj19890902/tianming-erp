from pathlib import Path
import importlib.util

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.config import Config
from alembic.script import ScriptDirectory


def test_quotation_migration_preserves_facts_and_blocks_lossy_downgrade(tmp_path):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('quote_migration', root/'alembic/versions/eh1009qr_quotation_reliability.py')
    migration = importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    assert ScriptDirectory.from_config(Config(str(root/'alembic.ini'))).get_heads() == ['eh1009qr']
    engine = sa.create_engine('sqlite:///'+(tmp_path/'migration.sqlite3').as_posix())
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE users(id INTEGER PRIMARY KEY)')
        conn.exec_driver_sql('CREATE TABLE customers(id INTEGER PRIMARY KEY)')
        conn.exec_driver_sql('CREATE TABLE quotation_orders(id INTEGER PRIMARY KEY, total_amount NUMERIC(14,2), remarks TEXT)')
        conn.exec_driver_sql("INSERT INTO quotation_orders VALUES(1,310.00,'preserve')")
        conn.exec_driver_sql('CREATE INDEX ix_fixture_quote ON quotation_orders(remarks)')
        conn.exec_driver_sql("CREATE TRIGGER fixture_quote_guard BEFORE DELETE ON quotation_orders BEGIN SELECT RAISE(ABORT,'keep history'); END")
        migration.op = Operations(MigrationContext.configure(conn))
        before = conn.exec_driver_sql('SELECT * FROM quotation_orders').all()
        migration.upgrade()
        assert conn.exec_driver_sql('SELECT id,total_amount,remarks FROM quotation_orders').all() == before
        assert conn.exec_driver_sql('SELECT version FROM quotation_orders').scalar_one() == 1
        migration.downgrade();migration.upgrade()
        assert conn.exec_driver_sql('SELECT id,total_amount,remarks FROM quotation_orders').all() == before
        assert conn.exec_driver_sql("SELECT count(*) FROM sqlite_master WHERE name IN ('ix_fixture_quote','fixture_quote_guard')").scalar_one() == 2
        conn.exec_driver_sql('UPDATE quotation_orders SET version=2')
        with pytest.raises(RuntimeError, match='禁止有损降级'):
            migration.downgrade()
        assert 'quotation_mutations' in sa.inspect(conn).get_table_names()
        assert conn.exec_driver_sql('PRAGMA integrity_check').scalar_one() == 'ok'
    engine.dispose()
