"""Legacy dev changes after reboot: full FD cipher verification precedes capture."""
from copy import deepcopy
from types import SimpleNamespace
import os
import time

import pytest
from desktop_assistant import local_backup_only as local
from desktop_assistant.storage import read_json,write_json,sha
from tests.desktop_assistant.test_local_backup_only import local_case,cutover

FIELDS={'dev':100,'ino':22,'uid':499,'gid':499,'mode':0o100600,'nlink':1,
        'size':20,'mtime_ns':1600000000000000000,'ctime_ns':1600000000000000100}


def value_and_held(monkeypatch, *, changed=None, boot=1600000001000000000):
    recorded=dict(FIELDS);current=dict(recorded,dev=101)
    if changed:current.update(changed)
    value={'identity':recorded,'size':20,'verified_at':'2020-09-13T12:26:40+00:00',
        'authenticated_recovery':{'source_time':'2020-09-13T12:26:40+00:00'}}
    monkeypatch.setattr(local,'_boot_time_ns',lambda:boot)
    return value,SimpleNamespace(check=lambda:SimpleNamespace(**{'st_'+k:v for k,v in current.items()}))


def test_preboot_dev_change_is_the_only_allowed_metadata_difference(monkeypatch):
    value,held=value_and_held(monkeypatch)
    assert local._historical_identity_matches(held,value)
    for field in FIELDS.keys()-{'dev'}:
        current={field:FIELDS[field]+1}
        _,held=value_and_held(monkeypatch,changed=current)
        assert not local._historical_identity_matches(held,value),field


def test_exact_identity_never_queries_boot_time(monkeypatch):
    value,held=value_and_held(monkeypatch,changed={'dev':100})
    monkeypatch.setattr(local,'_boot_time_ns',lambda:pytest.fail('exact identity queried boot'))
    assert local._historical_identity_matches(held,value)


@pytest.mark.parametrize('boot',[1600000000000000000,1599999999999999999,100])
def test_same_or_earlier_boot_cannot_explain_dev_change(monkeypatch,boot):
    value,held=value_and_held(monkeypatch,boot=boot)
    assert not local._historical_identity_matches(held,value)


@pytest.mark.parametrize('change',[
    lambda v:v['identity'].pop('dev'),
    lambda v:v['identity'].update(extra=1),
    lambda v:v['identity'].update(ctime_ns=True),
    lambda v:v['identity'].update(size=20.0),
    lambda v:v.update(size=True),
    lambda v:v.update(size=21),
    lambda v:v.update(verified_at='2020-09-13T12:26:41+00:00'),
    lambda v:v['authenticated_recovery'].update(source_time='2020-09-13T12:26:40'),
    lambda v:v['authenticated_recovery'].update(source_time='malformed'),
])
def test_malformed_or_unbound_old_receipt_fails_closed(monkeypatch,change):
    value,held=value_and_held(monkeypatch);change(value)
    assert not local._historical_identity_matches(held,value)


@pytest.mark.parametrize('field',['mtime_ns','ctime_ns'])
def test_metadata_at_boot_boundary_does_not_allow_dev_exception(monkeypatch,field):
    boot=1600000001000000000
    value,held=value_and_held(monkeypatch,changed={field:boot},boot=boot)
    value['identity'][field]=boot
    assert not local._historical_identity_matches(held,value)


def test_boot_query_failure_rejects_dev_exception(monkeypatch):
    value,held=value_and_held(monkeypatch)
    monkeypatch.setattr(local,'_boot_time_ns',lambda:(_ for _ in ()).throw(ValueError('synthetic')))
    assert not local._historical_identity_matches(held,value)


@pytest.mark.parametrize('raw',[
    b'{ sec = 1792739770, usec = 281066 } Fri Oct 23 15:16:10 2026\n',
    b'{ sec = 1792739770, usec = 0 }\n',
])
def test_fixed_boot_query_parses_native_output(monkeypatch,raw):
    seen=[]
    def run(argv,**kwargs):seen.append((argv,kwargs));return SimpleNamespace(stdout=raw)
    monkeypatch.setattr(local.subprocess,'run',run)
    assert 1792739770000000000<=local._boot_time_ns()<1792739771000000000
    assert seen[0][0]==['/usr/sbin/sysctl','-n','kern.boottime']
    assert seen[0][1]['timeout']==5 and seen[0][1]['check'] is True


@pytest.mark.parametrize('raw',[b'',b'secret or arbitrary output',b'{ sec = 0, usec = 1 }\n',b'{ sec = 1, usec = 1000000 }\n'])
def test_invalid_boot_output_rejected_without_echo(monkeypatch,raw):
    monkeypatch.setattr(local.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout=raw))
    with pytest.raises(ValueError) as error:local._boot_time_ns()
    if raw:assert raw.decode() not in str(error.value)


def _old_dev_receipt(scene,monkeypatch):
    proof=cutover(scene);local.finalize_local_update(scene.manager)
    root=scene.manager.root;cipher=root/'backups'/proof['backup_filename']
    receipt=local._receipt_path(root,proof['backup_sha256']);value=read_json(receipt)
    value['identity']['dev']+=1;write_json(receipt,value)
    # Synthetic post-capture reboot boundary; no real host metadata is changed.
    monkeypatch.setattr(local,'_boot_time_ns',lambda:time.time_ns()+10000000000)
    state=scene.manager.state;state['owner_preserved_backup_sha256']=[sha(cipher)]
    write_json(root/'state.json',state)
    return receipt,cipher


def test_new_capture_rechecks_cipher_fd_and_keeps_original_receipt(local_case,monkeypatch):
    scene=local_case;receipt,cipher=_old_dev_receipt(scene,monkeypatch)
    old_raw=receipt.read_bytes();old_hash=sha(cipher)
    # Status remains cheap and conservative; a dev exception never green-lights it.
    assert local.verified_at(scene.manager.root,scene.manager.state) is None
    original=local._digest;seen=[]
    def digest(fd):seen.append(os.fstat(fd).st_ino);return original(fd)
    monkeypatch.setattr(local,'_digest',digest)
    with scene.manager.lock():new=local.backup_locked(scene.manager,PASSWORD)
    assert seen==[cipher.stat().st_ino]
    assert new.exists() and cipher.exists() and sha(cipher)==old_hash
    assert receipt.read_bytes()==old_raw
    assert local.verified_at(scene.manager.root,scene.manager.state) is not None
    assert scene.events==['stop','start']


def test_failed_full_fd_hash_never_stops_or_creates_backup(local_case,monkeypatch):
    scene=local_case;receipt,cipher=_old_dev_receipt(scene,monkeypatch)
    root=scene.manager.root;before_state=deepcopy(scene.manager.state)
    raw=receipt.read_bytes();before=set((root/'backups').iterdir())
    monkeypatch.setattr(local,'_digest',lambda fd:'0'*64)
    with scene.manager.lock(),pytest.raises(ValueError,match='cipher identity changed'):
        local.backup_locked(scene.manager,PASSWORD)
    assert scene.manager.state==before_state and receipt.read_bytes()==raw
    assert set((root/'backups').iterdir())==before and cipher.exists() and scene.events==[]

# Reuse the existing synthetic archive fixture's password, never host credentials.
from tests.desktop_assistant.test_stopped_update import PASSWORD,case
