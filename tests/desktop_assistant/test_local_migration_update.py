"""Local policy: real signed packages, cold cipher and recovery, rehearsal and activation.

Only the host authority, process lifecycle and migration child are synthetic.
No production instance or network transport is used.
"""
import sqlite3
from unittest.mock import Mock

from desktop_assistant import local_backup_only as local, migration
from desktop_assistant.storage import pack_tree, sha, write_json, read_json
from tests.desktop_assistant.test_local_backup_only import local_case, case
from tests.desktop_assistant.test_stopped_update import PASSWORD


def test_full_local_migration_update_keeps_originals_and_publishes_recovery(local_case, monkeypatch):
    scene=local_case
    manager=scene.manager
    state=manager.state
    state['manual_stop']=False
    write_json(manager.root/'state.json',state)
    scene.live['running']=True
    old=state['current']
    package=scene.root/'local-migration-signed.zip'
    pack_tree(scene.root/'source-local-two',package,{
        'type':'tianming.release.v1','version':'local-migration','revision':'r2',
        'runtime_capabilities':{local.CAPABILITY:1},
        'migration':{'policy':'preserve_existing_facts_v1','from_revision':'r1',
                     'rollback_package_sha256':old}},scene.key)
    monkeypatch.setattr(migration,'prepare_migration',Mock())
    child_targets=[]
    def migrate(release,shared,revision,log,**kwargs):
        child_targets.append(shared)
        with sqlite3.connect(shared/'data/carton_erp.sqlite3') as db:
            db.execute('UPDATE alembic_version SET version_num=?',(revision,))
        log.write_text('synthetic migration child exited')
    monkeypatch.setattr(migration,'run_migration',migrate)
    monkeypatch.setattr(manager,'_backup_preflight',Mock(side_effect=AssertionError('NAS called')))
    monkeypatch.setattr(manager,'_backup_stopped',Mock(side_effect=AssertionError('NAS called')))
    database=manager.root/'shared/data/carton_erp.sqlite3'
    before=migration.facts(database)
    originals=migration.files(manager.root/'shared')
    with manager.lock():
        assert manager._update_locked(package,PASSWORD,scene.nas)=='local-migration'
    assert scene.events==['stop','start'] and scene.live['running']
    assert len(child_targets)==2 and child_targets[0]!=child_targets[1]
    assert child_targets[1]==manager.root/'shared'
    assert manager.state['current']==sha(package) and manager.state['previous']==old
    assert manager.state['operation']=='update' and manager.state['manual_stop'] is False
    assert migration.facts(database)==before
    assert migration.files(manager.root/'shared')==originals
    index=manager.state['last_backup_local']
    receipt=read_json(manager.root/'control'/index['receipt'])
    assert receipt['sha256']==sha(manager.root/'backups'/receipt['path'].split('/')[-1])
    assert local.verified_at(manager.root,manager.state)==index['source_time']
