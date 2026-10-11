"""Retirement touches only newly authenticated working files in synthetic jobs."""
from pathlib import Path
import json
import os
import sys
from unittest.mock import Mock

import pytest
from desktop_assistant import mac_smb_stream_work as subject
from desktop_assistant import mac_smb_stream_receipt as receipt
from desktop_assistant.storage import sha, write_json, read_json
from test_mac_smb_stream_receipt import case, run, stored
from tests.desktop_assistant.test_credential_restore import recovery_case, host_store
from tests.desktop_assistant.test_recovery import TestManager, PASSWORD

pytestmark=pytest.mark.skipif(sys.platform!='darwin',reason='Mac private file descriptors and ACL leases')


@pytest.fixture
def capture(case,monkeypatch):
    original=receipt.builder.build_and_verify_stopped
    def build(*args,**kwargs):
        result=original(*args,**kwargs);case.built=result
        return result
    monkeypatch.setattr(receipt.builder,'build_and_verify_stopped',build)
    return case


def test_success_retires_six_files_and_keeps_backup_receipt_and_small_journal(capture,host_store):
    a=capture; backup=run(a);job=Path(a.built['job'])
    assert {p.name for p in job.iterdir()}=={subject.PENDING,subject.COMPLETED}
    journal=read_json(job/subject.COMPLETED)
    assert journal['removed']==list(subject.names(a.built['filename']))
    assert journal['bytes_removed']==sum(v['identity']['size'] for v in a.built['work']['files'].values())
    assert subject.KEY not in a.manager.state and a.manager.state['backup_error'] is None
    state,path,record=stored(a)
    assert journal['receipt_sha256']==subject.cfg.digest(path.read_bytes())
    assert sha(backup)==record['sha256'] and receipt.verified_at(a.manager.root,state)==state['last_backup_at']
    target=TestManager(a.root/'after-retirement-restore',a.public)
    assert target.restore(backup,PASSWORD)['credentials']['restored']==['backup','mailbox','openai']
    assert (target.root/'shared/data/drawing.pdf').read_bytes()==b'synthetic attachment'


@pytest.mark.parametrize('kind',['extra','symlink','hardlink','modified','permissions','replaced','journal'])
def test_changed_or_unknown_work_preserved_before_any_unlink(capture,monkeypatch,kind):
    a=capture; original=subject.retire; old=a.root/'untouched';old.write_bytes(b'outside')
    calls=[]
    def changed(root,context,result,copy,raw,protect):
        job=Path(result['job']);target=job/'check.zip'
        if kind=='extra':(job/'unknown.bin').write_bytes(b'unknown')
        if kind=='symlink':target.rename(job/'original-check.zip');target.symlink_to(old)
        if kind=='hardlink':os.link(target,job/'second-link')
        if kind=='modified':target.write_bytes(b'changed')
        if kind=='permissions':target.chmod(0o644)
        if kind=='replaced':
            data=target.read_bytes();target.unlink();target.write_bytes(data);target.chmod(0o600)
        if kind=='journal':(job/subject.PENDING).write_bytes(b'prior journal')
        return original(root,context,result,copy,raw,protect)
    monkeypatch.setattr(subject,'retire',changed)
    unlink=subject.os.unlink
    def observe(name,*args,**kwargs):
        if kwargs.get('dir_fd') is not None:calls.append(name)
        return unlink(name,*args,**kwargs)
    monkeypatch.setattr(subject.os,'unlink',observe)
    backup=run(a)
    assert not calls and old.read_bytes()==b'outside' and backup.exists()
    assert a.manager.state[subject.KEY]==[a.built['job']]
    assert receipt.verified_at(a.manager.root,a.manager.state)==a.manager.state['last_backup_at']
    assert not (Path(a.built['job'])/subject.COMPLETED).exists()


def test_partial_unlink_failure_keeps_remaining_materials_and_attention(capture,monkeypatch):
    a=capture;original=subject.os.unlink;calls=[]
    def fail(name,*args,**kwargs):
        if kwargs.get('dir_fd') is not None:
            calls.append(name)
            if len(calls)==2:raise OSError('synthetic disk error')
        return original(name,*args,**kwargs)
    monkeypatch.setattr(subject.os,'unlink',fail)
    backup=run(a);job=Path(a.built['job'])
    assert calls==['recovery.zip','check.zip']
    assert not (job/'recovery.zip').exists() and (job/'check.zip').exists()
    assert (job/subject.PENDING).exists() and not (job/subject.COMPLETED).exists()
    assert a.manager.state[subject.KEY]==[str(job)] and backup.exists()
    assert a.manager.state['backup_error'] is None


def test_cleanup_stops_if_success_state_changes_between_leaf_deletions(capture,monkeypatch):
    a=capture;original=subject.os.unlink;calls=[]
    def changed(name,*args,**kwargs):
        result=original(name,*args,**kwargs)
        if (not calls and kwargs.get('dir_fd') is not None and name in subject.names(a.built['filename'])
                and os.fstat(kwargs['dir_fd']).st_ino==a.built['work']['directory']['ino']):
            calls.append(name)
            state=a.manager.state;state['unrelated_concurrent_note']='preserve'
            write_json(a.manager.root/'state.json',state);(a.manager.root/'state.json').chmod(0o600)
        return result
    monkeypatch.setattr(subject.os,'unlink',changed)
    backup=run(a)
    assert calls==['recovery.zip'] and backup.exists()
    assert a.manager.state['unrelated_concurrent_note']=='preserve'
    assert a.manager.state[subject.KEY]==[a.built['job']]


def test_local_copy_change_prevents_work_deletion(capture,monkeypatch):
    a=capture;original=subject.retire
    def changed(root,context,result,copy,raw,protect):
        with copy.path.open('r+b') as f:f.write(b'changed')
        return original(root,context,result,copy,raw,protect)
    monkeypatch.setattr(subject,'retire',changed)
    with pytest.raises(ValueError):run(a)
    job=Path(a.built['job'])
    assert set(p.name for p in job.iterdir())==set(subject.names(a.built['filename']))
    assert a.manager.state[subject.KEY]==[str(job)]
    assert receipt.verified_at(a.manager.root,a.manager.state) is None


def test_failed_backup_scene_and_preexisting_retained_jobs_are_never_scanned(capture):
    a=capture
    old=a.manager.root/'staging'/('smb-stream-'+'1'*32);old.mkdir(mode=0o700)
    original=old/'failed-evidence.bin';original.write_bytes(b'keep failed scene')
    state=a.manager.state;state[subject.KEY]=[str(old)];write_json(a.manager.root/'state.json',state)
    (a.manager.root/'state.json').chmod(0o600)
    run(a)
    assert original.read_bytes()==b'keep failed scene'
    assert list(old.iterdir())==[original] and a.manager.state[subject.KEY]==[str(old)]
    assert {p.name for p in Path(a.built['job']).iterdir()}=={subject.PENDING,subject.COMPLETED}


def test_backup_publication_failure_never_calls_retirement(capture,monkeypatch):
    a=capture;retire=Mock();monkeypatch.setattr(subject,'retire',retire)
    monkeypatch.setattr(receipt.manage,'replace_record',Mock(side_effect=ValueError('publication failed')))
    with pytest.raises(ValueError):run(a)
    assert not retire.called and set(p.name for p in Path(a.built['job']).iterdir())==set(subject.names(a.built['filename']))


def test_attention_clear_failure_preserves_reference_after_payloads_removed(capture,monkeypatch):
    a=capture;original=receipt.manage.replace_record;calls=[]
    def replace(*args,**kwargs):
        calls.append(args[1])
        if len(calls)==2:raise ValueError('synthetic state race')
        return original(*args,**kwargs)
    monkeypatch.setattr(receipt.manage,'replace_record',replace)
    backup=run(a)
    assert backup.exists() and calls==['state.json','state.json']
    assert a.manager.state[subject.KEY]==[a.built['job']]
    assert (Path(a.built['job'])/subject.COMPLETED).exists()


@pytest.mark.parametrize('bad',[None,'string',['/other/job'],['relative/smb-stream-'+'1'*32],['/instance/staging/../shared']])
def test_invalid_retained_reference_cannot_be_silently_dropped(bad):
    with pytest.raises(ValueError):subject.retained({subject.KEY:bad},Path('/instance'))


def test_retirement_never_targets_backups_or_other_job(capture,monkeypatch):
    a=capture;original=subject.retire
    def changed(root,context,result,copy,raw,protect):
        return original(root,context,{**result,'job':str(a.manager.root/'backups')},copy,raw,protect)
    monkeypatch.setattr(subject,'retire',changed)
    backup=run(a)
    assert backup.exists() and a.manager.state[subject.KEY]==[a.built['job']]
    assert set(p.name for p in Path(a.built['job']).iterdir())==set(subject.names(a.built['filename']))


@pytest.mark.parametrize('name',[subject.PENDING,subject.COMPLETED])
def test_journal_change_cannot_clear_attention(capture,monkeypatch,name):
    a=capture;original=subject._record
    def changed(folder,selected,value):
        original(folder,selected,value)
        if selected==name:(folder.path/selected).write_bytes(b'changed journal')
    monkeypatch.setattr(subject,'_record',changed)
    backup=run(a);job=Path(a.built['job'])
    assert backup.exists() and a.manager.state[subject.KEY]==[str(job)]
    if name==subject.PENDING:assert set(subject.names(a.built['filename'])) <= {p.name for p in job.iterdir()}


def test_running_process_prevents_any_work_deletion(capture,monkeypatch):
    a=capture;original=subject.retire
    def running(*args):
        monkeypatch.setattr(a.manager,'_process',lambda:object())
        return original(*args)
    monkeypatch.setattr(subject,'retire',running)
    backup=run(a)
    assert backup.exists() and a.manager.state[subject.KEY]==[a.built['job']]
    assert set(p.name for p in Path(a.built['job']).iterdir())==set(subject.names(a.built['filename']))
