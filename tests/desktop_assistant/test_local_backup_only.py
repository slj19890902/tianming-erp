"""Root policy substitute only; real signed package/cold cipher/recovery proofs."""
import os
from pathlib import Path
import shutil
from datetime import datetime,timedelta

import pytest
from desktop_assistant import local_backup_only as local
from desktop_assistant import local_code_update,nightly,mac_service_manage,mac_backup_storage
from desktop_assistant.storage import pack_tree,sha,write_json,read_json
from tests.desktop_assistant.test_stopped_update import case,PASSWORD
from tests.desktop_assistant.test_credential_restore import host_store


@pytest.fixture
def local_case(case,monkeypatch,request):
    previous=os.umask(0o077);request.addfinalizer(lambda:os.umask(previous))
    root=case.manager.root;monkeypatch.setattr(local,'ROOT',root)
    policy=case.root/'root-policy.json';write_json(policy,{'mode':'local_only'})
    monkeypatch.setattr(local,'POLICY',policy)
    monkeypatch.setattr(local,'_authority',lambda:({'mode':'local_only'},
        {'uid':os.getuid(),'gid':os.getgid()},'a'*64))
    case.release('local-two');source=case.root/'source-local-two'
    shutil.copyfile(local.__file__,source/'desktop_assistant/local_backup_only.py')
    package=case.root/'local-two-signed.zip'
    pack_tree(source,package,{'type':'tianming.release.v1','version':'local-two','revision':'r1',
        'runtime_capabilities':{local.CAPABILITY:1}},case.key)
    live={'running':False};events=[]
    monkeypatch.setattr(case.manager,'_process',lambda:('fixture',{}) if live['running'] else None)
    monkeypatch.setattr(case.manager,'start',lambda:(events.append('start'),live.update(running=True)))
    monkeypatch.setattr(case.manager,'stop',lambda:(events.append('stop'),live.update(running=False)))
    from desktop_assistant import cleanup
    monkeypatch.setattr(cleanup.psutil,'process_iter',lambda *args:iter(()))
    case.local_package=package;case.live=live;case.events=events
    return case


def cutover(scene):
    with scene.manager.lock():
        proof=local_code_update.update(scene.manager,scene.local_package,PASSWORD,sha(scene.manager.root/'state.json'))
    state=scene.manager.state;state['manual_stop']=False;write_json(scene.manager.root/'state.json',state)
    scene.live['running']=True
    return proof


def test_local_update_policy_never_calls_nas_and_finalize_reuses_real_cold_proof(local_case,monkeypatch):
    scene=local_case
    from desktop_assistant import nas_backup_rotation_worker as remote,mac_smb_stream_target as stream
    monkeypatch.setattr(remote,'local_update_slot_locked',lambda *args:(_ for _ in ()).throw(AssertionError('NAS slot called')))
    monkeypatch.setattr(stream,'metadata_present',lambda:True)
    proof=cutover(scene)
    assert proof['local_backup_only'] is True and proof['rotation_slot'] is None
    assert 'nas_rotation_pending' not in proof and proof['nas_verified'] is False
    count=len(list((scene.manager.root/'backups').glob('*.tmbackup')))
    monkeypatch.setattr(scene.manager,'_build_backup_payload',lambda *args:(_ for _ in ()).throw(AssertionError('second capture')))
    result=local.finalize_local_update(scene.manager)
    assert result['sourcepoint']=='before-local-code-update' and result['retention']['status']=='one_complete'
    assert local.verified_at(scene.manager.root,scene.manager.state)==proof['authenticated_recovery']['source_time']
    assert local.finalize_local_update(scene.manager)==result
    assert len(list((scene.manager.root/'backups').glob('*.tmbackup')))==count==1 and scene.events==[]


def test_corrupt_cipher_or_owner_proof_cannot_publish_local_success(local_case):
    scene=local_case;proof=cutover(scene);cipher=scene.manager.root/'backups'/proof['backup_filename']
    cipher.write_bytes(b'changed encrypted source')
    with pytest.raises(ValueError,match='size/hash'):local.finalize_local_update(scene.manager)
    assert 'last_backup_local' not in scene.manager.state


def test_manual_and_nightly_ignore_stale_nas_setting_under_explicit_policy(local_case,monkeypatch,host_store):
    scene=local_case;cutover(scene);local.finalize_local_update(scene.manager)
    from desktop_assistant import mac_smb_stream_target as stream
    monkeypatch.setattr(stream,'prepare',lambda *args:(_ for _ in ()).throw(AssertionError('NAS prepare')))
    monkeypatch.setattr(mac_backup_storage,'prepare_destination',lambda *args:(_ for _ in ()).throw(AssertionError('NAS destination')))
    from desktop_assistant import credential_store,credential_transfer
    monkeypatch.setattr(credential_transfer,'unprotect',credential_store.unprotect)
    settings={'nas':'tianming-smb-stream:'+'f'*64,'protected_password':credential_store.protect(PASSWORD,'backup')}
    write_json(scene.manager.root/'preferences.json',settings)
    backup=scene.manager.backup(PASSWORD,Path('tianming-smb-stream:'+'f'*64))
    assert backup.parent==scene.manager.root/'backups' and scene.events==['stop','start']
    assert len(list((scene.manager.root/'backups').glob('*.tmbackup')))==1
    monkeypatch.setattr(nightly,'unprotect_backup',lambda reference:PASSWORD)
    result=nightly.run_once(scene.manager,now=datetime.now(nightly.CN)+timedelta(days=2))
    assert result['status']=='verified' and scene.events==['stop','start','stop','start']
    assert len(list((scene.manager.root/'backups').glob('*.tmbackup')))==1


def test_manage_destination_local_even_with_old_smb_metadata(local_case,monkeypatch):
    scene=local_case;monkeypatch.setattr(mac_service_manage,'ROOT',scene.manager.root)
    from desktop_assistant import mac_smb_stream_target as stream
    monkeypatch.setattr(stream,'approved_destination',lambda *args:(_ for _ in ()).throw(AssertionError('NAS destination')))
    assert mac_service_manage.approved_destination({})==scene.manager.root/'backups'


def test_protected_migration_source_does_not_poison_next_local_backup(local_case):
    scene=local_case;root=scene.manager.root
    protected=root/'backups'/'20260101-010101-aaaaaaaa.tmbackup'
    protected.write_bytes(b'protected migration recovery source');protected.chmod(0o600)
    state=scene.manager.state;state['migration_backup_sha256']=sha(protected)
    write_json(root/'state.json',state)
    proof=cutover(scene);first=local.finalize_local_update(scene.manager)
    assert first['retention']=={'status':'one_complete','removed':[],'retained':[],'failures':[],
        'protected':[{'name':protected.name,'reasons':['migration_source']}]}
    previous=root/'backups'/proof['backup_filename'];assert previous.exists()
    with scene.manager.lock():
        current=local.backup_locked(scene.manager,PASSWORD)
    assert current.exists() and protected.exists() and not previous.exists()
    assert {path.name for path in (root/'backups').glob('*.tmbackup')}=={protected.name,current.name}


def test_previous_retained_receipt_is_reconciled_before_fresh_capture(local_case):
    scene=local_case;root=scene.manager.root
    protected=root/'backups'/'20260101-010101-bbbbbbbb.tmbackup'
    protected.write_bytes(b'protected migration recovery source');protected.chmod(0o600)
    state=scene.manager.state;state['migration_backup_sha256']=sha(protected)
    write_json(root/'state.json',state)
    proof=cutover(scene);local.finalize_local_update(scene.manager)
    previous=root/'backups'/proof['backup_filename']
    stale=root/'backups'/'20260101-020202-cccccccc.tmbackup'
    stale.write_bytes(b'unreferenced retired local backup');stale.chmod(0o600)
    receipt=local._receipt_path(root,proof['backup_sha256']);value=read_json(receipt)
    value['retention']={'status':'retained','removed':[],
        'retained':[protected.name,stale.name]}
    write_json(receipt,value)
    state=scene.manager.state;state['backup_error']=local.RETENTION_ATTENTION
    write_json(root/'state.json',state)
    with scene.manager.lock():
        current=local.backup_locked(scene.manager,PASSWORD)
    assert current.exists() and protected.exists() and not stale.exists() and not previous.exists()
    assert scene.manager.state.get('backup_error') is None


def test_real_cleanup_guard_failure_still_blocks_next_capture(local_case,monkeypatch):
    scene=local_case;root=scene.manager.root
    stale=root/'backups'/'20260101-030303-dddddddd.tmbackup'
    stale.write_bytes(b'backup still used by a process');stale.chmod(0o600)
    from desktop_assistant import cleanup
    monkeypatch.setattr(cleanup,'assert_idle',
        lambda path,**_:(_ for _ in ()).throw(ValueError('目录仍被运行进程引用')) if path==stale else None)
    proof=cutover(scene);result=local.finalize_local_update(scene.manager)
    assert result['retention']['status']=='retained'
    assert result['retention']['failures']==[{'name':stale.name,'reason':'目录仍被运行进程引用'}]
    before={path.name for path in (root/'backups').glob('*.tmbackup')}
    with scene.manager.lock(),pytest.raises(ValueError,match='previous local retention needs owner closure'):
        local.backup_locked(scene.manager,PASSWORD)
    assert {path.name for path in (root/'backups').glob('*.tmbackup')}==before
    assert (root/'backups'/proof['backup_filename']).exists() and stale.exists()
