"""Owner runtime branch on disposable signed records and root ownership adapters."""
import hashlib
import sys
from copy import deepcopy
from types import ModuleType
from pathlib import Path

import pytest
import desktop_assistant
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from desktop_assistant import owner_activation_contract as owner,owner_activation as codec
from desktop_assistant import mounted_nas_proof as mounted
from tests.desktop_assistant.test_current_mac_runtime import (
    current_case,current_protocol,runtime,published,contract,protocol,boot,system,Manager,
    activated,root_case,target,case,recovery_case,host_store,signed_activation_protocol,
    native_fixture,execution_protocol,approve_update,admit,environment)


@pytest.fixture
def owner_case(current_case,monkeypatch):
    c = current_case;prep = deepcopy(c.body['preparation']);request = deepcopy(c.body['request'])
    key = Ed25519PrivateKey.generate();public = key.public_key().public_bytes(
        serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
    request.update(type=owner.REQUEST,mac_publisher_sha256=codec.public_identity(public)[1])
    now = contract._time(c.body['issued_at'])
    declaration = owner.make_declaration(request,confirmed=True,now=now)
    authorization = codec.sign_authorization(request,prep,declaration,key.sign,public,now=now)
    record = owner.make_record(request,c.network,authorization,activated_at=now)
    trust = ModuleType('desktop_assistant.mac_publisher_trust')
    trust.MAC_PUBLIC = c.x.base/'mac-publisher-public.pem';trust.RECORD = c.x.base/'mac-publisher-trust.json'
    trust.MAC_PUBLIC.write_bytes(public);trust.MAC_PUBLIC.chmod(0o644)
    authority_record = dict(context_sha256=prep['context_sha256'],legacy_anchor_sha256=prep['publisher_sha256'],
        mac_publisher_sha256=request['mac_publisher_sha256'])
    trust.RECORD.write_bytes(owner.canonical(authority_record));trust.RECORD.chmod(0o644)
    def read_authority(**expected):
        mac = trust.MAC_PUBLIC.read_bytes();raw = trust.RECORD.read_bytes();actual = owner.decode(raw)
        for argument,field in (('expected_context_sha256','context_sha256'),
                ('expected_legacy_anchor_sha256','legacy_anchor_sha256'),
                ('expected_mac_publisher_sha256','mac_publisher_sha256')):
            owner.require(expected[argument] == actual[field])
        owner.require(codec.public_identity(mac)[1] == actual['mac_publisher_sha256'])
        return dict(mac_public=mac,raw=raw,record=actual)
    trust.read_authority = read_authority
    monkeypatch.setitem(sys.modules,'desktop_assistant.mac_publisher_trust',trust)
    monkeypatch.setattr(desktop_assistant,'mac_publisher_trust',trust,raising=False)
    monkeypatch.setattr(mounted,'ROOT',c.x.root)
    backup = prep['backup'];attempt = '7'*32
    checkpoint = dict(type='tianming.current-mac-local-checkpoint.v1',
        local_path=str(c.x.root/'backups/current.tmbackup'),
        proof_path=str(c.x.root/'staging'/('mounted-checkpoint-'+'8'*32)/'verified.json'),
        sha256=backup['sha256'],size=backup['size'],raw_sha256='0'*64,created_at=backup['created_at'],
        database={},shared_manifest_sha256=prep['shared_manifest_sha256'],release_sha256=prep['execution_package_sha256'],
        state_sha256=prep['state_sha256'],local_update_receipt_sha256='1'*64)
    # The current-runtime fixture uses a synthetic historical release marker.
    prep['backup']['release_sha256'] = prep['execution_package_sha256']
    request['preparation_sha256'] = owner.digest(prep)
    declaration = owner.make_declaration(request,confirmed=True,now=now)
    authorization = codec.sign_authorization(request,prep,declaration,key.sign,public,now=now)
    record = owner.make_record(request,c.network,authorization,activated_at=now)
    bound = dict(control=dict(package_sha256=backup['transport_package_sha256'],context_sha256=prep['context_sha256'],
        publisher_sha256=prep['publisher_sha256'],contract_sha256=backup['transport_contract_sha256'],
        receipt_sha256=backup['receipt_sha256']),control_sha=backup['transport_control_sha256'],
        contract=dict(checkpoint_attempt_id=attempt,local_update_receipt_sha256='1'*64),
        receipt=dict(sha256=backup['sha256'],size=backup['size'],verified_at=backup['verified_at']),raw={})
    for name in ('control','contract','receipt'):
        path = c.x.base/('owner-test-mounted-'+name+'.json');raw = owner.canonical(bound[name])
        path.write_bytes(raw);path.chmod(0o644);bound['raw'][path] = raw
    monkeypatch.setattr(mounted,'load',lambda:deepcopy(bound))
    monkeypatch.setattr(mounted,'checkpoint_result',lambda attempt_id:deepcopy(checkpoint))
    for name,value in (('factory-activation.json',record),('factory-target-'+request['request_id']+'.json',request)):
        path = c.x.base/name;path.write_bytes(owner.canonical(value));path.chmod(0o644)
    raw = (c.x.base/'factory-activation.json').read_bytes()
    gate = dict(type=owner.GATE,attempt_id='9'*32,plan_sha256='2'*64,
        activation_sha256=hashlib.sha256(raw).hexdigest(),request_sha256=owner.digest(request),ready=True)
    path = c.x.base/owner.GATE_NAME;path.write_bytes(owner.canonical(gate));path.chmod(0o644)
    c.owner = dict(record=record,request=request,prep=prep,trust=trust,bound=bound,checkpoint=checkpoint)
    return c


def test_owner_runtime_accepts_local_identity_with_original_restore_anchor(owner_case):
    c = owner_case
    before = (c.x.root/'state.json').read_bytes()
    result = runtime.configuration(c.x.root,c.x.id,c.t.c.public)
    assert result['package_sha256'] == c.x.id and result['network'] == c.network
    assert (c.x.root/'state.json').read_bytes() == before
    assert 'source_stop' not in owner.validate_record(c.owner['record'])


@pytest.mark.parametrize('fault',['missing-gate','unfinished-gate','failure','mac-public','trust-anchor',
    'mount-receipt','checkpoint-release','checkpoint-shared','checkpoint-state'])
def test_owner_runtime_refuses_authority_or_mounted_evidence_drift(owner_case,fault):
    c = owner_case
    if fault == 'missing-gate':(c.x.base/owner.GATE_NAME).unlink()
    elif fault == 'unfinished-gate':
        path = c.x.base/owner.GATE_NAME;gate = owner.decode(path.read_bytes());gate['ready'] = False;path.write_bytes(owner.canonical(gate))
    elif fault == 'failure':(c.x.base/owner.FAILED).write_bytes(b'')
    elif fault == 'mac-public':
        key = Ed25519PrivateKey.generate();c.owner['trust'].MAC_PUBLIC.write_bytes(key.public_key().public_bytes(
            serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo))
    elif fault == 'trust-anchor':
        path = c.owner['trust'].RECORD;value = owner.decode(path.read_bytes());value['legacy_anchor_sha256'] = '0'*64;path.write_bytes(owner.canonical(value))
    elif fault == 'mount-receipt':c.owner['bound']['receipt']['sha256'] = '0'*64
    elif fault == 'checkpoint-release':c.owner['checkpoint']['release_sha256'] = '0'*64
    elif fault == 'checkpoint-shared':c.owner['checkpoint']['shared_manifest_sha256'] = '0'*64
    else:c.owner['checkpoint']['state_sha256'] = '0'*64
    with pytest.raises((ValueError,OSError,InvalidSignature)):runtime.configuration(c.x.root,c.x.id,c.t.c.public)
    assert runtime.configured_mode(c.manager) == 'attention'


def test_owner_execution_cannot_use_old_or_boolean_capability(owner_case):
    c = owner_case;manifest = deepcopy(c.x.manifest)
    from desktop_assistant.mac_factory_runtime import CURRENT_MAC_CAPABILITY,CURRENT_MAC_FILES
    manifest['runtime_capabilities'][CURRENT_MAC_CAPABILITY] = 1
    for name in CURRENT_MAC_FILES | owner.FILES:manifest['files'].setdefault(name,'0'*64)
    raw = owner.canonical(c.owner['record'])
    with pytest.raises(ValueError):runtime.require_execution(manifest,activation_raw=raw)
    manifest['runtime_capabilities'][owner.CAPABILITY] = True
    with pytest.raises(ValueError):runtime.require_execution(manifest,activation_raw=raw)
    manifest['runtime_capabilities'][owner.CAPABILITY] = 1
    runtime.require_execution(manifest,activation_raw=raw)

