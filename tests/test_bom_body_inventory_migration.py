import hashlib
import os
from pathlib import Path
import shutil
import sqlite3

import pytest
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from tests.test_external_graph_cost_migration import migration
from tests.test_p1_131_material_cost_lineage_migration import _config


def test_body_identity_requires_body_lot_and_frozen_product(tmp_path):
    module = migration("sc15v8x9z77_bom_body_inventory.py")
    engine = create_engine(f"sqlite:///{tmp_path / 'body.sqlite3'}")
    with engine.begin() as db:
        db.exec_driver_sql("PRAGMA foreign_keys=ON")
        db.exec_driver_sql("CREATE TABLE inventory_lots(id INTEGER PRIMARY KEY, inventory_type TEXT NOT NULL CONSTRAINT ck_inventory_lots_type CHECK(inventory_type IN ('finished','semi_finished')))")
        db.exec_driver_sql("CREATE TABLE order_bom_graph_products(order_item_id INTEGER,product_id INTEGER,PRIMARY KEY(order_item_id,product_id))")
        db.exec_driver_sql("CREATE TABLE production_completions(id INTEGER PRIMARY KEY)")
        db.exec_driver_sql("INSERT INTO order_bom_graph_products VALUES(1,2)")
        db.exec_driver_sql("INSERT INTO production_completions VALUES(1)")
        with Operations.context(MigrationContext.configure(db)):
            module.upgrade()
            db.exec_driver_sql("INSERT INTO inventory_lots VALUES(1,'finished'),(2,'assembly_body')")
            sql = "INSERT INTO bom_body_inventory_details(inventory_lot_id,inventory_type,order_item_id,product_id,production_completion_id) VALUES(?,?,?,?,?)"
            for row in ((1,'assembly_body',1,2,1),(1,'finished',1,2,1),
                        (2,'assembly_body',2,2,1),(2,'assembly_body',1,2,999)):
                with pytest.raises(IntegrityError):
                    db.exec_driver_sql(sql, row)
            db.exec_driver_sql(sql,(2,'assembly_body',1,2,1))
            with pytest.raises(IntegrityError):
                db.exec_driver_sql("UPDATE inventory_lots SET inventory_type='finished' WHERE id=2")
            with pytest.raises(RuntimeError, match="本体库存"):
                module.downgrade()
            assert db.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    engine.dispose()


def test_isolated_body_upgrade_downgrade_preserves_inventory_and_triggers(monkeypatch, tmp_path):
    source = Path(os.environ.get("ERP_SUBKIT_UAT_SOURCE", "")).resolve()
    if source.name != "source-isolated.sqlite3" or "tm-uat" not in source.parts:
        pytest.skip("requires explicit isolated source")
    digest = hashlib.sha256(source.read_bytes()).digest()
    target, backup = tmp_path / "body.sqlite3", tmp_path / "backup.sqlite3"
    shutil.copy2(source, target)
    shutil.copy2(target, backup)
    assert hashlib.sha256(backup.read_bytes()).digest() == digest
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    config = _config(monkeypatch, target)
    command.upgrade(config, "sb14v8x9z76")
    def facts():
        with sqlite3.connect(target) as db:
            assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
            tables = ("inventory_lots", "inventory_movements", "warehouse_locations", "sales_orders", "sales_order_items")
            return ({t:db.execute(f"SELECT * FROM {t} ORDER BY id").fetchall() for t in tables},
                    db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").fetchall())
    before = facts()
    command.upgrade(config, "sc15v8x9z77")
    assert facts() == before
    command.downgrade(config, "sb14v8x9z76")
    assert facts() == before
    command.upgrade(config, "sc15v8x9z77")
    assert facts() == before
    with sqlite3.connect(target) as db:
        for assignment in ("quantity_available=-1", "status='invalid'", "source_type='invalid'"):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(f"UPDATE inventory_lots SET {assignment} WHERE id=(SELECT min(id) FROM inventory_lots)")
    assert hashlib.sha256(source.read_bytes()).digest() == digest
