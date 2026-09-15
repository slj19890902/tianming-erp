import sqlite3
from alembic import command
from alembic.config import Config
import pytest


def test_contract_migration_roundtrip_and_nonempty_downgrade_guard(tmp_path,monkeypatch):
    from app.core.database import create_sqlite_engine
    from app.models import Base
    path=tmp_path/'contract.sqlite3'
    engine=create_sqlite_engine(path);Base.metadata.create_all(engine);engine.dispose()
    with sqlite3.connect(path) as db:
        db.execute('ALTER TABLE sales_order_items DROP COLUMN sales_unit_snapshot')
        db.execute('ALTER TABLE sales_delivery_items DROP COLUMN sales_contract_json')
    monkeypatch.setenv('ERP_DATABASE_PATH',str(path))
    monkeypatch.setenv('ERP_BACKUP_DIR',str(tmp_path/'backup'))
    config=Config('alembic.ini');command.stamp(config,'la0915')
    command.upgrade(config,'cc0915');command.downgrade(config,'la0915');command.upgrade(config,'cc0915')
    with sqlite3.connect(path) as db:
        assert db.execute('pragma integrity_check').fetchall()==[('ok',)]
        assert db.execute('pragma foreign_key_check').fetchall()==[]
        # Synthetic isolated row, no business data or foreign-key dependency.
        db.execute("INSERT INTO sales_delivery_items(delivery_id,order_item_id,delivered_quantity,ordered_quantity_snapshot,order_remaining_snapshot,over_delivery_quantity,sales_contract_json) VALUES(1,1,1,1,1,0,'{}')")
        db.commit()
        with pytest.raises(sqlite3.IntegrityError,match='immutable'):
            db.execute("UPDATE sales_delivery_items SET sales_contract_json=NULL")
        db.rollback()
    with pytest.raises(RuntimeError,match='已有销售快照'):
        command.downgrade(config,'la0915')
