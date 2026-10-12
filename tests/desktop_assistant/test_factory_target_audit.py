"""Authenticated synthetic restore, real SQL/archives/FDs, adapted OS identity.

No production data, real service user, root installation or operational secret.
"""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import uuid

import pytest

from desktop_assistant import factory_target_audit as audit, factory_cutover as protocol
from desktop_assistant import credential_store, credential_transfer, mac_service_manage as manage
from desktop_assistant import mac_service_restore as restore, mac_release_publish as publication
from desktop_assistant import mac_smb_stream_runtime_contract as stream_cfg, mac_smb_stream_target as stream
from desktop_assistant.storage import (sha, read_json, write_json, decrypt_file, encrypt_file,
                                       extract_verified, pack_recovery)
from tests.desktop_assistant import test_mac_service_restore as fixture_module
from tests.desktop_assistant.test_mac_service_restore import case, recovery_case, host_store, PASSWORD
from tests.desktop_assistant.test_factory_network import lan

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='Mac target protected FD contract')


@pytest.fixture
def target(case, monkeypatch, request):
    c = case.fixture
    stream_requested = getattr(request, 'param', None) == 'stream'
    # A fresh synthetic Windows final backup with its true encryption password
    # embedded. The earlier fixture intentionally uses a different backup secret.
    original_raw=c.root/'original.zip';decrypt_file(case.backup, original_raw, PASSWORD)
    payload=c.root/'source-payload';metadata=extract_verified(original_raw,payload)
    record=credential_transfer.unseal((payload/'credentials.tmencrypted').read_bytes(),PASSWORD)
    record['source_platform']='win32';record['entries']['backup']['value']=PASSWORD
    (payload/'credentials.tmencrypted').write_bytes(credential_transfer.seal(record,PASSWORD))
    final_raw=c.root/'final.zip';final=c.root/'final.tmbackup'
    pack_recovery(payload/'shared', {n:payload/n for n in metadata['files'] if not n.startswith('shared/')},
                  final_raw,metadata)
    encrypt_file(final_raw,final,PASSWORD)
    native_pack=fixture_module.pack_tree
    def pack(source,destination,meta,key):
        if meta.get('runtime_platform')=='macos-arm64':
            meta=deepcopy(meta);meta['runtime_capabilities'][audit.CAPABILITY]=1
            meta['reader_capabilities']['factory_cutover_v1']=1
            if stream_requested: meta['runtime_capabilities'][stream_cfg.CAPABILITY]=1
            if getattr(request,'param',None)=='missing-audit':meta['runtime_capabilities'].pop(audit.CAPABILITY)
            elif getattr(request,'param',None)=='bool-audit':meta['runtime_capabilities'][audit.CAPABILITY]=True
            elif getattr(request,'param',None)=='missing-cutover':meta['reader_capabilities'].pop('factory_cutover_v1')
        return native_pack(source,destination,meta,key)
    monkeypatch.setattr(fixture_module,'pack_tree',pack)
    extra = stream_cfg.FILES | {stream.MODULE} if stream_requested else set()
    monkeypatch.setattr(publication,'SERVICE_RESTORE_FILES',publication.SERVICE_RESTORE_FILES|{audit.MODULE}|extra)
    def protect(value,purpose):
        reference='tm-keychain-v1:'+purpose+':'+uuid.uuid4().hex
        case.secrets[reference]=value
        return reference
    monkeypatch.setattr(credential_store,'protect',protect)
    x=case.install(final);x.run()
    monkeypatch.setattr(audit,'__file__',str(x.slot/'migration'/audit.MODULE))
    monkeypatch.setattr(manage,'BASE',x.base);monkeypatch.setattr(manage,'ROOT',x.root)
    monkeypatch.setattr(credential_transfer,'unprotect',credential_store.unprotect)
    monkeypatch.setattr(credential_store,'unprotect_backup',lambda ref:case.secrets[ref])
    # No remote IO and no real system NAS contract are simulated as verified.
    contract={'destination':str(x.base/'synthetic-nas')}
    monkeypatch.setattr(audit.remote,'_contract',lambda:(deepcopy(contract),'b'*64,deepcopy(x.context)))
    def forbidden(*args,**kwargs):raise AssertionError('no process, network, host writes or migration')
    monkeypatch.setattr(audit.remote,'before_write',forbidden)
    monkeypatch.setattr(credential_store,'protect',forbidden)
    monkeypatch.setattr(audit.Manager,'stop',forbidden)
    preferences=read_json(x.root/'preferences.json');preferences['nas']=contract['destination']
    write_json(x.root/'preferences.json',preferences);(x.root/'preferences.json').chmod(0o600)
    now=datetime.now(timezone.utc)
    identity={'host_sha256':'c'*64,'installation_id':x.context['installation_id'],
        'service_uid':499,'service_gid':499,'service_account':'_tianmingerp','root':str(x.root)}
    monkeypatch.setattr(protocol,'local_target',lambda *a:deepcopy(identity))
    request=protocol.make_network_request(identity,x.id,lan(),c.public,now=now-timedelta(minutes=5))
    snapshot=protocol.capture_snapshot(payload/'shared',recorded_root=metadata['source_shared'])
    evidence={'source_sha256':'d'*64,'backup_sha256':sha(final),'source_package_sha256':metadata['release'],
        'backup_created_at':protocol._stamp(datetime.fromisoformat(metadata['created'])),
        'manual_stop':True,'process_stopped':True,'credential_purposes':sorted(record['entries']),'snapshot':snapshot}
    auth=protocol.sign_authorization(request,evidence,c.key,now=now)
    (x.attempt/'authorization.json').write_bytes(auth);(x.attempt/'authorization.json').chmod(0o440)
    for name,value in [('factory-target-'+request['request_id'],request),('factory-network-'+request['request_id'],lan())]:
        write_json(x.base/(name+'.json'),value)
    def run():return audit.verified_restored_target(request['request_id'],x.args.input_attempt,
                                                   sha(x.attempt/'authorization.json'))
    return SimpleNamespace(x=x,c=c,case=case,request=request,evidence=evidence,auth=auth,run=run,
                           metadata=metadata,payload=payload,record=record,contract=contract,now=now)


def test_target_audit_context_exists():
    from desktop_assistant.factory_target_audit import verified_restored_target
    assert callable(verified_restored_target)


def protected_facts(x):
    return {str(p.relative_to(x.root)):sha(p) for p in x.root.rglob('*')
            if p.is_file() and not p.is_symlink() and 'staging' not in p.relative_to(x.root).parts}


def test_authenticates_restored_facts_credentials_and_originals_without_mutation(target):
    t=target;before=protected_facts(t.x);secrets=dict(t.case.secrets)
    with t.run() as (evidence,recheck):
        assert evidence['credentials_verified'] is True and evidence['factory_activated'] is False
        assert evidence['backup_sha256']==t.evidence['backup_sha256']
        protocol.require_matching_snapshots(t.evidence['snapshot'],evidence['target_snapshot'])
        assert evidence['target_snapshot']['database_sha256']!=t.evidence['snapshot']['database_sha256']
        assert evidence['credential_purposes']==['backup','mailbox','openai']
        assert not evidence['network_reachability_verified'] and not evidence['source_physical_isolation_verified']
        job=Path(evidence['retained_workspace'])
        assert job.is_dir() and job.stat().st_mode & 0o777==0o700
        assert (job/'recovery.zip').stat().st_mode & 0o777==0o600
        assert PASSWORD not in str(evidence) and 'synthetic-secret' not in str(evidence)
        recheck()
    assert job.exists() and protected_facts(t.x)==before and secrets==t.case.secrets


def test_saved_mounted_destination_is_not_redirected_by_new_stream_metadata(target, monkeypatch):
    def forbidden(*args, **kwargs):raise AssertionError('saved mounted route must remain selected')
    monkeypatch.setattr(stream, 'metadata_present', lambda:True)
    monkeypatch.setattr(stream, 'prepare', forbidden)
    before=protected_facts(target.x)
    with target.run() as (evidence,recheck):
        assert evidence['credentials_verified']; recheck()
    assert protected_facts(target.x)==before


def rewrite(path, change):
    mode=path.stat().st_mode & 0o777
    value=read_json(path);change(value);write_json(path,value);path.chmod(mode)


@pytest.mark.parametrize('fault',[
    'state-backup','state-current','state-running','state-operation','state-previous',
    'binding-bool','binding-context','credential-report','receipt-bool','receipt-external',
    'network-digest','request-host','request-publisher','auth-signature','auth-hash','expired',
    'backup-bytes','extra-input','wrong-nas','extra-preference','ai-provider-option',
    'mail-position','mail-version','business-row','attachment','environment-stable','environment-network',
    'wal','shm','journal','source-original','source-authority','source-state','missing-config',
    'credential-ai','credential-mail','credential-backup','process-running','extra-groups','wrong-module',
    'shared-mode','hardlink','symlink','db-mode','input-mode','lock-content','current-cache',
])
def test_mismatch_refuses_and_never_repairs_or_mutates(target,monkeypatch,fault):
    t=target;x=t.x;state=x.root/'state.json'
    receipt=x.home/('service-restore-'+x.args.input_attempt+'.json')
    db=x.root/'shared/data/carton_erp.sqlite3'
    change_state={
        'state-backup':('restored_backup_sha256','f'*64),'state-current':('current','f'*64),
        'state-running':('manual_stop',False),'state-operation':('operation','update'),
        'state-previous':('previous','f'*64),'credential-report':('credential_restore',{}),
        'source-state':('restored_release_sha256','f'*64),
    }
    if fault in change_state:
        key,value=change_state[fault];rewrite(state,lambda s:s.update({key:value}))
    elif fault.startswith('binding-'):
        rewrite(state,lambda s:s['native_restore_binding'].update(
            {'schema_migrated':0} if fault=='binding-bool' else {'context_sha256':'f'*64}))
    elif fault.startswith('receipt-'):
        rewrite(receipt,lambda v:v.update({'started':0} if fault=='receipt-bool' else {'credentials_sha256':'f'*64}))
    elif fault=='network-digest':
        rewrite(x.base/('factory-network-'+t.request['request_id']+'.json'),lambda v:v.update(ERP_PORT='8001'))
    elif fault.startswith('request-'):
        key='host_sha256' if fault=='request-host' else 'publisher_sha256'
        rewrite(x.base/('factory-target-'+t.request['request_id']+'.json'),
                lambda v:(v['target'] if fault=='request-host' else v).update({key:'f'*64}))
    elif fault=='auth-signature':
        rewrite(x.attempt/'authorization.json',lambda v:v['body']['source'].update(source_sha256='f'*64))
    elif fault=='auth-hash':
        t.run=lambda:audit.verified_restored_target(t.request['request_id'],x.args.input_attempt,'f'*64)
    elif fault=='expired':monkeypatch.setattr(audit,'_current',lambda:t.now+timedelta(hours=9))
    elif fault=='backup-bytes':
        raw=x.backup.read_bytes();x.backup.chmod(0o600);x.backup.write_bytes(raw[:-1]+bytes([raw[-1]^1]));x.backup.chmod(0o440)
    elif fault=='extra-input':(x.attempt/'unexpected').write_bytes(b'keep')
    elif fault in {'wrong-nas','extra-preference'}:
        rewrite(x.root/'preferences.json',lambda v:v.update({'nas':'different'} if fault=='wrong-nas' else {'new':True}))
    elif fault=='ai-provider-option':rewrite(x.root/'control/ai-provider.json',lambda v:v.update(model='changed'))
    elif fault in {'mail-position','mail-version','business-row'}:
        with closing(sqlite3.connect(db)) as connection:
            connection.execute({'mail-position':"UPDATE email_intake_settings SET uid='changed'",
                'mail-version':'UPDATE email_intake_settings SET version=10',
                'business-row':"UPDATE sales_orders SET note='changed'"}[fault]);connection.commit()
    elif fault=='attachment':(x.root/'shared/data/drawing.pdf').write_bytes(b'changed')
    elif fault.startswith('environment-'):
        rewrite(x.root/'shared/environment.json',lambda v:v.update(
            {'ERP_SECRET_KEY':'changed'} if fault=='environment-stable' else {'ERP_PORT':'19999'}))
    elif fault in {'wal','shm','journal'}:Path(str(db)+'-'+fault).touch(mode=0o600)
    elif fault.startswith('source-'):
        key='restored_release_sha256' if fault=='source-original' else 'restored_schema_authority_sha256'
        (x.root/'packages'/(read_json(state)[key]+'.zip')).write_bytes(b'changed')
    elif fault=='missing-config':(x.root/'control/ai-provider.json').rename(x.root/'control/ai-preserved.json')
    elif fault.startswith('credential-'):
        purpose={'credential-ai':'openai','credential-mail':'mailbox','credential-backup':'backup'}[fault]
        key=next(k for k in t.case.secrets if ':'+purpose+':' in k);t.case.secrets[key]='changed'
    elif fault=='process-running':monkeypatch.setattr(audit.Manager,'_process',lambda s:(1,{}))
    elif fault=='extra-groups':
        monkeypatch.setattr(restore.bootstrap,'_actual',lambda:{'uid':499,'euid':499,'gid':499,'egid':499,'kernel_groups':[499,61]})
    elif fault=='wrong-module':monkeypatch.setattr(audit,'__file__',str(t.c.root/'unapproved.py'))
    elif fault=='shared-mode':(x.root/'shared/data').chmod(0o755)
    elif fault=='hardlink':os.link(x.root/'shared/data/drawing.pdf',x.root/'shared/data/linked.pdf')
    elif fault=='symlink':(x.root/'shared/data/linked.pdf').symlink_to(x.root/'shared/data/drawing.pdf')
    elif fault=='db-mode':db.chmod(0o644)
    elif fault=='input-mode':x.backup.chmod(0o640)
    elif fault=='lock-content':(x.root/'control/operation.lock').write_bytes(b'1')
    elif fault=='current-cache':(x.root/'packages'/(x.id+'.zip')).write_bytes(b'changed')
    else:raise AssertionError(fault)
    before=protected_facts(x);secrets=dict(t.case.secrets)
    with pytest.raises(Exception) as error:
        with t.run():pytest.fail('audit accepted a mismatched target')
    assert not isinstance(error.value,AssertionError)
    assert protected_facts(x)==before and t.case.secrets==secrets
    assert PASSWORD not in str(error.value) and 'synthetic-secret' not in str(error.value)


@pytest.mark.parametrize('fault',['database','configuration','new-config','backup','state','lock','contract',
                                  'directory','secret','expired','empty-shm','source-original'])
def test_changes_while_consumer_holds_context_refuse_on_exit_and_remain_for_inspection(target,monkeypatch,fault):
    t=target;x=t.x
    with pytest.raises(ValueError):
        with t.run() as (evidence,recheck):
            if fault=='database':
                with closing(sqlite3.connect(x.root/'shared/data/carton_erp.sqlite3')) as db:
                    db.execute("UPDATE customers SET note='late change'");db.commit()
            elif fault=='configuration':rewrite(x.root/'preferences.json',lambda v:v.update(extra='keep'))
            elif fault=='new-config':(x.root/'control/deepseek-provider.json').write_text('{}')
            elif fault=='backup':x.backup.chmod(0o600)
            elif fault=='state':rewrite(x.root/'state.json',lambda v:v.update(manual_stop=False))
            elif fault=='lock':
                lock=x.root/'control/operation.lock';lock.rename(lock.with_name('operation.preserved'));lock.write_bytes(b'0');lock.chmod(0o600)
            elif fault=='contract':t.contract['destination']='different'
            elif fault=='directory':(x.root/'shared/data').chmod(0o755)
            elif fault=='secret':t.case.secrets[next(k for k in t.case.secrets if ':mailbox:' in k)]='late change'
            elif fault=='expired':monkeypatch.setattr(audit,'_current',lambda:t.now+timedelta(hours=9))
            elif fault=='empty-shm':Path(str(x.root/'shared/data/carton_erp.sqlite3')+'-shm').touch(mode=0o600)
            elif fault=='source-original':
                source=read_json(x.root/'state.json')['restored_release_sha256']
                (x.root/'packages'/(source+'.zip')).write_bytes(b'late change')
    assert Path(evidence['retained_workspace']).exists()


def test_evidence_mutation_cannot_change_internal_verification(target):
    with target.run() as (evidence,recheck):
        evidence['target_snapshot']['facts_sha256']='f'*64
        evidence['credential_purposes'].clear()
        recheck()


def test_busy_lock_fails_before_decryption_without_replacing_lock(target):
    import fcntl
    lock=target.x.root/'control/operation.lock';before=lock.stat().st_ino
    with lock.open('rb') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(ValueError):
            with target.run():pytest.fail('accepted busy lock')
    assert lock.stat().st_ino==before and not list((target.x.root/'staging').glob('factory-audit-*'))


@pytest.mark.parametrize('target',['missing-audit','bool-audit','missing-cutover'],indirect=True)
def test_signed_native_package_must_declare_exact_audit_and_factory_capabilities(target):
    with pytest.raises(ValueError,match='最终核验'):
        with target.run():pytest.fail('accepted incomplete signed protocol')
    assert not list((target.x.root/'staging').glob('factory-audit-*'))


@pytest.mark.parametrize('fault',['snapshot-facts','snapshot-database','snapshot-files','source-package',
                                  'source-time','credential-purposes','gcm-tag'])
def test_signature_alone_cannot_substitute_for_authenticated_target_evidence(target,fault):
    t=target;x=t.x;evidence=deepcopy(t.evidence)
    if fault.startswith('snapshot-'):
        key={'snapshot-facts':'facts_sha256','snapshot-database':'database_sha256','snapshot-files':'files_sha256'}[fault]
        evidence['snapshot'][key]='f'*64
    elif fault=='source-package':evidence['source_package_sha256']='f'*64
    elif fault=='source-time':evidence['backup_created_at']=protocol._stamp(t.now-timedelta(minutes=2))
    elif fault=='credential-purposes':
        evidence['credential_purposes'].remove('openai')
        def omit(value):value['credential_restore']['restored'].remove('openai')
        rewrite(x.root/'state.json',omit)
        rewrite(x.home/('service-restore-'+x.args.input_attempt+'.json'),
                lambda v:v['result']['credentials']['restored'].remove('openai'))
    elif fault=='gcm-tag':
        raw=x.backup.read_bytes();x.backup.chmod(0o600);x.backup.write_bytes(raw[:-1]+bytes([raw[-1]^1]));x.backup.chmod(0o440)
        evidence['backup_sha256']=sha(x.backup)
        rewrite(x.root/'state.json',lambda s:(s.update(restored_backup_sha256=evidence['backup_sha256']),
                         s['native_restore_binding'].update(backup_sha256=evidence['backup_sha256'])))
        rewrite(x.home/('service-restore-'+x.args.input_attempt+'.json'),lambda v:(
            v.update(backup_sha256=evidence['backup_sha256']),v['native_binding'].update(backup_sha256=evidence['backup_sha256'])))
    auth=protocol.sign_authorization(t.request,evidence,t.c.key,now=t.now)
    file=x.attempt/'authorization.json';file.chmod(0o600);file.write_bytes(auth);file.chmod(0o440)
    before=protected_facts(x)
    with pytest.raises(Exception) as error:
        with t.run():pytest.fail('accepted inconsistent authenticated content')
    assert not isinstance(error.value,AssertionError) and protected_facts(x)==before


def test_late_attachment_change_during_secret_read_is_refused_before_yield(target,monkeypatch):
    collect=credential_transfer.collect
    def changed(*args,**kwargs):
        result=collect(*args,**kwargs)
        (target.x.root/'shared/data/drawing.pdf').write_bytes(b'late retained attachment')
        return result
    monkeypatch.setattr(credential_transfer,'collect',changed)
    with pytest.raises(ValueError):
        with target.run():pytest.fail('stale attachment evidence reached activation consumer')
