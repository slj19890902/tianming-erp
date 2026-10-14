"""Real small ciphertext/recovery/SMB wire integration, fake host authority only."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json
import os

import pytest
from desktop_assistant import nas_backup_rotation_worker as worker
from desktop_assistant import nas_backup_rotation as rotation
from desktop_assistant import mac_smb_stream_receipt as receipt
from desktop_assistant import mac_smb_stream_target as target
from desktop_assistant.storage import sha,write_json
from tests.desktop_assistant.test_mac_smb_stream_receipt import case
from tests.desktop_assistant.test_credential_restore import host_store,recovery_case
from tests.desktop_assistant.test_recovery import PASSWORD


class LocalAdapter(rotation.MacStreamReceiptAdapter):
    """TEST ONLY root substitution; same actual FD records/CAS/collector methods."""
    def __init__(self,manager):self.manager=manager


@pytest.fixture
def integrated(case,host_store,monkeypatch,request):
    previous_umask=os.umask(0o077);request.addfinalizer(lambda:os.umask(previous_umask))
    manager=case.manager
    # Synthetic host has no process referencing these disposable fixture targets.
    # Production still uses the unchanged actual process/AccessDenied guard.
    from desktop_assistant import cleanup
    monkeypatch.setattr(cleanup.psutil,'process_iter',lambda *args:iter(()))
    maps=manager.root/'shared/factory_twin_data';maps.mkdir(exist_ok=True)
    write_json(maps/'layout.json',{'revision':'isolated-map','areas':[]})
    for folder in ('releases','packages'):(manager.root/folder).chmod(0o700)
    monkeypatch.setattr(worker,'_binding',lambda manager:case.binding)
    monkeypatch.setattr(worker,'_adapter',lambda manager:LocalAdapter(manager))
    monkeypatch.setattr(receipt.runtime,'verify_network_policy',lambda binding:None)
    monkeypatch.setattr(receipt.runtime,'retire_next',lambda:{'status':'idle','removed_bytes':0})
    actions=[]
    def dispatch(binding,request):
        actions.append(request['action'])
        with receipt.runtime.files.open_archive(case.peer.tree(),binding.value) as remote:
            if request['action']=='upload':
                return remote.upload(request['file_fd'],request['filename'],request['size'],request['sha256'],request['attempt_id'])
            return remote.download(request['file_fd'],request['upload'])
    monkeypatch.setattr(receipt.runtime,'_dispatch',dispatch)
    case.binding.control_sha=receipt.cfg.digest(receipt.cfg.canonical(case.binding.control))
    case.bound['control_sha']=case.binding.control_sha
    # Create ONE real encrypted first point through the existing legacy worker.
    with manager.lock():first=receipt.archive_and_publish_stopped(manager,PASSWORD)
    old=deepcopy(manager.state);case.old_receipt=(manager.root/'control/backup-receipts'/old[receipt.INDEX]['receipt']).read_bytes()
    adapter=LocalAdapter(manager)
    for job in (manager.root/'staging').iterdir():
        assert (job/receipt.work.COMPLETED).is_file()
        adapter.record_job(worker._job(job.name,'backup','completed',first,sha(first),old[receipt.INDEX]))
    live={'running':True};events=[]
    monkeypatch.setattr(manager,'_process',lambda:('isolated',{}) if live['running'] else None)
    monkeypatch.setattr(manager,'stop',lambda:(events.append('stop'),live.update(running=False)))
    monkeypatch.setattr(manager,'start',lambda:(events.append('start'),live.update(running=True)))
    monkeypatch.setattr(manager,'_backup_preflight',lambda nas:events.append('preflight'))
    state=manager.state;state['manual_stop']=False;write_json(manager.root/'state.json',state)
    monkeypatch.setattr(worker.runtime.keychain,'load_context',lambda:case.binding.context)
    from desktop_assistant import mac_system_service as system,mac_published_runtime as published
    monkeypatch.setattr(system,'metadata',lambda identity:dict(context=case.binding.context,public=case.public))
    manifest=dict(runtime_capabilities={receipt.cfg.CAPABILITY:1,receipt.cfg.ROTATION_CAPABILITY:1},files={name:'a'*64 for name in receipt.cfg.FILES|receipt.cfg.ROTATION_FILES})
    case.binding.manifest=manifest
    monkeypatch.setattr(published,'resolve',lambda *a,**k:(case.binding.tree,manifest))
    monkeypatch.setattr(target,'metadata_present',lambda:True)
    case.live=live;case.events=events;case.actions=actions;case.adapter=adapter
    return case


def establish_first(case):
    authority=worker.capture_first_authority(case.manager,PASSWORD)
    assert authority['proof']['source_time']==authority['recovery']['source_time']
    assert authority['proof']['manifest_sha256']==authority['recovery']['manifest_sha256']
    case.binding.control=dict(case.binding.control,package_sha256='f'*64)
    case.binding.control_sha=receipt.cfg.digest(receipt.cfg.canonical(case.binding.control))
    case.bound=dict(context=case.binding.context,control=case.binding.control,value=case.binding.value,control_sha=case.binding.control_sha)
    result=worker.reauthenticate_first(case.manager,PASSWORD)
    assert result['status']=='first_authenticated_waiting_second'
    assert case.actions==['download']
    assert (case.manager.root/'control/backup-receipts'/authority['old_pointer']['receipt']).read_bytes()==case.old_receipt
    assert result['pointer']['receipt'].startswith('reauth-'+case.binding.control_sha+'-')
    assert receipt.verified_at(case.manager.root,case.manager.state)
    return result


def test_first_reauthentication_downloads_same_cipher_and_reserves_only_one_slot(integrated):
    case=integrated;result=establish_first(case)
    reservation=worker.reserve_bootstrap_second(case.manager)
    assert worker.reserve_bootstrap_second(case.manager)['task_id']==reservation['task_id']
    replay=worker.reauthenticate_first(case.manager,PASSWORD)
    assert replay['status']=='replayed' and replay['pointer']==result['pointer']
    assert case.actions==['download'] and not case.events
    assert len(list((case.manager.root/'backups').glob('*.tmbackup')))==1


def test_local_cutover_reuses_one_cipher_then_real_restore_and_indexes_pair(integrated):
    from desktop_assistant.local_code_update import update
    case=integrated;establish_first(case)
    reservation=worker.reserve_bootstrap_second(case.manager)
    state=case.manager.state;state['manual_stop']=True;write_json(case.manager.root/'state.json',state);case.live['running']=False
    with case.manager.lock():proof=update(case.manager,case.release('two'),PASSWORD,sha(case.manager.root/'state.json'))
    assert proof['rotation_slot']['task_id']==reservation['task_id']
    assert case.actions==['download']
    state=case.manager.state;state['manual_stop']=False;write_json(case.manager.root/'state.json',state);case.live['running']=True
    result=worker.resume_local_update(case.manager,PASSWORD)
    assert result['status']=='indexed'
    assert case.actions==['download','upload','download']
    _,index=case.adapter.read_index();assert len(index['kept'])==2 and index['bootstrap'] is None
    before=len(list((case.manager.root/'backups').glob('*.tmbackup')))
    assert worker.resume_local_update(case.manager,PASSWORD)['status']=='replayed'
    assert len(list((case.manager.root/'backups').glob('*.tmbackup')))==before==1
    assert case.actions==['download','upload','download']
    assert case.adapter.load_verified(index['kept'][0])['receipt']['sha256']==proof['backup_sha256']



def establish_pair(case):
    from desktop_assistant.local_code_update import update
    establish_first(case);worker.reserve_bootstrap_second(case.manager)
    state=case.manager.state;state['manual_stop']=True;write_json(case.manager.root/'state.json',state);case.live['running']=False
    with case.manager.lock():update(case.manager,case.release('two'),PASSWORD,sha(case.manager.root/'state.json'))
    state=case.manager.state;state['manual_stop']=False;write_json(case.manager.root/'state.json',state);case.live['running']=True
    worker.resume_local_update(case.manager,PASSWORD)
    case.actions.clear();case.events.clear()


def test_ordinary_manager_route_captures_once_then_transfers_outside_lock(integrated,monkeypatch):
    case=integrated;establish_pair(case)
    original=receipt.runtime._dispatch
    def outside(binding,request):
        assert case.live['running']
        with case.manager.lock():pass
        return original(binding,request)
    monkeypatch.setattr(receipt.runtime,'_dispatch',outside)
    result=case.manager.backup(PASSWORD,target.destination(case.binding.control))
    assert case.events==['preflight','stop','start']
    assert case.actions==['upload','download']
    assert result==Path(case.manager.state['last_backup'])
    _,index=case.adapter.read_index();assert len(index['kept'])==2 and len(index['retired'])==1
    job=case.manager.root/'staging'/('smb-stream-'+case.manager.state['backup_rotation_task_id'])
    assert set(p.name for p in job.iterdir())=={receipt.work.PENDING,receipt.work.COMPLETED}
    assert case.adapter.load_verified(index['kept'][0])


def test_unknown_staging_blocks_before_stopping_or_creating_another_cipher(integrated):
    case=integrated;establish_pair(case)
    (case.manager.root/'staging'/'unknown-pre-existing').mkdir()
    before=set(p.name for p in (case.manager.root/'backups').iterdir())
    with pytest.raises(ValueError,match='unregistered'):
        case.manager.backup(PASSWORD,target.destination(case.binding.control))
    assert not case.events and case.live['running']
    assert before==set(p.name for p in (case.manager.root/'backups').iterdir())


def test_transfer_failure_keeps_ready_erp_pair_and_exact_pending_task(integrated,monkeypatch):
    case=integrated;establish_pair(case)
    _,before=case.adapter.read_index();captures=[]
    original=case.manager._build_backup_payload
    def count(*args,**kwargs):captures.append(True);return original(*args,**kwargs)
    monkeypatch.setattr(case.manager,'_build_backup_payload',count)
    monkeypatch.setattr(receipt.runtime,'_dispatch',lambda *args:(_ for _ in ()).throw(OSError('synthetic connection failure')))
    with pytest.raises(OSError):case.manager.backup(PASSWORD,target.destination(case.binding.control))
    assert case.live['running'] and case.events==['preflight','stop','start']
    assert case.adapter.read_index()[1]==before
    marker=case.manager.state['backup_rotation_task_id']
    assert worker.pending_locked(case.manager)['plan']['task_id']==marker
    with pytest.raises(FileExistsError):case.manager.backup(PASSWORD,target.destination(case.binding.control))
    assert len(captures)==1 and case.live['running']
    assert case.adapter.read_index()[1]==before


def test_nightly_stream_route_transfers_after_releasing_admission_lock(integrated,monkeypatch):
    from desktop_assistant import nightly
    from desktop_assistant.manager import CN
    from datetime import datetime
    case=integrated;establish_pair(case)
    preferences=json.loads((case.manager.root/'preferences.json').read_text())
    preferences['nas']=str(target.destination(case.binding.control));write_json(case.manager.root/'preferences.json',preferences)
    monkeypatch.setattr(nightly,'unprotect_backup',lambda reference:PASSWORD)
    monkeypatch.setattr(nightly.mac_backup_storage,'verified_at',lambda *a,**k:None)
    monkeypatch.setattr(nightly.mac_backup_storage,'prepare_destination',lambda path:None)
    original=receipt.runtime._dispatch
    def outside(binding,request):
        with case.manager.lock():pass
        assert case.live['running']
        return original(binding,request)
    monkeypatch.setattr(receipt.runtime,'_dispatch',outside)
    result=nightly.run_once(case.manager,now=datetime(2026,10,14,23,5,tzinfo=CN),restart_allowed=lambda:True)
    assert result['status']=='verified' and case.events==['preflight','stop','start']
    assert case.actions==['upload','download']



def test_recovery_failure_preserves_kept_pair_and_replays_same_download(integrated,monkeypatch):
    case=integrated;establish_pair(case)
    _,before=case.adapter.read_index();original=worker._restore;captures=[]
    build=case.manager._build_backup_payload
    monkeypatch.setattr(case.manager,'_build_backup_payload',lambda *a,**kw:(captures.append(True),build(*a,**kw))[1])
    monkeypatch.setattr(worker,'_restore',lambda *args:(_ for _ in ()).throw(RuntimeError('isolated restore injected')))
    with pytest.raises(RuntimeError):case.manager.backup(PASSWORD,target.destination(case.binding.control))
    _,failed=case.adapter.read_index()
    assert failed['kept']==before['kept'] and failed['staged'] is not None and not failed['retired']
    assert case.live['running'] and case.manager.state['backup_error']
    assert case.actions==['upload','download']
    monkeypatch.setattr(worker,'_restore',original)
    result=case.manager.backup(PASSWORD,target.destination(case.binding.control))
    assert result==Path(case.manager.state['last_backup'])
    assert len(captures)==1 and case.actions==['upload','download']
    assert case.adapter.read_index()[1]['staged'] is None


@pytest.mark.parametrize('fault',['old_bytes','latest_bytes','active_reference','restore_reference','update_proof','unknown_cache','hardlink'])
def test_local_retention_refuses_changed_or_referenced_cipher_without_another_capture(integrated,monkeypatch,fault):
    case=integrated;establish_pair(case);captures=[]
    original_build=case.manager._build_backup_payload;original=worker._local_retention
    def build(*args,**kwargs):captures.append(True);return original_build(*args,**kwargs)
    monkeypatch.setattr(case.manager,'_build_backup_payload',build)
    def refused(manager,binding,plan):
        latest=Path(plan['local_copy']['path']);old=Path(manager.state['update_backup'])
        if fault=='old_bytes':old.write_bytes(b'changed old cipher')
        elif fault=='latest_bytes':latest.write_bytes(b'changed newest cipher')
        elif fault=='active_reference':
            case.adapter.record_job(worker._job('completion-'+'b'*32,'restore','active',old,sha(old)))
        elif fault=='restore_reference':
            state=manager.state;state['restored_backup_sha256']=sha(old);write_json(manager.root/'state.json',state)
        elif fault=='update_proof':
            state=manager.state;state['local_code_update']['authenticated_recovery']['raw_sha256']='0'*64;write_json(manager.root/'state.json',state)
        elif fault=='unknown_cache':(manager.root/'backups'/'unknown-history.tmbackup').write_bytes(b'never adopted')
        elif fault=='hardlink':os.link(old,manager.root/'unrelated-hardlink')
        return original(manager,binding,plan)
    monkeypatch.setattr(worker,'_local_retention',refused)
    with pytest.raises(ValueError):case.manager.backup(PASSWORD,target.destination(case.binding.control))
    assert case.live['running'] and len(captures)==1
    _,plan=worker._read(case.adapter,worker._task_name(case.manager.state['backup_rotation_task_id']))
    assert plan['phase']=='indexed' and 'local_retention' not in plan
    assert Path(case.manager.state['update_backup']).exists()
    assert Path(case.manager.state['last_backup']).exists()
    # Existing retirement/pending owner prevents creation of another complete copy.
    case.manager.backup(PASSWORD,target.destination(case.binding.control))
    assert len(captures)==1


def test_access_denied_recovery_cleanup_retains_scene_and_retries_same_owner(integrated,monkeypatch):
    from desktop_assistant import cleanup
    case=integrated;establish_pair(case);before=case.adapter.read_index()[1];captures=[]
    build=case.manager._build_backup_payload
    monkeypatch.setattr(case.manager,'_build_backup_payload',lambda *args,**kwargs:(captures.append(True),build(*args,**kwargs))[1])
    idle=cleanup.assert_idle
    monkeypatch.setattr(cleanup,'assert_idle',lambda path:(_ for _ in ()).throw(ValueError('synthetic AccessDenied')))
    with pytest.raises(ValueError,match='cleanup guard refused'):
        case.manager.backup(PASSWORD,target.destination(case.binding.control))
    assert case.live['running'] and case.adapter.read_index()[1]['kept']==before['kept']
    task=case.manager.state['backup_rotation_task_id'];_,plan=worker._read(case.adapter,worker._task_name(task))
    assert plan['phase']=='verified' and plan['recovery_cleanup']['status']=='retained'
    assert (case.manager.root/'control/rotation-recovery'/plan['job_id']).is_dir()
    with pytest.raises(ValueError,match='cleanup guard refused'):
        case.manager.backup(PASSWORD,target.destination(case.binding.control))
    assert len(captures)==1 and case.actions==['upload','download']
    monkeypatch.setattr(cleanup,'assert_idle',idle)
    assert case.manager.backup(PASSWORD,target.destination(case.binding.control))==Path(case.manager.state['last_backup'])
    assert len(captures)==1 and case.actions==['upload','download']
    assert not (case.manager.root/'control/rotation-recovery'/plan['job_id']).exists()
    assert len(list((case.manager.root/'backups').iterdir()))==1


def test_first_authority_and_reauth_root_cleanup_resume_without_repeating_restore(integrated,monkeypatch):
    from desktop_assistant import cleanup
    case=integrated;idle=cleanup.assert_idle;restores=[];original=worker._restore
    def count(*args,**kwargs):restores.append(True);return original(*args,**kwargs)
    monkeypatch.setattr(worker,'_restore',count)
    monkeypatch.setattr(cleanup,'assert_idle',lambda path:(_ for _ in ()).throw(ValueError('UID cannot inspect unrelated Python process')))
    with pytest.raises(ValueError,match='cleanup guard refused'):worker.capture_first_authority(case.manager,PASSWORD)
    pending=worker.retained_recovery_targets(case.manager)
    assert len(pending)==1 and pending[0]['exists'] and pending[0]['entry']=='capture_first_authority'
    with monkeypatch.context() as root:
        root.setattr(cleanup,'assert_idle',idle)
        cleanup.remove_owned_tree(Path(pending[0]['target']),Path(pending[0]['owner']))
    authority=worker.capture_first_authority(case.manager,PASSWORD)
    assert len(restores)==1 and authority['cleanup']['status']=='retained'
    case.binding.control=dict(case.binding.control,package_sha256='f'*64)
    case.binding.control_sha=receipt.cfg.digest(receipt.cfg.canonical(case.binding.control))
    case.bound=dict(context=case.binding.context,control=case.binding.control,value=case.binding.value,control_sha=case.binding.control_sha)
    with pytest.raises(ValueError,match='cleanup guard refused'):worker.reauthenticate_first(case.manager,PASSWORD)
    pending=[v for v in worker.retained_recovery_targets(case.manager) if v['exists']]
    assert len(pending)==1 and pending[0]['entry']=='reauthenticate_first'
    with monkeypatch.context() as root:
        root.setattr(cleanup,'assert_idle',idle)
        cleanup.remove_owned_tree(Path(pending[0]['target']),Path(pending[0]['owner']))
    assert worker.reauthenticate_first(case.manager,PASSWORD)['status']=='first_authenticated_waiting_second'
    assert len(restores)==2 and case.actions==['download']
    assert worker.reserve_bootstrap_second(case.manager)['status']=='bootstrap_second_reserved'
