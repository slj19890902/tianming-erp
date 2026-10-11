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


def test_approved_new_execution_package_completes_the_original_restore_target(partial,monkeypatch):
    import shutil
    from desktop_assistant.storage import pack_tree, write_json
    p=partial;x=p.x
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
