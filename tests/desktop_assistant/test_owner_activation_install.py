"""Root publication transaction on private temporary files and real FD locks."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime,timezone
import fcntl
import hashlib
import os
from pathlib import Path
import sys
from types import ModuleType,SimpleNamespace
from unittest.mock import Mock

import pytest
import desktop_assistant
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from desktop_assistant import owner_activation_install as a,owner_activation_contract as c
from desktop_assistant import owner_activation as codec
from desktop_assistant import mounted_nas_proof as m,mac_system_upgrade as root
from tests.desktop_assistant.test_current_mac_cutover_contract import valid_body,valid_network,at


@pytest.fixture
def scene(tmp_path,monkeypatch):
    base = tmp_path/'base';base.mkdir(mode=0o755)
    instance = base/'instance';instance.mkdir(mode=0o700)
    control = instance/'control';control.mkdir(mode=0o700)
    lock = control/'operation.lock';lock.write_bytes(b'0');lock.chmod(0o600)
    state = dict(current='3'*64,previous='4'*64,manual_stop=True,pid=None,operation='update',original_restore='unchanged')
    state_path = instance/'state.json';state_raw = c.canonical(state);state_path.write_bytes(state_raw);state_path.chmod(0o600)
    prep = valid_body(c.facts)['preparation'];prep['execution_package_sha256'] = state['current']
    prep['state_sha256'] = hashlib.sha256(state_raw).hexdigest()
    prep['backup']['release_sha256'] = state['current']
    ciphertext = tmp_path/'temporary-synthetic-cipher.tmbackup';ciphertext.write_bytes(b'private encrypted fixture only')
    prep['backup']['sha256'] = hashlib.sha256(ciphertext.read_bytes()).hexdigest();prep['backup']['size'] = ciphertext.stat().st_size
    key = Ed25519PrivateKey.generate();mac_public = key.public_key().public_bytes(
        serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
    trust = ModuleType('desktop_assistant.mac_publisher_trust');trust.MAC_PUBLIC = base/'mac-publisher-public.pem'
    trust.RECORD = base/'mac-publisher-trust.json';trust.MAC_PUBLIC.write_bytes(mac_public);trust.MAC_PUBLIC.chmod(0o644)
    trust_raw = c.canonical(dict(context_sha256=prep['context_sha256'],legacy_anchor_sha256=prep['publisher_sha256'],
        mac_publisher_sha256=hashlib.sha256(mac_public).hexdigest()))
    trust.RECORD.write_bytes(trust_raw);trust.RECORD.chmod(0o644)
    def authority(**expected):
        actual = c.decode(trust.RECORD.read_bytes())
        for arg,field in (('expected_context_sha256','context_sha256'),('expected_legacy_anchor_sha256','legacy_anchor_sha256'),
                ('expected_mac_publisher_sha256','mac_publisher_sha256')):
            c.require(actual[field] == expected[arg])
        return dict(raw=trust.RECORD.read_bytes(),mac_public=trust.MAC_PUBLIC.read_bytes())
    trust.public_fingerprint = lambda public:hashlib.sha256(public).hexdigest()
    trust.read_authority = authority;monkeypatch.setitem(sys.modules,'desktop_assistant.mac_publisher_trust',trust)
    monkeypatch.setattr(desktop_assistant,'mac_publisher_trust',trust,raising=False)
    bound = dict(control=dict(package_sha256=prep['backup']['transport_package_sha256'],
        context_sha256=prep['context_sha256'],publisher_sha256=prep['publisher_sha256']),
        contract=dict(checkpoint_attempt_id='6'*32),receipt=dict(sha256=prep['backup']['sha256'],size=prep['backup']['size']),raw={})
    for name in ('contract','receipt'):
        path = base/('mounted-'+name+'.json');raw = c.canonical(bound[name]);path.write_bytes(raw);path.chmod(0o644)
        bound['raw'][path] = raw;bound['control'][name+'_sha256'] = hashlib.sha256(raw).hexdigest()
        prep['backup']['transport_contract_sha256' if name == 'contract' else 'receipt_sha256'] = hashlib.sha256(raw).hexdigest()
    path = base/'mounted-control.json';raw = c.canonical(bound['control']);path.write_bytes(raw);path.chmod(0o644)
    bound['raw'][path] = raw;bound['control_sha'] = hashlib.sha256(raw).hexdigest();prep['backup']['transport_control_sha256'] = bound['control_sha']
    monkeypatch.setattr(m,'load',lambda:deepcopy(bound))
    @contextmanager
    def broker(plan):
        with ciphertext.open('rb') as fd:yield fd.fileno()
    monkeypatch.setattr(m,'broker',broker)
    original_pin = root.Pin
    def pin(path,**kw):
        kw['uid'] = os.getuid();kw['gid'] = os.getgid();return original_pin(path,**kw)
    monkeypatch.setattr(root,'Pin',pin);monkeypatch.setattr(root,'DATA',tmp_path)
    monkeypatch.setattr(root.boot,'no_acl',lambda fd:None)
    monkeypatch.setattr(root.boot,'protected',lambda path,**kw:b'' if kw.get('directory') else Path(path).read_bytes())
    monkeypatch.setattr(root,'_entry',lambda:None)
    monkeypatch.setattr(a,'BASE',base);monkeypatch.setattr(a,'ROOT',instance);monkeypatch.setattr(root,'BASE',base)
    publication = base/'publication.lock';publication.write_bytes(b'0')
    @contextmanager
    def root_publication():
        with publication.open('rb') as fd:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            try:yield
            finally:fcntl.flock(fd,fcntl.LOCK_UN)
    monkeypatch.setattr(root,'_publication',root_publication)
    for setting_name,name,field in (('CONTROL','system.json','system_control_sha256'),('PLIST','system.plist','system_plist_sha256')):
        path = base/name;raw = b'synthetic original system control';path.write_bytes(raw);path.chmod(0o644)
        monkeypatch.setattr(root.system,setting_name,path);prep[field] = hashlib.sha256(raw).hexdigest()
    metadata = lambda identity:dict(context=dict(uid=499,gid=499),**{k:prep[k] for k in ('context_sha256','publisher_sha256','registry_sha256')})
    monkeypatch.setattr(root.system,'metadata',metadata)
    monkeypatch.setattr(root.system,'_program_manifest',lambda meta:dict(runtime_capabilities={c.CAPABILITY:1},files={name:'0'*64 for name in c.FILES}))
    monkeypatch.setattr(a,'datetime',SimpleNamespace(now=lambda tz:at()))
    verifier = Mock(return_value='a'*64)
    def audited(raw,lock_fd,nas_fd,verify):
        plan = c.decode(raw);assert os.pread(lock_fd,1,0) == b'0'
        codec.verify_authorization(plan['authorization'].encode(),mac_public,plan['request'])
        with lock.open('rb') as other:
            with pytest.raises(BlockingIOError):fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert hashlib.sha256(os.pread(nas_fd,ciphertext.stat().st_size,0)).hexdigest() == prep['backup']['sha256']
        record = c.make_record(plan['request'],plan['network'],plan['authorization'].encode(),activated_at=at())
        return dict(record=record)
    worker = Mock(side_effect=audited);monkeypatch.setattr(a,'_dispatch',worker)
    return SimpleNamespace(base=base,instance=instance,prep=prep,key=key,worker=worker,verifier=verifier,
        state_path=state_path,state_raw=state_raw,lock=lock,bound=bound,ciphertext=ciphertext,trust=trust)


def activate(s,confirmed=True):
    return a.activate('b'*32,c.canonical(s.prep),c.canonical(valid_network()),'c'*32,
        signer=s.key.sign,verify_code=s.verifier,owner_declaration_confirmed=confirmed)


def test_root_signs_owner_record_and_keeps_current_business_stopped(scene):
    s = scene;result = activate(s)
    assert result['factory_activated'] is True and result['started'] is False and result['manual_stop'] is True
    assert s.state_path.read_bytes() == s.state_raw
    raw = (s.base/'factory-activation.json').read_bytes();body = c.validate_record(c.decode(raw))
    assert body['owner_declaration']['statement'] == c.STATEMENT and 'source_stop' not in body
    c.validate_gate(c.decode((s.base/c.GATE_NAME).read_bytes()),activation_raw=raw,ready=True)
    with s.lock.open('rb') as fd:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)


def test_missing_owner_permission_never_signs_or_dispatches(scene):
    with pytest.raises(ValueError):activate(scene,confirmed=False)
    assert not scene.worker.called and not (scene.base/'factory-activation.json').exists()


@pytest.mark.parametrize('fault',['no-checkpoint','state','capability','cipher','worker','signature','late-publish'])
def test_failure_keeps_state_and_blocks_formal_runtime(scene,monkeypatch,fault):
    s = scene
    if fault == 'no-checkpoint':s.bound['contract']['checkpoint_attempt_id'] = None
    elif fault == 'state':s.state_path.write_bytes(c.canonical(dict(current=s.prep['execution_package_sha256'],manual_stop=False,pid=None)))
    elif fault == 'capability':monkeypatch.setattr(root.system,'_program_manifest',lambda meta:dict(runtime_capabilities={c.CAPABILITY:True},files={name:'0'*64 for name in c.FILES}))
    elif fault == 'cipher':s.ciphertext.write_bytes(b'x'*s.ciphertext.stat().st_size)
    elif fault == 'worker':s.worker.side_effect = ValueError('private synthetic audit failure')
    elif fault == 'signature':s.key = SimpleNamespace(sign=lambda raw:b'x'*64)
    else:
        original = root._new
        def fail(parent,name,raw,**kw):
            if name == 'factory-activation.json':raise OSError('synthetic late publication failure')
            return original(parent,name,raw,**kw)
        monkeypatch.setattr(root,'_new',fail)
    before = s.state_path.read_bytes()
    with pytest.raises((ValueError,OSError,InvalidSignature)):activate(s)
    assert s.state_path.read_bytes() == before and not (s.base/'factory-activation.json').exists()
    if fault in ('cipher','worker','signature','late-publish'):
        assert (s.base/c.FAILED).exists()


def test_actual_apple_python_no_site_import_never_loads_crypto():
    import subprocess
    checkout = Path(a.__file__).resolve().parent.parent
    script = 'import sys;sys.path.insert(0,sys.argv[1]);import desktop_assistant.owner_activation_install;assert "cryptography" not in sys.modules;assert sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode'
    subprocess.run(['/usr/bin/python3','-I','-S','-B','-c',script,str(checkout)],check=True,capture_output=True)

