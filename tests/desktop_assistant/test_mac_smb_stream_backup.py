"""Full synthetic ERP backup/restore through actual SMB structures and FD IO."""
import copy
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from desktop_assistant import mac_smb_stream_backup as subject
from desktop_assistant import mac_smb_stream_files as files
from desktop_assistant.storage import sha, database_info
from tests.desktop_assistant.test_credential_restore import recovery_case, host_store
from tests.desktop_assistant.test_mac_smb_stream_files import Peer, contract
from tests.desktop_assistant.test_recovery import TestManager, PASSWORD

pytestmark=pytest.mark.skipif(sys.platform!='darwin',reason='Mac private directory/ACL leases')


@pytest.fixture
def case(recovery_case):
    case=recovery_case
    (case.manager.root/'staging').chmod(0o700)
    case.peer=Peer();case.calls=[]
    def transfer(source,destination,name,size,digest,attempt):
        case.calls.append(name)
        with files.open_archive(case.peer.tree(),contract()) as archive:
            up=archive.upload(source,name,size,digest,attempt)
        with files.open_archive(case.peer.tree(),contract()) as archive:
            down=archive.download(destination,up)
        return dict(upload=up,download=down)
    case.transfer=transfer
    return case


def run(case,transfer=None):
    with case.manager.lock():
        return subject.build_and_verify_stopped(case.manager,PASSWORD,contract(),transfer or case.transfer)


def test_full_backup_roundtrip_restores_all_facts_attachments_and_credentials(case,host_store):
    root=case.manager.root;state=(root/'state.json').read_bytes()
    db=root/'shared/data/carton_erp.sqlite3';before=sha(db)
    result=run(case)
    assert result['backup_authenticated'] is True and result['state_published'] is False
    assert (root/'state.json').read_bytes()==state and sha(db)==before
    assert not (root/'control/backup-receipts').exists()
    assert len(case.calls)==1 and not case.peer.handles
    job=Path(result['job']);cipher=job/'download.tmbackup'
    assert cipher.stat().st_size==result['size'] and sha(cipher)==result['sha256']
    assert sha(job/'recovery.zip')==sha(job/'readback-authenticated.zip')==result['raw_sha256']
    assert job.parent==root/'staging' and job.stat().st_mode & 0o777==0o700
    assert all(p.stat().st_mode & 0o777==0o600 for p in job.iterdir())
    target=TestManager(case.root/'stream-restored',case.public)
    restored=target.restore(cipher,PASSWORD)
    assert restored['credentials']['restored']==['backup','mailbox','openai']
    assert restored['started'] is False
    assert database_info(target.root/'shared/data/carton_erp.sqlite3')==database_info(db)
    assert (target.root/'shared/data/drawing.pdf').read_bytes()==(root/'shared/data/drawing.pdf').read_bytes()
    with sqlite3.connect(target.root/'shared/data/carton_erp.sqlite3') as connection:
        row=connection.execute('SELECT encrypted_secret,version,automatic_enabled,uid FROM email_intake_settings').fetchone()
    assert row[1:]==(9,1,'read-position-42')
    assert host_store[row[0]]=='synthetic-secret-mailbox'
    assert sha(db)==before and target.state['current']==case.manager.state['current']


@pytest.mark.parametrize('fault',['upload_hash','download_hash','connection','file_identity',
    'directory_identity','already_authenticated','contract','missing','extra'])
def test_unbound_transport_proof_never_authenticates_or_updates_state(case,fault):
    state=case.manager.state
    def transfer(*args):
        result=case.transfer(*args);result=copy.deepcopy(result)
        if fault=='upload_hash':result['upload']['sha256']='0'*64
        elif fault=='download_hash':result['download']['sha256']='0'*64
        elif fault=='connection':result['download']['connection_guid']=result['upload']['connection_guid']
        elif fault=='file_identity':result['download']['file']['index']+=1
        elif fault=='directory_identity':result['download']['directories'][0]['index']+=1
        elif fault=='already_authenticated':result['download']['backup_authenticated']=True
        elif fault=='contract':result['download']['contract_sha256']='0'*64
        elif fault=='missing':result.pop('download')
        else:result['extra']='not allowed'
        return result
    with pytest.raises(ValueError):run(case,transfer)
    assert case.manager.state==state
    jobs=list((case.manager.root/'staging').glob('smb-stream-*'))
    assert len(jobs)==1 and (jobs[0]/'download.tmbackup').exists()
    assert not (jobs[0]/'readback-authenticated.zip').exists()


def test_download_bytes_checked_independently_of_transport_claim(case):
    state=case.manager.state
    def transfer(src,dst,*args):
        result=case.transfer(src,dst,*args)
        os.pwrite(dst,b'corrupt',100)
        return result
    with pytest.raises(ValueError):run(case,transfer)
    assert case.manager.state==state


def test_database_change_during_transfer_prevents_final_verification(case):
    state=case.manager.state
    def transfer(*args):
        result=case.transfer(*args)
        with sqlite3.connect(case.manager.root/'shared/data/carton_erp.sqlite3') as db:
            db.execute("INSERT INTO customers VALUES(2,'synthetic concurrent row')")
        return result
    with pytest.raises(ValueError):run(case,transfer)
    assert case.manager.state==state


@pytest.mark.parametrize('fault',['root_permissions','staging_permissions','staging_link','running','space'])
def test_local_readiness_failure_never_calls_transport_or_creates_job(case,monkeypatch,fault):
    root=case.manager.root;staging=root/'staging'
    if fault=='root_permissions':root.chmod(0o755)
    elif fault=='staging_permissions':staging.chmod(0o755)
    elif fault=='staging_link':
        staging.rename(root/'kept-staging');staging.symlink_to(root/'kept-staging')
    elif fault=='running':monkeypatch.setattr(case.manager,'_process',lambda:object())
    else:monkeypatch.setattr(subject.shutil,'disk_usage',lambda *_:SimpleNamespace(free=0))
    with pytest.raises(ValueError):run(case)
    assert not case.calls and not list(staging.glob('smb-stream-*'))


def test_missing_original_signature_stops_before_transfer(case):
    package=case.manager.root/'packages'/(case.manager.state['current']+'.zip')
    package.write_bytes(b'synthetic damaged release')
    with pytest.raises(ValueError):run(case)
    assert not case.calls


def test_credential_export_failure_keeps_job_without_transfer(case,monkeypatch):
    from desktop_assistant import credential_transfer
    def fail(*args):raise ValueError('synthetic unavailable credential')
    monkeypatch.setattr(credential_transfer,'export',fail)
    state=case.manager.state
    with pytest.raises(ValueError):run(case)
    assert not case.calls and case.manager.state==state
    assert len(list((case.manager.root/'staging').glob('smb-stream-*')))==1


def test_no_automatic_stream_route_on_existing_nas_failure(case,monkeypatch):
    monkeypatch.setattr(subject,'build_and_verify_stopped',lambda *a:pytest.fail('silent local fallback'))
    with pytest.raises(ValueError):case.manager.backup(PASSWORD,case.root/'absent-nas')
    assert not list((case.manager.root/'staging').glob('smb-stream-*'))


@pytest.mark.parametrize('fault',['job_replaced','source_permissions','state_changed'])
def test_late_namespace_or_state_changes_prevent_verified_result(case,fault):
    original_state=case.manager.state
    def transfer(*args):
        result=case.transfer(*args)
        job=next((case.manager.root/'staging').glob('smb-stream-*'))
        if fault=='job_replaced':
            job.rename(job.with_name('retained-'+job.name));job.mkdir(mode=0o700)
        elif fault=='source_permissions':(job/args[2]).chmod(0o644)
        else:
            from desktop_assistant.storage import write_json
            write_json(case.manager.root/'state.json',{**original_state,'unexpected_synthetic_change':True})
        return result
    with pytest.raises(ValueError):run(case,transfer)
    assert 'last_backup' not in case.manager.state


def test_private_creation_mask_restored_after_export_failure(case,monkeypatch):
    from desktop_assistant import credential_transfer
    original=os.umask(0o022);os.umask(original)
    def fail(*args):raise ValueError('synthetic secret unavailable')
    monkeypatch.setattr(credential_transfer,'export',fail)
    with pytest.raises(ValueError):run(case)
    observed=os.umask(original)
    assert observed==original
