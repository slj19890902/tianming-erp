import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _migration():
    path = Path(__file__).resolve().parents[1] / "alembic/versions/eg1008sc_sheet_cutting_contract.py"
    spec = importlib.util.spec_from_file_location("cutting_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_additive_roundtrip_preserves_legacy_facts_indexes_and_triggers(tmp_path):
    module = _migration()
    engine = sa.create_engine("sqlite:///" + (tmp_path / "isolated.sqlite3").as_posix())
    with engine.begin() as db:
        module.op = Operations(MigrationContext.configure(db))
        for table in module.COLUMNS:
            db.execute(sa.text(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, legacy TEXT)'))
            db.execute(sa.text(f'INSERT INTO "{table}" VALUES (1, \'unchanged\')'))
        db.execute(sa.text('CREATE INDEX keep_index ON products(legacy)'))
        db.execute(sa.text("CREATE TRIGGER keep_trigger BEFORE DELETE ON products BEGIN SELECT RAISE(ABORT, 'preserved'); END"))
        module.upgrade()
        for table, column in module.COLUMNS.items():
            assert db.execute(sa.text(f'SELECT legacy, "{column}" FROM "{table}"')).one() == ("unchanged", None)
        module.downgrade()
        module.upgrade()
        assert db.execute(sa.text("SELECT name FROM sqlite_master WHERE name IN ('keep_index','keep_trigger')")).scalars().all() == ['keep_index', 'keep_trigger']
        assert db.execute(sa.text('PRAGMA integrity_check')).scalar() == 'ok'
        assert db.execute(sa.text('PRAGMA foreign_key_check')).all() == []
    engine.dispose()


def test_facts_block_downgrade_before_any_schema_change(tmp_path):
    module = _migration()
    engine = sa.create_engine("sqlite:///" + (tmp_path / "isolated.sqlite3").as_posix())
    with engine.begin() as db:
        module.op = Operations(MigrationContext.configure(db))
        for table in module.COLUMNS:
            db.execute(sa.text(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY)'))
        module.upgrade()
        db.execute(sa.text("INSERT INTO supplier_requisition_order_items (id,sheet_cutting_snapshot) VALUES (1, '{}')"))
        with pytest.raises(RuntimeError, match="禁止有损降级"):
            module.downgrade()
        for table, column in module.COLUMNS.items():
            assert column in {c['name'] for c in sa.inspect(db).get_columns(table)}
        db.execute(sa.text("INSERT INTO sales_order_item_bom_components (id,sheet_cutting_settings_snapshot) VALUES (1, '{}')"))
        with pytest.raises(sa.exc.IntegrityError, match="immutable"):
            db.execute(sa.text("UPDATE sales_order_item_bom_components SET sheet_cutting_settings_snapshot=NULL WHERE id=1"))
    engine.dispose()
