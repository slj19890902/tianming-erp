"""Real synthetic ZIP/GCM/Ed25519/SQLite; Windows adapters are explicitly faked."""
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace
import zipfile

import pytest

from desktop_assistant import factory_source_audit as audit, credential_transfer as credentials
from desktop_assistant import factory_cutover as protocol
from desktop_assistant.storage import sha, read_json, write_json, pack_tree, pack_recovery, decrypt_file, extract_verified, encrypt_file
from desktop_assistant.operation_lock import operation_lock
from tests.desktop_assistant import test_recovery as recovery_fixtures

PASSWORD = recovery_fixtures.PASSWORD


def hashes(root, *, held_lock=False):
    return {p.relative_to(root).as_posix(): sha(p) for p in root.rglob('*') if p.is_file()
            and not (held_lock and p == root / 'control/operation.lock')}


@pytest.fixture
def case(tmp_path, monkeypatch):
    fixture = recovery_fixtures.RecoveryTests()
    fixture.setUp()
    root = fixture.manager.root
    try:
        (root / 'control/operation.lock').write_bytes(b'0')
        write_json(root / 'preferences.json', {'protected_password': 'synthetic-cipher-backup', 'nas': str(fixture.nas)})
        write_json(root / 'control/deepseek-provider.json', {'provider': 'deepseek', 'protected_api_key': 'synthetic-cipher-ai'})
        with closing(sqlite3.connect(root / 'shared/data/carton_erp.sqlite3')) as db, db:
            db.executescript("CREATE TABLE email_intake_settings(id INTEGER PRIMARY KEY,encrypted_secret TEXT,version INTEGER); INSERT INTO email_intake_settings VALUES(1,'synthetic-mail-cipher',7);")
        monkeypatch.setattr(credentials, 'unprotect', lambda value,purpose: PASSWORD if purpose=='backup' else 'synthetic-secret-'+purpose)
        real_collect = credentials.collect
        def collect(*args, **kwargs):
            record = real_collect(*args, **kwargs)
            record['source_platform'] = 'win32'  # Synthetic platform adapter only.
            return record
        monkeypatch.setattr(credentials, 'collect', collect)
        target_source = fixture.root / 'mac-target'
        shutil.copytree(fixture.root / 'source-one', target_source)
        native = target_source / 'runtime/bin/python3.12'
        native.parent.mkdir(); (target_source / 'runtime/python.exe').rename(native)
        target = fixture.root / 'mac-target.zip'
        pack_tree(target_source, target, {'type':'tianming.release.v1','version':'synthetic-mac',
            'revision':'r1','runtime_platform':'macos-arm64','reader_capabilities':{'factory_cutover_v1':1}}, fixture.key)
        request = protocol.make_request({'host_sha256':'a'*64,'installation_id':'b'*32,
            'service_uid':502,'service_gid':502,'service_account':'_tianmingerp','root':'/Library/Application Support/TianmingERP'},
            sha(target),'c'*64,fixture.public,now=datetime.now(timezone.utc)-timedelta(minutes=1))
        backup = fixture.manager._backup_stopped(PASSWORD, fixture.nas)  # Synthetic source only, before sentinels.
        state = fixture.manager.state; state['manual_stop']=True;write_json(root/'state.json',state)
        parent = fixture.root / 'private-audit-parent';parent.mkdir()
        events=[]
        @contextmanager
        def workspace(parent):
            job=parent/'synthetic-private-job';job.mkdir();events.append('private-enter')
            yield job
            events.append('private-final-check')
        @contextmanager
        def lease(path):
            events.append('lease-enter');yield;events.append('lease-exit')
        monkeypatch.setattr(audit,'_require_windows',lambda:None)
        monkeypatch.setattr(audit,'read_file_lease',lease)
        monkeypatch.setattr(audit,'private_workspace',workspace)
        monkeypatch.setattr(audit,'archive_synced',lambda p:True)
        monkeypatch.setattr(audit,'assert_idle',lambda p:None)
        for name in ('start','stop','resume','backup','_backup_locked','_backup_stopped','pause_after_backup','restore','stage_release'):
            def prohibited(*args, **kwargs): raise AssertionError('Source mutation or service operation called')
            monkeypatch.setattr(audit.Manager,name,prohibited)
        yield SimpleNamespace(fixture=fixture,root=root,backup=backup,target=target,request=request,
                              public=fixture.public,parent=parent,events=events,collect=collect,
                              expected=sha(backup),workspace=workspace)
    finally:
        fixture.tearDown()


def run(case, **changes):
    args=dict(root=case.root,backup=case.backup,expected_backup_sha256=case.expected,password=PASSWORD,
              request=case.request,target_package=case.target,public=case.public,private_parent=case.parent)
    args.update(changes)
    return audit.verified_stopped_backup(**args)


def rewrite_backup(case, mutate):
    job=case.fixture.root/'rewrite';job.mkdir()
    decrypt_file(case.backup,job/'before.zip',PASSWORD)
    metadata=extract_verified(job/'before.zip',job/'payload')
    mutate(metadata,job/'payload')
    packages={name:job/'payload'/name for name in ('release.zip','credentials.tmencrypted','compatibility.zip') if (job/'payload'/name).exists()}
    metadata.pop('files')
    pack_recovery(job/'payload/shared',packages,job/'after.zip',metadata)
    encrypt_file(job/'after.zip',job/'after.tmbackup',PASSWORD)
    shutil.copyfile(job/'after.tmbackup',case.backup)
    case.expected=sha(case.backup)
    receipt_path=case.root/'control/backup-receipts'/(case.backup.stem+'.json')
    receipt=read_json(receipt_path);receipt.update(sha256=case.expected,size=case.backup.stat().st_size);write_json(receipt_path,receipt)


def test_success_authenticates_real_synthetic_backup_without_source_writes(case):
    before=hashes(case.root)
    lock=case.root/'control/operation.lock'
    def lock_metadata():
        info=lock.stat()
        return info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_mode
    lock_before=lock_metadata()
    with run(case) as evidence:
        assert case.events[-2:]==['private-enter','private-final-check']
        assert all(event=='lease-enter' for event in case.events[:-2])
        assert evidence['backup_sha256']==case.expected
        assert evidence['credential_purposes']==['backup','deepseek','mailbox']
        assert evidence['snapshot']['database_sha256']==sha(case.root/'shared/data/carton_erp.sqlite3')
        assert 'synthetic-secret' not in json.dumps(evidence)
        # Windows enforces the byte lock against this second reader too.
        # Check its identity while held, and its complete bytes after release.
        assert lock_metadata()==lock_before
        assert hashes(case.root,held_lock=True)=={k:v for k,v in before.items() if k!='control/operation.lock'}
        with pytest.raises(ValueError,match='另一个'):
            with operation_lock(case.root/'control/operation.lock'): pass
    assert case.events[-1]=='lease-exit'
    assert lock_metadata()==lock_before
    assert hashes(case.root)==before


@pytest.mark.parametrize('field,value', [('manual_stop',False),('manual_stop',1),('onboarding_pending',True),
    ('backup_error','synthetic error'),('operation','update_failed'),('operation','update_failed_reader_contract'),
    ('operation','recovery_start_failed'),('operation','migration_running'),('operation','unexpected')])
def test_pending_or_failed_source_refused_before_plaintext(case,field,value):
    state=read_json(case.root/'state.json');state[field]=value;write_json(case.root/'state.json',state)
    with pytest.raises(ValueError):
        with run(case): pytest.fail('must not yield')
    assert 'private-enter' not in case.events


@pytest.mark.parametrize('kind',['running','inaccessible','missing-directory','missing-lock','busy-lock','nas-unsynced','sha','receipt','last-backup'])
def test_preconditions_never_create_installation_or_staging(case,monkeypatch,kind):
    if kind=='running': monkeypatch.setattr(audit.Manager,'_process',lambda self:object())
    elif kind=='inaccessible': monkeypatch.setattr(audit,'assert_idle',lambda p:(_ for _ in ()).throw(PermissionError('synthetic')))
    elif kind=='missing-directory': (case.root/'backups').rmdir()
    elif kind=='missing-lock': (case.root/'control/operation.lock').unlink()
    elif kind=='nas-unsynced': monkeypatch.setattr(audit,'archive_synced',lambda p:False)
    elif kind=='sha': case.expected='0'*64
    elif kind=='receipt':
        p=case.root/'control/backup-receipts'/(case.backup.stem+'.json');v=read_json(p);v['verified']=1;write_json(p,v)
    elif kind=='last-backup':
        p=case.root/'state.json';v=read_json(p);v['last_backup']=str(case.backup.parent/'missing.tmbackup');write_json(p,v)
    before=hashes(case.root)
    if kind=='busy-lock':
        with operation_lock(case.root/'control/operation.lock'),pytest.raises(ValueError):
            with run(case): pytest.fail('must not yield')
    else:
        with pytest.raises((ValueError,OSError)):
            with run(case): pytest.fail('must not yield')
    assert hashes(case.root)==before and not list(case.parent.iterdir())
    if kind=='missing-directory': assert not (case.root/'backups').exists()


@pytest.mark.parametrize('kind',['business-value','network-environment','extra-file','mailbox-cipher','empty-wal','empty-shm','empty-journal'])
def test_same_counts_or_normalized_facts_cannot_hide_raw_source_changes(case,kind):
    shared=case.root/'shared'
    if kind=='business-value':
        with closing(sqlite3.connect(shared/'data/carton_erp.sqlite3')) as db, db: db.execute("UPDATE users SET note='changed'")
    elif kind=='mailbox-cipher':
        with closing(sqlite3.connect(shared/'data/carton_erp.sqlite3')) as db, db: db.execute("UPDATE email_intake_settings SET encrypted_secret='different-cipher'")
    elif kind=='network-environment':
        p=shared/'environment.json';v=read_json(p);v['ERP_PORT']='19999';write_json(p,v)
    elif kind=='extra-file': (shared/'unregistered.bin').write_bytes(b'new source evidence')
    else: (shared/('data/carton_erp.sqlite3-'+kind.removeprefix('empty-'))).write_bytes(b'')
    with pytest.raises(ValueError):
        with run(case): pytest.fail('must not yield')


@pytest.mark.parametrize('value',['2000-01-01T00:00:00Z','2099-01-01T00:00:00Z','2026-10-11T00:00:00',None])
def test_authenticated_backup_time_must_fit_request(case,value):
    rewrite_backup(case,lambda meta,payload:meta.update(created=value))
    with pytest.raises(ValueError,match='时间'):
        with run(case): pytest.fail('must not yield')


def test_private_exit_failure_never_yields_usable_evidence(case,monkeypatch):
    @contextmanager
    def failure(parent):
        with case.workspace(parent) as job: yield job
        raise ValueError('synthetic final ACL refusal')
    monkeypatch.setattr(audit,'private_workspace',failure)
    with pytest.raises(ValueError,match='ACL'):
        with run(case): pytest.fail('must not yield')


@pytest.mark.parametrize('kind',['actual-secret','purpose-removed','config-text','source-platform'])
def test_complete_credentials_are_compared_without_leaking_values(case,monkeypatch,kind):
    def changed(*args,**kwargs):
        record=case.collect(*args,**kwargs)
        if kind=='actual-secret': record['entries']['mailbox']['value']='synthetic-secret-must-not-leak'
        elif kind=='purpose-removed': del record['entries']['mailbox']
        elif kind=='config-text': record['files']['preferences.json']+=' '
        else: record['source_platform']='darwin'
        return record
    monkeypatch.setattr(credentials,'collect',changed)
    with pytest.raises(ValueError) as error:
        with run(case): pytest.fail('must not yield')
    assert 'synthetic-secret' not in str(error.value)


@pytest.mark.parametrize('kind',['shared','state','config','backup'])
def test_change_after_private_checks_is_refused_before_yield(case,monkeypatch,kind):
    @contextmanager
    def changed(parent):
        with case.workspace(parent) as job: yield job
        if kind=='shared': (case.root/'shared/late-evidence').write_text('changed')
        elif kind=='state':
            p=case.root/'state.json';v=read_json(p);v['manual_stop']=False;write_json(p,v)
        elif kind=='config':
            with (case.root/'preferences.json').open('ab') as f:f.write(b' ')
        else:
            with case.backup.open('ab') as f:f.write(b'bad')
    monkeypatch.setattr(audit,'private_workspace',changed)
    with pytest.raises(ValueError):
        with run(case): pytest.fail('must not yield')


def test_direct_revision_normalizes_unused_historical_authority(case):
    p=case.root/'state.json';v=read_json(p);v['schema_authority']='d'*64;write_json(p,v)
    with run(case) as evidence: assert evidence['backup_sha256']==case.expected


def test_missing_inventory_history_is_not_fabricated_and_reader_still_required(case):
    source={'reader_capabilities':{'order_inventory_v1':1}}
    target={'reader_capabilities':{'factory_cutover_v1':1}}
    with pytest.raises(ValueError,match='reader'):
        audit._target_readers(case.root/'shared/data/carton_erp.sqlite3',source,target,None,None)
    target['reader_capabilities']['order_inventory_v1']=1
    audit._target_readers(case.root/'shared/data/carton_erp.sqlite3',source,target,None,None)


@pytest.mark.parametrize('table,reader',list(audit.TABLE_READERS.items()))
def test_each_existing_business_reader_is_required(case,table,reader):
    database=case.root/'shared/data/carton_erp.sqlite3'
    with closing(sqlite3.connect(database)) as db, db:db.execute(f'CREATE TABLE {table}(id INTEGER PRIMARY KEY)')
    target={'reader_capabilities':{'factory_cutover_v1':1}}
    with pytest.raises(ValueError,match='reader'): audit._target_readers(database,{},target,None,None)
    target['reader_capabilities'][reader]=1
    audit._target_readers(database,{},target,None,None)


def test_target_nonnumeric_reader_cannot_pass_as_one(case):
    with pytest.raises(ValueError,match='损坏'):
        audit._target_readers(case.root/'shared/data/carton_erp.sqlite3',{},
                             {'reader_capabilities':{'factory_cutover_v1':True}},None,None)


def test_clean_wal_source_never_gets_sidecars_created_by_audit(case):
    # Switch the synthetic source and its authenticated backup to WAL format.
    database=case.root/'shared/data/carton_erp.sqlite3'
    with closing(sqlite3.connect(database)) as db, db:
        assert db.execute('PRAGMA journal_mode=WAL').fetchone()==('wal',)
    assert not any(Path(str(database)+s).exists() for s in ('-wal','-shm','-journal'))
    def changed(meta,payload):
        shutil.copyfile(database,payload/'shared/data/carton_erp.sqlite3')
    rewrite_backup(case,changed)
    before=hashes(case.root)
    with run(case):
        assert not any(Path(str(database)+s).exists() for s in ('-wal','-shm','-journal'))
    assert hashes(case.root)==before


def test_directory_removed_after_initial_preflight_is_not_recreated(case,monkeypatch):
    original=audit._preflight
    calls=0
    def changed(root):
        nonlocal calls
        checked=original(root);calls+=1
        if calls==1:(case.root/'staging').rmdir()
        return checked
    monkeypatch.setattr(audit,'_preflight',changed)
    with pytest.raises(ValueError,match='不补建'):
        with run(case):pytest.fail('must not yield')
    assert not (case.root/'staging').exists()


def test_non_model_target_member_tamper_is_rejected(case):
    # Keep the signed manifest and all models intact; corrupt an unrelated byte.
    replacement=case.target.with_name('tampered-target.zip')
    with zipfile.ZipFile(case.target) as old,zipfile.ZipFile(replacement,'w') as new:
        for name in old.namelist():new.writestr(name,b'corrupted' if name=='main.py' else old.read(name))
    case.target=replacement
    case.request['package_sha256']=sha(replacement)
    with pytest.raises(ValueError,match='哈希'):
        with run(case):pytest.fail('must not yield')


@pytest.mark.parametrize('kind',['removed','truncated'])
def test_lock_race_does_not_create_or_initialize_source_file(case,monkeypatch,kind):
    @contextmanager
    def race(path,**kwargs):
        if kind=='removed':path.unlink()
        else:path.write_bytes(b'')
        with operation_lock(path,**kwargs):yield
    monkeypatch.setattr(audit,'operation_lock',race)
    with pytest.raises((ValueError,OSError)):
        with run(case):pytest.fail('must not yield')
    path=case.root/'control/operation.lock'
    assert not path.exists() if kind=='removed' else path.read_bytes()==b''
    assert not list(case.parent.iterdir())


@pytest.mark.parametrize('bad_policy',[False,True])
def test_signed_schema_authority_is_verified_only_when_revision_differs(case,bad_policy):
    from desktop_assistant.storage import database_info
    root=case.root;current=read_json(root/'state.json')['current']
    case.fixture.release('authority',revision='r2')
    authority=case.fixture.root/'authority-with-policy.zip'
    pack_tree(case.fixture.root/'source-authority',authority,
        {'type':'tianming.release.v1','version':'authority','revision':'r2',
         'migration':{'policy':'wrong' if bad_policy else 'preserve_existing_facts_v1','rollback_package_sha256':current}},case.fixture.key)
    identity=sha(authority);shutil.copyfile(authority,root/'packages'/(identity+'.zip'))
    state=read_json(root/'state.json');state['schema_authority']=identity;write_json(root/'state.json',state)
    database=root/'shared/data/carton_erp.sqlite3'
    with closing(sqlite3.connect(database)) as db, db:db.execute("UPDATE alembic_version SET version_num='r2'")
    info=database_info(database)
    new_target=case.fixture.root/'target-r2.zip'
    pack_tree(case.fixture.root/'mac-target',new_target,{'type':'tianming.release.v1','version':'synthetic-mac',
        'revision':'r2','runtime_platform':'macos-arm64','reader_capabilities':{'factory_cutover_v1':1}},case.fixture.key)
    case.target=new_target;case.request['package_sha256']=sha(new_target)
    def changed(meta,payload):
        meta.update(database=info,schema_authority=identity)
        shutil.copyfile(database,payload/'shared/data/carton_erp.sqlite3')
        shutil.copyfile(authority,payload/'compatibility.zip')
    rewrite_backup(case,changed)
    path=root/'control/backup-receipts'/(case.backup.stem+'.json');receipt=read_json(path)
    receipt['database']=info;write_json(path,receipt)
    if bad_policy:
        with pytest.raises(ValueError):
            with run(case):pytest.fail('must not yield')
    else:
        with run(case) as evidence:assert evidence['snapshot']['revision']=='r2'


def test_target_without_factory_capability_is_not_eligible(case):
    target=case.fixture.root/'home-only.zip'
    pack_tree(case.fixture.root/'mac-target',target,{'type':'tianming.release.v1','version':'home-only',
        'revision':'r1','runtime_platform':'macos-arm64'},case.fixture.key)
    case.target=target;case.request['package_sha256']=sha(target)
    with pytest.raises(ValueError,match='正式切换'):
        with run(case):pytest.fail('must not yield')


def test_request_expiring_during_private_audit_never_yields(case,monkeypatch):
    original=audit.datetime
    values=iter([datetime.now(timezone.utc),datetime.now(timezone.utc)+timedelta(days=8)])
    class Clock(original):
        @classmethod
        def now(cls,tz=None):return next(values)
    monkeypatch.setattr(audit,'datetime',Clock)
    with pytest.raises(ValueError,match='完成时.*过期'):
        with run(case):pytest.fail('must not yield')


@pytest.mark.parametrize('entry',['runtime/bin/python3.12','main.py','app/main.py','desktop_assistant/server_entry.py'])
def test_signed_but_unrunnable_target_is_refused_before_decryption(case,entry):
    source=case.fixture.root/'mac-target';(source/entry).unlink()
    target=case.fixture.root/'missing-entry.zip'
    pack_tree(source,target,{'type':'tianming.release.v1','version':'missing-entry',
        'revision':'r1','runtime_platform':'macos-arm64','reader_capabilities':{'factory_cutover_v1':1}},case.fixture.key)
    case.target=target;case.request['package_sha256']=sha(target)
    with pytest.raises(ValueError):
        with run(case):pytest.fail('must not yield')
    assert 'private-enter' not in case.events
