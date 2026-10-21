import importlib.util
from pathlib import Path
from unittest.mock import patch
import pytest
from sqlalchemy import create_engine
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_additive_edges_keep_existing_triggers_and_refuse_loss(tmp_path):
    path = Path(__file__).parents[1]/'alembic/versions/ey1021se_supplier_paper_edges.py'
    spec = importlib.util.spec_from_file_location('supplier_edges_migration',path)
    m = importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    engine = create_engine('sqlite:///'+str(tmp_path/'edges.sqlite3'))
    with engine.begin() as db:
        db.exec_driver_sql('CREATE TABLE inventory_reservations(id INTEGER PRIMARY KEY, cut_plan_json TEXT)')
        for table in m.COLUMNS:
            db.exec_driver_sql(f'CREATE TABLE {table}(id INTEGER PRIMARY KEY, original TEXT)')
            db.exec_driver_sql(f"INSERT INTO {table} VALUES(1,'retained')")
        db.exec_driver_sql('CREATE TRIGGER keep_products BEFORE DELETE ON products BEGIN SELECT RAISE(ABORT,"keep"); END')
        op = Operations(MigrationContext.configure(db))
        with patch.object(m,'op',op):
            m.upgrade();m.downgrade();m.upgrade()
            for table in m.COLUMNS:
                assert db.exec_driver_sql(f'SELECT id,original FROM {table}').all()==[(1,'retained')]
            assert db.exec_driver_sql("SELECT count(*) FROM sqlite_master WHERE name='keep_products'").scalar_one()==1
            db.exec_driver_sql("INSERT INTO stock_replenishment_order_items(id,original,supplier_edge_type) VALUES(2,'new','净料')")
            with pytest.raises(Exception,match='snapshot is immutable'):
                db.exec_driver_sql("UPDATE stock_replenishment_order_items SET supplier_edge_type='毛片' WHERE id=2")
            with pytest.raises(RuntimeError,match='禁止有损降级'):
                m.downgrade()
            assert db.exec_driver_sql('SELECT supplier_edge_type FROM stock_replenishment_order_items WHERE id=2').scalar_one()=='净料'
