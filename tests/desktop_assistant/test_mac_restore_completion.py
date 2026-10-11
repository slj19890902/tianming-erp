"""Real interrupted synthetic restore, crypto, SQL, files and comparisons.

Only OS identity/root ownership/process presence and secret storage are adapted.
No production data, privileged installation, service execution or real secrets.
"""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from desktop_assistant import mac_restore_completion as completion, mac_service_restore as restore
from desktop_assistant import mac_service_manage as manage, credential_transfer, credential_store
from desktop_assistant import mac_release_publish as publication, credential_restore
from desktop_assistant.manager import Manager
from desktop_assistant.storage import decrypt_file, extract_verified, pack_recovery, encrypt_file, read_json, sha
from tests.desktop_assistant import test_mac_service_restore as fixture_module
from tests.desktop_assistant.test_mac_service_restore import case, recovery_case, host_store, PASSWORD

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='Mac protected FD and dedicated UID contract')


@pytest.fixture(params=['embedded','external'])
def partial(case, monkeypatch, request):
    c = case.fixture
    raw=c.root/'original.zip';decrypt_file(case.backup,raw,PASSWORD)
    payload=c.root/'original-payload';metadata=extract_verified(raw,payload)
    record=credential_transfer.unseal((payload/'credentials.tmencrypted').read_bytes(),PASSWORD)
    record['source_platform']='win32';record['entries']['backup']['value']=PASSWORD
    external=request.param=='external'
    members={n:payload/n for n in metadata['files'] if not n.startswith('shared/')}
    if external:
        metadata.pop('credential_transfer');members.pop('credentials.tmencrypted')
    else:(payload/'credentials.tmencrypted').write_bytes(credential_transfer.seal(record,PASSWORD))
    final_raw=c.root/'complete.zip';final=c.root/'complete.tmbackup'
    pack_recovery(payload/'shared',members,final_raw,metadata);encrypt_file(final_raw,final,PASSWORD)
    if external:record['bound_backup_sha256']=sha(final)
    original_pack=fixture_module.pack_tree
    def pack(source,destination,meta,key):
        if meta.get('runtime_platform')=='macos-arm64':
            meta=deepcopy(meta);meta['runtime_capabilities'][completion.CAPABILITY]=1
            meta['runtime_capabilities'][completion.RESUME_CAPABILITY]=1
            meta['runtime_capabilities'][completion.REAUTH_CAPABILITY]=1
        return original_pack(source,destination,meta,key)
    monkeypatch.setattr(fixture_module,'pack_tree',pack)
    restore_files=publication.SERVICE_RESTORE_FILES
    monkeypatch.setattr(publication,'SERVICE_RESTORE_FILES',restore_files|{completion.ENTRY,completion.MODULE})
    x=case.install(final)
    monkeypatch.setattr(publication,'SERVICE_RESTORE_FILES',restore_files)
    if external:
        cipher=x.attempt/'credentials.tmencrypted';cipher.write_bytes(credential_transfer.seal(record,PASSWORD));cipher.chmod(0o440)
        x.args.credentials_sha256=sha(cipher);x.args.backup_password_from_transfer=True
    original_resolve=Manager._published_release
    def late_failure(manager,identity,*,kind='server'):
        if kind=='server' and (manager.root/'shared/data/carton_erp.sqlite3').exists():
            raise PermissionError('synthetic published link denied after promotion')
        return original_resolve(manager,identity,kind=kind)
    with monkeypatch.context() as stop:
        stop.setattr(Manager,'_published_release',late_failure)
        with pytest.raises(PermissionError,match='after promotion'):x.run()
    assert not (x.root/'state.json').exists()
    assert not (x.home/('service-restore-'+x.args.input_attempt+'.json')).exists()
    job=next((x.root/'staging').iterdir());assert not (job/'payload/shared').exists()
    monkeypatch.setattr(manage,'BASE',x.base);monkeypatch.setattr(manage,'ROOT',x.root)
    monkeypatch.setattr(completion,'__file__',str(x.slot/'migration'/completion.MODULE))
    # OS foreground identity is adapted here; real private PTYs are tested below.
    monkeypatch.setattr(completion.os,'tcgetpgrp',lambda fd:os.getpgrp())
    monkeypatch.setattr(credential_transfer,'unprotect',credential_store.unprotect)
    def forbidden(*a,**kw):raise AssertionError('completion cannot restore, rewrap, start, repair or migrate')
    monkeypatch.setattr(Manager,'restore',forbidden)
    monkeypatch.setattr(credential_restore,'prepare',forbidden)
    monkeypatch.setattr(credential_store,'protect',forbidden)
    args=SimpleNamespace(**vars(x.args),execution_package=x.id,context_sha256=sha(x.base/'keychain-context.json'),
                         staging_identity_sha256=hashlib.sha256(job.name.encode()).hexdigest())
    def run(prompt=lambda _:PASSWORD):
        return completion.run(args,script=x.slot/'migration'/completion.ENTRY,executable=sys.executable,prompt=prompt)
    return SimpleNamespace(case=case,x=x,args=args,job=job,run=run,record=record,source=payload,external=external)


def protected(p):
    roots={'shared':p.x.root/'shared','prior':p.job}
    return {label+'/'+str(path.relative_to(root)):(sha(path),path.stat().st_mode)
            for label,root in roots.items() for path in root.rglob('*') if path.is_file()}


def test_completes_promoted_checkpoint_without_restoring_or_changing_existing_data(partial):
    p=partial;before=protected(p);secrets=dict(p.case.secrets)
    result=p.run();state=read_json(p.x.root/'state.json')
    assert result['started'] is False and result['factory_cutover'] is False
    assert state['current']==p.x.id and state['manual_stop'] is True and state['operation']=='restore'
    assert state['restored_backup_sha256']==p.args.backup_sha256
    assert state['native_restore_binding']['context_sha256']==p.args.context_sha256
    assert result['credentials_sha256']==p.args.credentials_sha256
    assert result['result']['credentials']['restored']==['backup','mailbox','openai']
    assert protected(p)==before and p.case.secrets==secrets
    assert 'synthetic-secret' not in json.dumps(result) and PASSWORD not in json.dumps(result)
    for path in (p.x.root/'state.json',p.x.home/('service-restore-'+p.args.input_attempt+'.json')):
        assert path.stat().st_mode & 0o777==0o600
    assert (p.x.home/('restore-completion-'+p.args.input_attempt+'.json')).is_file()
    assert (p.x.root/'staging'/('completion-'+p.args.input_attempt)/'payload/shared/data/carton_erp.sqlite3').is_file()
    state_bytes=(p.x.root/'state.json').read_bytes()
    with pytest.raises(ValueError,match='覆盖重试'):p.run()
    assert (p.x.root/'state.json').read_bytes()==state_bytes and protected(p)==before


@pytest.mark.parametrize('fault',[
    'business-row','mail-position','mail-version','attachment','environment','config-option','config-nas',
    'secret-mailbox','secret-backup','secret-openai','input-hash','input-mode','context','stage-name','stage-zip',
    'stage-manifest','stage-config','source-cache','native-cache','cache-tree','state','pending','receipt','attempt',
    'extra-root','extra-control','extra-stage','extra-backup','missing-lock','lock-content','wal','hardlink','symlink',
    'shared-mode','no-tty','wrong-module','extra-groups','wrong-password','server-link',
])
def test_mismatch_refuses_and_preserves_prior_data_and_secrets(partial,monkeypatch,fault):
    p=partial;x=p.x;db=x.root/'shared/data/carton_erp.sqlite3';prompt=lambda _:PASSWORD
    if fault in {'business-row','mail-position','mail-version'}:
        with closing(sqlite3.connect(db)) as conn:
            conn.execute({'business-row':"UPDATE customers SET note='changed'",'mail-position':"UPDATE email_intake_settings SET uid='changed'",
                          'mail-version':'UPDATE email_intake_settings SET version=10'}[fault]);conn.commit()
    elif fault=='attachment':(x.root/'shared/data/drawing.pdf').write_bytes(b'changed')
    elif fault=='environment':
        path=x.root/'shared/environment.json';v=read_json(path);v['ERP_PORT']='19009';path.write_text(json.dumps(v))
    elif fault.startswith('config-'):
        path=x.root/'preferences.json';v=read_json(path);v['nas' if fault=='config-nas' else 'extra']='changed';path.write_text(json.dumps(v))
    elif fault.startswith('secret-'):
        purpose=fault.removeprefix('secret-');key=next(k for k in p.case.secrets if purpose in k);p.case.secrets[key]='changed'
    elif fault=='input-hash':p.args.backup_sha256='f'*64
    elif fault=='input-mode':x.backup.chmod(0o640)
    elif fault=='context':p.args.context_sha256='f'*64
    elif fault=='stage-name':p.args.staging_identity_sha256='f'*64
    elif fault=='stage-zip':(p.job/'recovery.zip').write_bytes(b'changed')
    elif fault=='stage-manifest':
        path=p.job/'payload/manifest.json';v=read_json(path);v['created']='changed';path.write_text(json.dumps(v))
    elif fault=='stage-config':(p.job/'payload/credential-config/preferences.json').write_text('{}')
    elif fault in {'source-cache','native-cache'}:
        identity=x.id if fault=='native-cache' else read_json(p.job/'payload/manifest.json')['release']
        (x.root/'packages'/(identity+'.zip')).write_bytes(b'changed')
    elif fault=='cache-tree':(x.root/'releases'/x.id/'app/models/fixture.py').write_bytes(b'changed')
    elif fault in {'state','pending'}:(x.root/('state.json' if fault=='state' else 'state.json.pending')).write_bytes(b'keep')
    elif fault in {'receipt','attempt'}:
        name=('service-restore-' if fault=='receipt' else 'restore-completion-')+p.args.input_attempt+'.json'
        (x.home/name).write_bytes(b'keep')
    elif fault.startswith('extra-') and fault!='extra-groups':
        directory={'extra-root':x.root,'extra-control':x.root/'control','extra-stage':x.root/'staging','extra-backup':x.root/'backups'}[fault]
        (directory/'keep').write_bytes(b'keep')
    elif fault=='missing-lock':(x.home/'service-restore.lock').rename(x.home/'preserved.lock')
    elif fault=='lock-content':(x.root/'control/operation.lock').write_bytes(b'1')
    elif fault=='wal':Path(str(db)+'-wal').touch(mode=0o600)
    elif fault=='hardlink':os.link(x.root/'shared/data/drawing.pdf',x.root/'shared/data/hardlink.pdf')
    elif fault=='symlink':(x.root/'shared/data/link.pdf').symlink_to(x.root/'shared/data/drawing.pdf')
    elif fault=='shared-mode':(x.root/'shared/data').chmod(0o755)
    elif fault=='no-tty':monkeypatch.setattr(restore,'_terminal',lambda:False)
    elif fault=='wrong-module':monkeypatch.setattr(completion,'__file__',str(x.base/'unapproved.py'))
    elif fault=='extra-groups':monkeypatch.setattr(manage.boot,'_actual',lambda:manage.boot._expected(499,499)|{'kernel_groups':[499,61]})
    elif fault=='wrong-password':prompt=lambda _:'wrong-synthetic-password'
    elif fault=='server-link':(x.slot/'server/data').unlink();(x.slot/'server/data').symlink_to(x.root/'shared/logs')
    else:raise AssertionError(fault)
    before=protected(p);secrets=dict(p.case.secrets)
    with pytest.raises(Exception) as caught:p.run(prompt)
    assert not isinstance(caught.value,AssertionError)
    assert protected(p)==before and p.case.secrets==secrets
    if fault!='state':assert not (x.root/'state.json').exists()
    if fault!='receipt':assert not (x.home/('service-restore-'+p.args.input_attempt+'.json')).exists()
    assert PASSWORD not in str(caught.value) and 'synthetic-secret' not in str(caught.value)


@pytest.mark.parametrize('phase',['authentication','after-state'])
def test_failure_retains_exclusive_attempt_and_never_overwrites_on_retry(partial,monkeypatch,phase):
    p=partial;before=protected(p)
    if phase=='authentication':
        def fail(*a,**kw):raise ValueError('synthetic authentication interrupted')
        monkeypatch.setattr(completion,'_decrypt_retained',fail)
    else:
        receipt=restore._receipt
        def fail(parent,name,*a,**kw):
            if name.startswith('service-restore-'):raise OSError('synthetic receipt IO interrupted')
            return receipt(parent,name,*a,**kw)
        monkeypatch.setattr(restore,'_receipt',fail)
    with pytest.raises((ValueError,OSError)):p.run()
    assert (p.x.home/('restore-completion-'+p.args.input_attempt+'.json')).exists()
    assert (p.x.root/'state.json').exists()==(phase=='after-state')
    assert protected(p)==before
    with pytest.raises(ValueError,match='覆盖重试'):p.run()
    assert protected(p)==before


@pytest.mark.parametrize('change',['missing','bool','entry','module'])
def test_completion_capability_cannot_be_inferred_from_ordinary_restore(partial,monkeypatch,change):
    original=completion.published.resolve
    def resolve(*a,**kw):
        tree,manifest=original(*a,**kw);manifest=deepcopy(manifest)
        if change=='missing':manifest['runtime_capabilities'].pop(completion.CAPABILITY)
        elif change=='bool':manifest['runtime_capabilities'][completion.CAPABILITY]=True
        else:manifest['files'].pop(completion.ENTRY if change=='entry' else completion.MODULE)
        return tree,manifest
    monkeypatch.setattr(completion.published,'resolve',resolve)
    with pytest.raises(ValueError,match='有限恢复完成协议'):partial.run()
    assert not (partial.x.home/('restore-completion-'+partial.args.input_attempt+'.json')).exists()


@pytest.mark.parametrize('resume',[False,True])
def test_approved_new_execution_package_completes_the_original_restore_target(partial,monkeypatch,resume):
    import shutil
    from desktop_assistant.storage import pack_tree, write_json
    p=partial;x=p.x
    if resume:old=interrupted_before_password(p)
    source=p.case.fixture.root/'completion-program'
    shutil.copytree(x.slot/'migration',source)
    (source/'manifest.json').unlink()  # Generated synthetic package metadata only.
    (source/completion.MODULE).write_bytes(b'# distinct signed synthetic completion program')
    package=p.case.fixture.root/'completion-program.zip'
    meta={k:v for k,v in x.manifest.items() if k!='files'}
    meta['version']='completion-only'
    pack_tree(source,package,meta,p.case.fixture.key);identity=sha(package)
    slot=x.base/'published'/identity;slot.mkdir()
    shutil.copyfile(package,slot/'package.zip')
    for kind in ('migration','server'):extract_verified(package,slot/kind,p.case.fixture.public)
    for relative,name in publication.MANAGED.items():
        link=slot/'server'/relative;link.parent.mkdir(parents=True,exist_ok=True);link.symlink_to(x.root/'shared'/name)
    registry=read_json(x.base/'published.json');registry['packages']=sorted([x.id,identity]);write_json(x.base/'published.json',registry)
    p.args.execution_package=identity
    monkeypatch.setattr(completion,'__file__',str(slot/'migration'/completion.MODULE))
    monkeypatch.setattr(sys,'executable',str(slot/'migration/runtime/bin/python3.12'))
    result=completion.run(p.args,script=slot/'migration'/completion.ENTRY,executable=sys.executable,prompt=lambda _:PASSWORD)
    assert result['package_sha256']==x.id!=identity
    assert result['result']['version']=='native'
    assert read_json(x.root/'state.json')['current']==x.id
    assert not (x.root/'packages'/(identity+'.zip')).exists()
    if resume:
        assert old.read_bytes()==completion.manage.boot.canonical(completion._attempt_record(p.args,x.id))
        assert read_json(x.home/('restore-completion-resume-'+p.args.input_attempt+'.json'))['execution_package']==identity


@pytest.mark.parametrize('change',['business','secret','state','staging'])
def test_changes_during_final_comparison_cannot_publish_completion(partial,monkeypatch,change):
    p=partial;original=completion._same_credentials;done=False
    def changed(*args):
        nonlocal done
        original(*args)
        if done:return
        done=True
        if change=='business':
            with closing(sqlite3.connect(p.x.root/'shared/data/carton_erp.sqlite3')) as conn:
                conn.execute("UPDATE customers SET note='late change'");conn.commit()
        elif change=='secret':
            key=next(k for k in p.case.secrets if 'mailbox' in k);p.case.secrets[key]='late change'
        elif change=='state':(p.x.root/'state.json').write_text('{"keep":"unexpected"}')
        else:(p.x.root/'staging/keep').mkdir()
    monkeypatch.setattr(completion,'_same_credentials',changed)
    with pytest.raises((ValueError,FileExistsError)):p.run()
    assert not (p.x.home/('service-restore-'+p.args.input_attempt+'.json')).exists()
    # A late secret change may be detected after the stopped state was saved;
    # the state and attempt remain, never turn that partial result into success.
    if change not in {'state','secret'}:assert not (p.x.root/'state.json').exists()


def test_existing_maintenance_lock_competition_refuses_before_attempt(partial):
    import fcntl
    p=partial
    with (p.x.root/'control/operation.lock').open('rb') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(ValueError):p.run()
    assert not (p.x.home/('restore-completion-'+p.args.input_attempt+'.json')).exists()


def test_bad_gcm_tag_never_writes_completed_state(partial):
    p=partial;path=p.x.backup;data=bytearray(path.read_bytes());data[-1]^=1
    path.chmod(0o600);path.write_bytes(data);path.chmod(0o440);p.args.backup_sha256=sha(path)
    with pytest.raises(ValueError):p.run()
    assert not (p.x.root/'state.json').exists()
    if not p.external:
        retained=p.x.root/'staging'/('completion-'+p.args.input_attempt)/'recovery.zip'
        assert retained.is_file() and retained.stat().st_size > 0
        assert retained.stat().st_mode & 0o777==0o600
        assert not (retained.parent/'payload').exists()


def test_entry_rejects_ordinary_actual_user_before_importing_erp(tmp_path):
    import subprocess
    command=[fixture_module.TEST_PYTHON,'-I','-B',str(Path(__file__).resolve().parents[2]/completion.ENTRY)]
    result=subprocess.run(command,capture_output=True,text=True,cwd=tmp_path)
    assert result.returncode!=0 and '有限恢复完成未确认' in result.stderr
    assert not list(tmp_path.iterdir())


def interrupted_before_password(p):
    import getpass
    def no_prompt(_):raise getpass.GetPassWarning('synthetic foreground interruption before prompt')
    with pytest.raises(getpass.GetPassWarning):p.run(no_prompt)
    path=p.x.home/('restore-completion-'+p.args.input_attempt+'.json')
    assert path.is_file() and not (p.x.root/'staging'/('completion-'+p.args.input_attempt)).exists()
    p.args.resume_before_decryption_sha256=sha(path)
    return path


def test_explicit_resume_authenticates_again_and_preserves_original_attempt(partial):
    p=partial;old=interrupted_before_password(p);raw=old.read_bytes();info=restore._fingerprint(old.stat())
    before=protected(p);secrets=dict(p.case.secrets);prompts=[]
    result=p.run(lambda message:prompts.append(message) or PASSWORD)
    assert len(prompts)==1 and result['manual_stop'] and not result['started']
    assert old.read_bytes()==raw and restore._fingerprint(old.stat())==info
    assert protected(p)==before and p.case.secrets==secrets
    resume=p.x.home/('restore-completion-resume-'+p.args.input_attempt+'.json')
    assert resume.stat().st_mode & 0o777==0o600
    record=read_json(resume)
    assert record['original_attempt_sha256']==sha(old)
    assert record['credentials_sha256']==p.args.credentials_sha256
    with pytest.raises(ValueError,match='覆盖重试'):p.run()


@pytest.mark.parametrize('fault',[
    'wrong-hash','invalid-hash','missing','mode','hardlink','symlink','input','target','backup','context',
    'extra-field','noncanonical','old-execution','old-capability','new-capability','bool-capability',
    'state','pending','receipt','resume','decrypt-slot',
])
def test_resume_requires_exact_protected_pre_decryption_checkpoint(partial,monkeypatch,fault):
    p=partial;old=interrupted_before_password(p)
    if fault=='wrong-hash':p.args.resume_before_decryption_sha256='f'*64
    elif fault=='invalid-hash':p.args.resume_before_decryption_sha256=True
    elif fault=='missing':old.rename(old.with_suffix('.preserved'))
    elif fault=='mode':old.chmod(0o644)
    elif fault=='hardlink':os.link(old,old.with_suffix('.hardlink'))
    elif fault=='symlink':
        saved=old.with_suffix('.preserved');old.rename(saved);old.symlink_to(saved)
    elif fault in {'input','target','backup','context','extra-field','noncanonical','old-execution'}:
        record=read_json(old)
        if fault=='extra-field':record['extra']=True
        elif fault!='noncanonical':
            key={'input':'input_attempt','target':'restore_package','backup':'backup_sha256',
                 'context':'context_sha256','old-execution':'execution_package'}[fault]
            record[key]='f'*(32 if fault=='input' else 64)
        raw=completion.manage.boot.canonical(record)
        old.write_bytes(raw+b'\n' if fault=='noncanonical' else raw)
        p.args.resume_before_decryption_sha256=sha(old)
    elif fault in {'old-capability','new-capability','bool-capability'}:
        resolve=completion.published.resolve;calls=0
        def changed(*a,**kw):
            nonlocal calls
            tree,manifest=resolve(*a,**kw);calls+=1;manifest=deepcopy(manifest)
            if fault=='old-capability' and calls==3:manifest['runtime_capabilities'].pop(completion.CAPABILITY)
            elif fault=='new-capability':manifest['runtime_capabilities'].pop(completion.RESUME_CAPABILITY)
            elif fault=='bool-capability':manifest['runtime_capabilities'][completion.RESUME_CAPABILITY]=True
            return tree,manifest
        monkeypatch.setattr(completion.published,'resolve',changed)
    elif fault in {'state','pending'}:(p.x.root/('state.json' if fault=='state' else 'state.json.pending')).write_bytes(b'keep')
    elif fault in {'receipt','resume'}:
        prefix='service-restore-' if fault=='receipt' else 'restore-completion-resume-'
        (p.x.home/(prefix+p.args.input_attempt+'.json')).write_bytes(b'keep')
    elif fault=='decrypt-slot':(p.x.root/'staging'/('completion-'+p.args.input_attempt)).mkdir()
    else:raise AssertionError(fault)
    before=protected(p);secrets=dict(p.case.secrets)
    def no_input(_):raise AssertionError('invalid checkpoint must be rejected before hidden input')
    with pytest.raises(Exception) as caught:p.run(no_input)
    assert not isinstance(caught.value,AssertionError)
    assert protected(p)==before and p.case.secrets==secrets
    if fault!='resume':assert not (p.x.home/('restore-completion-resume-'+p.args.input_attempt+'.json')).exists()


@pytest.mark.parametrize('change',['original-bytes','original-name','resume-bytes','authentication'])
def test_resume_rechecks_attempts_and_never_allows_a_second_resume(partial,change):
    p=partial;old=interrupted_before_password(p);before=protected(p)
    def prompt(_):
        if change=='original-bytes':old.write_bytes(old.read_bytes()+b' ')
        elif change=='original-name':old.rename(old.with_suffix('.preserved'));old.write_bytes(b'changed')
        elif change=='resume-bytes':
            path=p.x.home/('restore-completion-resume-'+p.args.input_attempt+'.json');path.write_bytes(b'changed')
        else:raise ValueError('synthetic authentication interruption')
        return PASSWORD
    with pytest.raises(ValueError):p.run(prompt)
    assert not (p.x.root/'state.json').exists() and protected(p)==before
    assert (p.x.home/('restore-completion-resume-'+p.args.input_attempt+'.json')).exists()
    with pytest.raises(ValueError,match='覆盖重试'):p.run()


@pytest.mark.parametrize('point',['entry','before-attempt','before-prompt'])
def test_foreground_failure_does_not_read_password_or_create_new_attempt(partial,monkeypatch,point):
    p=partial;calls=0
    def foreground(fd):
        nonlocal calls
        calls+=1
        return os.getpgrp()+(calls=={'entry':1,'before-attempt':2,'before-prompt':3}[point])
    monkeypatch.setattr(completion.os,'tcgetpgrp',foreground)
    def no_input(_):raise AssertionError('background input is forbidden')
    with pytest.raises(ValueError,match='前台控制终端'):p.run(no_input)
    assert (p.x.home/('restore-completion-'+p.args.input_attempt+'.json')).exists()==(point=='before-prompt')
    assert len(list((p.x.root/'staging').iterdir()))==1
    assert not (p.x.root/'state.json').exists()


@pytest.mark.parametrize('background',[False,True])
def test_real_private_pty_foreground_gate_rejects_background_without_signal_stop(tmp_path,background):
    """A real private controlling terminal; no sudo or service credentials."""
    import subprocess
    child = r'''
import json,os,sys
sys.path.insert(0,sys.argv[1])
from desktop_assistant.mac_restore_completion import _foreground_terminal
try:
    _foreground_terminal()
    result={'accepted':True}
except ValueError:
    result={'accepted':False}
result['foreground']=os.getpgrp()==os.tcgetpgrp(0)
print(json.dumps(result),flush=True)
'''
    harness = r'''
import errno,json,os,pty,select,signal,sys,time
pid,master=pty.fork()
if pid==0:
    child=os.fork()
    if child==0:
        if sys.argv[1]=='background':os.setpgid(0,0)
        os.execv(sys.executable,[sys.executable,'-I','-B','-c',sys.argv[2],sys.argv[3]])
    _,status=os.waitpid(child,os.WUNTRACED)
    stopped=os.WIFSTOPPED(status)
    if stopped:os.kill(child,signal.SIGKILL);os.waitpid(child,0)
    print(json.dumps({'stopped':stopped,'exit':os.waitstatus_to_exitcode(status) if not stopped else None}),flush=True)
    os._exit(0)
data=bytearray();deadline=time.monotonic()+15
try:
    while time.monotonic()<deadline:
        if select.select([master],[],[],0.1)[0]:
            try:block=os.read(master,65536)
            except OSError as error:
                if error.errno==errno.EIO:break
                raise
            if not block:break
            data.extend(block)
    else:raise TimeoutError('synthetic terminal child did not finish')
finally:os.close(master)
_,status=os.waitpid(pid,0)
assert os.waitstatus_to_exitcode(status)==0
print(data.decode(),end='')
'''
    result=subprocess.run([fixture_module.TEST_PYTHON,'-I','-B','-c',harness,
        'background' if background else 'foreground',child,str(Path(__file__).resolve().parents[2])],
        cwd=tmp_path,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    records=[json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
    assert records==[{'accepted':not background,'foreground':not background},{'stopped':False,'exit':0}]
    assert not list(tmp_path.iterdir())


def failed_password_resume(p):
    assert p.external
    original=interrupted_before_password(p)
    with pytest.raises(ValueError):p.run(lambda _:'wrong-synthetic-password')
    previous=p.x.home/('restore-completion-resume-'+p.args.input_attempt+'.json')
    assert previous.is_file() and len(list((p.x.root/'staging').iterdir()))==1
    p.args.reauthenticate_before_decryption_sha256=sha(previous)
    return original,previous


def unchanged_records(paths):
    return [(path.read_bytes(),restore._fingerprint(path.stat())) for path in paths]


@pytest.mark.parametrize('partial',['external'],indirect=True)
@pytest.mark.parametrize('mistakes',[0,1,2])
def test_reauthentication_corrects_input_before_new_attempt_and_finishes_full_verification(partial,mistakes,capsys):
    p=partial;paths=failed_password_resume(p);old=unchanged_records(paths);before=protected(p)
    secrets=dict(p.case.secrets);prompts=[]
    new=p.x.home/('restore-completion-reauth-'+p.args.input_attempt+'.json')
    def prompt(message):
        prompts.append(message)
        assert not new.exists() and len(list((p.x.root/'staging').iterdir()))==1
        assert unchanged_records(paths)==old and protected(p)==before
        return 'wrong-synthetic-password' if len(prompts)<=mistakes else PASSWORD
    result=p.run(prompt)
    assert result['manual_stop'] and not result['started'] and not result['factory_cutover']
    assert len(prompts)==mistakes+1 and unchanged_records(paths)==old
    assert protected(p)==before and p.case.secrets==secrets
    record=read_json(new)
    assert record['type']=='tianming.restore-completion-reauth.v1'
    assert record['previous_resume_sha256']==sha(paths[1])
    assert record['original_attempt_sha256']==sha(paths[0])
    assert record['execution_package']==p.args.execution_package
    assert new.stat().st_mode & 0o777==0o600
    captured=capsys.readouterr()
    assert PASSWORD not in captured.out+captured.err and 'wrong-synthetic-password' not in captured.out+captured.err
    with pytest.raises(ValueError,match='覆盖重试'):p.run()


@pytest.mark.parametrize('partial',['external'],indirect=True)
def test_three_wrong_inputs_preserve_no_new_attempt_and_allow_later_correct_authentication(partial):
    p=partial;paths=failed_password_resume(p);old=unchanged_records(paths);before=protected(p)
    new=p.x.home/('restore-completion-reauth-'+p.args.input_attempt+'.json');prompts=[]
    with pytest.raises(ValueError,match='未创建新的恢复尝试'):
        p.run(lambda message:prompts.append(message) or 'wrong-synthetic-password')
    assert len(prompts)==3 and not new.exists() and unchanged_records(paths)==old and protected(p)==before
    assert len(list((p.x.root/'staging').iterdir()))==1 and not (p.x.root/'state.json').exists()
    assert p.run()['manual_stop'] and unchanged_records(paths)==old


@pytest.mark.parametrize('partial',['external'],indirect=True)
@pytest.mark.parametrize('error',[KeyboardInterrupt,EOFError])
def test_cancelled_hidden_input_does_not_consume_authentication_attempt(partial,error):
    p=partial;paths=failed_password_resume(p);old=unchanged_records(paths)
    def cancel(_):raise error()
    with pytest.raises(error):p.run(cancel)
    assert unchanged_records(paths)==old
    assert not (p.x.home/('restore-completion-reauth-'+p.args.input_attempt+'.json')).exists()
    assert len(list((p.x.root/'staging').iterdir()))==1


@pytest.mark.parametrize('partial',['external'],indirect=True)
@pytest.mark.parametrize('fault',[
    'wrong-sha','bool-sha','missing-original-sha','missing-credentials','missing-resume','resume-mode',
    'resume-hardlink','resume-symlink','original-sha','credentials-sha','publisher-sha','stage-sha',
    'input','target','backup','context','old-execution','extra','noncanonical','old-capability',
    'new-capability','bool-capability','state','pending','receipt','reauth-record','decryption-slot'])
def test_reauthentication_checkpoint_mismatch_never_asks_for_password(partial,monkeypatch,fault):
    p=partial;original,previous=failed_password_resume(p)
    if fault=='wrong-sha':p.args.reauthenticate_before_decryption_sha256='f'*64
    elif fault=='bool-sha':p.args.reauthenticate_before_decryption_sha256=True
    elif fault=='missing-original-sha':p.args.resume_before_decryption_sha256=None
    elif fault=='missing-credentials':p.args.credentials_sha256=None
    elif fault=='missing-resume':previous.rename(previous.with_suffix('.preserved'))
    elif fault=='resume-mode':previous.chmod(0o644)
    elif fault=='resume-hardlink':os.link(previous,previous.with_suffix('.hardlink'))
    elif fault=='resume-symlink':
        saved=previous.with_suffix('.preserved');previous.rename(saved);previous.symlink_to(saved)
    elif fault in {'original-sha','credentials-sha','publisher-sha','stage-sha','input','target','backup','context',
                   'old-execution','extra','noncanonical'}:
        record=read_json(previous)
        fields={'original-sha':'original_attempt_sha256','credentials-sha':'credentials_sha256',
            'publisher-sha':'publisher_sha256','stage-sha':'staging_identity_sha256','input':'input_attempt',
            'target':'restore_package','backup':'backup_sha256','context':'context_sha256','old-execution':'execution_package'}
        if fault=='extra':record['extra']=True
        elif fault!='noncanonical':record[fields[fault]]='f'*(32 if fault=='input' else 64)
        previous.write_bytes(manage.boot.canonical(record)+(b'\n' if fault=='noncanonical' else b''))
        p.args.reauthenticate_before_decryption_sha256=sha(previous)
    elif fault in {'old-capability','new-capability','bool-capability'}:
        resolve=completion.published.resolve;calls=0
        def changed(*a,**kw):
            nonlocal calls
            tree,manifest=resolve(*a,**kw);calls+=1;manifest=deepcopy(manifest)
            if fault=='old-capability' and calls==4:manifest['runtime_capabilities'].pop(completion.RESUME_CAPABILITY)
            elif fault=='new-capability':manifest['runtime_capabilities'].pop(completion.REAUTH_CAPABILITY)
            elif fault=='bool-capability':manifest['runtime_capabilities'][completion.REAUTH_CAPABILITY]=True
            return tree,manifest
        monkeypatch.setattr(completion.published,'resolve',changed)
    elif fault in {'state','pending'}:(p.x.root/('state.json' if fault=='state' else 'state.json.pending')).write_bytes(b'keep')
    elif fault in {'receipt','reauth-record'}:
        prefix='service-restore-' if fault=='receipt' else 'restore-completion-reauth-'
        (p.x.home/(prefix+p.args.input_attempt+'.json')).write_bytes(b'keep')
    else:(p.x.root/'staging'/('completion-'+p.args.input_attempt)).mkdir(mode=0o700)
    before=protected(p);secrets=dict(p.case.secrets)
    def forbidden(_):raise AssertionError('checkpoint mismatch must precede any password prompt')
    with pytest.raises(Exception) as caught:p.run(forbidden)
    assert not isinstance(caught.value,AssertionError)
    assert protected(p)==before and p.case.secrets==secrets
    if fault!='reauth-record':assert not (p.x.home/('restore-completion-reauth-'+p.args.input_attempt+'.json')).exists()


@pytest.mark.parametrize('partial',['external'],indirect=True)
@pytest.mark.parametrize('fault',['original','resume','shared','staging','foreground'])
def test_scene_change_during_reauthentication_refuses_before_new_attempt(partial,monkeypatch,fault):
    p=partial;original,previous=failed_password_resume(p)
    def prompt(_):
        if fault in {'original','resume'}:
            path=original if fault=='original' else previous;path.write_bytes(path.read_bytes()+b' ')
        elif fault=='shared':(p.x.root/'shared/data/drawing.pdf').write_bytes(b'changed')
        elif fault=='staging':(p.x.root/'staging/foreign').mkdir()
        else:monkeypatch.setattr(completion.os,'tcgetpgrp',lambda fd:os.getpgrp()+1)
        return PASSWORD
    with pytest.raises(ValueError):p.run(prompt)
    assert not (p.x.home/('restore-completion-reauth-'+p.args.input_attempt+'.json')).exists()
    assert not (p.x.root/'state.json').exists()


@pytest.mark.parametrize('partial',['external'],indirect=True)
def test_authenticated_decryption_failure_retains_attempt_and_refuses_later_reauthentication(partial,monkeypatch):
    p=partial;paths=failed_password_resume(p);old=unchanged_records(paths);before=protected(p)
    def fail(*a,**kw):raise ValueError('synthetic GCM verification failure')
    monkeypatch.setattr(completion,'_decrypt_retained',fail)
    with pytest.raises(ValueError,match='synthetic GCM'):p.run()
    new=p.x.home/('restore-completion-reauth-'+p.args.input_attempt+'.json')
    assert new.exists() and unchanged_records(paths)==old and protected(p)==before
    assert len(list((p.x.root/'staging').iterdir()))==2
    with pytest.raises(ValueError,match='覆盖重试'):p.run()


@pytest.mark.parametrize('partial',['external'],indirect=True)
def test_valid_transfer_for_other_backup_is_not_reprompted_or_recorded(partial,monkeypatch):
    p=partial;paths=failed_password_resume(p);old=unchanged_records(paths)
    unseal=completion.credentials.unseal;prompts=[]
    def wrong_binding(*a,**kw):
        record=unseal(*a,**kw);record['bound_backup_sha256']='f'*64;return record
    monkeypatch.setattr(completion.credentials,'unseal',wrong_binding)
    with pytest.raises(ValueError):p.run(lambda msg:prompts.append(msg) or PASSWORD)
    assert len(prompts)==1 and unchanged_records(paths)==old
    assert not (p.x.home/('restore-completion-reauth-'+p.args.input_attempt+'.json')).exists()


@pytest.mark.parametrize('partial',['external'],indirect=True)
def test_new_signed_execution_reauthenticates_older_approved_attempts(partial,monkeypatch):
    p=partial;paths=failed_password_resume(p);old=unchanged_records(paths)
    before=protected(p);secrets=dict(p.case.secrets)
    test_approved_new_execution_package_completes_the_original_restore_target(p,monkeypatch,False)
    assert unchanged_records(paths)==old and protected(p)==before and p.case.secrets==secrets
    record=read_json(p.x.home/('restore-completion-reauth-'+p.args.input_attempt+'.json'))
    assert record['execution_package']==p.args.execution_package!=p.x.id
    assert record['previous_resume_sha256']==sha(paths[1])
