import hashlib
import os
from pathlib import Path
import shutil
import sqlite3

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from tests.test_p1_131_material_cost_lineage_migration import _config


def test_graph_cost_foreign_keys_and_nonempty_downgrade(tmp_path):
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import IntegrityError
    path = Path(__file__).resolve().parents[1] / "alembic/versions/rx11v8x9z72_graph_delivery_cost.py"
    spec = importlib.util.spec_from_file_location("graph_cost_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(f"sqlite:///{tmp_path / 'constraint-test.sqlite3'}")
    with engine.begin() as db:
        db.exec_driver_sql("PRAGMA foreign_keys=ON")
        for table in ("sales_delivery_items", "delivery_inventory_allocations", "unordered_finished_delivery_allocations",
                      "inventory_lots", "inventory_movements", "users", "purchase_receipt_facts", "incoming_receipt_purpose_allocations"):
            db.exec_driver_sql(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
            db.exec_driver_sql(f"INSERT INTO {table} VALUES (1)")
        with Operations.context(MigrationContext.configure(db)):
            migration.upgrade()
            statement = text("INSERT INTO finance_delivery_graph_cost_facts (id,delivery_item_id,delivery_inventory_allocation_id,inventory_lot_id,consume_movement_id,snapshot_version,source_quantity,source_offset,consumed_quantity,total_cost,currency,source_fingerprint) VALUES (1,1,1,:lot,1,1,10,0,:qty,1,'CNY',:fingerprint)")
            for lot, qty in ((999, 1), (1, 11), (1, 0)):
                with pytest.raises(IntegrityError):
                    db.execute(statement, dict(lot=lot, qty=qty, fingerprint="a"*64))
            db.execute(statement, dict(lot=1, qty=4, fingerprint="a"*64))
            with pytest.raises(RuntimeError, match="已有多来源发货成本事实"):
                migration.downgrade()
            assert db.scalar(text("SELECT count(*) FROM finance_delivery_graph_cost_facts")) == 1
            assert db.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    engine.dispose()


def test_graph_cost_real_isolated_copy_roundtrip(monkeypatch, tmp_path):
    source = Path(os.environ.get("ERP_SUBKIT_UAT_SOURCE", "" )).resolve()
    if source.name != "source-isolated.sqlite3" or "tm-uat" not in source.parts:
        pytest.skip("requires explicitly paired isolated source")
    database = tmp_path / "graph-cost-roundtrip.sqlite3"
    shutil.copy2(source, database)
    backup = tmp_path / "before-migration.sqlite3"
    shutil.copy2(database, backup)
    assert hashlib.sha256(database.read_bytes()).digest() == hashlib.sha256(backup.read_bytes()).digest()
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        counts = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("sales_orders", "sales_order_items", "inventory_lots", "warehouse_locations")}
    config = _config(monkeypatch, database)
    assert ScriptDirectory.from_config(config).get_heads() == ["se17v8x9z79"]
    command.upgrade(config, "rx11v8x9z72")
    command.downgrade(config, "rw09v8x9z71")
    command.upgrade(config, "rx11v8x9z72")
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        for table, count in counts.items():
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == count
        assert db.execute("SELECT count(*) FROM finance_delivery_graph_cost_facts").fetchone() == (0,)
    assert hashlib.sha256(source.read_bytes()).digest() == hashlib.sha256(backup.read_bytes()).digest()
