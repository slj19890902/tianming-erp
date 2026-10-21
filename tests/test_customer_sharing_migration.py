import importlib.util
from pathlib import Path
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.test_shared_finished_stock import reservation_db


def test_additive_sharing_schema_roundtrip_and_no_lossy_downgrade(reservation_db):
    db,data=reservation_db;root=Path(__file__).resolve().parents[1]
    spec=importlib.util.spec_from_file_location('sharing_migration',root/'alembic/versions/ex1021cp_customer_sharing.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    assert ScriptDirectory.from_config(Config(str(root/'alembic.ini'))).get_heads()==['ex1021cp']
    conn=db.connection();module.op=Operations(MigrationContext.configure(conn))
    module.downgrade()
    def facts():
        names=[r[0] for r in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'customer_sharing_%' ORDER BY name")]
        return {name:conn.exec_driver_sql('SELECT * FROM "'+name+'"').all() for name in names}
    before=facts()
    schema=conn.exec_driver_sql("SELECT type,name,sql FROM sqlite_master WHERE type IN ('index','trigger') ORDER BY type,name").all()
    module.upgrade();module.downgrade()
    assert facts()==before
    assert conn.exec_driver_sql("SELECT type,name,sql FROM sqlite_master WHERE type IN ('index','trigger') ORDER BY type,name").all()==schema
    module.upgrade();assert facts()==before
    conn.exec_driver_sql("INSERT INTO customer_sharing_groups(id,name,version,new_products_shared) VALUES(1,'fixture',1,0)")
    with pytest.raises(RuntimeError,match='禁止有损降级'):module.downgrade()
    assert conn.exec_driver_sql('SELECT name FROM customer_sharing_groups').scalar_one()=='fixture'
    assert conn.exec_driver_sql('PRAGMA integrity_check').scalar_one()=='ok'
    assert conn.exec_driver_sql('PRAGMA foreign_key_check').all()==[]
    assert facts()==before
