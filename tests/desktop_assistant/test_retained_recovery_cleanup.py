import json,os,shutil
from pathlib import Path
from types import SimpleNamespace
import pytest
from desktop_assistant import retained_recovery_cleanup as close
from desktop_assistant.storage import sha,database_info,write_json
from tests.desktop_assistant.test_recovery import RecoveryTests


@pytest.fixture
def scene(monkeypatch):
    f=RecoveryTests();f.setUp();root=f.manager.root;owner={'uid':os.getuid(),'gid':os.getgid()}
    job=root/'staging'/('local-backup-'+'a'*32);target=job/'independent-recovery';(target/'shared/data').mkdir(parents=True)
    shutil.copyfile(root/'shared/data/carton_erp.sqlite3',target/'shared/data/carton_erp.sqlite3')
    current=f.manager.state['current'];shutil.copyfile(root/'packages'/(current+'.zip'),target/'release.zip')
    files={p.relative_to(target).as_posix():sha(p) for p in target.rglob('*') if p.is_file()}
    manifest={'files':files};write_json(target/'manifest.json',manifest)
    cipher=root/'backups'/'20261016-010101-aaaaaaaa.tmbackup';cipher.write_bytes(b'synthetic encrypted bytes');identity=sha(cipher)
    proof={'backup_sha256':identity,'job_id':job.name,'backup_filename':cipher.name,'authenticated_recovery':{'manifest_sha256':__import__('hashlib').sha256(close.canonical(manifest)).hexdigest(),'database':database_info(target/'shared/data/carton_erp.sqlite3')}}
    write_json(job/'verified.json',proof);receipt_name='local-backup-'+identity+'.json'
    receipt=dict(type='tianming.local-only-backup-receipt.v1',sha256=identity,authenticated_recovery=proof['authenticated_recovery'],path=str(cipher),verified_at='2026-10-16T00:00:00+00:00')
    write_json(root/'control'/receipt_name,receipt)
    state=f.manager.state;state.update(current=current,operation='update',manual_stop=False,backup_work_retained=[str(target)],last_backup=str(cipher),last_backup_at=receipt['verified_at'],last_backup_local={'receipt':receipt_name,'sha256':identity,'source_time':receipt['verified_at']});write_json(root/'state.json',state)
    for p in (root/'state.json',job/'verified.json',root/'control'/receipt_name):p.chmod(0o600)
    monkeypatch.setattr('desktop_assistant.cleanup.assert_idle',lambda p:None)
    s=SimpleNamespace(manager=f.manager,root=root,owner=owner,current=current,target=target,job=job,cipher=cipher,proof=proof,receipt=root/'control'/receipt_name,verify_calls=0)
    def verify(m,p,o):
        s.verify_calls+=1;assert m is s.manager and p==s.proof and o==s.owner;return s.cipher
    s.verify=verify
    yield s
    f.tearDown()


def run(s):return close.close_locked(s.manager,s.owner,s.current,verify_cipher=s.verify,healthy=lambda:True)


def test_verified_payload_removed_backup_and_business_preserved_and_replay_idle(scene):
    s=scene;before=sha(s.root/'shared/data/carton_erp.sqlite3');cipher=sha(s.cipher)
    r=run(s);assert r['status']=='closed' and not s.target.exists() and s.verify_calls==1
    assert 'backup_work_retained' not in s.manager.state and sha(s.cipher)==cipher
    assert sha(s.root/'shared/data/carton_erp.sqlite3')==before
    assert run(s)['status']=='already_closed'


@pytest.mark.parametrize('problem',['manual_stop','operation','current','proof','receipt','payload','escape','link','active'])
def test_unknown_or_changed_owner_never_clears_flag(scene,problem,monkeypatch):
    s=scene
    if problem in ('manual_stop','operation','current','escape'):
        state=s.manager.state
        if problem=='escape':state['backup_work_retained']=[str(s.root/'shared')]
        else:state[problem]={'manual_stop':True,'operation':'migration_running','current':'b'*64}[problem]
        write_json(s.root/'state.json',state)
    elif problem=='proof':write_json(s.job/'verified.json',{**s.proof,'job_id':'wrong'})
    elif problem=='receipt':write_json(s.receipt,{**json.loads(s.receipt.read_text()),'sha256':'b'*64})
    elif problem=='payload':(s.target/'release.zip').write_bytes(b'changed')
    elif problem=='link':(s.target/'link').symlink_to(s.root/'shared')
    elif problem=='active':monkeypatch.setattr('desktop_assistant.cleanup.assert_idle',lambda p:(_ for _ in ()).throw(ValueError('active')))
    with pytest.raises((ValueError,OSError)):run(s)
    assert s.target.exists() and s.manager.state.get('backup_work_retained')


def test_interrupted_metadata_publish_can_finish_only_its_recorded_removal(scene,monkeypatch):
    s=scene;original=close.owned_write
    def fail_state(path,*a):
        if path==s.root/'state.json':raise OSError('injected state publication failure')
        return original(path,*a)
    monkeypatch.setattr(close,'owned_write',fail_state)
    with pytest.raises(OSError):run(s)
    assert not s.target.exists() and s.manager.state['backup_work_retained']
    monkeypatch.setattr(close,'owned_write',original);assert run(s)['status']=='closed'


def test_unrecorded_missing_directory_is_not_success(scene):
    shutil.rmtree(scene.target)
    with pytest.raises(OSError):run(scene)
    assert scene.manager.state['backup_work_retained']


def test_partial_deletion_resumes_only_the_authenticated_manifest_subset(scene,monkeypatch):
    s=scene;original=close.remove_owned_tree
    def interrupted(target,parent):
        (target/'manifest.json').unlink();(target/'release.zip').unlink()
        raise OSError('injected partial cleanup interruption')
    monkeypatch.setattr(close,'remove_owned_tree',interrupted)
    with pytest.raises(OSError):run(s)
    assert s.target.exists() and s.manager.state['backup_work_retained']
    monkeypatch.setattr(close,'remove_owned_tree',original)
    assert run(s)['status']=='closed' and not s.target.exists()
