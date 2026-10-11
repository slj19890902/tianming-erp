"""Real archives/SQL/FDs with explicit OS identity and root ownership adapters.

No real service account, privileged directory, runtime execution or operational
keychain. The source Windows package needs a distinct signed schema authority.
"""
from contextlib import closing, contextmanager
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from desktop_assistant import mac_service_restore as m, mac_published_runtime as published
from desktop_assistant import mac_release_publish as publication, mac_system_service as service
from desktop_assistant import release_verification, restore_provenance as history
from desktop_assistant import delivery_dispatch_contract as dispatch, migration, credential_transfer, credential_store
from desktop_assistant.manager import Manager
from desktop_assistant.storage import extract_verified, pack_tree, read_json, sha, write_json, database_info
from tests.desktop_assistant.test_credential_restore import recovery_case, host_store
from tests.desktop_assistant.synthetic_storage import approved_local_storage
from tests.desktop_assistant.test_recovery import PASSWORD

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='dedicated Mac restore FD/identity contract')
TEST_PYTHON = sys.executable


@pytest.fixture
def case(recovery_case, host_store, monkeypatch):
    c = recovery_case
    monkeypatch.setenv('ERP_HOME_REHEARSAL', '1') # Restore the enclosing test process on exit.
    origin = c.root/'source-origin'
    shutil.copytree(c.root/'source-one', origin)
    source = c.root/'windows-origin.zip'
    caps = {dispatch.CAPABILITY: 1, 'order_inventory_v1': 1}
    pack_tree(origin, source, {'type':'tianming.release.v1','version':'windows-origin','revision':'r0',
                             'reader_capabilities':caps}, c.key)
    source_id = c.manager.stage_release(source)['id']
    authority = c.root/'windows-authority.zip'
    pack_tree(origin, authority, {'type':'tianming.release.v1','version':'windows-authority','revision':'r1',
        'migration':{'policy':'preserve_existing_facts_v1','rollback_package_sha256':source_id}}, c.key)
    authority_id = c.manager.stage_release(authority)['id']
    marker = c.manager.root/'shared/data/delivery-dispatch-contract.json'
    write_json(marker, {'reader_capability':dispatch.CAPABILITY, 'version':1,
        'activated_package':source_id, 'activated_at':'2026-10-10T12:00:00+08:00',
        'pre_activation_package':sha(c.package), 'pre_activation_backup':'synthetic-before.tmbackup'})
    state = c.manager.state
    state.update(current=source_id, schema_authority=authority_id,
                 **{dispatch.STATE_KEY:dispatch.state_index(dispatch.activation(c.manager.root/'shared'))})
    write_json(c.manager.root/'state.json', state)
    backup = c.manager.backup(PASSWORD, c.nas)
    test_uid, test_gid = os.getuid(), os.getgid()
    real_owned = m._owned
    canary = Mock()
    monkeypatch.setattr(service, 'verify_keychain', canary)
    monkeypatch.setattr(service, 'present', lambda:False)
    monkeypatch.setattr(m, '_isolated', lambda:True)
    monkeypatch.setattr(m, '_terminal', lambda:True)
    monkeypatch.setattr(m.keychain, 'selected', lambda:True)
    monkeypatch.setattr(published, 'selected', lambda:True)
    # Only identity/ownership are adapted. Mode/type/links/Data/ACL, signatures,
    # full trees, package hashes, GCM and SQL checks run on real temporary files.
    monkeypatch.setattr(m, '_owned', lambda info,uid,gid:info.st_uid==test_uid and info.st_gid==test_gid)
    def immutable(path, *, directory=False, link=False):
        info=path.lstat()
        if link:
            assert stat.S_ISLNK(info.st_mode); return
        mode=0o755 if directory or info.st_mode & 0o111 else 0o644
        if stat.S_ISLNK(info.st_mode) or stat.S_IMODE(info.st_mode)!=mode:
            raise ValueError('synthetic immutable ownership/mode adapter')
    monkeypatch.setattr(published, 'immutable', immutable)
    monkeypatch.setattr(release_verification, 'immutable', immutable)
    @contextmanager
    def checked(path, *, uid, directory=False, mode=0o600):
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            info=os.fstat(fd)
            if stat.S_IMODE(info.st_mode)!=mode or not (stat.S_ISDIR if directory else stat.S_ISREG)(info.st_mode):
                raise ValueError('synthetic root/UID adapter mode refusal')
            m.keychain._no_acl(fd)
            yield fd
        finally:os.close(fd)
    monkeypatch.setattr(m.keychain, '_checked_path', checked)
    from app.core import home_rehearsal
    guard=Mock();monkeypatch.setattr(home_rehearsal,'install_network_guard',guard)
    monkeypatch.setattr(Manager,'_process',lambda self:None)
    monkeypatch.setattr(Manager,'stop',lambda self:None)
    def forbidden(*a,**kw):raise AssertionError('must never execute, migrate, activate, or start')
    for method in ('start','update','_activate_dispatch_reader'):
        monkeypatch.setattr(Manager,method,forbidden)
    for name in ('run_migration','rehearse'):monkeypatch.setattr(migration,name,forbidden)
    calls=[]
    def install(input_backup=backup, *, revision='r1', readers=None, package=None, model_extra=''):
        base=c.root/('service-'+str(len(calls)));base.mkdir(mode=0o755)
        root=base/'instance';root.mkdir(mode=0o700)
        home=base/'home';home.mkdir(mode=0o700)
        inputs=base/'restore-inputs';inputs.mkdir(mode=0o750)
        attempt=inputs/('a'*32);attempt.mkdir(mode=0o750)
        ciphertext=attempt/'complete.tmbackup';shutil.copyfile(input_backup,ciphertext);ciphertext.chmod(0o440)
        context={'type':m.keychain.TYPE,'installation_id':'a'*32,'host_sha256':'b'*64,'uid':499,'gid':499}
        write_json(base/'keychain-context.json',context)
        public=base/'publisher-public.pem';public.write_bytes(c.public)
        if package is None:
            tree=c.root/('native-source-'+str(len(calls)));shutil.copytree(origin,tree)
            (tree/'runtime/python.exe').unlink()
            for name in publication.REQUIRED | publication.SERVICE_RESTORE_FILES:
                path=tree/name
                if not path.exists():path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'# signed synthetic runtime, never executed')
            if model_extra:
                with (tree/'app/models/fixture.py').open('a') as f:f.write(model_extra)
            package=c.root/('native-'+str(len(calls))+'.zip')
            pack_tree(tree,package,{'type':'tianming.release.v1','version':'native','revision':revision,
                'runtime_platform':'macos-arm64','executable_files':['runtime/bin/python3.12'],
                'runtime_capabilities':{'immutable_execution_v1':1,'system_service_v1':1,'service_restore_v1':1},
                'reader_capabilities':readers if readers is not None else {**caps,history.CAPABILITY:1}},c.key)
        identity=sha(package)
        slot=base/'published'/identity;slot.mkdir(parents=True)
        shutil.copyfile(package,slot/'package.zip')
        for kind in ('server','migration'):manifest=extract_verified(package,slot/kind,c.public)
        for relative,name in publication.MANAGED.items():
            link=slot/'server'/relative;link.parent.mkdir(parents=True,exist_ok=True)
            link.symlink_to(root/'shared'/name)
        write_json(base/'published.json',{'type':published.TYPE,'context_sha256':sha(base/'keychain-context.json'),
            'publisher_sha256':hashlib.sha256(c.public).hexdigest(),'packages':[identity]})
        for module in (m,published,publication,service):monkeypatch.setattr(module,'BASE',base)
        for module in (m,published,service):monkeypatch.setattr(module,'ROOT',root)
        for name,path in {'PUBLISHED':base/'published','REGISTRY':base/'published.json','PUBLIC':public}.items():
            monkeypatch.setattr(published,name,path)
        monkeypatch.setattr(m.keychain,'CONTEXT',base/'keychain-context.json')
        monkeypatch.setattr(m.keychain,'load_context',lambda:deepcopy(context))
        monkeypatch.setattr(m.bootstrap,'_actual',lambda:{'uid':499,'euid':499,'gid':499,'egid':499,'kernel_groups':[499]})
        monkeypatch.setattr(service,'PLIST',base/'synthetic.plist')
        monkeypatch.setattr(m,'__file__',str(slot/'migration'/m.MODULE))
        monkeypatch.setattr(sys,'executable',str(slot/'migration/runtime/bin/python3.12'))
        args=SimpleNamespace(input_attempt='a'*32,restore_package=identity,backup_sha256=sha(ciphertext),
            publisher_sha256=hashlib.sha256(c.public).hexdigest(),credentials_sha256=None,backup_password_from_transfer=False)
        entry=slot/'migration'/m.ENTRY
        installed=SimpleNamespace(base=base,root=root,home=home,attempt=attempt,backup=ciphertext,args=args,
            package=package,id=identity,slot=slot,manifest=manifest,context=context,
            run=lambda prompt=(lambda message:PASSWORD):m.run(args,script=entry,
                                                             executable=sys.executable,prompt=prompt))
        calls.append(installed)
        return installed
    return SimpleNamespace(fixture=c,install=install,backup=backup,source=source,authority=authority,
        canary=canary,guard=guard,secrets=host_store,real_owned=real_owned,calls=calls)


def test_windows_authority_native_binding_backup_and_second_empty_restore(case, monkeypatch):
    c=case.fixture
    source_db=c.manager.root/'shared/data/carton_erp.sqlite3'
    columns=migration.facts(source_db)['columns'];columns['email_intake_settings'].remove('encrypted_secret')
    facts=migration.facts(source_db,columns)
    original_db=sha(source_db)
    first=case.install()
    with pytest.raises(ValueError):published.resolve(first.root,first.id,c.public,kind='server')
    receipt=first.run()
    state=read_json(first.root/'state.json')
    expected={history.RELEASE:sha(case.source),history.AUTHORITY:sha(case.authority)}
    assert history.identities(state)==expected
    assert state['current']==first.id and state['schema_authority'] is None and state['previous'] is None
    assert state['manual_stop'] is True and state['operation']=='restore'
    assert receipt['started'] is False and receipt['factory_cutover'] is False
    assert not receipt['native_binding']['schema_migrated']
    assert case.canary.call_count==1 and case.guard.call_count==1
    assert (first.home/('service-restore-'+'a'*32+'.json')).is_file()
    for kind in ('server','migration'):published.resolve(first.root,first.id,c.public,kind=kind)
    monkeypatch.setattr(credential_transfer,'unprotect',credential_store.unprotect)
    manager=Manager(first.root,c.public)
    with approved_local_storage(c.root):next_backup=manager.backup(PASSWORD,c.nas)
    second=case.install(next_backup,package=first.package)
    second.run()
    final=read_json(second.root/'state.json')
    assert history.identities(final)==expected
    assert final['restored_backup_sha256']==sha(next_backup)!=sha(case.backup)
    assert migration.facts(second.root/'shared/data/carton_erp.sqlite3',columns)==facts
    assert sha(source_db)==original_db
    for identity in expected.values():assert sha(second.root/'packages'/(identity+'.zip'))==identity
    assert (second.root/'shared/data/drawing.pdf').read_bytes()==b'synthetic attachment'
    assert (second.root/'shared/data/delivery-dispatch-contract.json').read_bytes()==(c.manager.root/'shared/data/delivery-dispatch-contract.json').read_bytes()
    with closing(sqlite3.connect(second.root/'shared/data/carton_erp.sqlite3')) as db:
        assert case.secrets[db.execute('SELECT encrypted_secret FROM email_intake_settings').fetchone()[0]]=='synthetic-secret-mailbox'
    assert case.secrets[read_json(second.root/'preferences.json')['protected_password']]=='synthetic-secret-backup'
    assert case.secrets[read_json(second.root/'control/ai-provider.json')['protected_api_key']]=='synthetic-secret-openai'
    assert {k:v for k,v in migration.files(second.root/'shared').items() if k!='data/carton_erp.sqlite3'} == {
        k:v for k,v in migration.files(c.manager.root/'shared').items() if k!='data/carton_erp.sqlite3'}
    # A synthetic successful later update permits retention; original Windows
    # identities remain protected. No real archive or process is involved.
    from desktop_assistant import retention, cleanup
    manager=Manager(second.root,c.public)
    state=manager.state;state['operation']='update';write_json(second.root/'state.json',state)
    obsolete=manager.stage_release(c.package)['id']
    archive=c.nas/'retention';archive.mkdir();shutil.copyfile(c.package,archive/(obsolete+'.zip'))
    monkeypatch.setattr(retention,'assert_idle',lambda path:None)
    monkeypatch.setattr(cleanup,'assert_idle',lambda path:None)
    monkeypatch.setattr(retention,'archive_synced',lambda path:True)
    report=retention.cleanup_releases(manager,archive)
    assert set(expected.values()) <= set(report['protected'])
    assert obsolete in report['removed_packages'] and obsolete in report['removed_releases']
    for identity in expected.values():assert sha(second.root/'packages'/(identity+'.zip'))==identity


@pytest.mark.parametrize('change', ['tty','isolated','uid','group','public','entry','module','interpreter',
    'attempt','extra','mode','hardlink','symlink','fifo','device','nonempty','canary','sha','code','lock'])
def test_preflight_refusals_leave_empty_instance_and_no_credentials(case, monkeypatch, change):
    x=case.install(); prompt=Mock(return_value=PASSWORD)
    if change=='tty':monkeypatch.setattr(m,'_terminal',lambda:False)
    elif change=='isolated':monkeypatch.setattr(m,'_isolated',lambda:False)
    elif change in ('uid','group'):
        actual={'uid':499,'euid':499,'gid':499,'egid':499,'kernel_groups':[499]}
        if change=='uid':actual['uid']=0
        else:actual['kernel_groups']=[499,80]
        monkeypatch.setattr(m.bootstrap,'_actual',lambda:actual)
    elif change=='public':x.args.publisher_sha256='0'*64
    elif change=='entry':monkeypatch.setattr(m,'ENTRY','unexpected-entry.py')
    elif change=='module':monkeypatch.setattr(m,'__file__',str(x.slot/'server'/m.MODULE))
    elif change=='interpreter':monkeypatch.setattr(sys,'executable','/usr/bin/python3')
    elif change=='attempt':x.args.input_attempt='../'+'a'*32
    elif change=='extra':(x.attempt/'.hidden').write_bytes(b'extra')
    elif change=='mode':x.backup.chmod(0o640)
    elif change=='hardlink':os.link(x.backup,x.base/'linked-ciphertext')
    elif change in ('symlink','fifo'):
        x.backup.unlink()
        if change=='symlink':x.backup.symlink_to(case.backup)
        else:os.mkfifo(x.backup,0o440)
    elif change=='device':monkeypatch.setattr(m,'DATA_VOLUME',SimpleNamespace(stat=lambda:SimpleNamespace(st_dev=-1)))
    elif change=='nonempty':(x.root/'.keep').write_bytes(b'preserve')
    elif change=='canary':case.canary.side_effect=ValueError('canary refused')
    elif change=='sha':x.args.backup_sha256='0'*64
    elif change=='code':(x.slot/'migration'/m.MODULE).write_bytes(b'changed')
    elif change=='lock':(x.home/'service-restore.lock').write_bytes(b'invalid')
    expected={p.name:p.read_bytes() for p in x.root.iterdir()}
    constructor=Mock(side_effect=AssertionError('Manager must not be constructed'))
    monkeypatch.setattr(Manager,'__init__',constructor)
    with pytest.raises((ValueError,OSError)):x.run(prompt)
    constructor.assert_not_called();prompt.assert_not_called()
    assert case.secrets=={}
    assert {p.name:p.read_bytes() for p in x.root.iterdir()}==expected
    assert not list(x.home.glob('service-restore-*.json'))


@pytest.mark.parametrize('change',['password','interrupt','warning','input-replaced','input-edited','root-replaced','root-filled','lock-replaced'])
def test_prompt_and_authentication_failure_precedes_instance_initialization(case,monkeypatch,change):
    x=case.install();saved=x.base/'saved-object'
    def prompt(message):
        if change=='password':return 'wrong-synthetic-password'
        if change=='interrupt':raise KeyboardInterrupt()
        if change=='warning':
            import warnings,getpass
            warnings.warn('fallback refused',getpass.GetPassWarning)
        elif change=='input-replaced':
            x.backup.rename(saved);shutil.copyfile(saved,x.backup);x.backup.chmod(0o440)
        elif change=='input-edited':
            x.backup.chmod(0o640);x.backup.write_bytes(x.backup.read_bytes()+b'tampered');x.backup.chmod(0o440)
        elif change=='root-replaced':x.root.rename(saved);x.root.mkdir(mode=0o700)
        elif change=='root-filled':(x.root/'.foreign').write_bytes(b'preserve')
        elif change=='lock-replaced':
            lock=x.home/'service-restore.lock';lock.rename(x.home/'saved-lock');lock.write_bytes(b'0');lock.chmod(0o600)
        return PASSWORD
    constructor=Mock(side_effect=AssertionError('Manager must not be constructed'))
    monkeypatch.setattr(Manager,'__init__',constructor)
    with pytest.raises((ValueError,OSError,KeyboardInterrupt,Warning)):x.run(prompt)
    constructor.assert_not_called();assert case.secrets=={}
    assert not (x.root/'state.json').exists()
    assert not list(x.home.glob('service-restore-*.json'))


@pytest.mark.parametrize('change',['revision','schema','reader','dispatch','inventory'])
def test_target_contract_refused_before_secret_writes(case,change):
    kwargs={}
    if change=='revision':kwargs['revision']='r2'
    elif change=='schema':kwargs['model_extra']='\nclass Missing(Base):\n    __tablename__ = "missing_required"\n    id = Column(Integer, primary_key=True)\n'
    else:
        caps={dispatch.CAPABILITY:1,'order_inventory_v1':1,history.CAPABILITY:1}
        if change=='reader':caps['order_inventory_v1']=True
        elif change=='dispatch':caps.pop(dispatch.CAPABILITY)
        else:caps.pop('order_inventory_v1')
        kwargs['readers']=caps
    x=case.install(**kwargs)
    with pytest.raises(ValueError):x.run()
    assert case.secrets=={}
    assert Manager(x.root,case.fixture.public).state['current'] is None
    assert not list((x.root/'shared').iterdir())
    assert not list(x.home.glob('service-restore-*.json'))


def test_final_server_check_failure_preserves_restored_scene_without_completion(case,monkeypatch):
    x=case.install();original=Manager._published_release
    def resolver(self,identity,*,kind='server'):
        if kind=='server':raise ValueError('synthetic late immutable-tree refusal')
        return original(self,identity,kind=kind)
    monkeypatch.setattr(Manager,'_published_release',resolver)
    with pytest.raises(ValueError,match='late'):x.run()
    assert (x.root/'shared/data/carton_erp.sqlite3').is_file()
    assert case.secrets # Credential preparation happened; failure is not a rollback.
    assert Manager(x.root,case.fixture.public).state['current'] is None
    assert not list(x.home.glob('service-restore-*.json'))
    snapshot=migration.files(x.root/'shared');before=dict(case.secrets)
    with pytest.raises(ValueError,match='为空'):x.run()
    assert migration.files(x.root/'shared')==snapshot and case.secrets==before


def test_existing_receipt_blocks_before_prompt_or_manager(case,monkeypatch):
    x=case.install();path=x.home/('service-restore-'+x.args.input_attempt+'.json')
    path.write_bytes(b'preserve prior receipt');path.chmod(0o600)
    prompt=Mock(return_value=PASSWORD)
    constructor=Mock(side_effect=AssertionError('Manager must not be constructed'))
    monkeypatch.setattr(Manager,'__init__',constructor)
    with pytest.raises(ValueError):x.run(prompt)
    prompt.assert_not_called();constructor.assert_not_called()
    assert path.read_bytes()==b'preserve prior receipt' and not list(x.root.iterdir())


@pytest.mark.parametrize('change',['replacement','bytes','acl-mode'])
def test_receipt_fd_and_bytes_are_verified_before_success(case,monkeypatch,change):
    x=case.install();path=x.home/('service-restore-'+x.args.input_attempt+'.json')
    original=os.fsync;touched=[]
    def fsync(fd):
        original(fd)
        if path.exists() and not touched and os.fstat(fd).st_ino==path.stat().st_ino:
            touched.append(True)
            if change=='replacement':
                path.rename(x.home/'original-receipt');path.write_bytes(b'preserve replacement');path.chmod(0o600)
            elif change=='bytes':path.write_bytes(b'preserve changed receipt')
            else:path.chmod(0o640)
    monkeypatch.setattr(os,'fsync',fsync)
    with pytest.raises(ValueError):x.run()
    assert touched and path.exists()
    assert read_json(x.root/'state.json')['current']==x.id # Late failure, no automatic rollback.
    if change=='replacement':assert path.read_bytes()==b'preserve replacement'


def legacy_backup(case):
    from desktop_assistant.storage import decrypt_file,pack_recovery,encrypt_file
    c=case.fixture;raw=c.root/'legacy-payload.zip';payload=c.root/'legacy-payload'
    decrypt_file(case.backup,raw,PASSWORD);metadata=extract_verified(raw,payload)
    metadata.pop('files');metadata.pop('credential_transfer')
    members={'release.zip':payload/'release.zip','compatibility.zip':payload/'compatibility.zip'}
    rebuilt=c.root/'legacy-rebuilt.zip';backup=c.root/'legacy-rebuilt.tmbackup'
    pack_recovery(payload/'shared',members,rebuilt,metadata);encrypt_file(rebuilt,backup,PASSWORD)
    return backup


def attach_transfer(case,x,*,binding=None):
    record=credential_transfer.collect(case.fixture.manager.root,bound_backup_sha256=binding or x.args.backup_sha256)
    record['entries']['backup']['value']=PASSWORD
    raw=credential_transfer.seal(record,'synthetic-transfer-password')
    path=x.attempt/'credentials.tmencrypted';path.write_bytes(raw);path.chmod(0o440)
    x.args.credentials_sha256=hashlib.sha256(raw).hexdigest();x.args.backup_password_from_transfer=True
    return path


def test_native_binding_requires_authenticated_credential_record(case):
    x=case.install(legacy_backup(case))
    with pytest.raises(ValueError,match='凭据'):x.run()
    assert case.secrets=={} and not list((x.root/'shared').iterdir())
    assert Manager(x.root,case.fixture.public).state['current'] is None


def test_legacy_backup_and_external_transfer_restore_with_one_hidden_prompt(case):
    x=case.install(legacy_backup(case));transfer=attach_transfer(case,x)
    before=transfer.read_bytes();prompt=Mock(return_value='synthetic-transfer-password')
    receipt=x.run(prompt)
    assert receipt['credentials_sha256']==hashlib.sha256(before).hexdigest()
    assert receipt['result']['credentials']['status']=='restored'
    assert receipt['result']['credentials']['restored']==['backup','mailbox','openai']
    assert prompt.call_count==1 and transfer.read_bytes()==before


@pytest.mark.parametrize('change',['password','binding','sha','extra-bytes','replace'])
def test_external_transfer_authentication_precedes_manager(case,monkeypatch,change):
    x=case.install(legacy_backup(case));path=attach_transfer(case,x,binding='b'*64 if change=='binding' else None)
    if change=='sha':x.args.credentials_sha256='0'*64
    if change=='extra-bytes':path.chmod(0o640);path.write_bytes(path.read_bytes()+b'extra');path.chmod(0o440)
    def prompt(message):
        if change=='replace':path.rename(x.attempt/'saved-transfer');path.write_bytes(b'changed');path.chmod(0o440)
        return 'wrong-synthetic-password' if change=='password' else 'synthetic-transfer-password'
    constructor=Mock(side_effect=AssertionError('Manager must not be constructed'))
    monkeypatch.setattr(Manager,'__init__',constructor)
    with pytest.raises(ValueError):x.run(prompt)
    constructor.assert_not_called();assert not list(x.root.iterdir()) and case.secrets=={}


@pytest.mark.parametrize('missing',['dispatch','inventory'])
def test_legacy_without_source_reader_evidence_cannot_bind_or_invent_activation(case,missing):
    from desktop_assistant.storage import decrypt_file,pack_recovery,encrypt_file
    c=case.fixture;payload=c.root/'no-marker';raw=c.root/'no-marker.zip'
    decrypt_file(case.backup,raw,PASSWORD);metadata=extract_verified(raw,payload)
    if missing=='dispatch':(payload/'shared/data/delivery-dispatch-contract.json').unlink()
    source=c.root/'legacy-no-marker-release.zip'
    pack_tree(c.root/'source-origin',source,{'type':'tianming.release.v1','version':'legacy',
        'revision':'r1','reader_capabilities':{'order_inventory_v1':1} if missing=='dispatch' else {dispatch.CAPABILITY:1}},c.key)
    metadata.pop('files');metadata.pop('schema_authority')
    if missing=='dispatch':metadata.pop(dispatch.STATE_KEY)
    metadata.update(release=sha(source),version='legacy')
    rebuilt=c.root/'no-marker-rebuilt.zip';backup=c.root/'no-marker.tmbackup'
    pack_recovery(payload/'shared',{'release.zip':source,'credentials.tmencrypted':payload/'credentials.tmencrypted'},rebuilt,metadata)
    encrypt_file(rebuilt,backup,PASSWORD)
    x=case.install(backup)
    with pytest.raises(ValueError,match='发货|激活|库存'):x.run()
    assert case.secrets=={} and not list((x.root/'shared').iterdir())
    assert not list(x.home.glob('service-restore-*.json'))


def test_independent_process_lock_contention_precedes_prompt_and_initialization(case,monkeypatch):
    import subprocess,selectors
    x=case.install();path=x.home/'service-restore.lock';path.write_bytes(b'0');path.chmod(0o600)
    inode=path.stat().st_ino
    code='import fcntl,sys; f=open(sys.argv[1],"r+b"); fcntl.flock(f,fcntl.LOCK_EX); print("held",flush=True); sys.stdin.readline()'
    process=subprocess.Popen([TEST_PYTHON,'-I','-B','-c',code,str(path)],stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout,selectors.EVENT_READ)
            assert selector.select(10), 'synthetic lock process did not become ready'
        assert process.stdout.readline()=='held\n'
        constructor=Mock(side_effect=AssertionError('Manager must not be constructed'))
        monkeypatch.setattr(Manager,'__init__',constructor)
        prompt=Mock(return_value=PASSWORD)
        from desktop_assistant.operation_lock import BUSY
        with pytest.raises(ValueError,match=BUSY):x.run(prompt)
        prompt.assert_not_called();constructor.assert_not_called()
    finally:
        process.communicate('\n',timeout=10)
    assert process.returncode==0 and path.stat().st_ino==inode
    assert path.read_bytes()==b'0' and not list(x.root.iterdir())


def test_lease_checks_real_ownership_and_native_acl(tmp_path):
    import subprocess,pwd
    path=tmp_path/'native-lease';path.write_bytes(b'synthetic');path.chmod(0o600)
    with pytest.raises(ValueError):m.Lease(path,uid=0,gid=os.getgid(),mode=0o600)
    with m.Lease(path,uid=os.getuid(),gid=os.getgid(),mode=0o600) as lease:
        result=subprocess.run(['/bin/chmod','+a',pwd.getpwuid(os.getuid()).pw_name+' allow read',str(path)],capture_output=True)
        assert result.returncode==0
        try:
            with pytest.raises(ValueError):lease.check()
        finally:
            subprocess.run(['/bin/chmod','-N',str(path)],check=True,capture_output=True)
        # ctime changed, so this fixed lease must still refuse even after ACL removal.
        lease.mutable=True


@pytest.mark.parametrize('behavior',['short','truncated','oversized'])
def test_fixed_fd_complete_read_handles_short_reads_and_rejects_incomplete(tmp_path,monkeypatch,behavior):
    path=tmp_path/'ciphertext';raw=b'0123456789'*10;path.write_bytes(raw);path.chmod(0o600)
    with m.Lease(path,uid=os.getuid(),gid=os.getgid(),mode=0o600) as lease:
        read=os.read;calls=[]
        def limited(fd,count):
            if fd==lease.fd:
                calls.append(count)
                if behavior=='truncated' and len(calls)>1:return b''
                return read(fd,min(count,7))
            return read(fd,count)
        monkeypatch.setattr(os,'read',limited)
        if behavior=='short':assert m._read_complete(lease,len(raw))==raw and len(calls)>2
        else:
            with pytest.raises(ValueError):m._read_complete(lease,len(raw)-1 if behavior=='oversized' else len(raw))


@pytest.mark.parametrize('arguments',[['--password','SYNTHETIC_SECRET'],['--input-a','SYNTHETIC_SECRET']])
def test_argument_errors_never_echo_unknown_secret(monkeypatch,capsys,arguments):
    monkeypatch.setattr(sys,'argv',['service_restore_entry.py',*arguments])
    with pytest.raises(SystemExit):m.main(script='unused',executable='unused')
    captured=capsys.readouterr()
    assert 'SYNTHETIC_SECRET' not in captured.out+captured.err
