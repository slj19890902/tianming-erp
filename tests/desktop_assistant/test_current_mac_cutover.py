"""Temporary Ed25519 identities only; no issuer, DPAPI or private-key IO."""
import importlib.util
import base64
from copy import deepcopy
from datetime import timedelta
import hashlib
import json

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric import ec

from desktop_assistant import current_mac_cutover as codec
from desktop_assistant import current_mac_cutover_contract as contract
from desktop_assistant import factory_cutover as v1
from desktop_assistant import retained_cutover as v2
from desktop_assistant import retained_snapshot_contract as retained
from tests.desktop_assistant.test_current_mac_cutover_contract import valid_body, valid_network, at, h, put, refresh


def test_explicit_current_mac_codec_is_available():
    assert importlib.util.find_spec('desktop_assistant.current_mac_cutover') is not None


def public_bytes(key):
    return key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


@pytest.fixture
def inputs():
    key = Ed25519PrivateKey.generate()
    public = public_bytes(key)
    body = valid_body(contract)
    publisher = hashlib.sha256(public).hexdigest()
    body['preparation']['publisher_sha256'] = body['request']['publisher_sha256'] = publisher
    refresh(contract, body)
    return body, key, public


def signed(inputs):
    body, key, public = inputs
    raw = codec.sign_authorization(body['request'], body['source_stop'], body['preparation'], key, now=at())
    return raw, codec.verify_authorization(raw, public, body['request'])


def test_real_signature_mac_business_differs_from_windows_and_no_implicit_expiry(inputs):
    before = deepcopy(inputs[0])
    raw, body = signed(inputs)
    assert body == before and inputs[0] == before
    assert body['source_stop']['database_sha256'] != body['preparation']['target_snapshot']['database_sha256']
    codec.require_install_window(body, now=at())
    with pytest.raises(ValueError): codec.require_install_window(body, now=at()+timedelta(days=30))
    assert codec.verify_authorization(raw, inputs[2], body['request']) == body
    record = contract.make_record(body['request'], valid_network(), raw, activated_at=at(),
        operator_attestation={key: True for key in contract.ATTESTATION})
    assert contract.validate_record(record) == body
    assert codec.verify_authorization(record['authorization'].encode(), inputs[2], record['request']) == body


@pytest.mark.parametrize('domain', [v1.DOMAIN, retained.DOMAIN, b'', b'TIANMING-ERP-FACTORY-CUTOVER\0current-mac-v2\0'])
def test_old_or_missing_domain_real_signatures_are_refused(inputs, domain):
    body, key, public = inputs
    signature = key.sign(domain+contract.canonical(body))
    raw = contract.canonical({'body': body, 'signature': base64.b64encode(signature).decode()})
    with pytest.raises(InvalidSignature): codec.verify_authorization(raw, public, body['request'])


def test_new_domain_and_type_cannot_enter_v1_or_v2(inputs):
    raw, body = signed(inputs)
    assert len({contract.DOMAIN, v1.DOMAIN, retained.DOMAIN}) == 3
    for old in (v1, v2):
        with pytest.raises(ValueError): old.verify_authorization(raw, inputs[2], body['request'])


@pytest.mark.parametrize('path', ['preparation.restore.backup_sha256', 'preparation.restore.credentials_sha256',
    'preparation.restore.restore_package_sha256', 'preparation.restore.restore_receipt_sha256',
    'preparation.restore.native_binding_sha256', 'preparation.restore.restored_release_sha256',
    'preparation.backup.sha256', 'preparation.backup.receipt_sha256', 'preparation.backup.release_sha256',
    'preparation.backup.transport_package_sha256', 'preparation.backup.credentials_sha256',
    'preparation.execution_package_sha256', 'preparation.monitor_package_sha256', 'preparation.state_sha256',
    'preparation.target_snapshot.facts_sha256', 'preparation.shared_manifest_sha256',
    'source_stop.database_sha256', 'source_stop.source_package_sha256'])
def test_resigned_binding_digests_cannot_reuse_existing_signature(inputs, path):
    raw, _ = signed(inputs)
    envelope = contract.decode(raw)
    body = envelope['body']; put(body, path, h(777))
    for key in ('execution_package_sha256', 'monitor_package_sha256'):
        body['request'][key] = body['preparation'][key]
    refresh(contract, body)
    contract.validate_body(body)  # Prove the mutation reaches actual crypto verification.
    with pytest.raises(InvalidSignature):
        codec.verify_authorization(contract.canonical(envelope), inputs[2], body['request'])


def test_original_key_identity_and_exact_expected_request(inputs):
    body, key, public = inputs
    other = Ed25519PrivateKey.generate()
    with pytest.raises(ValueError):
        codec.sign_authorization(body['request'], body['source_stop'], body['preparation'], other, now=at())
    raw, _ = signed(inputs)
    with pytest.raises(ValueError): codec.verify_authorization(raw, public_bytes(other), body['request'])
    changed = deepcopy(body['request']); changed['network_sha256'] = h(777)
    with pytest.raises(ValueError): codec.verify_authorization(raw, public, changed)
    with pytest.raises(ValueError):
        codec.sign_authorization(body['request'], body['source_stop'], body['preparation'], object(), now=at())


def test_sign_rejects_bad_binding_and_requires_explicit_time(inputs):
    body, key, _ = inputs
    with pytest.raises(TypeError):
        codec.sign_authorization(body['request'], body['source_stop'], body['preparation'], key)
    with pytest.raises(ValueError):
        codec.sign_authorization(body['request'], body['source_stop'], body['preparation'], key, now=None)
    body['source_stop']['request_sha256'] = h(777)
    with pytest.raises(ValueError):
        codec.sign_authorization(body['request'], body['source_stop'], body['preparation'], key, now=at())


@pytest.mark.parametrize('public', [b'', b'not a PEM', 'public', bytes(8193), bytearray(b'public')])
def test_public_key_encoding_and_size(inputs, public):
    raw, body = signed(inputs)
    with pytest.raises(ValueError): codec.verify_authorization(raw, public, body['request'])


def test_other_crypto_key_type_is_not_the_original_publisher(inputs):
    body, _, _ = inputs
    other = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(ValueError):
        codec.sign_authorization(body['request'], body['source_stop'], body['preparation'], other, now=at())
    raw, _ = signed(inputs)
    with pytest.raises(ValueError): codec.verify_authorization(raw, public_bytes(other), body['request'])


def test_detached_verified_body_and_noncanonical_json_preserve_exact_raw_hash(inputs):
    raw, body = signed(inputs)
    # Encoding whitespace is not a new identity; signatures cover canonical body.
    pretty = json.dumps(contract.decode(raw), ensure_ascii=False, indent=2).encode()
    assert codec.verify_authorization(pretty, inputs[2], body['request']) == body
    record = contract.make_record(body['request'], valid_network(), pretty, activated_at=at(),
        operator_attestation={key: True for key in contract.ATTESTATION})
    assert record['authorization_sha256'] == hashlib.sha256(pretty).hexdigest()
    body['preparation']['target_snapshot']['facts_sha256'] = h(777)
    assert codec.verify_authorization(raw, inputs[2], inputs[0]['request']) == inputs[0]


def test_signature_bytes_mutation_refused(inputs):
    raw, body = signed(inputs)
    envelope = contract.decode(raw)
    signature = bytearray(base64.b64decode(envelope['signature'])); signature[0] ^= 1
    envelope['signature'] = base64.b64encode(signature).decode()
    with pytest.raises(InvalidSignature):
        codec.verify_authorization(contract.canonical(envelope), inputs[2], body['request'])
