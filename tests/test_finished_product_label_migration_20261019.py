import importlib.util
import json
from pathlib import Path
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from app.models.finished_product_label import FinishedProductLabelPrintJob as Job
from test_finished_product_labels_20261019 import mobile_erp_app, setup

ROOT=Path(__file__).resolve().parents[1]


def test_empty_migration_roundtrip_preserves_old_facts_and_guards_printed_history(mobile_erp_app):
    _,_,factory,lot=setup(mobile_erp_app)
    with factory() as db:
        actor=db.scalar(text('select id from users limit 1'))
        engine=db.get_bind()
    Job.__table__.drop(engine)
    spec=importlib.util.spec_from_file_location('finished_label_migration',ROOT/'alembic/versions/et1019fl_finished_product_labels.py')
    migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    assert ScriptDirectory.from_config(Config(str(ROOT/'alembic.ini'))).get_heads()==['et1019fl']
    with engine.connect() as c:
        names=[r[0] for r in c.execute(text("select name from sqlite_master where type='table' order by name"))]
        before={n:c.execute(text(f'SELECT * FROM "{n}" ORDER BY rowid')).fetchall() for n in names if n!='sqlite_sequence'}
        schema=c.execute(text("select name,sql from sqlite_master where name not like 'sqlite_%' order by name")).fetchall()
        with Operations.context(MigrationContext.configure(c)):
            migration.upgrade();migration.downgrade();migration.upgrade()
            assert {n:c.execute(text(f'SELECT * FROM "{n}" ORDER BY rowid')).fetchall() for n in before}==before
            for name,sql in schema:
                assert c.execute(text('select sql from sqlite_master where name=:n'),{'n':name}).scalar_one()==sql
            c.execute(text("""INSERT INTO finished_product_label_print_jobs(inventory_lot_id,idempotency_key,request_hash,plan_fingerprint,payload_json,payload_hash,operator_id,status)
                VALUES(:lot,'migration-test',:hash,:hash,'{}',:hash,:actor,'prepared')"""),dict(lot=lot,actor=actor,hash='a'*64))
            with pytest.raises(RuntimeError,match='禁止有损降级'):migration.downgrade()
            with pytest.raises(IntegrityError):c.execute(text("UPDATE finished_product_label_print_jobs SET payload_json='changed'"))
            with pytest.raises(IntegrityError):c.execute(text('DELETE FROM finished_product_label_print_jobs'))
            c.execute(text("UPDATE finished_product_label_print_jobs SET status='printed',printed_confirmation_key='confirm',printed_by_user_id=:actor,printed_at=CURRENT_TIMESTAMP"),dict(actor=actor))
            with pytest.raises(IntegrityError):c.execute(text("UPDATE finished_product_label_print_jobs SET printed_confirmation_key='new'"))
            assert c.execute(text('PRAGMA integrity_check')).scalar_one()=='ok'
            assert not c.execute(text('PRAGMA foreign_key_check')).fetchall()
