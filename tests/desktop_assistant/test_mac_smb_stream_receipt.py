"""Full synthetic recovery + exclusive receipts; no installed NAS or secrets."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest

from desktop_assistant import mac_smb_stream_receipt as subject
from desktop_assistant.storage import sha, database_info, write_json
from tests.desktop_assistant.test_credential_restore import recovery_case, host_store
from tests.desktop_assistant.test_mac_smb_stream_files import Peer, contract
from tests.desktop_assistant.test_recovery import TestManager, PASSWORD
from test_mac_smb_stream_runtime import control

pytestmark=pytest.mark.skipif(sys.platform!='darwin',reason='Mac private receipt and file leases')


@pytest.fixture
def case(recovery_case,monkeypatch):
    case=recovery_case;root=case.root/'instance'
    case.manager.root.rename(root);case.manager.root=root
    for name in ('','control','backups','staging'):(root/name).chmod(0o700)
    (root/'state.json').chmod(0o600)
    cfg=subject.cfg
    monkeypatch.setattr(cfg,'BASE',case.root)
    value=contract();ctl=control();ctl['contract_sha256']=subject.archive.digest(value)
    ctl['credential_account']=cfg.account(ctl['context_sha256'],ctl['contract_sha256'],ctl['attempt_id'])
    context=dict(uid=os.geteuid(),gid=os.getegid())
    binding=SimpleNamespace(control=ctl,value=value,context=context,control_sha='9'*64,
        tree=Path(subject.__file__).resolve().parent.parent,check=lambda:None)
    case.binding=binding
    case.bound=dict(context=context,control=ctl,value=value,control_sha=binding.control_sha)
    monkeypatch.setattr(subject.runtime,'load_binding',lambda:binding)
    monkeypatch.setattr(subject,'approval',lambda:deepcopy(case.bound))
    case.peer=Peer();case.calls=[]
    def transfer(source,destination,name,size,digest,attempt):
        case.calls.append(name)
        with subject.runtime.files.open_archive(case.peer.tree(),value) as remote:
            up=remote.upload(source,name,size,digest,attempt)
        with subject.runtime.files.open_archive(case.peer.tree(),value) as remote:
            down=remote.download(destination,up)
        return dict(upload=up,download=down)
    monkeypatch.setattr(subject.runtime,'transfer',transfer)
    return case


def run(case):
    with case.manager.lock():return subject.archive_and_publish_stopped(case.manager,PASSWORD)


def stored(case):
    state=case.manager.state;index=state[subject.INDEX]
    path=case.manager.root/'control/backup-receipts'/index['receipt']
    return state,path,json.loads(path.read_bytes())


def test_full_backup_receipt_and_retained_copy_restore_original_facts(case,host_store):
    db=case.manager.root/'shared/data/carton_erp.sqlite3';before=sha(db)
    result=run(case);state,path,receipt=stored(case)
    assert result.parent==case.manager.root/'backups' and state['last_backup']==str(result)
    assert receipt['type']==subject.TYPE and receipt['local_copy']['path']==str(result)
    assert sha(result)==receipt['sha256'] and sha(db)==before
    assert receipt['transfer']['upload']['connection_guid']!=receipt['transfer']['download']['connection_guid']
    assert receipt['transfer']['download']['backup_authenticated'] is False
    assert subject.verified_at(case.manager.root,state)==state['last_backup_at']
    assert subject.local.verified_at(case.manager.root,state)==state['last_backup_at']
    assert state[subject.INDEX]['receipt_sha256']==subject.cfg.digest(path.read_bytes())
    assert state['backup_error'] is None and len(case.calls)==1
    target=TestManager(case.root/'roundtrip',case.public)
    restored=target.restore(result,PASSWORD)
    assert restored['credentials']['restored']==['backup','mailbox','openai']
    assert restored['started'] is False
    assert database_info(target.root/'shared/data/carton_erp.sqlite3')==database_info(db)
    assert (target.root/'shared/data/drawing.pdf').read_bytes()==b'synthetic attachment'
    assert list((case.manager.root/'staging').glob('smb-stream-*'))


@pytest.mark.parametrize('kind',['network','copy','receipt','state'])
def test_failure_does_not_record_success_and_preserves_scene(case,monkeypatch,kind):
    before=(case.manager.root/'state.json').read_bytes()
    def fail(*a,**kw):raise OSError('synthetic failure')
    if kind=='network':monkeypatch.setattr(subject.runtime,'transfer',fail)
    if kind=='copy':monkeypatch.setattr(subject,'_copy_authenticated',fail)
    if kind=='receipt':monkeypatch.setattr(subject.local,'_exclusive',fail)
    if kind=='state':monkeypatch.setattr(subject.manage,'replace_record',fail)
    with pytest.raises(ValueError,match=subject.ERROR):run(case)
    assert (case.manager.root/'state.json').read_bytes()==before
    assert list((case.manager.root/'staging').glob('smb-stream-*'))
    assert subject.verified_at(case.manager.root,case.manager.state) is None
    if kind in ('receipt','state'):assert list((case.manager.root/'backups').glob('*.tmbackup'))
    if kind=='state':assert list((case.manager.root/'control/backup-receipts').glob('*.smb-stream.json'))


def test_source_changed_after_full_authentication_cannot_be_published(case,monkeypatch):
    original=subject.builder.build_and_verify_stopped
    def changed(*a,**kw):
        result=original(*a,**kw)
        path=Path(result['job'])/'download.tmbackup'
        with path.open('r+b') as f:f.write(b'bad')
        return result
    monkeypatch.setattr(subject.builder,'build_and_verify_stopped',changed)
    before=case.manager.state
    with pytest.raises(ValueError):run(case)
    assert case.manager.state==before and not (case.manager.root/'control/backup-receipts').exists()


def test_existing_local_copy_never_overwritten(case,monkeypatch):
    original=subject._copy_authenticated
    def copy(root,context,result):
        existing=root.path/'backups'/result['filename'];existing.write_bytes(b'older-evidence');existing.chmod(0o600)
        return original(root,context,result)
    monkeypatch.setattr(subject,'_copy_authenticated',copy)
    before=case.manager.state
    with pytest.raises(ValueError):run(case)
    assert case.manager.state==before
    assert [p.read_bytes() for p in (case.manager.root/'backups').iterdir()]==[b'older-evidence']


def test_concurrent_state_change_is_preserved_not_replaced(case,monkeypatch):
    original=subject._copy_authenticated
    def changed(*a):
        copy=original(*a)
        state=case.manager.state;state['concurrent_marker']='preserve-me'
        write_json(case.manager.root/'state.json',state);(case.manager.root/'state.json').chmod(0o600)
        return copy
    monkeypatch.setattr(subject,'_copy_authenticated',changed)
    with pytest.raises(ValueError):run(case)
    assert case.manager.state['concurrent_marker']=='preserve-me' and 'last_backup' not in case.manager.state


@pytest.mark.parametrize('kind',['copy-content','copy-link','copy-mode','receipt-content','receipt-missing',
    'state-hash','state-time','state-path','state-name','root-contract'])
def test_status_never_blesses_missing_or_modified_evidence(case,kind):
    result=run(case);state,path,receipt=stored(case)
    if kind=='copy-content':
        with result.open('r+b') as f:f.write(b'x')
    if kind=='copy-link':result.rename(result.with_suffix('.original'));result.symlink_to(result.with_suffix('.original'))
    if kind=='copy-mode':result.chmod(0o644)
    if kind=='receipt-content':path.write_bytes(path.read_bytes()+b' ')
    if kind=='receipt-missing':path.rename(path.with_suffix('.preserved'))
    if kind=='state-hash':state[subject.INDEX]['receipt_sha256']='0'*64
    if kind=='state-time':state['last_backup_at']='2000-01-01T00:00:00+00:00'
    if kind=='state-path':state['last_backup']=str(case.root/'foreign.tmbackup')
    if kind=='state-name':state[subject.INDEX]['receipt']='../../other.json'
    if kind=='root-contract':case.bound['control_sha']='0'*64
    assert subject.verified_at(case.manager.root,state) is None


def test_status_reads_no_secret_network_or_payload_hash(case,monkeypatch):
    run(case);state=case.manager.state
    def fail(*a,**kw):raise AssertionError('status must be metadata only')
    monkeypatch.setattr(subject.runtime,'load_binding',fail)
    monkeypatch.setattr(subject.runtime,'transfer',fail)
    monkeypatch.setattr(subject.runtime.keychain,'ServiceStore',fail)
    monkeypatch.setattr(subject,'sha',fail)
    assert subject.verified_at(case.manager.root,state)==state['last_backup_at']


def test_stale_stream_index_does_not_redirect_later_legacy_backup(monkeypatch):
    called=[]
    monkeypatch.setattr(subject.local,'required',lambda:True)
    monkeypatch.setattr(subject,'verified_at',lambda *a,**kw:called.append('unexpected'))
    state={'last_backup':'/other/backup.tmbackup',subject.INDEX:{'path':'/old/backup.tmbackup'}}
    assert subject.local.verified_at('/other',state) is None and called==[]


@pytest.mark.parametrize('field,bad',[('type','mounted-nas'),('control_sha256','0'*64),
    ('context_sha256','0'*64),('contract_sha256','0'*64),('package_sha256','0'*64),
    ('sha256','bad'),('size',True),('raw_sha256',None),('database',[]),('filename','../x'),
    ('verified_at','2039-01-01T00:00:00+00:00'),('verified_at','2026-10-12T01:00:00')])
def test_receipt_rejects_unbound_or_invalid_fields(case,field,bad):
    run(case);_,_,value=stored(case);value[field]=bad
    with pytest.raises((ValueError,TypeError)):subject.validate(value,case.bound)


def test_proof_not_fully_authenticated_cannot_publish(case,monkeypatch):
    original=subject.builder.build_and_verify_stopped
    def incomplete(*a,**kw):
        value=original(*a,**kw);value['backup_authenticated']=False;return value
    monkeypatch.setattr(subject.builder,'build_and_verify_stopped',incomplete)
    before=case.manager.state
    with pytest.raises(ValueError):run(case)
    assert case.manager.state==before and not list((case.manager.root/'backups').iterdir())


@pytest.mark.parametrize('kind',['copy','receipt','state'])
def test_late_change_after_receipt_creation_still_prevents_success(case,monkeypatch,kind):
    original=subject.local._exclusive;before=case.manager.state
    def changed(directory,name,raw,uid):
        original(directory,name,raw,uid)
        value=json.loads(raw)
        if kind=='copy':
            with Path(value['local_copy']['path']).open('r+b') as f:f.write(b'changed')
        elif kind=='receipt':
            path=case.manager.root/'control/backup-receipts'/name
            path.write_bytes(raw+b' ')
        else:
            state=case.manager.state;state['other-operation']='preserve'
            write_json(case.manager.root/'state.json',state);(case.manager.root/'state.json').chmod(0o600)
    monkeypatch.setattr(subject.local,'_exclusive',changed)
    with pytest.raises(ValueError):run(case)
    state=case.manager.state
    assert 'last_backup' not in state
    if kind=='state':assert state['other-operation']=='preserve'
    else:assert state==before
    assert list((case.manager.root/'control/backup-receipts').glob('*.smb-stream.json'))


@pytest.fixture
def public_records(monkeypatch):
    cfg=subject.cfg;runtime=subject.runtime
    context=dict(uid=499,gid=499);context_raw=cfg.canonical(context)
    context_sha=cfg.digest(context_raw);public=b'synthetic-public'
    value=contract();value['context_sha256']=context_sha
    ctl=control();ctl.update(context_sha256=context_sha,contract_sha256=subject.archive.digest(value),
        publisher_sha256=cfg.digest(public))
    ctl['credential_account']=cfg.account(context_sha,ctl['contract_sha256'],ctl['attempt_id'])
    registry=dict(type=runtime.publication.REGISTRY_TYPE,context_sha256=context_sha,
        publisher_sha256=cfg.digest(public),packages=[ctl['package_sha256']])
    records={cfg.CONTROL:cfg.canonical(ctl),cfg.CONTRACT:cfg.canonical(value),
        runtime.keychain.CONTEXT:context_raw,runtime.published.PUBLIC:public,
        runtime.published.REGISTRY:cfg.canonical(registry)}
    monkeypatch.setattr(runtime.keychain,'load_context',lambda:deepcopy(context))
    monkeypatch.setattr(runtime,'actual_identity',lambda ctx:ctx)
    monkeypatch.setattr(runtime.publication,'read_record',lambda path:records[path])
    return SimpleNamespace(context=context,control=ctl,contract=value,registry=registry,records=records)


def test_historical_approval_binds_all_small_root_records(public_records):
    case=public_records;value=subject.approval()
    assert value['control']==case.control and value['context']==case.context
    assert value['value']==case.contract


@pytest.mark.parametrize('kind',['context','public','registry-context','registry-public','registry-package',
    'registry-type','registry-field','canonical','control','contract'])
def test_historical_approval_rejects_changed_or_incomplete_root_records(public_records,kind):
    case=public_records;cfg=subject.cfg;runtime=subject.runtime
    if kind=='context':case.context['gid']=498
    if kind=='public':case.records[runtime.published.PUBLIC]=b'new-public'
    if kind.startswith('registry-'):
        key,bad={'registry-context':('context_sha256','0'*64),'registry-public':('publisher_sha256','0'*64),
            'registry-package':('packages',['0'*64]),'registry-type':('type','other'),
            'registry-field':('unknown',True)}[kind]
        case.registry[key]=bad;case.records[runtime.published.REGISTRY]=cfg.canonical(case.registry)
    if kind=='canonical':case.records[cfg.CONTROL]+=b'\n'
    if kind=='control':case.control['credential_account']='backup:wrong';case.records[cfg.CONTROL]=cfg.canonical(case.control)
    if kind=='contract':case.contract['share']='other';case.records[cfg.CONTRACT]=cfg.canonical(case.contract)
    with pytest.raises(ValueError):subject.approval()
