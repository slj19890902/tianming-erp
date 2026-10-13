"""Root IO transaction on synthetic files; OS/signature/audit are adapters.

Actual publication file/FD locks are exercised; no installed ERP or keychain.
Cryptographic full worker audit has its separate integration suite.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime,timezone
import fcntl
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from desktop_assistant import current_mac_activation_install as a
from desktop_assistant import current_mac_publication as gate
from desktop_assistant import current_mac_worker_contract as c,current_mac_cutover_contract as v3
from desktop_assistant import mac_system_upgrade as root
from tests.desktop_assistant.test_current_mac_dispatch import scene,chain


@pytest.fixture
def target(scene,monkeypatch):
    s=scene
    monkeypatch.setattr(a,'BASE',s.base);monkeypatch.setattr(a,'ROOT',s.base/'instance')
    monkeypatch.setattr(root,'BASE',s.base)
    monkeypatch.setattr(root.boot,'protected',lambda path,**kwargs:
        None if kwargs.get('directory') else Path(path).read_bytes())
    monkeypatch.setattr(a,'verify_code',s.verifier)
    stamp=datetime.fromtimestamp(s.plans['activate']['created_at']+10,timezone.utc)
    monkeypatch.setattr(a,'datetime',SimpleNamespace(now=lambda tz:stamp))
    state_raw=c.canonical(dict(current=s.plans['activate']['execution_package_sha256'],
        manual_stop=True,pid=None,operation='update',original_restore='preserved'))
    state=s.base/'instance/state.json';state.write_bytes(state_raw);state.chmod(0o600)
    record=deepcopy(s.replies['activate']['result']['record'])
    body=v3.decode(record['authorization'].encode())
    prep=body['body']['preparation'];prep['state_sha256']=root.boot.digest(state_raw)
    request=body['body']['request'];request['preparation_sha256']=v3.digest(prep)
    body['body']['source_stop']['request_sha256']=v3.digest(request)
    authorization=c.canonical(body)
    record=v3.make_record(request,record['network'],authorization,activated_at=stamp,
        operator_attestation=s.plans['activate']['operator_attestation'])
    plan=s.plans['activate']
    values={'preparation.json':c.canonical(prep),'request.json':c.canonical(request),
        'authorization.json':authorization,'network.json':c.canonical(record['network'])}
    plan.update(state_stopped_sha256=root.boot.digest(state_raw),preparation_sha256=v3.digest(prep),
        request_sha256=v3.digest(request),preparation_file_sha256=c.raw_digest(values['preparation.json']),
        request_file_sha256=c.raw_digest(values['request.json']),
        authorization_file_sha256=c.raw_digest(authorization),network_file_sha256=c.raw_digest(values['network.json']))
    controls=[(root.system,'CONTROL','system.json','system_control_sha256',b'system control'),
        (root.system,'PLIST','system.plist','system_plist_sha256',b'system plist'),
        (a.stream,'CONTROL','stream.json','transport_control_sha256',b'stream control'),
        (a.stream,'CONTRACT','archive.json','transport_contract_sha256',b'{ "synthetic": true }')]
    for module,key,name,field,raw in controls:
        path=s.base/name;path.write_bytes(raw);path.chmod(0o644)
        monkeypatch.setattr(module,key,path)
        plan[field]=a.stream.archive.digest(c.decode(raw)) if key=='CONTRACT' else c.raw_digest(raw)
    for key in ('system_control_sha256','system_plist_sha256'):prep[key]=plan[key]
    for key in ('transport_control_sha256','transport_contract_sha256'):prep['backup'][key]=plan[key]
    request['preparation_sha256']=v3.digest(prep)
    body['body']['source_stop']['request_sha256']=v3.digest(request)
    authorization=c.canonical(body)
    record=v3.make_record(request,record['network'],authorization,activated_at=stamp,
        operator_attestation=plan['operator_attestation'])
    values.update({'preparation.json':c.canonical(prep),'request.json':c.canonical(request),
        'authorization.json':authorization})
    plan.update(preparation_sha256=v3.digest(prep),request_sha256=v3.digest(request),
        preparation_file_sha256=c.raw_digest(values['preparation.json']),
        request_file_sha256=c.raw_digest(values['request.json']),authorization_file_sha256=c.raw_digest(authorization))
    for name,raw in values.items():
        path=s.slot/name;path.write_bytes(raw);path.chmod(0o644)
    s.slot.joinpath('activate-plan.json').write_bytes(c.canonical(plan))
    publication=s.base/'publication.lock';publication.write_bytes(b'publication lock\n');publication.chmod(0o600)
    @contextmanager
    def held_publication():
        with publication.open('rb') as held:
            fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
            try:yield
            finally:fcntl.flock(held,fcntl.LOCK_UN)
    monkeypatch.setattr(root.publication,'lock',held_publication)
    worker=Mock()
    def audited(plan_raw,*,verify_code,inherited_lock):
        assert verify_code()==plan['code_manifest_sha256']
        assert os.pread(inherited_lock,1,0)==b'0'
        with s.lock.open('rb') as other:
            with pytest.raises(BlockingIOError):fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return dict(result=dict(record=deepcopy(record)))
    worker.side_effect=audited;monkeypatch.setattr(a.dispatch,'dispatch',worker)
    return SimpleNamespace(s=s,plan=plan,worker=worker,state=state,state_raw=state_raw,
        authorization=authorization,record=record,controls=controls)


def activate(t,**kwargs):
    return a.activate(t.plan['attempt_id'],c.raw_digest(c.canonical(t.plan)),
        factory_cutover_authorized=kwargs.get('authorized',True),
        source_isolation_confirmed=kwargs.get('isolated',True))


def test_normal_publication_keeps_original_state_and_complete_receipt(target):
    t=target;result=activate(t)
    assert result['factory_activated'] is True and result['started'] is False
    assert result['source_physical_isolation_verified'] is False
    active=(t.s.base/'factory-activation.json').read_bytes()
    receipt=gate.validate(c.decode((t.s.base/gate.NAME).read_bytes()),activation_raw=active,ready=True)
    assert receipt['attempt_id']==t.plan['attempt_id']
    assert t.state.read_bytes()==t.state_raw and not (t.s.base/gate.FAILED).exists()
    assert (t.s.base/('current-mac-activation-'+t.plan['attempt_id']+'.pending.json')).exists()
    with t.s.lock.open('rb') as held:fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)


@pytest.mark.parametrize('field',['authorized','isolated'])
def test_missing_explicit_attestation_prevents_worker(target,field):
    with pytest.raises(ValueError):activate(target,**{field:False})
    assert not target.worker.called


@pytest.mark.parametrize('name',['network.json','preparation.json','request.json','authorization.json','activate-plan.json'])
def test_bad_raw_input_prevents_worker(target,name):
    path=target.s.slot/name;path.write_bytes(path.read_bytes()+b' ')
    with pytest.raises(ValueError):activate(target)
    assert not target.worker.called and not (target.s.base/'factory-activation.json').exists()


@pytest.mark.parametrize('name',['factory-activation.json',gate.NAME,gate.FAILED])
def test_existing_authority_or_failure_preserved_without_retry(target,name):
    path=target.s.base/name;path.write_bytes(b'prior');path.chmod(0o644)
    with pytest.raises(ValueError):activate(target)
    assert path.read_bytes()==b'prior' and not target.worker.called


@pytest.mark.parametrize('prefix',['factory-activation-','current-mac-activation-'])
def test_original_or_new_pending_transaction_cannot_be_ignored(target,prefix):
    path=target.s.base/(prefix+'0'*32+'.pending.json');path.write_bytes(b'prior');path.chmod(0o600)
    with pytest.raises(ValueError):activate(target)
    assert path.read_bytes()==b'prior' and not target.worker.called


def test_worker_failure_creates_sentinel_before_releasing_lock(target,monkeypatch):
    t=target;t.worker.side_effect=ValueError('synthetic worker failure')
    original=root._new;observed=[]
    def checked(parent,name,raw,**kwargs):
        if name==gate.FAILED:
            with t.s.lock.open('rb') as other:
                with pytest.raises(BlockingIOError):fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
            observed.append(True)
        return original(parent,name,raw,**kwargs)
    monkeypatch.setattr(root,'_new',checked)
    with pytest.raises(ValueError):activate(t)
    assert observed==[True] and (t.s.base/gate.FAILED).exists()
    assert not (t.s.base/'factory-activation.json').exists() and t.state.read_bytes()==t.state_raw


@pytest.mark.parametrize('failure',['state','authorization','network','registry'])
def test_changed_facts_after_worker_preserve_stopped_failure(target,failure,monkeypatch):
    t=target;previous=t.worker.side_effect
    def changed(*args,**kwargs):
        reply=previous(*args,**kwargs)
        if failure=='state':t.state.write_bytes(t.state_raw+b' ')
        elif failure in ('authorization','network'):
            path=t.s.slot/(failure+'.json');path.write_bytes(path.read_bytes()+b' ')
        else:
            metadata=t.s.metadata
            monkeypatch.setattr(root.system,'metadata',lambda package:{**metadata(package),'registry_sha256':'0'*64})
        return reply
    t.worker.side_effect=changed
    with pytest.raises(ValueError):activate(t)
    assert (t.s.base/gate.FAILED).exists() and not (t.s.base/'factory-activation.json').exists()


@pytest.mark.parametrize('stage',[gate.NAME,'factory-activation.json','commit'])
def test_late_publication_failure_retains_all_files_and_blocks_runtime(target,stage,monkeypatch):
    t=target;new=root._new;cas=root._cas
    def fail_new(parent,name,raw,**kwargs):
        new(parent,name,raw,**kwargs)
        if name==stage:raise OSError('synthetic readback failure')
    def fail_cas(*args,**kwargs):
        cas(*args,**kwargs)
        if stage=='commit':raise OSError('synthetic post-commit failure')
    monkeypatch.setattr(root,'_new',fail_new);monkeypatch.setattr(root,'_cas',fail_cas)
    with pytest.raises(OSError):activate(t)
    assert (t.s.base/gate.NAME).exists() and (t.s.base/gate.FAILED).exists()
    assert t.state.read_bytes()==t.state_raw
    if stage=='commit':assert c.decode((t.s.base/gate.NAME).read_bytes())['ready'] is True


@pytest.fixture
def codepack(tmp_path,monkeypatch):
    import shutil
    folder=tmp_path/'code';folder.mkdir()
    source=Path(a.__file__).resolve().parent.parent
    for name in a.CODE_FILES:
        path=folder/name;path.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(source/name,path)
    value=dict(type='tianming.keychain-code.v1',source='a'*40,
        files={name:root.boot.digest((folder/name).read_bytes()) for name in a.CODE_FILES})
    raw=c.canonical(value);(folder/'install-manifest.json').write_bytes(raw)
    for module,name in ((a,a.MODULE),(root,root.MODULE),(a.dispatch,'desktop_assistant/current_mac_dispatch.py'),
            (c,'desktop_assistant/current_mac_worker_contract.py'),
            (v3,'desktop_assistant/current_mac_cutover_contract.py'),
            (gate,'desktop_assistant/current_mac_publication.py'),
            (a.factory_network,'desktop_assistant/factory_network.py'),
            (a.mounted,a.mounted.MODULE),(a.mounted.mounts,'desktop_assistant/mac_mount.py'),
            (root.boot,'desktop_assistant/mac_keychain_install.py'),
            (root.system,'desktop_assistant/mac_system_service.py'),
            (root.publication,'desktop_assistant/mac_release_publish.py'),
            (root.contract,root.CONTRACT_MODULE)):
        monkeypatch.setattr(module,'__file__',str(folder/name))
    monkeypatch.setattr(root.boot,'protected',lambda path,**kwargs:
        None if kwargs.get('directory') else Path(path).read_bytes())
    return folder,raw


def test_closed_code_pack_hashes_every_file_without_self_authorizing(codepack):
    folder,raw=codepack
    assert a.verify_code()==c.raw_digest(raw)


@pytest.mark.parametrize('failure',['modified','extra_file','extra_directory','symlink','missing','manifest'])
def test_bad_admin_code_pack_rejected(codepack,failure):
    folder,_=codepack;path=folder/a.MODULE
    if failure=='modified':path.write_bytes(path.read_bytes()+b'\n')
    elif failure=='extra_file':(folder/'extra.py').write_bytes(b'pass')
    elif failure=='extra_directory':(folder/'unexpected').mkdir()
    elif failure=='symlink':path.unlink();path.symlink_to(folder/'install-manifest.json')
    elif failure=='missing':path.unlink()
    else:
        value=c.decode((folder/'install-manifest.json').read_bytes());value['approved']=True
        (folder/'install-manifest.json').write_bytes(c.canonical(value))
    with pytest.raises((ValueError,OSError)):a.verify_code()
