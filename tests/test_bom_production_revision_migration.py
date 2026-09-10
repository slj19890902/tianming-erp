import hashlib
import os
from pathlib import Path
import shutil
import sqlite3

import pytest
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from tests.test_external_graph_cost_migration import migration
from tests.test_p1_131_material_cost_lineage_migration import _config


def test_revision_constraints_and_nonempty_downgrade(tmp_path):
    module = migration("sb14v8x9z76_bom_production_revisions.py")
    engine = create_engine(f"sqlite:///{tmp_path / 'revision-constraints.sqlite3'}")
    with engine.begin() as db:
        db.exec_driver_sql("PRAGMA foreign_keys=ON")
        db.exec_driver_sql("CREATE TABLE order_bom_graphs(order_item_id INTEGER PRIMARY KEY)")
        db.exec_driver_sql("INSERT INTO order_bom_graphs VALUES (1),(2)")
        db.exec_driver_sql("CREATE TABLE users(id INTEGER PRIMARY KEY)")
        db.exec_driver_sql("INSERT INTO users VALUES(1)")
        with Operations.context(MigrationContext.configure(db)):
            module.upgrade()
            sql = "INSERT INTO order_bom_production_revisions(id,order_item_id,revision,previous_id,document_json,content_hash,created_by) VALUES(?,?,?,?,?,?,?)"
            for values in ((1,999,1,None,"{}","a"*64,1), (1,1,0,None,"{}","a"*64,1),
                           (1,1,1,None,"{}","bad",1), (1,1,2,None,"{}","a"*64,1),
                           (1,1,1,None,"{}","a"*64,999)):
                with pytest.raises(IntegrityError):
                    db.exec_driver_sql(sql, values)
            db.exec_driver_sql(sql, (1,1,1,None,"{}","a"*64,1))
            for values in ((2,1,1,None,"{}","a"*64,1), (2,2,2,1,"{}","a"*64,1)):
                with pytest.raises(IntegrityError):
                    db.exec_driver_sql(sql, values)
            db.exec_driver_sql(sql, (2,1,2,1,"{}","b"*64,1))
            with pytest.raises(RuntimeError, match="已有BOM生产资料修订"):
                module.downgrade()
            assert db.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    engine.dispose()


def test_isolated_source_revision_upgrade_downgrade_upgrade(monkeypatch, tmp_path):
    source = Path(os.environ.get("ERP_SUBKIT_UAT_SOURCE", "")).resolve()
    if source.name != "source-isolated.sqlite3" or "tm-uat" not in source.parts:
        pytest.skip("requires explicit isolated source")
    before = hashlib.sha256(source.read_bytes()).digest()
    target, backup = tmp_path / "revision.sqlite3", tmp_path / "before.sqlite3"
    shutil.copy2(source, target)
    shutil.copy2(target, backup)
    assert hashlib.sha256(backup.read_bytes()).digest() == before
    tables = ("sales_orders", "sales_order_items", "inventory_lots", "warehouse_locations")
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        counts = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables}
    config = _config(monkeypatch, target)
    assert ScriptDirectory.from_config(config).get_heads() == ["se17v8x9z79"]
    command.upgrade(config, "sb14v8x9z76")
    command.downgrade(config, "sa13v8x9z75")
    command.upgrade(config, "sb14v8x9z76")
    with sqlite3.connect(target) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("sb14v8x9z76",)
        for table, count in counts.items():
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == count
    assert hashlib.sha256(source.read_bytes()).digest() == before
