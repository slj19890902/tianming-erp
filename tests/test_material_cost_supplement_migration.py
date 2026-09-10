import sqlite3
from alembic import command
from alembic.config import Config
import pytest


def test_empty_round_trip_and_frozen_reference_guard(tmp_path, monkeypatch):
    from app.core.database import create_sqlite_engine
    from app.models import Base
    path=tmp_path/'supplement.sqlite3'
    engine=create_sqlite_engine(path)
    Base.metadata.create_all(engine)
    engine.dispose()
    with sqlite3.connect(path) as db:
        db.execute('DROP TABLE finance_material_cost_supplements')
    monkeypatch.setenv('ERP_DATABASE_PATH',str(path))
    monkeypatch.setenv('ERP_BACKUP_DIR',str(tmp_path/'backup'))
    config=Config('alembic.ini')
    command.stamp(config,'rt09v8x9z68')
    command.upgrade(config,'ru10v8x9z69')
    command.downgrade(config,'rt09v8x9z68')
    command.upgrade(config,'ru10v8x9z69')
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA integrity_check').fetchall()==[('ok',)]
        assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
        # Isolated synthetic row, FKs intentionally off for trigger-only test.
        db.execute("INSERT INTO finance_material_cost_supplements(delivery_item_id,month,source_kind,source_id,target_fingerprint,target_json,quantity_limit,unit_cost,currency,reference_kind,evidence_json,evidence_fingerprint,algorithm_version,batch_id,reason,created_by) VALUES(1,'2026-08','untraced_delivery',1,?,'{}',5,1.25,'CNY','test','{}',?,'v1','test','test',1)",('a'*64,'b'*64))
        db.commit()
        for sql in ('UPDATE finance_material_cost_supplements SET unit_cost=9','DELETE FROM finance_material_cost_supplements'):
            with pytest.raises(sqlite3.IntegrityError,match='immutable'):
                db.execute(sql)
            db.rollback()
    with pytest.raises(RuntimeError,match='Refusing'):
        command.downgrade(config,'rt09v8x9z68')
