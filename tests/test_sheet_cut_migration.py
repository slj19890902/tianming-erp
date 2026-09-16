import importlib.util
from pathlib import Path
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_cut_contract_cannot_be_rewritten_or_downgraded_with_facts():
    path = Path(__file__).resolve().parents[1] / 'alembic/versions/sc0916_sheet_cut_contract.py'
    spec = importlib.util.spec_from_file_location('cut_migration', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    engine = sa.create_engine('sqlite://')
    with engine.begin() as db:
        db.exec_driver_sql('CREATE TABLE inventory_reservations(id INTEGER PRIMARY KEY,yield_factor INTEGER)')
        db.exec_driver_sql('INSERT INTO inventory_reservations VALUES(1,1)')
        module.op = Operations(MigrationContext.configure(db))
        module.upgrade()
        assert db.exec_driver_sql('SELECT * FROM inventory_reservations').all() == [(1,1,None)]
        module.downgrade(); module.upgrade()
        db.exec_driver_sql("UPDATE inventory_reservations SET cut_plan_json='{}',yield_factor=2 WHERE id=1")
        for sql in ["UPDATE inventory_reservations SET cut_plan_json=NULL", "UPDATE inventory_reservations SET yield_factor=1"]:
            with pytest.raises(sa.exc.IntegrityError, match='immutable'):
                db.exec_driver_sql(sql)
        with pytest.raises(RuntimeError, match='已有裁切'):
            module.downgrade()
