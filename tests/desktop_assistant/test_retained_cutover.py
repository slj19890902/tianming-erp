"""Synthetic original-key signatures; no operational signing or private-key IO."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import base64
import json

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from desktop_assistant import retained_cutover as codec
from desktop_assistant import retained_snapshot_contract as contract
from desktop_assistant import factory_cutover as original
from desktop_assistant.release_request import public_identity

def valid_body(contract=None):
    """An old external-credential restore plus a new request and stopped audit."""
    if contract is None:
        from desktop_assistant import retained_snapshot_contract as contract
    h = lambda number: format(number, '064x')
    target = {'host_sha256': h(1), 'installation_id': '1' * 32,
        'service_uid': 499, 'service_gid': 499, 'service_account': '_tianmingerp',
        'root': '/Library/TianmingERP/instance'}
    snapshot = {'database_sha256': h(2), 'schema_sha256': h(3), 'facts_sha256': h(4),
        'files_sha256': h(5), 'preserved_environment_sha256': h(6),
        'pdf_sources_sha256': h(7), 'tables': 325, 'files': 3121,
        'registered_attachments': 104, 'revision': 'ep1010md'}
    target_snapshot = dict(snapshot, database_sha256=h(8), facts_sha256=h(9),
                           preserved_environment_sha256=h(10))
    restore = {'backup_sha256': h(11), 'backup_created_at': '2026-10-10T12:12:10Z',
        'credentials_sha256': h(12), 'restore_package_sha256': h(13),
        'restore_receipt_sha256': h(14), 'context_sha256': h(15), 'native_binding_sha256': h(16)}
    delta = {
        'operation_logs': {'columns_sha256': h(17), 'baseline_count': 4, 'target_count': 6,
            'baseline_sha256': h(18), 'target_sha256': h(19), 'appended_count': 2,
            'appended_sha256': h(20), 'first_appended_id': 5, 'last_appended_id': 6},
        'sqlite_sequence': {'table_present': True, 'baseline_sha256': h(21), 'target_sha256': h(22),
            'before': {'present': True, 'value': 4}, 'after': {'present': True, 'value': 6}},
        'environment_chain': [
            {'kind': 'home', 'receipt_sha256': h(23), 'script_sha256': h(24),
             'before_sha256': h(25), 'after_sha256': h(26),
             'changes_sha256': h(27), 'changed_fields_count': 13},
            {'kind': 'lan', 'receipt_sha256': h(28), 'script_sha256': h(29),
             'before_sha256': h(26), 'after_sha256': h(30),
             'changes_sha256': h(31), 'changed_fields_count': 4}]}
    preparation = {'type': contract.PREPARATION, 'policy': 'preview-login-only.v1',
        'prepared_at': '2026-10-12T02:00:00Z', 'target': target,
        'package_sha256': h(32), 'restore': restore,
        'baseline_snapshot': snapshot, 'target_snapshot': target_snapshot, 'preview_delta': delta}
    retained = {k: restore[k] for k in ('backup_sha256', 'backup_created_at',
        'credentials_sha256', 'restore_package_sha256', 'restore_receipt_sha256')}
    retained['preparation_sha256'] = contract.digest(preparation)
    request = {'type': contract.REQUEST, 'request_id': '2' * 32, 'target': deepcopy(target),
        'package_sha256': h(32), 'network_sha256': contract.digest(valid_network()),
        'publisher_sha256': h(33), 'created_at': '2026-10-12T02:00:01Z',
        'expires_at': '2026-10-19T02:00:01Z', 'retained_snapshot': retained}
    source = {'type': contract.SOURCE, 'source_sha256': h(34), 'backup_sha256': h(11),
        'backup_created_at': restore['backup_created_at'], 'source_package_sha256': h(35),
        'credentials_sha256': h(12), 'credential_purposes': ['backup', 'deepseek', 'mailbox'],
        'snapshot': deepcopy(snapshot), 'current_snapshot': dict(snapshot, database_sha256=h(36)),
        'audit_started_at': '2026-10-12T02:00:02Z', 'verified_at': '2026-10-12T02:00:03Z',
        'stop_proof': {'type': contract.STOP_PROOF, 'state_sha256': h(37),
            'database_sha256': h(36), 'files_sha256': h(5), 'environment_sha256': h(25),
            'credential_config_sha256': h(38), 'process_record_sha256': h(39),
            'backup_receipt_sha256': h(40), 'manual_stop': True, 'process_stopped': True,
            'maintenance_lock_held': True, 'database_read_lease_held': True, 'journals_absent': True}}
    return {'type': contract.AUTHORIZATION, 'request': request, 'preparation': preparation,
        'source': source, 'issued_at': '2026-10-12T02:00:04Z', 'install_before': '2026-10-12T10:00:04Z'}

def valid_network():
    origin = 'http://192.168.3.80:8000'
    return {'ERP_HOME_REHEARSAL': '0', 'ERP_PRODUCTION_TRANSPORT': 'lan_http',
        'ERP_BIND_HOST': '0.0.0.0', 'ERP_PORT': '8000', 'ERP_ALLOWED_ORIGINS': origin,
        'ERP_TRUSTED_HOSTS': '192.168.3.80', 'ERP_TRUSTED_PROXY_IPS': '',
        'ERP_SESSION_COOKIE_SECURE': 'false', 'ERP_BROWSER_URL': origin + '/',
        'ERP_HEALTH_URL': origin + '/api/health', 'ERP_LAN_HTTP_ORIGIN': '',
        'ERP_ADDITIONAL_PRIVATE_HTTP_ORIGINS': '', 'ERP_SHELF_LABEL_ORIGIN': origin,
        'ERP_MOBILE_QR_ORIGIN': origin}

@pytest.fixture
def signed_inputs():
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                          serialization.PublicFormat.SubjectPublicKeyInfo)
    body = valid_body(contract)
    body['request']['publisher_sha256'] = public_identity(public)[1]
    contract.validate_body(body)
    now = datetime(2026, 10, 12, 2, 0, 4, tzinfo=timezone.utc)
    return body, key, public, now


def signed(inputs):
    body, key, public, now = inputs
    raw = codec.sign_authorization(body['request'], body['source'], body['preparation'], key, now=now)
    return raw, codec.verify_authorization(raw, public, body['request'])


def test_real_signature_preserves_historical_backup_and_separate_packages(signed_inputs):
    before = deepcopy(signed_inputs[0])
    raw, body = signed(signed_inputs)
    assert body == before
    assert signed_inputs[0] == before
    assert body['source']['backup_created_at'] == '2026-10-10T12:12:10Z'
    assert body['request']['package_sha256'] != body['request']['retained_snapshot']['restore_package_sha256']
    assert body['source']['credentials_sha256'] is not None
    assert len(raw) <= contract.LIMIT
    codec.require_install_window(body, now=signed_inputs[3])


@pytest.mark.parametrize('bad_key', [None, b'not-a-private-key', object()])
def test_sign_rejects_non_ed25519_identity(signed_inputs, bad_key):
    b, _, _, now = signed_inputs
    with pytest.raises(ValueError):
        codec.sign_authorization(b['request'], b['source'], b['preparation'], bad_key, now=now)


def test_cannot_sign_using_replacement_identity(signed_inputs):
    b, _, _, now = signed_inputs
    with pytest.raises(ValueError, match='原发布身份'):
        codec.sign_authorization(b['request'], b['source'], b['preparation'], Ed25519PrivateKey.generate(), now=now)


def test_verify_requires_original_public_identity(signed_inputs):
    raw, _ = signed(signed_inputs)
    other = Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    with pytest.raises(InvalidSignature):
        codec.verify_authorization(raw, other, signed_inputs[0]['request'])


@pytest.mark.parametrize('field', ['network_sha256', 'package_sha256', 'request_id'])
def test_complete_expected_request_binding(signed_inputs, field):
    raw, _ = signed(signed_inputs)
    request = deepcopy(signed_inputs[0]['request'])
    request[field] = 'a' * (32 if field == 'request_id' else 64)
    with pytest.raises(ValueError):
        codec.verify_authorization(raw, signed_inputs[2], request)


def test_valid_shape_source_tampering_fails_signature(signed_inputs):
    raw, _ = signed(signed_inputs)
    value = json.loads(raw)
    value['body']['source']['source_sha256'] = 'a' * 64
    contract.validate_body(value['body'])
    with pytest.raises(InvalidSignature):
        codec.verify_authorization(contract.canonical(value), signed_inputs[2], signed_inputs[0]['request'])


def test_preparation_tampering_cannot_hide_behind_valid_signature(signed_inputs):
    raw, _ = signed(signed_inputs)
    value = json.loads(raw)
    value['body']['preparation']['preview_delta']['operation_logs']['appended_sha256'] = 'a' * 64
    with pytest.raises(ValueError):
        codec.verify_authorization(contract.canonical(value), signed_inputs[2], signed_inputs[0]['request'])


def test_v1_signature_domain_cannot_authorize_v2_body(signed_inputs):
    body, key, public, _ = signed_inputs
    wrong = base64.b64encode(key.sign(original.DOMAIN + contract.canonical(body))).decode('ascii')
    with pytest.raises(InvalidSignature):
        codec.verify_authorization(contract.canonical({'body': body, 'signature': wrong}), public, body['request'])


def test_v1_decoder_does_not_accept_v2_authorization(signed_inputs):
    raw, _ = signed(signed_inputs)
    with pytest.raises(ValueError):
        original.verify_authorization(raw, signed_inputs[2], signed_inputs[0]['request'])


def test_v2_decoder_does_not_accept_authentic_v1_authorization(signed_inputs):
    b, key, public, now = signed_inputs
    request = {k: deepcopy(v) for k, v in b['request'].items() if k != 'retained_snapshot'}
    request.update(type=original.REQUEST, created_at='2026-10-10T12:00:00Z', expires_at='2026-10-17T12:00:00Z')
    source = {k: deepcopy(b['source'][k]) for k in ('source_sha256','backup_sha256','source_package_sha256',
                                                  'backup_created_at','credential_purposes','snapshot')}
    source.update(manual_stop=True, process_stopped=True)
    raw = original.sign_authorization(request, source, key, now=now)
    with pytest.raises(ValueError):
        codec.verify_authorization(raw, public, b['request'])


@pytest.mark.parametrize('signature', [None, 42, '*', base64.b64encode(b'x').decode(), base64.b64encode(b'x'*64).decode()])
def test_malformed_or_false_signature_rejected(signed_inputs, signature):
    body, _, public, _ = signed_inputs
    with pytest.raises((ValueError, InvalidSignature)):
        codec.verify_authorization(contract.canonical({'body':body,'signature':signature}), public, body['request'])


def test_duplicate_json_and_nonstandard_numbers_rejected(signed_inputs):
    raw, _ = signed(signed_inputs)
    value = json.loads(raw)
    body = contract.canonical(value['body'])
    duplicate = b'{"body":' + body + b',"body":' + body + b',"signature":' + json.dumps(value['signature']).encode() + b'}'
    for invalid in (duplicate, raw.replace(b'"tables":325',b'"tables":NaN'), b'[]', b'{}', raw+b'junk'):
        with pytest.raises(ValueError):
            codec.verify_authorization(invalid, signed_inputs[2], signed_inputs[0]['request'])


def test_signature_encoding_matches_protected_record_canonical_rule(signed_inputs):
    raw, _ = signed(signed_inputs)
    value = json.loads(raw)
    signature = value['signature']
    alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
    assert signature.endswith('==')
    alternative = signature[:-3] + alphabet[alphabet.index(signature[-3]) + 1] + '=='
    assert base64.b64decode(alternative, validate=True) == base64.b64decode(signature)
    value['signature'] = alternative
    with pytest.raises(ValueError):
        codec.verify_authorization(contract.canonical(value), signed_inputs[2], signed_inputs[0]['request'])


def test_first_install_window_is_separate_from_persistent_signature(signed_inputs, monkeypatch):
    raw, body = signed(signed_inputs)
    issued = signed_inputs[3]
    codec.require_install_window(body, now=issued)
    codec.require_install_window(body, now=issued + timedelta(hours=8, microseconds=-1))
    for when in (issued-timedelta(seconds=1), issued+timedelta(hours=8), issued+timedelta(days=30)):
        with pytest.raises(ValueError):
            codec.require_install_window(body, now=when)
    def no_clock(*args, **kwargs):
        raise AssertionError('Signature verification must not expire an installed server')
    monkeypatch.setattr(codec, '_now', no_clock)
    assert codec.verify_authorization(raw, signed_inputs[2], signed_inputs[0]['request']) == body


def test_new_request_expiry_caps_install_window(signed_inputs):
    b, key, public, now = signed_inputs
    b['request']['expires_at'] = '2026-10-12T03:00:00Z'
    raw = codec.sign_authorization(b['request'], b['source'], b['preparation'], key, now=now)
    assert codec.verify_authorization(raw, public, b['request'])['install_before'] == '2026-10-12T03:00:00Z'
    with pytest.raises(ValueError):
        codec.sign_authorization(b['request'], b['source'], b['preparation'], key,
                                 now=datetime(2026,10,12,3,tzinfo=timezone.utc))


def test_output_envelope_has_its_own_size_limit(signed_inputs, monkeypatch):
    b, key, _, now = signed_inputs
    monkeypatch.setattr(contract,'LIMIT',len(contract.canonical(b))+1)
    with pytest.raises(ValueError, match='过大'):
        codec.sign_authorization(b['request'], b['source'], b['preparation'], key, now=now)


def test_codec_never_reads_keyfiles_or_connects_network(signed_inputs, monkeypatch):
    import builtins
    import socket
    import subprocess
    from pathlib import Path
    def denied(*args, **kwargs):
        raise AssertionError('codec performed operational IO')
    with monkeypatch.context() as m:
        m.setattr(builtins,'open',denied)
        m.setattr(Path,'open',denied)
        m.setattr(socket,'socket',denied)
        m.setattr(subprocess,'Popen',denied)
        signed(signed_inputs)
