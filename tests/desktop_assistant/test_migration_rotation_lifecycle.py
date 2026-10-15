"""Synthetic signed migration rehearsals emit real protected owner lifecycle."""
from pathlib import Path
import sqlite3

import pytest
from desktop_assistant import migration, nas_backup_rotation as rotation
from desktop_assistant import nas_backup_rotation_worker as worker
from desktop_assistant import mac_published_runtime as published
from desktop_assistant.storage import read_json
from tests.desktop_assistant.test_nas_rotation_worker_integration import (
    integrated, establish_pair, case, host_store, recovery_case)


def test_migration_namespace_is_rehearsal_only():
    job=dict(job_id='migration-'+'a'*32,kind='rehearsal',status='active',
        backup_path='/fixed/staging/migration-'+'a'*32,backup_sha256=None,receipt='')
    rotation.validate_job(job)
    for kind in ('backup','upload','restore'):
        with pytest.raises(ValueError):rotation.validate_job(dict(job,kind=kind))
    for name in ('migration-'+ 'a'*31,'migration-'+ 'A'*32,'migration-../outside'):
        with pytest.raises(ValueError):rotation.validate_job(dict(job,job_id=name))


@pytest.mark.parametrize('result',['success','retained','failed_closed','failed_active'])
def test_rehearsal_real_registration_and_terminal_state(integrated,monkeypatch,result):
    scene=integrated;establish_pair(scene);monkeypatch.setattr(published,'selected',lambda:False)
    observed=[]
    def run(release,shared,revision,log,**kwargs):
        job=shared.parent;_,registry=worker._read(scene.adapter,'retention-jobs.json')
        owner=registry['jobs'][job.name]
        assert owner==worker._job(job.name,'rehearsal','active',job)
        observed.append(job)
        if result.startswith('failed'):raise ValueError('synthetic migration failed')
        with sqlite3.connect(shared/'data/carton_erp.sqlite3') as database:
            database.execute('UPDATE alembic_version SET version_num=?',(revision,))
        log.write_text('synthetic observed child exited')
    monkeypatch.setattr(migration,'run_migration',run)
    if result in ('retained','failed_active'):
        from desktop_assistant import cleanup
        monkeypatch.setattr(cleanup,'assert_idle',lambda job:(_ for _ in ()).throw(ValueError('process evidence unavailable')))
    package=scene.release('migration-target','r2')
    if result.startswith('failed'):
        with pytest.raises(ValueError,match='synthetic migration failed'):scene.manager.preview_update(package)
        report=read_json(observed[0]/'report.json')
    else:report=scene.manager.preview_update(package)
    _,registry=worker._read(scene.adapter,'retention-jobs.json');owner=registry['jobs'][observed[0].name]
    assert owner['status']=={'success':'completed','retained':'completed','failed_closed':'failed_closed','failed_active':'active'}[result]
    assert owner['kind']=='rehearsal' and owner['backup_sha256'] is None and owner['receipt']==''
    assert report['status']==('failed' if result.startswith('failed') else 'passed')
    if result=='retained':assert 'retained' in report['temporary_cleanup']['shared']
    # A completed task is registered even when only its audit report remains.
    scene.adapter.collect_active_references(scene.manager.state)
    assert scene.actions==[] and scene.live['running']


@pytest.mark.parametrize('fault',['mkdir','initial_report'])
def test_initial_owner_failure_stays_active_before_any_writer(integrated,monkeypatch,fault):
    scene=integrated;establish_pair(scene);monkeypatch.setattr(published,'selected',lambda:False)
    package=scene.release('migration-target','r2')
    if fault=='mkdir':
        original=Path.mkdir
        def fail(path,*args,**kwargs):
            if path.parent==scene.manager.root/'staging' and path.name.startswith('migration-'):
                raise OSError('synthetic initial owner failure')
            return original(path,*args,**kwargs)
        monkeypatch.setattr(Path,'mkdir',fail)
    else:
        original=migration.write_json
        def fail(path,*args,**kwargs):
            if path.name=='report.json':raise OSError('synthetic initial owner failure')
            return original(path,*args,**kwargs)
        monkeypatch.setattr(migration,'write_json',fail)
    with pytest.raises(OSError,match='initial owner failure'):scene.manager.preview_update(package)
    _,registry=worker._read(scene.adapter,'retention-jobs.json')
    jobs=[value for key,value in registry['jobs'].items() if key.startswith('migration-')]
    assert len(jobs)==1 and jobs[0]['status']=='active' and jobs[0]['kind']=='rehearsal'
    assert scene.actions==[] and scene.live['running']


def test_rehearsal_does_not_overwrite_existing_identity(integrated,monkeypatch):
    from types import SimpleNamespace
    scene=integrated;establish_pair(scene);monkeypatch.setattr(published,'selected',lambda:False)
    package=scene.release('migration-target','r2');job_id='migration-'+'a'*32
    original=worker._job(job_id,'rehearsal','completed','original audited owner')
    scene.adapter.record_job(original)
    monkeypatch.setattr(migration.uuid,'uuid4',lambda:SimpleNamespace(hex='a'*32))
    with pytest.raises(ValueError,match='already registered'):scene.manager.preview_update(package)
    _,registry=worker._read(scene.adapter,'retention-jobs.json')
    assert registry['jobs'][job_id]==original
    assert not (scene.manager.root/'staging'/job_id).exists()


def test_old_archive_capability_does_not_enable_migration_registration(integrated):
    scene=integrated;before=worker._read(scene.adapter,'retention-jobs.json')[0]
    scene.binding.manifest=dict(scene.binding.manifest,runtime_capabilities={
        key:value for key,value in scene.binding.manifest['runtime_capabilities'].items()
        if key!=worker.cfg.ROTATION_CAPABILITY})
    assert migration._rotation_rehearsal_owner(scene.manager,scene.manager.root/'staging'/('migration-'+'0'*32)) is None
    assert worker._read(scene.adapter,'retention-jobs.json')[0]==before


def test_family_root_never_reads_mac_rotation_metadata(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from desktop_assistant import mac_smb_stream_target as target
    monkeypatch.setattr(target,'metadata_present',lambda:(_ for _ in ()).throw(AssertionError('family must not read Mac metadata')))
    assert migration._rotation_rehearsal_owner(SimpleNamespace(root=tmp_path/'family'),tmp_path/'job') is None


def test_local_policy_rehearsal_ignores_stale_smb_binding(integrated,tmp_path,monkeypatch):
    """Reproduce the production local-backup -> stale SMB rehearsal collision."""
    from desktop_assistant import local_backup_only as local
    scene=integrated
    monkeypatch.setattr(published,'selected',lambda:False)
    monkeypatch.setattr(local,'selected',lambda root:root==scene.manager.root)
    scene.binding.tree=tmp_path/'older-published-package'/'migration'
    state=scene.manager.state
    def run(release,shared,revision,log,**kwargs):
        with sqlite3.connect(shared/'data/carton_erp.sqlite3') as database:
            database.execute('UPDATE alembic_version SET version_num=?',(revision,))
        log.write_text('isolated child exited')
    monkeypatch.setattr(migration,'run_migration',run)
    report=scene.manager.preview_update(scene.release('local-migration-target','r2'))
    assert report['status']=='passed' and report['source_unchanged']
    assert scene.manager.state==state and scene.live['running']
    assert scene.actions==[] and scene.events==[]


def test_invalid_local_policy_cannot_fall_through_to_smb(integrated,monkeypatch):
    from desktop_assistant import local_backup_only as local,mac_smb_stream_target as target
    def invalid(root):raise ValueError('invalid fixed root policy')
    monkeypatch.setattr(local,'selected',invalid)
    monkeypatch.setattr(target,'metadata_present',lambda:pytest.fail('invalid policy fell through to SMB'))
    with pytest.raises(ValueError,match='invalid fixed root policy'):
        migration._rotation_rehearsal_owner(integrated.manager,integrated.manager.root/'staging/new')
    assert integrated.live['running'] and integrated.events==[]


@pytest.mark.parametrize('rotation_enabled',[False,True])
def test_distinct_binding_tree_legacy_noop_but_enabled_rotation_rejects(integrated,tmp_path,rotation_enabled):
    scene=integrated;before=worker._read(scene.adapter,'retention-jobs.json')[0]
    if not rotation_enabled:
        scene.binding.manifest=dict(scene.binding.manifest,runtime_capabilities={
            key:value for key,value in scene.binding.manifest['runtime_capabilities'].items()
            if key!=worker.cfg.ROTATION_CAPABILITY})
    old_tree=tmp_path/'old-published'/('e'*64)/'migration';old_tree.mkdir(parents=True)
    scene.binding.tree=old_tree
    with scene.manager.lock():
        job=scene.manager.root/'staging'/('migration-'+'d'*32)
        if rotation_enabled:
            with pytest.raises(ValueError):migration._rotation_rehearsal_owner(scene.manager,job)
        else:assert migration._rotation_rehearsal_owner(scene.manager,job) is None
    assert worker._read(scene.adapter,'retention-jobs.json')[0]==before
    assert scene.live['running'] and scene.actions==[]
