"""Local policy: real signed packages, cold cipher and recovery, rehearsal and activation.

Only the host authority, process lifecycle and migration child are synthetic.
No production instance or network transport is used.
"""
import sqlite3
from unittest.mock import Mock
import pytest

from desktop_assistant import local_backup_only as local, migration
from desktop_assistant.storage import pack_tree, sha, write_json, read_json
from tests.desktop_assistant.test_local_backup_only import local_case, case
from tests.desktop_assistant.test_stopped_update import PASSWORD


@pytest.mark.parametrize('startup_failure',[False,True])
def test_full_local_migration_update_keeps_originals_and_publishes_recovery(local_case, monkeypatch,startup_failure):
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
    start=manager.start
    def candidate_start():
        if startup_failure and manager.state['current']==sha(package):
            scene.events.append('candidate-failed')
            raise ValueError('synthetic candidate start failure')
        start()
    monkeypatch.setattr(manager,'start',candidate_start)
    with manager.lock():
        if startup_failure:
            with pytest.raises(ValueError,match='已切回原程序'):
                manager._update_locked(package,PASSWORD,scene.nas)
        else:assert manager._update_locked(package,PASSWORD,scene.nas)=='local-migration'
    assert scene.events==(['stop','candidate-failed','stop','start'] if startup_failure else ['stop','start'])
    assert scene.live['running']
    assert len(child_targets)==2 and child_targets[0]!=child_targets[1]
    assert child_targets[1]==manager.root/'shared'
    if startup_failure:
        assert manager.state['current']==old
        assert manager.state['operation']=='update_recovered'
        assert manager.state['last_update_result']['outcome']=='failed'
        assert manager.state['last_update_result']['target']==sha(package)
        assert manager.state['last_update_result']['recovery']=='healthy_previous'
        from desktop_assistant import service_supervisor
        scene.live['running']=False
        monkeypatch.setattr(manager,'_process',lambda:('fixture',{'pid':1,'created':1,'exe':'fixture'}) if scene.live['running'] else None)
        assert service_supervisor.tick(manager)=='running'
        assert scene.live['running'] and scene.events[-1]=='start'
    else:
        assert manager.state['current']==sha(package) and manager.state['previous']==old
        assert manager.state['operation']=='update'
    assert manager.state['manual_stop'] is False
    assert migration.facts(database)==before
    assert migration.files(manager.root/'shared')==originals
    index=manager.state['last_backup_local']
    receipt=read_json(manager.root/'control'/index['receipt'])
    assert receipt['sha256']==sha(manager.root/'backups'/receipt['path'].split('/')[-1])
    assert local.verified_at(manager.root,manager.state)==index['source_time']
