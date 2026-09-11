import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


@pytest.fixture
def migration_db(tmp_path):
    path = Path(__file__).resolve().parents[1] / "alembic/versions/rw09v8x9z71_bom_assembly_lineage.py"
    spec = importlib.util.spec_from_file_location("assembly_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine(f"sqlite:///{tmp_path / 'assembly-migration.sqlite3'}")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        for table in ("products", "inventory_lots", "users", "inventory_movements"):
            connection.exec_driver_sql(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql(f"INSERT INTO {table} VALUES (1)")
        connection.exec_driver_sql("CREATE TABLE order_bom_graphs (order_item_id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO order_bom_graphs VALUES (1)")
        connection.exec_driver_sql("CREATE TABLE order_bom_graph_products (order_item_id INTEGER, product_id INTEGER, PRIMARY KEY(order_item_id, product_id))")
        connection.exec_driver_sql("INSERT INTO order_bom_graph_products VALUES (1,1)")
        with Operations.context(MigrationContext.configure(connection)):
            yield connection, module
    engine.dispose()


def test_empty_roundtrip_and_legacy_references_unchanged(migration_db):
    db, migration = migration_db
    migration.upgrade()
    assert "bom_assembly_inputs" in inspect(db).get_table_names()
    assert db.execute(text("PRAGMA foreign_key_check")).all() == []
    migration.downgrade()
    migration.upgrade()
    assert db.execute(text("SELECT COUNT(*) FROM inventory_lots")).scalar() == 1


def test_foreign_keys_quantity_and_nonempty_downgrade_guards(migration_db):
    db, migration = migration_db
    migration.upgrade()
    insert = text("INSERT INTO bom_assemblies (order_item_id,output_product_id,idempotency_key,request_hash,quantity,total_cost,cost_detail_json,status) VALUES (1,:pid,'test',:hash,:qty,0,'{}','posted')")
    with pytest.raises(IntegrityError):
        db.execute(insert, {"pid": 999, "qty": 1, "hash": "a" * 64})
    db.execute(text("INSERT INTO products VALUES (2)"))
    with pytest.raises(IntegrityError):
        db.execute(insert, {"pid": 2, "qty": 1, "hash": "a" * 64})
    with pytest.raises(IntegrityError):
        db.execute(insert, {"pid": 1, "qty": -1, "hash": "a" * 64})
    db.execute(insert, {"pid": 1, "qty": 1, "hash": "a" * 64})
    with pytest.raises(RuntimeError, match="已有多级组装"):
        migration.downgrade()
    assert db.execute(text("SELECT COUNT(*) FROM bom_assemblies")).scalar() == 1
