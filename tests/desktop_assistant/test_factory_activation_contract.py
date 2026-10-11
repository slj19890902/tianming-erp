"""Public record syntax and binding; cryptographic authority remains separate."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import base64
import hashlib

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from desktop_assistant import factory_activation_contract as c, factory_cutover as cutover
from desktop_assistant.signing import public_bytes
from tests.desktop_assistant.test_factory_network import lan


@pytest.fixture
def record():
    key=Ed25519PrivateKey.generate();public=public_bytes(key);now=datetime(2026,10,11,tzinfo=timezone.utc)
    target={'host_sha256':'a'*64,'installation_id':'b'*32,'service_uid':499,'service_gid':499,
            'service_account':'_tianmingerp','root':'/Library/TianmingERP/instance'}
    request=cutover.make_network_request(target,'c'*64,lan(),public,now=now)
    snapshot={**{k:'d'*64 for k in c.SNAPSHOT_HASHES},'revision':'synthetic','tables':1,'files':1,'registered_attachments':0}
    source={'source_sha256':'e'*64,'backup_sha256':'f'*64,'source_package_sha256':'d'*64,
        'backup_created_at':c._stamp(now+timedelta(minutes=1)),'manual_stop':True,'process_stopped':True,
        'credential_purposes':['backup'],'snapshot':snapshot}
    auth=cutover.sign_authorization(request,source,key,now=now+timedelta(minutes=2))
    return {'type':c.TYPE,'request':request,'network':lan(),'authorization':auth.decode(),
        'authorization_sha256':hashlib.sha256(auth).hexdigest(),'context_sha256':'e'*64,'registry_sha256':'f'*64,
        'activated_at':c._stamp(now+timedelta(minutes=3)),
        'target_snapshot':{**snapshot,'database_sha256':'a'*64},'operator_attestation':dict(c.ATTESTATION)},public


def test_original_source_protocol_is_shared_and_signature_verified_separately(record):
    value,public=record
    assert cutover._body is c._body and cutover._snapshot is c._snapshot and cutover._evidence is c._evidence
    assert c.validate_record(value)==cutover.verify_authorization(value['authorization'].encode(),public,value['request'])
    encoded=c.canonical(value)
    assert c.decode(encoded)==value


@pytest.mark.parametrize('fault',['extra','missing','type','network','context','registry','authorization-sha','request',
    'target-facts','target-schema','target-files','target-environment','target-pdf','target-count','target-revision',
    'bool-count','early','late','time-zone','no-attestation','bool-attestation','extra-attestation','auth-type'])
def test_record_rejects_unknown_or_mismatched_bindings(record,fault):
    value,_=record
    if fault=='extra':value['unknown']='never retained'
    elif fault=='missing':value.pop('registry_sha256')
    elif fault=='type':value['type']='not-activation'
    elif fault=='network':value['network']['ERP_PORT']='8001'
    elif fault in {'context','registry','authorization-sha'}:
        value[{'context':'context_sha256','registry':'registry_sha256','authorization-sha':'authorization_sha256'}[fault]]='invalid'
    elif fault=='request':value['request']['target']['host_sha256']='f'*64
    elif fault.startswith('target-'):
        field={'target-facts':'facts_sha256','target-schema':'schema_sha256','target-files':'files_sha256',
            'target-environment':'preserved_environment_sha256','target-pdf':'pdf_sources_sha256',
            'target-count':'files','target-revision':'revision'}[fault]
        value['target_snapshot'][field]=2 if field=='files' else 'changed' if field=='revision' else 'f'*64
    elif fault=='bool-count':value['target_snapshot']['files']=True
    elif fault=='early':value['activated_at']='2026-10-11T00:00:00Z'
    elif fault=='late':value['activated_at']='2026-10-11T08:02:00Z'
    elif fault=='time-zone':value['activated_at']='2026-10-11T00:03:00'
    elif fault=='no-attestation':value['operator_attestation']['factory_cutover_authorized']=False
    elif fault=='bool-attestation':value['operator_attestation']['source_isolation_confirmed']=1
    elif fault=='extra-attestation':value['operator_attestation']['unknown']=True
    elif fault=='auth-type':value['authorization']={}
    with pytest.raises(ValueError):c.validate_record(value)


def test_shape_validation_never_claims_to_authenticate_a_signature(record):
    value,public=record;auth=c.decode(value['authorization'].encode())
    auth['signature']=base64.b64encode(bytes(64)).decode()
    raw=c.canonical(auth);value['authorization']=raw.decode();value['authorization_sha256']=hashlib.sha256(raw).hexdigest()
    c.validate_record(value)  # Deliberately syntax only; root trusts its pinned worker.
    with pytest.raises(InvalidSignature):cutover.verify_authorization(raw,public,value['request'])


@pytest.mark.parametrize('raw',[b'[]',b'{"x":1,"x":2}',b'{"x":NaN}',b'{"x":{"a":1,"a":2}}',b'x'*65537])
def test_strict_public_json(raw):
    with pytest.raises(ValueError):c.decode(raw)


def test_install_time_is_historical_not_a_server_shutdown_deadline(record,monkeypatch):
    value,_=record
    monkeypatch.setattr(c,'_now',lambda:datetime(2030,1,1,tzinfo=timezone.utc))
    c.validate_record(value)


@pytest.mark.parametrize('field',['factory_activation_v1','factory_audit_v1','factory_execution_v1','factory_cutover_v1'])
@pytest.mark.parametrize('bad',[None,True,0,2,'1'])
def test_protocol_requires_actual_complete_runtime_and_reader_integers(field,bad):
    manifest={'runtime_capabilities':{k:1 for k in ('factory_activation_v1','factory_audit_v1','factory_execution_v1')},
        'reader_capabilities':{'factory_cutover_v1':1},'files':{k:'a'*64 for k in
        (c.ENTRY,c.MODULE,'desktop_assistant/factory_target_audit.py','desktop_assistant/factory_activation_contract.py')}}
    manifest['reader_capabilities' if field=='factory_cutover_v1' else 'runtime_capabilities'][field]=bad
    with pytest.raises(ValueError):c.require_protocol(manifest)
