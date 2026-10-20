import sqlite3
from unittest.mock import Mock
import pytest

from desktop_assistant import local_backup_only as local, migration
from desktop_assistant.storage import pack_tree, sha, write_json
from tests.desktop_assistant.test_local_backup_only import local_case, case
from tests.desktop_assistant.test_stopped_update import PASSWORD


@pytest.mark.parametrize('failure', ['none', 'before_sale', 'after_sale'])
def test_exact_activation_recovery_preserves_real_decimal_facts(local_case, monkeypatch, failure):
    scene = local_case
    manager = scene.manager
    old = manager.state['current']
    state = manager.state
    state['manual_stop'] = False
    write_json(manager.root/'state.json', state)
    scene.live['running'] = True
    package = scene.root/'exact-sale-migration.zip'
    pack_tree(scene.root/'source-local-two', package, dict(type='tianming.release.v1',
        version='exact-sale', revision='r2', reader_capabilities={'exact_sales_decimal_v1':1},
        runtime_capabilities={local.CAPABILITY:1}, migration=dict(policy='preserve_existing_facts_v1',
        from_revision='r1', rollback_package_sha256=old)), scene.key)
    monkeypatch.setattr(migration, 'prepare_migration', Mock())
    def migrate(release, shared, revision, log, **kwargs):
        with sqlite3.connect(shared/'data/carton_erp.sqlite3') as db:
            db.execute('UPDATE alembic_version SET version_num=?', (revision,))
            db.execute('ALTER TABLE sales_orders ADD COLUMN total_amount_exact TEXT')
        log.write_text('synthetic migration child exited')
    monkeypatch.setattr(migration, 'run_migration', migrate)
    start = manager.start
    def candidate_start():
        if manager.state['current'] == sha(package):
            # The rollback exception must be closed before first business readiness.
            assert manager.state['previous'] is None and manager.state['schema_authority'] is None
            if failure == 'after_sale':
                with sqlite3.connect(manager.root/'shared/data/carton_erp.sqlite3') as db:
                    db.execute("UPDATE sales_orders SET total_amount_exact='6.675' WHERE id=(SELECT min(id) FROM sales_orders)")
            if failure != 'none':
                raise ValueError('synthetic startup failure')
        return start()
    monkeypatch.setattr(manager, 'start', candidate_start)
    with manager.lock():
        if failure == 'none':
            assert manager._update_locked(package, PASSWORD, scene.nas) == 'exact-sale'
        else:
            with pytest.raises(ValueError):
                manager._update_locked(package, PASSWORD, scene.nas)
    if failure == 'before_sale':
        assert manager.state['current'] == old and scene.live['running']
        assert manager.state['operation'] == 'update_recovered'
        assert manager.state.get('exact_sales_activation') is None
    elif failure == 'after_sale':
        assert not scene.live['running']
        assert manager.state['operation'] == 'update_failed_reader_contract'
        with sqlite3.connect(manager.root/'shared/data/carton_erp.sqlite3') as db:
            assert db.execute('SELECT total_amount_exact FROM sales_orders').fetchall() == [('6.675',)]
    else:
        assert scene.live['running'] and manager.state['current'] == sha(package)
        assert manager.state['previous'] is None and manager.state['schema_authority'] is None
        assert not manager.compatible(old, 'r2')
