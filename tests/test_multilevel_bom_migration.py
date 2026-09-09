import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


@pytest.fixture
def migration_db(tmp_path):
    path = Path(__file__).resolve().parents[1] / "alembic/versions/ru09v8x9z69_order_bom_graph.py"
    spec = importlib.util.spec_from_file_location("bom_graph_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(f"sqlite:///{tmp_path / 'migration-only.sqlite3'}")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        for table in ("sales_order_items", "products", "customers", "users"):
            connection.exec_driver_sql(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql(f"INSERT INTO {table} (id) VALUES (1)")
        with Operations.context(MigrationContext.configure(connection)):
            yield connection, migration
    engine.dispose()


def test_upgrade_downgrade_upgrade_preserves_preexisting_rows(migration_db):
    connection, migration = migration_db
    migration.upgrade()
    assert "order_bom_graphs" in inspect(connection).get_table_names()
    migration.downgrade()
    assert "order_bom_graphs" not in inspect(connection).get_table_names()
    migration.upgrade()
    for table in ("sales_order_items", "products", "customers", "users"):
        assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 1
    assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []


def test_downgrade_with_frozen_order_is_refused_before_deleting_tables(migration_db):
    connection, migration = migration_db
    migration.upgrade()
    connection.execute(text("""INSERT INTO order_bom_graphs
        (order_item_id, root_product_id, customer_id, schema_version, document_json, content_hash, created_by)
        VALUES (1,1,1,1,'{}',:digest,1)"""), {"digest": "a" * 64})
    with pytest.raises(RuntimeError, match="已有冻结记录"):
        migration.downgrade()
    assert {"order_bom_graphs", "order_bom_graph_products"} <= set(inspect(connection).get_table_names())
    assert connection.scalar(text("SELECT count(*) FROM order_bom_graphs")) == 1


def test_real_bom_profiles_upgrade_round_trip_and_nonempty_downgrade_guard(tmp_path):
    path = Path(__file__).resolve().parents[1] / "alembic/versions/rv09v8x9z70_product_bom_relations.py"
    spec = importlib.util.spec_from_file_location("bom_profile_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(f"sqlite:///{tmp_path / 'profile-migration.sqlite3'}")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        for table in ("products", "product_bom_components"):
            connection.exec_driver_sql(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql(f"INSERT INTO {table} (id) VALUES (1)")
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.downgrade()
            migration.upgrade()
            connection.exec_driver_sql("INSERT INTO product_bom_profiles VALUES (1, 'assembled')")
            connection.exec_driver_sql("INSERT INTO product_bom_inventory_relations VALUES (1, 'assembly')")
            with pytest.raises(RuntimeError, match="已有多级BOM设置"):
                migration.downgrade()
            assert connection.scalar(text("SELECT count(*) FROM product_bom_profiles")) == 1
            assert connection.scalar(text("SELECT count(*) FROM product_bom_components")) == 1
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    engine.dispose()
