"""Owner claims and domain separation on disposable keys and public facts only."""
import base64
from copy import deepcopy
from datetime import timedelta
import hashlib

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from desktop_assistant import owner_activation_contract as c,owner_activation as codec
from desktop_assistant import current_mac_cutover_contract as old
from tests.desktop_assistant.test_current_mac_cutover_contract import valid_body,valid_network,at


@pytest.fixture
def owner():
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
    prep = valid_body(old)['preparation']
    request = c.make_request(prep,c.digest(valid_network()),codec.public_identity(public)[1],request_id='a'*32,now=at())
    declaration = c.make_declaration(request,confirmed=True,now=at())
    raw = codec.sign_authorization(request,prep,declaration,key.sign,public,now=at())
    return dict(key=key,public=public,prep=prep,request=request,declaration=declaration,raw=raw)


def test_owner_domain_binds_current_facts_without_windows_claims(owner):
    x = owner;body = codec.verify_authorization(x['raw'],x['public'],x['request'])
    assert 'source_stop' not in body and 'operator_attestation' not in c.make_record(
        x['request'],valid_network(),x['raw'],activated_at=at())
    assert body['owner_declaration']['statement'] == c.STATEMENT
    assert body['preparation'] == x['prep']
    record = c.make_record(x['request'],valid_network(),x['raw'],activated_at=at())
    assert c.validate_record(record) == body
    with pytest.raises(ValueError):old.validate_record(record)
    with pytest.raises(ValueError):old.authorization_body(x['raw'],x['request'])


@pytest.mark.parametrize('fault',['declaration','legacy-anchor','mac-publisher','database','credentials','network'])
def test_rejects_tampered_owner_claims_and_snapshot(owner,fault):
    x = owner;envelope = c.decode(x['raw']);body = envelope['body']
    if fault == 'declaration':body['owner_declaration']['old_erp_removed'] = False
    elif fault == 'legacy-anchor':body['preparation']['publisher_sha256'] = '0'*64
    elif fault == 'mac-publisher':body['request']['mac_publisher_sha256'] = '0'*64
    elif fault == 'database':body['preparation']['target_snapshot']['database_sha256'] = '0'*64
    elif fault == 'credentials':body['preparation']['credential_purposes'] = ['backup']
    else:body['request']['network_sha256'] = '0'*64
    with pytest.raises((ValueError,InvalidSignature)):
        codec.verify_authorization(c.canonical(envelope),x['public'],x['request'])


def test_wrong_key_domain_and_expired_window_rejected(owner):
    x = owner;envelope = c.decode(x['raw']);body = envelope['body']
    signature = x['key'].sign(old.DOMAIN+c.canonical(body))
    envelope['signature'] = base64.b64encode(signature).decode()
    with pytest.raises(InvalidSignature):codec.verify_authorization(c.canonical(envelope),x['public'],x['request'])
    key = Ed25519PrivateKey.generate();public = key.public_key().public_bytes(
        serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
    with pytest.raises(ValueError):codec.verify_authorization(x['raw'],public,x['request'])
    with pytest.raises(ValueError):c.require_install_window(body,now=at()+timedelta(hours=8))
    assert codec.verify_authorization(x['raw'],x['public'],x['request']) == body


def test_explicit_owner_confirmation_and_signer_output_required(owner):
    x = owner
    with pytest.raises(ValueError):c.make_declaration(x['request'],confirmed=False,now=at())
    with pytest.raises(ValueError):codec.sign_authorization(x['request'],x['prep'],x['declaration'],lambda raw:b'x',x['public'],now=at())
    bad = deepcopy(x['request']);bad['mac_publisher_sha256'] = bad['publisher_sha256']
    with pytest.raises(ValueError):c.validate_request(bad)


def test_independent_gate_binds_exact_owner_record(owner):
    x = owner;record = c.make_record(x['request'],valid_network(),x['raw'],activated_at=at());raw = c.canonical(record)
    gate = dict(type=c.GATE,attempt_id='1'*32,plan_sha256='2'*64,activation_sha256=hashlib.sha256(raw).hexdigest(),
        request_sha256=c.digest(x['request']),ready=True)
    assert c.validate_gate(gate,activation_raw=raw,ready=True) == gate
    with pytest.raises(ValueError):c.validate_gate({**gate,'ready':False},activation_raw=raw,ready=True)
    with pytest.raises(ValueError):c.validate_gate({**gate,'activation_sha256':'0'*64},activation_raw=raw)


def test_new_protocol_capability_requires_all_new_files():
    from desktop_assistant import build
    from tests.desktop_assistant.test_current_mac_build import sources
    source = sources();source['desktop_assistant/mac_publisher_trust.py'] = b'test-only-authority-interface'
    assert build.current_mac_capabilities(source,stream_enabled=False)[c.CAPABILITY] == 1
    source.pop(c.MODULE)
    assert c.CAPABILITY not in build.current_mac_capabilities(source,stream_enabled=False)

