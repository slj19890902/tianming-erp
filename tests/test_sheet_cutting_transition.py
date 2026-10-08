from copy import deepcopy
from pathlib import Path
import sqlite3

import pytest

from tests.test_phase11_requisition import requisition_app
from scripts.admin.transition_sheet_cutting_settings import build_plan, apply_plan, digest


def prepare(sessions, tmp_path):
    from app.models.product import Product
    with sessions() as db:
        database = Path(db.bind.url.database)
        db.connection().exec_driver_sql('CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)')
        db.connection().exec_driver_sql('DELETE FROM alembic_version')
        db.connection().exec_driver_sql("INSERT INTO alembic_version VALUES ('eg1008sc')")
        product = db.get(Product, 1)
        product.default_cutting_mode = '一开四'
        product.production_process = '印刷,钉箱'
        product.mold_tool_id = None
        db.commit()
    backup = tmp_path / 'before.sqlite3'
    with sqlite3.connect(database) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    return database, backup


def test_owner_confirmation_converts_number_without_inventing_mold(requisition_app, tmp_path):
    from app.models.product import Product
    _, sessions = requisition_app
    database, backup = prepare(sessions, tmp_path)
    unconfirmed = build_plan(database)
    assert any(row['before']['id'] == 1 and row['reason'] == 'non_die_multi_output' for row in unconfirmed['review'])
    plan = build_plan(database, [1])
    apply_plan(database, plan, backup, digest(backup))
    with sessions() as db:
        product = db.get(Product, 1)
        assert product.production_process == '印刷,钉箱,模切'
        assert product.sheet_cutting_settings['whole']['mold_count'] == 4
        assert product.default_cutting_mode == '一开一'
        assert product.mold_tool_id is None


def test_stale_plan_or_bad_backup_never_writes(requisition_app, tmp_path):
    _, sessions = requisition_app
    database, backup = prepare(sessions, tmp_path)
    plan = build_plan(database, [1])
    before = digest(database)
    with pytest.raises(AssertionError):
        apply_plan(database, plan, backup, 'incorrect')
    altered = deepcopy(plan)
    altered['products'][0]['before']['version'] += 1
    with pytest.raises(AssertionError, match='资料已变化'):
        apply_plan(database, altered, backup, digest(backup))
    assert digest(database) == before


def test_mid_transaction_failure_rolls_back_masters_and_audit(requisition_app, tmp_path, monkeypatch):
    import app.services.master_data_versioning as module
    _, sessions = requisition_app
    database, backup = prepare(sessions, tmp_path)
    plan = build_plan(database, [1])
    original = module.apply_versioned_update
    def fail_after_update(*args, **kwargs):
        result = original(*args, **kwargs)
        args[0].flush()
        raise RuntimeError('injected transition failure')
    monkeypatch.setattr(module, 'apply_versioned_update', fail_after_update)
    with sqlite3.connect(database) as db:
        audit_before = db.execute('SELECT count(*) FROM operation_logs').fetchone()
    with pytest.raises(RuntimeError, match='injected'):
        apply_plan(database, plan, backup, digest(backup))
    assert build_plan(database, [1]) == plan
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT count(*) FROM operation_logs').fetchone() == audit_before
