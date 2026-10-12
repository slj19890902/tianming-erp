"""Synthetic current Mac cold backups; never an installed instance or NAS."""
import importlib
import sys

import pytest


def test_current_mac_audit_context_exists():
    subject = importlib.import_module('desktop_assistant.current_mac_target_audit')
    assert callable(subject.verified_current_mac_backup)


if sys.platform != 'darwin':
    pytest.skip('Mac ownership/FD integration; module import is platform neutral', allow_module_level=True)

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
from types import SimpleNamespace
import uuid
import zipfile

from desktop_assistant import current_mac_target_audit as audit, current_mac_cutover_contract as contract
from desktop_assistant import factory_cutover as facts, credential_transfer as transfer, credential_store
from desktop_assistant import mac_service_restore as restore, mac_service_manage as manage
from desktop_assistant import mac_release_publish as publication, mac_published_runtime as published
from desktop_assistant import mac_system_service as system, mac_smb_stream_runtime as runtime
from desktop_assistant import mac_smb_stream_runtime_contract as cfg, mac_smb_stream_receipt as receipts
from desktop_assistant import mac_smb_stream_target as stream, migration
from desktop_assistant.manager import Manager
from desktop_assistant.storage import (sha, write_json, read_json, decrypt_file, encrypt_file,
    extract_verified, pack_recovery, pack_tree)
from tests.desktop_assistant import test_mac_service_restore as fixtures
from tests.desktop_assistant.test_mac_service_restore import case, recovery_case, host_store, PASSWORD


def forbidden(*args, **kwargs):
    raise AssertionError('no transport, migration, credential install or process action')


def rewrite(path, change):
    mode = stat.S_IMODE(path.stat().st_mode)
    value = read_json(path); change(value); write_json(path, value); path.chmod(mode)


@pytest.fixture
def target(case, monkeypatch, request):
    """Real signed history + five Mac packages, GCM/ZIP/SQL and complete files.

    OS root/499, code location, registered launchd and canary are adapters only.
    The receipt's transport observations are synthetic; no network is opened.
    """
    c = case.fixture
    raw = c.root/'historical.zip'; decrypt_file(case.backup, raw, PASSWORD)
    historical = c.root/'historical-payload'; meta = extract_verified(raw, historical)
    record = transfer.unseal((historical/'credentials.tmencrypted').read_bytes(), PASSWORD)
    record['source_platform'] = 'win32'; record['entries']['backup']['value'] = PASSWORD
    members = {n:historical/n for n in meta['files'] if not n.startswith('shared/') and n != 'credentials.tmencrypted'}
    meta.pop('credential_transfer')
    full_raw = c.root/'external-history.zip'; full = c.root/'external-history.tmbackup'
    pack_recovery(historical/'shared', members, full_raw, meta); encrypt_file(full_raw, full, PASSWORD)
    record['bound_backup_sha256'] = sha(full)
    original_pack = fixtures.pack_tree
    def native_pack(source, destination, metadata, key):
        metadata = deepcopy(metadata)
        if metadata.get('runtime_platform') == 'macos-arm64':
            metadata['runtime_capabilities'].update({audit.CAPABILITY:1, cfg.CAPABILITY:1,
                audit.EXECUTION_CAPABILITY:1, 'service_manage_v1':1, 'system_nightly_v1':1})
            metadata['reader_capabilities']['factory_cutover_v1'] = 1
        return original_pack(source, destination, metadata, key)
    monkeypatch.setattr(fixtures, 'pack_tree', native_pack)
    monkeypatch.setattr(publication, 'SERVICE_RESTORE_FILES', publication.SERVICE_RESTORE_FILES |
        cfg.FILES | {audit.MODULE, system.ENTRY, 'desktop_assistant/mac_system_service.py',
                     system.MANAGE_ENTRY, system.MANAGE_MODULE})
    def protect(value, purpose):
        reference = 'tm-keychain-v1:'+purpose+':'+uuid.uuid4().hex
        case.secrets[reference] = value
        return reference
    monkeypatch.setattr(credential_store, 'protect', protect)
    x = case.install(full)
    external = x.attempt/'credentials.tmencrypted'
    external.write_bytes(transfer.seal(record, PASSWORD)); external.chmod(0o440)
    x.args.credentials_sha256 = sha(external); x.args.backup_password_from_transfer = True
    x.run()
    monkeypatch.setattr(manage, 'ROOT', x.root); monkeypatch.setattr(manage, 'BASE', x.base)
    monkeypatch.setattr(contract, 'ROOT', str(x.root))
    monkeypatch.setattr(cfg, 'BASE', x.base)
    monkeypatch.setattr(cfg, 'CONTROL', x.base/'smb-stream-service.json')
    monkeypatch.setattr(cfg, 'CONTRACT', x.base/'smb-stream-archive.json')
    monkeypatch.setattr(system, 'CONTROL', x.base/'system-service.json')
    monkeypatch.setattr(system, 'PLISTS', x.base)
    # These adapters retain mode/type/link/inode/ACL/hash/signature checks while
    # recognizing the temporary current user instead of privileged root/499.
    def guard(path, *, directory=False, mode=0o644, uid=0, link=False):
        info = Path(path).lstat()
        if link:
            assert stat.S_ISLNK(info.st_mode)
            return
        if (stat.S_IMODE(info.st_mode) != mode or info.st_nlink != 1 and not directory
                or not (stat.S_ISDIR if directory else stat.S_ISREG)(info.st_mode)):
            raise ValueError('synthetic protected ownership adapter')
    monkeypatch.setattr(publication, 'guard', guard)
    monkeypatch.setattr(system.bootstrap, 'protected', lambda path, *, directory=False, **kw:
                       None if directory else Path(path).read_bytes())
    monkeypatch.setattr(runtime.identity, '_parent', lambda:None)
    monkeypatch.setattr(runtime.identity, 'permitted_groups', lambda gid:{499})
    monkeypatch.setattr(runtime.mounts, 'local_addresses', lambda:{'127.0.0.1'})
    monkeypatch.setattr(system, 'present', lambda:True)
    monkeypatch.setattr(transfer, 'unprotect', credential_store.unprotect)
    monkeypatch.setattr(credential_store, 'unprotect_backup', lambda reference:case.secrets[reference])
    manager = Manager(x.root, c.public)
    roles = {'restore':x.id}
    for role in ('cold', 'execution', 'monitor', 'transport'):
        tree = c.root/('source-'+role); shutil.copytree(x.slot/'migration', tree)
        (tree/'manifest.json').unlink()
        metadata = {k:v for k,v in x.manifest.items() if k not in ('files','signature')}
        metadata['version'] = 'native-'+role
        fault = getattr(request, 'param', '')
        if fault == 'audit-cap' and role == 'execution':metadata['runtime_capabilities'][audit.CAPABILITY] = True
        if fault == role+'-v3-cap':metadata['runtime_capabilities'].pop(audit.EXECUTION_CAPABILITY)
        if fault == 'monitor-schedule' and role == 'monitor':metadata['runtime_capabilities']['system_nightly_v1'] = True
        package = c.root/(role+'.zip'); pack_tree(tree, package, metadata, c.key)
        identity = sha(package); roles[role] = identity
        slot = x.base/'published'/identity; slot.mkdir(mode=0o755)
        shutil.copyfile(package, slot/'package.zip')
        for kind in ('server','migration'): extract_verified(package, slot/kind, c.public)
        for relative, name in publication.MANAGED.items():
            link = slot/'server'/relative; link.parent.mkdir(parents=True, exist_ok=True); link.symlink_to(x.root/'shared'/name)
        manager.stage_release(package)
        (x.root/'packages'/(identity+'.zip')).chmod(0o600)
    rewrite(published.REGISTRY, lambda value:value.update(packages=sorted(roles.values())))
    for identity in roles.values():
        slot = x.base/'published'/identity
        with zipfile.ZipFile(slot/'package.zip') as archive:
            manifest_raw = archive.read('manifest.json')
        for kind in ('server','migration'):
            (slot/kind/'manifest.json').write_bytes(manifest_raw)
    execution_tree = x.base/'published'/roles['execution']/'migration'
    monkeypatch.setattr(audit, '__file__', str(execution_tree/audit.MODULE))
    monkeypatch.setattr(sys, 'executable', str(execution_tree/'runtime/bin/python3.12'))
    ctl_meta = system.metadata(roles['monitor'])
    system.CONTROL.write_bytes(manage.boot.canonical(system.record_for(ctl_meta, True)))
    system.PLIST.write_bytes(system._plist(roles['monitor'], ctl_meta['publisher_sha256']))
    value = dict(type=runtime.archive.TYPE, context_sha256=sha(restore.keychain.CONTEXT), uid=499,
        server_address='192.168.1.245',port=445,username='synthetic_archive',share='synthetic-backups',
        relative_directory=runtime.archive.DIRECTORY,server_guid='b'*32,protection='smb311-session-aes-gcm')
    ctl = dict(type=cfg.TYPE,attempt_id='1'*32,plan_sha256='c'*64,package_sha256=roles['transport'],
        context_sha256=sha(restore.keychain.CONTEXT),publisher_sha256=sha(published.PUBLIC),
        contract_sha256=runtime.archive.digest(value))
    ctl['credential_account'] = cfg.account(ctl['context_sha256'], ctl['contract_sha256'], ctl['attempt_id'])
    cfg.CONTROL.write_bytes(cfg.canonical(ctl)); cfg.CONTRACT.write_bytes(cfg.canonical(value))
    rewrite(x.root/'preferences.json', lambda value:value.update(nas=str(stream.destination(ctl))))
    rewrite(x.root/'state.json', lambda value:value.update(current=roles['cold']))
    # Real later Mac business differs from the authenticated Windows history.
    with closing(sqlite3.connect(x.root/'shared/data/carton_erp.sqlite3')) as db:
        db.execute("UPDATE customers SET note='real synthetic Mac business'"); db.commit()
    extra = x.root/'shared/data/current-only.txt'; extra.write_bytes(b'complete current attachment'); extra.chmod(0o600)
    if getattr(request, 'param', None) == 'large':
        folder = x.root/'shared/data/large'; folder.mkdir(mode=0o700)
        for number in range(801):
            path = folder/('%04d-full-attachment.pdf' % number)
            path.write_bytes(b'synthetic complete attachment'); path.chmod(0o600)
    source_job = x.root/'staging'/'synthetic-cold'; source_job.mkdir(mode=0o700)
    mask = os.umask(0o077)
    try: cold = manager._build_backup_payload(PASSWORD, source_job, manager._backup_inputs())
    finally: os.umask(mask)
    copy = x.root/'backups'/cold['encrypted'].name; shutil.copyfile(cold['encrypted'], copy); copy.chmod(0o600)
    real_fingerprint = receipts.fingerprint
    monkeypatch.setattr(receipts, 'fingerprint', lambda info:{**real_fingerprint(info), 'uid':499, 'gid':499})
    binding = runtime.load_binding()
    def observation(kind, guid):
        return dict(type=kind,contract_sha256=ctl['contract_sha256'],connection_guid=guid*32,
            filename=copy.name,bytes=cold['size'],sha256=cold['sha256'],
            directories=[dict(index=i) for i in (1,2,3)],file=dict(index=4),
            backup_authenticated=False)
    receipt = dict(type=receipts.TYPE,context_sha256=ctl['context_sha256'],control_sha256=binding.control_sha,
        contract_sha256=ctl['contract_sha256'],package_sha256=roles['transport'],filename=copy.name,
        sha256=cold['sha256'],size=cold['size'],raw_sha256=cold['raw_sha256'],database=cold['database'],
        verified_at=datetime.now(timezone.utc).isoformat(),
        transfer=dict(upload=observation(runtime.files.UPLOAD,'6'),download=observation(runtime.files.READBACK,'7')),
        local_copy=dict(path=str(copy),sha256=cold['sha256'],size=cold['size'],identity=receipts.fingerprint(copy.stat())))
    directory = x.root/'control/backup-receipts'; directory.mkdir(mode=0o700)
    receipt_path = directory/receipts.receipt_name(copy.name)
    receipt_path.write_bytes(cfg.canonical(receipt)); receipt_path.chmod(0o600)
    rewrite(x.root/'state.json', lambda value:value.update(current=roles['execution'],previous=roles['cold'],
        operation='update',manual_stop=True,update_backup=str(copy),last_backup=str(copy),backup_error=None,
        last_backup_at=receipt['verified_at'],last_backup_stream=dict(path=str(copy),receipt=receipt_path.name,
                                                                  receipt_sha256=sha(receipt_path))))
    identity = dict(host_sha256='c'*64,installation_id=x.context['installation_id'],service_uid=499,service_gid=499,
                    service_account='_tianmingerp',root=str(x.root))
    monkeypatch.setattr(facts, 'local_target', lambda *a:deepcopy(identity))
    for module, names in ((runtime,('transfer','probe')), (stream,('prepare',)),
                          (Manager,('start','stop','update','update_stopped')), (credential_store,('protect',))):
        for name in names:
            if callable(getattr(module, name, None)): monkeypatch.setattr(module, name, forbidden)
    kwargs = dict(context_sha256=sha(restore.keychain.CONTEXT),publisher_sha256=sha(published.PUBLIC),
        registry_sha256=sha(published.REGISTRY),restore_receipt_sha256=sha(x.home/('service-restore-'+x.args.input_attempt+'.json')))
    def run(**extra):
        return audit.verified_current_mac_backup(roles['execution'],roles['monitor'],x.args.input_attempt,**{**kwargs,**extra})
    return SimpleNamespace(x=x,c=c,case=case,roles=roles,manager=manager,copy=copy,receipt_path=receipt_path,
        receipt=receipt,kwargs=kwargs,run=run,cold=cold,historical=historical,source_job=source_job)


def protected(target):
    root = target.x.root
    return {str(p.relative_to(root)):sha(p) for p in root.rglob('*')
            if p.is_file() and not p.is_symlink() and 'staging' not in p.relative_to(root).parts}


def test_complete_current_mac_business_and_five_package_roles(target):
    t=target; before=protected(t); secrets=deepcopy(t.case.secrets)
    assert len(set(t.roles.values())) == 5
    with t.run() as (prep, recheck):
        assert contract.validate_preparation(prep) == prep
        assert prep['execution_package_sha256'] == t.roles['execution']
        assert prep['monitor_package_sha256'] == t.roles['monitor']
        assert prep['backup']['release_sha256'] == t.roles['cold']
        assert prep['backup']['transport_package_sha256'] == t.roles['transport']
        assert prep['restore']['restore_package_sha256'] == t.roles['restore']
        assert prep['target_snapshot']['database_sha256'] != sha(t.historical/'shared/data/carton_erp.sqlite3')
        assert prep == recheck()
        assert PASSWORD not in str(prep) and 'synthetic-secret' not in str(prep)
    with pytest.raises(ValueError):recheck()
    assert protected(t) == before and t.case.secrets == secrets
    assert list((t.x.root/'staging').glob('current-mac-audit-*/recovery.zip'))
    with t.run(expected_preparation=prep) as (again, check):
        assert again == prep == check()


@pytest.mark.parametrize('target', ['audit-cap','execution-v3-cap','monitor-v3-cap','monitor-schedule'], indirect=True)
def test_real_signed_capabilities_are_required_before_secret_access(target, monkeypatch):
    monkeypatch.setattr(credential_store, 'unprotect_backup', forbidden)
    with pytest.raises(ValueError):
        with target.run():pytest.fail('unsigned capability accepted')
    assert not list((target.x.root/'staging').glob('current-mac-audit-*'))


@pytest.mark.parametrize('fault', ['context','public','registry','old-receipt-approval','uid','groups','isolated',
    'module','interpreter','control','plist','registered','activation','state-running','state-operation',
    'previous','update-backup','backup-error','retained-work','pending','cas-pending','journal',
    'input-extra','input-link','input-hardlink','input-mode','old-credentials','native','source-time',
    'original-package','current-package','execution-tree','monitor-tree','transport-tree','mounted-route',
    'receipt-sha','receipt-connections','copy-identity','copy-size'])
def test_preflight_refuses_root_history_state_or_receipt_before_secret(target, monkeypatch, fault):
    t=target; x=t.x; state=x.root/'state.json'
    if fault in ('context','public','registry','old-receipt-approval'):
        key={'context':'context_sha256','public':'publisher_sha256','registry':'registry_sha256',
             'old-receipt-approval':'restore_receipt_sha256'}[fault]
        t.kwargs[key]='0'*64
    elif fault in ('uid','groups'):
        actual=dict(uid=499,euid=499,gid=499,egid=499,kernel_groups=[499])
        if fault=='uid':actual['euid']=0
        else:actual['kernel_groups']=[499,80]
        monkeypatch.setattr(manage.boot,'_actual',lambda:actual)
    elif fault=='isolated':monkeypatch.setattr(restore,'_isolated',lambda:False)
    elif fault=='module':monkeypatch.setattr(audit,'__file__',str(x.slot/'server'/audit.MODULE))
    elif fault=='interpreter':monkeypatch.setattr(sys,'executable','/usr/bin/python3')
    elif fault=='control':rewrite(system.CONTROL,lambda value:value.update(enabled=False))
    elif fault=='plist':system.PLIST.write_bytes(b'foreign plist')
    elif fault=='registered':monkeypatch.setattr(system,'present',lambda:False)
    elif fault=='activation':(x.base/'factory-activation.json').symlink_to('absent')
    elif fault=='state-running':rewrite(state,lambda value:value.update(manual_stop=False))
    elif fault=='state-operation':rewrite(state,lambda value:value.update(operation='migration_failed'))
    elif fault=='previous':rewrite(state,lambda value:value.update(previous=None))
    elif fault=='update-backup':rewrite(state,lambda value:value.update(update_backup='other'))
    elif fault=='backup-error':rewrite(state,lambda value:value.update(backup_error='interrupted'))
    elif fault=='retained-work':rewrite(state,lambda value:value.update(backup_work_retained={'job':'retained'}))
    elif fault in ('pending','cas-pending'):
        name='state.json.pending' if fault=='pending' else 'state-'+'a'*32+'.pending.json'
        (x.root/name).symlink_to('absent')
    elif fault=='journal':Path(str(x.root/'shared/data/carton_erp.sqlite3')+'-shm').symlink_to('absent')
    elif fault=='input-extra':(x.attempt/'extra').touch()
    elif fault=='input-link':x.backup.rename(x.base/'saved');x.backup.symlink_to(x.base/'saved')
    elif fault=='input-hardlink':os.link(x.backup,x.base/'saved')
    elif fault=='input-mode':x.backup.chmod(0o600)
    elif fault=='old-credentials':
        path=x.attempt/'credentials.tmencrypted';path.chmod(0o600)
        path.write_bytes(b'changed external cipher');path.chmod(0o440)
    elif fault=='native':rewrite(state,lambda value:value['native_restore_binding'].update(package_sha256=t.roles['execution']))
    elif fault=='source-time':rewrite(state,lambda value:value.update(source_time='2026-10-10T00:00:00+00:00'))
    elif fault in ('original-package','current-package'):
        identity=read_json(state)['restored_release_sha256'] if fault=='original-package' else t.roles['execution']
        (x.root/'packages'/(identity+'.zip')).write_bytes(b'changed signed package')
    elif fault.endswith('-tree'):
        role=fault.split('-')[0]
        (x.base/'published'/t.roles[role]/'migration'/audit.MODULE).write_bytes(b'changed signed tree')
    elif fault=='mounted-route':rewrite(x.root/'preferences.json',lambda value:value.update(nas='/Volumes/other'))
    elif fault=='receipt-sha':rewrite(state,lambda value:value['last_backup_stream'].update(receipt_sha256='0'*64))
    elif fault=='receipt-connections':
        receipt=read_json(t.receipt_path)
        receipt['transfer']['download']['connection_guid']=receipt['transfer']['upload']['connection_guid']
        t.receipt_path.write_bytes(cfg.canonical(receipt))
        rewrite(state,lambda value:value['last_backup_stream'].update(receipt_sha256=sha(t.receipt_path)))
    elif fault=='copy-identity':
        t.copy.rename(t.copy.with_suffix('.preserved'));shutil.copyfile(t.copy.with_suffix('.preserved'),t.copy);t.copy.chmod(0o600)
    elif fault=='copy-size':t.copy.write_bytes(t.copy.read_bytes()+b'changed')
    else:raise AssertionError(fault)
    before=protected(t);monkeypatch.setattr(credential_store,'unprotect_backup',forbidden)
    with pytest.raises((ValueError,OSError)):
        with t.run():pytest.fail('preflight accepted')
    assert protected(t)==before
    assert not list((x.root/'staging').glob('current-mac-audit-*'))


@pytest.mark.parametrize('fault',['database','attachment','environment','mailbox-version','secret','config','new-config'])
def test_current_changes_since_cold_backup_fail_and_keep_plaintext_attempt(target,fault):
    t=target;root=t.x.root
    if fault in ('database','mailbox-version'):
        with closing(sqlite3.connect(root/'shared/data/carton_erp.sqlite3')) as db:
            db.execute("UPDATE customers SET note='after cold business'" if fault=='database'
                       else 'UPDATE email_intake_settings SET version=version+1');db.commit()
    elif fault=='attachment':(root/'shared/data/current-only.txt').write_bytes(b'after cold attachment')
    elif fault=='environment':rewrite(root/'shared/environment.json',lambda value:value.update(ERP_NEW='changed'))
    elif fault=='secret':t.case.secrets[next(k for k in t.case.secrets if ':mailbox:' in k)]='different'
    elif fault=='config':rewrite(root/'preferences.json',lambda value:value.update(extra='changed'))
    elif fault=='new-config':
        path=root/'control/deepseek-provider.json';path.write_text('{}');path.chmod(0o600)
    before=protected(t)
    with pytest.raises(ValueError):
        with t.run():pytest.fail('changed current facts accepted')
    assert protected(t)==before
    assert list((root/'staging').glob('current-mac-audit-*/recovery.zip'))


@pytest.mark.parametrize('fault',['state','shared','secret','receipt','context','transport','old-input','private-payload',
    'private-raw','lock','new-config','journal','process','activation'])
def test_consumer_interval_recheck_detects_mutation_and_callback_expires(target,monkeypatch,fault):
    t=target;root=t.x.root
    with pytest.raises((ValueError,OSError)):
        with t.run() as (prep,recheck):
            if fault=='state':rewrite(root/'state.json',lambda value:value.update(extra='late'))
            elif fault=='shared':(root/'shared/data/current-only.txt').write_bytes(b'late')
            elif fault=='secret':t.case.secrets[next(k for k in t.case.secrets if ':mailbox:' in k)]='late'
            elif fault=='receipt':t.receipt_path.write_bytes(t.receipt_path.read_bytes()+b' ')
            elif fault=='context':restore.keychain.CONTEXT.write_bytes(restore.keychain.CONTEXT.read_bytes()+b' ')
            elif fault=='transport':cfg.CONTROL.write_bytes(cfg.CONTROL.read_bytes()+b' ')
            elif fault=='old-input':
                t.x.backup.chmod(0o600);t.x.backup.write_bytes(t.x.backup.read_bytes()+b' ');t.x.backup.chmod(0o440)
            elif fault in ('private-payload','private-raw'):
                job=next((root/'staging').glob('current-mac-audit-*'))
                path=job/'recovery.zip' if fault=='private-raw' else job/'payload/shared/data/current-only.txt'
                path.write_bytes(b'late private mutation')
            elif fault=='lock':
                path=root/'control/operation.lock';path.rename(path.with_suffix('.preserved'));path.write_bytes(b'0');path.chmod(0o600)
            elif fault=='new-config':(root/'control/deepseek-provider.json').touch(mode=0o600)
            elif fault=='journal':Path(str(root/'shared/data/carton_erp.sqlite3')+'-wal').touch(mode=0o600)
            elif fault=='process':monkeypatch.setattr(Manager,'_process',lambda self:object())
            elif fault=='activation':(t.x.base/'factory-activation.json').touch()
    with pytest.raises(ValueError):recheck()
    assert list((root/'staging').glob('current-mac-audit-*'))


def test_borrowed_lock_stays_with_parent_and_foreign_fd_refuses(target):
    path=target.x.root/'control/operation.lock'
    with path.open('rb') as parent, path.open('rb') as other:
        fcntl.flock(parent.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        with target.run(inherited_lock=parent.fileno()) as (_,check):check()
        with pytest.raises(BlockingIOError):fcntl.flock(other.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(ValueError):
            with target.run(inherited_lock=True):pytest.fail('bool descriptor')
    with target.copy.open('rb') as foreign:
        with pytest.raises(ValueError):
            with target.run(inherited_lock=foreign.fileno()):pytest.fail('foreign descriptor')


def test_unavailable_process_identity_and_busy_lock_do_not_mean_stopped(target,monkeypatch):
    path=target.x.root/'control/operation.lock'
    with path.open('rb') as other:
        fcntl.flock(other.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(ValueError):
            with target.run():pytest.fail('busy lock accepted')
    def denied(self):raise PermissionError('synthetic process authority unavailable')
    monkeypatch.setattr(Manager,'_process',denied)
    with pytest.raises(PermissionError):
        with target.run():pytest.fail('unknown process treated as stopped')


def test_preparation_is_detached_and_expected_facts_cannot_be_changed(target):
    with target.run() as (prep,check):
        original=deepcopy(prep);prep['target_snapshot']['facts_sha256']='0'*64
        assert check()==original
    invalid=deepcopy(original);invalid['shared_manifest_sha256']='0'*64
    with pytest.raises(ValueError):
        with target.run(expected_preparation=invalid):pytest.fail('different prep accepted')


def test_expected_preparation_cannot_claim_future_preparation_time(target):
    with target.run() as (prep,_):pass
    prep['prepared_at']=contract._stamp(datetime.now(timezone.utc)+timedelta(days=1))
    with pytest.raises(ValueError):
        with target.run(expected_preparation=prep):pass


@pytest.mark.parametrize('target',['large'],indirect=True)
def test_full_private_manifest_and_state_are_not_limited_to_public_64k(target):
    manifest=migration.files(target.x.root/'shared')
    assert len(facts.canonical(manifest))>65536
    rewrite(target.x.root/'state.json',lambda value:value.update(migration_files=manifest))
    assert (target.x.root/'state.json').stat().st_size>65536
    with target.run() as (prep,_):
        assert prep['shared_manifest_sha256']==facts.digest(manifest)
        assert len(contract.canonical(prep))<65536


@pytest.mark.parametrize('raw',[b'{"current":1,"current":2}',b'{"bad":NaN}',b'{"bad":1e309}'])
def test_state_strict_decoder_rejects_duplicate_or_nonfinite(target,raw):
    (target.x.root/'state.json').write_bytes(raw)
    with pytest.raises(ValueError):
        with target.run():pytest.fail('invalid state accepted')


def test_change_during_secret_comparison_is_caught_before_yield(target,monkeypatch):
    original=transfer.collect
    def changed(*args,**kwargs):
        value=original(*args,**kwargs)
        cfg.CONTROL.write_bytes(cfg.CONTROL.read_bytes()+b' ')
        return value
    monkeypatch.setattr(transfer,'collect',changed)
    with pytest.raises(ValueError):
        with target.run():pytest.fail('late root mutation accepted')


def test_gcm_authentication_failure_keeps_partial_but_does_not_extract(target,monkeypatch):
    # Refresh only the synthetic public receipt/hash to reach GCM, not merely
    # fail its earlier byte/FD check. The encrypted tag is deliberately invalid.
    t=target;raw=t.copy.read_bytes();t.copy.write_bytes(raw[:-1]+bytes([raw[-1]^1]))
    value=deepcopy(t.receipt);value['sha256']=sha(t.copy)
    for observation in value['transfer'].values():observation['sha256']=value['sha256']
    value['local_copy'].update(sha256=value['sha256'],identity=receipts.fingerprint(t.copy.stat()))
    t.receipt_path.write_bytes(cfg.canonical(value))
    rewrite(t.x.root/'state.json',lambda state:state['last_backup_stream'].update(receipt_sha256=sha(t.receipt_path)))
    from cryptography.exceptions import InvalidTag
    with pytest.raises((ValueError,InvalidTag)):
        with t.run():pytest.fail('bad GCM accepted')
    job=next((t.x.root/'staging').glob('current-mac-audit-*'))
    assert (job/'recovery.zip').exists() and not (job/'payload').exists()


def credential_record():
    reference='tm-keychain-v1:backup:'+'a'*32
    return dict(type=transfer.TYPE,source_platform='darwin',bound_backup_sha256=None,
        files={'preferences.json':json.dumps(dict(protected_password=reference,nas='synthetic stream'))},
        entries={'backup':dict(value='synthetic-transfer-password',source_protected_sha256=hashlib.sha256(reference.encode()).hexdigest()),
                 'mailbox':dict(value='synthetic-mailbox',source_protected_sha256='b'*64,source_version=2)})


@pytest.mark.parametrize('fault',['platform','bound','purpose','secret','protected-reference','mailbox-version',
                                 'configuration','extra-field','backup-password'])
def test_credentials_have_no_normalization_or_ignored_configuration(fault):
    original=credential_record();fresh=deepcopy(original);password=original['entries']['backup']['value']
    audit._credentials(original,fresh,password)
    if fault=='platform':fresh['source_platform']='win32'
    elif fault=='bound':fresh['bound_backup_sha256']='c'*64
    elif fault=='purpose':fresh['entries'].pop('mailbox')
    elif fault=='secret':fresh['entries']['mailbox']['value']='changed'
    elif fault=='protected-reference':fresh['entries']['mailbox']['source_protected_sha256']='c'*64
    elif fault=='mailbox-version':fresh['entries']['mailbox']['source_version']=3
    elif fault=='configuration':fresh['files']['preferences.json']+=' '
    elif fault=='extra-field':fresh['entries']['mailbox']['extra']='not allowed'
    elif fault=='backup-password':password='wrong synthetic password'
    with pytest.raises(ValueError):audit._credentials(original,fresh,password)


def test_preparation_composes_with_real_v3_signature_and_later_drift_refuses(target):
    from desktop_assistant import current_mac_cutover as codec
    from tests.desktop_assistant.test_current_mac_cutover_contract import valid_body, valid_network
    t=target
    with t.run() as (prep,check):
        now=datetime.now(timezone.utc)
        request=contract.make_request(prep,contract.digest(valid_network()),prep['publisher_sha256'],
                                      request_id='2'*32,now=now)
        # Explicit synthetic Windows stop evidence. No original Windows/DPAPI
        # audit is claimed by this Mac-only integration test.
        source=valid_body(contract)['source_stop']
        source.update(request_sha256=contract.digest(request),audit_started_at=contract._stamp(now),
                      verified_at=contract._stamp(now))
        authorization=codec.sign_authorization(request,source,prep,t.c.key,now=now)
        body=codec.verify_authorization(authorization,t.c.public,request)
        codec.require_install_window(body,now=now)
        assert body['preparation']==check()
        assert body['source_stop']['database_sha256'] != prep['target_snapshot']['database_sha256']
    with closing(sqlite3.connect(t.x.root/'shared/data/carton_erp.sqlite3')) as db:
        db.execute("UPDATE customers SET note='newer business after signed preparation'");db.commit()
    with pytest.raises(ValueError):
        with t.run(expected_preparation=body['preparation']):pytest.fail('stale signed preparation accepted')


def test_supervisor_maintenance_busy_status_is_not_mistaken_for_a_live_erp(target):
    path=target.x.root/'control/service-status.json'
    path.write_text('{"status":"maintenance-busy"}');path.chmod(0o600)
    with target.run() as (_,check):
        path.write_text('{"status":"maintenance-busy","tick":2}')
        check()
