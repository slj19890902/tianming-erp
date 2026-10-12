"""Synthetic public claims only; no live data, credentials or services."""
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


def h(number):
    return format(number, '064x')


def valid_network():
    origin = 'http://192.168.3.80:8000'
    return {'ERP_HOME_REHEARSAL': '0', 'ERP_PRODUCTION_TRANSPORT': 'lan_http',
        'ERP_BIND_HOST': '0.0.0.0', 'ERP_PORT': '8000', 'ERP_ALLOWED_ORIGINS': origin,
        'ERP_TRUSTED_HOSTS': '192.168.3.80', 'ERP_TRUSTED_PROXY_IPS': '',
        'ERP_SESSION_COOKIE_SECURE': 'false', 'ERP_BROWSER_URL': origin+'/',
        'ERP_HEALTH_URL': origin+'/api/health', 'ERP_LAN_HTTP_ORIGIN': '',
        'ERP_ADDITIONAL_PRIVATE_HTTP_ORIGINS': '', 'ERP_SHELF_LABEL_ORIGIN': origin,
        'ERP_MOBILE_QR_ORIGIN': origin}


def valid_body(c):
    target = {'host_sha256': h(1), 'installation_id': '1'*32, 'service_uid': 499,
        'service_gid': 499, 'service_account': '_tianmingerp', 'root': '/Library/TianmingERP/instance'}
    snapshot = {'database_sha256': h(2), 'schema_sha256': h(3), 'facts_sha256': h(4),
        'files_sha256': h(5), 'preserved_environment_sha256': h(6), 'pdf_sources_sha256': h(7),
        'revision': 'ep1010md', 'tables': 325, 'files': 3121, 'registered_attachments': 104}
    restore = {'backup_sha256': h(8), 'backup_created_at': '2026-10-10T12:12:10Z',
        'credentials_sha256': h(9), 'restore_package_sha256': h(10),
        'restore_receipt_sha256': h(11), 'native_binding_sha256': h(12),
        'restored_release_sha256': h(13), 'restored_schema_authority_sha256': None}
    backup = {'sha256': h(14), 'size': 8096, 'created_at': '2026-10-12T02:00:00Z',
        'verified_at': '2026-10-12T02:00:01Z', 'receipt_sha256': h(15),
        'transport_control_sha256': h(16), 'transport_contract_sha256': h(17),
        'transport_package_sha256': h(18), 'release_sha256': h(19),
        'schema_authority_sha256': None, 'credentials_sha256': h(20)}
    prep = {'type': c.PREPARATION, 'prepared_at': '2026-10-12T02:00:02Z', 'target': target,
        'publisher_sha256': h(21), 'context_sha256': h(22), 'registry_sha256': h(23),
        'state_sha256': h(24), 'execution_package_sha256': h(25), 'monitor_package_sha256': h(26),
        'system_control_sha256': h(27), 'system_plist_sha256': h(28), 'restore': restore,
        'backup': backup, 'target_snapshot': snapshot, 'shared_manifest_sha256': h(29),
        'credential_purposes': ['backup', 'deepseek', 'mailbox']}
    request = {'type': c.REQUEST, 'request_id': '2'*32, 'created_at': '2026-10-12T02:00:03Z',
        'expires_at': '2026-10-19T02:00:03Z', 'target': deepcopy(target),
        'publisher_sha256': prep['publisher_sha256'], 'network_sha256': c.digest(valid_network()),
        'execution_package_sha256': prep['execution_package_sha256'],
        'monitor_package_sha256': prep['monitor_package_sha256'], 'preparation_sha256': c.digest(prep)}
    source = {'type': c.SOURCE_STOP, 'source_sha256': h(30), 'source_package_sha256': h(31),
        'request_sha256': c.digest(request), 'state_sha256': h(32), 'database_sha256': h(33),
        'files_sha256': h(34), 'environment_sha256': h(35), 'credential_config_sha256': h(36),
        'process_record_sha256': h(37), 'audit_started_at': '2026-10-12T02:00:04Z',
        'verified_at': '2026-10-12T02:00:05Z', 'manual_stop': True, 'process_stopped': True,
        'maintenance_lock_held': True, 'database_read_lease_held': True, 'journals_absent': True}
    return {'type': c.AUTHORIZATION, 'request': request, 'preparation': prep, 'source_stop': source,
        'issued_at': '2026-10-12T02:00:06Z', 'install_before': '2026-10-12T10:00:06Z'}


def test_explicit_current_mac_contract_is_available():
    assert importlib.util.find_spec('desktop_assistant.current_mac_cutover_contract') is not None


@pytest.fixture
def contract():
    from desktop_assistant import current_mac_cutover_contract
    return current_mac_cutover_contract


@pytest.fixture
def body(contract):
    return valid_body(contract)


def at(value='2026-10-12T02:00:06Z'):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def get(value, path):
    for part in path.split('.'):
        value = value[part]
    return value


def put(value, path, replacement):
    parts = path.split('.')
    for part in parts[:-1]:
        value = value[part]
    value[parts[-1]] = replacement


def refresh(c, value):
    value['request']['preparation_sha256'] = c.digest(value['preparation'])
    value['source_stop']['request_sha256'] = c.digest(value['request'])


def envelope(c, body):
    # Deliberately NOT cryptographically valid. Contract shape must say so.
    return c.canonical({'body': body, 'signature': base64.b64encode(bytes(64)).decode()})


def record(c, body):
    return c.make_record(body['request'], valid_network(), envelope(c, body),
        activated_at=at(), operator_attestation={key: True for key in c.ATTESTATION})


def test_separate_roles_mac_business_and_immutable_inputs(contract, body):
    before = deepcopy(body)
    assert contract.validate_body(body) is body
    assert body['source_stop']['database_sha256'] != body['preparation']['target_snapshot']['database_sha256']
    p = body['preparation']
    roles = [p['restore']['restore_package_sha256'], p['backup']['release_sha256'],
        p['backup']['transport_package_sha256'], p['execution_package_sha256'], p['monitor_package_sha256']]
    assert len(set(roles)) == 5
    assert body == before


def test_all_roles_may_share_one_originally_approved_package(contract, body):
    for path in ('preparation.restore.restore_package_sha256', 'preparation.backup.release_sha256',
            'preparation.backup.transport_package_sha256', 'preparation.execution_package_sha256',
            'preparation.monitor_package_sha256', 'request.execution_package_sha256', 'request.monitor_package_sha256'):
        put(body, path, h(900))
    refresh(contract, body)
    contract.validate_body(body)


def test_constructors_detach_all_mutable_input_and_require_attestation(contract, body):
    original = deepcopy(body)
    p = body['preparation']
    prep = contract.make_preparation(**{k: v for k, v in p.items() if k not in {'type', 'prepared_at'}},
        now=at(p['prepared_at']))
    req = contract.make_request(prep, body['request']['network_sha256'], p['publisher_sha256'],
        request_id=body['request']['request_id'], now=at(body['request']['created_at']))
    result = contract.make_body(req, prep, body['source_stop'], now=at())
    assert result == body
    prep['restore']['backup_sha256'] = h(500)
    req['target']['host_sha256'] = h(501)
    assert result == original and body == original
    assert contract.validate_record(record(contract, body)) == body
    with pytest.raises(TypeError):
        contract.make_record(body['request'], valid_network(), envelope(contract, body), activated_at=at())


@pytest.mark.parametrize('section', ['', 'request', 'request.target', 'preparation', 'preparation.target',
    'preparation.restore', 'preparation.backup', 'preparation.target_snapshot', 'source_stop'])
@pytest.mark.parametrize('change', ['extra', 'missing'])
def test_exact_fields_every_level(contract, body, section, change):
    value = get(body, section) if section else body
    if change == 'extra': value['unknown'] = None
    else: value.pop(next(iter(value)))
    with pytest.raises(ValueError): contract.validate_body(body)


@pytest.mark.parametrize('kind', ['tianming.factory-target.v1', 'tianming.factory-target-retained.v2',
    'tianming.factory-cutover-retained.v2', '', None, True])
@pytest.mark.parametrize('section', ['', 'request', 'preparation', 'source_stop'])
def test_v3_never_reinterprets_an_old_type(contract, body, section, kind):
    (get(body, section) if section else body)['type'] = kind
    with pytest.raises(ValueError): contract.validate_body(body)


@pytest.mark.parametrize('key,value', [('service_uid', True), ('service_uid', 501), ('service_gid', 0),
    ('service_gid', 499.0), ('service_account', 'root'), ('root', '/Library/TianmingERP/instance/'),
    ('root', '/tmp/instance'), ('installation_id', 'A'*32), ('host_sha256', 'a'*63)])
def test_fixed_mac_identity(contract, body, key, value):
    body['preparation']['target'][key] = value
    body['request']['target'][key] = value
    with pytest.raises(ValueError):
        refresh(contract, body); contract.validate_body(body)


@pytest.mark.parametrize('path', ['preparation.'+name for name in (
    'publisher_sha256', 'context_sha256', 'registry_sha256', 'state_sha256', 'execution_package_sha256',
    'monitor_package_sha256', 'system_control_sha256', 'system_plist_sha256', 'shared_manifest_sha256')]
    + ['preparation.restore.'+name for name in ('backup_sha256', 'credentials_sha256', 'restore_package_sha256',
        'restore_receipt_sha256', 'native_binding_sha256', 'restored_release_sha256')]
    + ['preparation.backup.'+name for name in ('sha256', 'receipt_sha256', 'transport_control_sha256',
        'transport_contract_sha256', 'transport_package_sha256', 'release_sha256', 'credentials_sha256')]
    + ['preparation.target_snapshot.'+name for name in ('database_sha256', 'schema_sha256', 'facts_sha256',
        'files_sha256', 'preserved_environment_sha256', 'pdf_sources_sha256')])
def test_every_preparation_digest_change_breaks_existing_request(contract, body, path):
    put(body, path, h(999))
    with pytest.raises(ValueError): contract.validate_body(body)


@pytest.mark.parametrize('key', ['request_id', 'publisher_sha256', 'network_sha256',
    'execution_package_sha256', 'monitor_package_sha256', 'preparation_sha256'])
def test_source_must_bind_entire_request(contract, body, key):
    body['request'][key] = '4'*32 if key == 'request_id' else h(999)
    with pytest.raises(ValueError): contract.validate_body(body)


@pytest.mark.parametrize('key', ['publisher_sha256', 'execution_package_sha256', 'monitor_package_sha256'])
def test_rehashing_request_cannot_hide_direct_preparation_binding(contract, body, key):
    body['request'][key] = h(999)
    body['source_stop']['request_sha256'] = contract.digest(body['request'])
    with pytest.raises(ValueError): contract.validate_body(body)


@pytest.mark.parametrize('path', ['preparation.restore.restored_schema_authority_sha256',
    'preparation.backup.schema_authority_sha256', 'source_stop.process_record_sha256'])
def test_only_explicit_nullable_fields(contract, body, path):
    for value in (None, h(333)):
        put(body, path, value); refresh(contract, body)
        contract.validate_body(body)
    put(body, path, '')
    refresh(contract, body)
    with pytest.raises(ValueError): contract.validate_body(body)


@pytest.mark.parametrize('path', ['preparation.restore.credentials_sha256', 'preparation.backup.credentials_sha256',
    'preparation.restore.native_binding_sha256', 'preparation.state_sha256', 'source_stop.database_sha256',
    'source_stop.credential_config_sha256'])
def test_required_evidence_cannot_be_null(contract, body, path):
    put(body, path, None); refresh(contract, body)
    with pytest.raises(ValueError): contract.validate_body(body)


@pytest.mark.parametrize('flag', ['manual_stop', 'process_stopped', 'maintenance_lock_held',
    'database_read_lease_held', 'journals_absent'])
@pytest.mark.parametrize('value', [False, 1, 'true', None])
def test_stop_claims_require_exact_true(contract, body, flag, value):
    body['source_stop'][flag] = value
    with pytest.raises(ValueError): contract.validate_body(body)


@pytest.mark.parametrize('purposes', [[], ['mailbox'], ['backup', 'backup'], ['mailbox', 'backup'],
    ['backup', 'password'], ['backup', 1], 'backup', None])
def test_credential_purpose_list_is_exact_sorted_and_nonsecret(contract, body, purposes):
    body['preparation']['credential_purposes'] = purposes
    refresh(contract, body)
    with pytest.raises(ValueError): contract.validate_body(body)


@pytest.mark.parametrize('path', ['preparation.backup.size', 'preparation.target_snapshot.tables',
    'preparation.target_snapshot.files', 'preparation.target_snapshot.registered_attachments'])
@pytest.mark.parametrize('value', [True, 1.0, -1, '1', 2**63])
def test_counts_are_bounded_integers(contract, body, path, value):
    put(body, path, value)
    with pytest.raises(ValueError): refresh(contract, body); contract.validate_body(body)


@pytest.mark.parametrize('raw', [b'', b'[]', b'{"x":1,"x":2}', b'{"x":{"y":1,"y":2}}',
    b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}', b'{"x":1e309}', b'{"x":1.0}',
    b'{"x":9223372036854775808}', b'{"x":-9223372036854775809}', b'{"x":"\\ud800"}',
    b'\xef\xbb\xbf{}', b'\xff', b'{} trailing', b'{}\0', '{}', bytearray(b'{}')])
def test_strict_json(contract, raw):
    with pytest.raises(ValueError): contract.decode(raw)


def test_utf8_byte_limit_and_depth_in_decode_and_constructors(contract, body):
    exact = b'{"x":"'+b'a'*(contract.LIMIT-8)+b'"}'
    assert len(exact) == contract.LIMIT and contract.decode(exact)['x']
    with pytest.raises(ValueError): contract.decode(exact+b' ')
    with pytest.raises(ValueError): contract.canonical({'x': '汉'*(contract.LIMIT//3)})
    deep = {}
    for _ in range(contract.MAX_DEPTH+1): deep = {'x': deep}
    with pytest.raises(ValueError): contract.canonical(deep)
    with pytest.raises(ValueError): contract.decode(json.dumps(deep).encode())
    for value in (float('inf'), float('nan'), 1.0, {1: 'x'}, (1, 2), b'x'):
        with pytest.raises(ValueError): contract.canonical(value)


@pytest.mark.parametrize('original,expected', [
    ('2026-10-12T10:00:00.987654+08:00', '2026-10-12T02:00:00Z'),
    ('2026-10-12T01:00:00.1-01:00', '2026-10-12T02:00:00Z'),
    ('2026-10-12T02:00:00Z', '2026-10-12T02:00:00Z'),
    ('2026-10-12T00:00:00.9+08:00', '2026-10-11T16:00:00Z')])
def test_authenticated_iso_public_normalization_preserves_original(contract, original, expected):
    saved = original
    assert contract.normalize_timestamp(original) == expected
    assert original == saved


@pytest.mark.parametrize('value', ['2026-10-12T02:00:00', '2026-10-12 02:00:00Z',
    '2026-02-30T02:00:00Z', '2026-10-12T02:00:00.1234567Z', '2026-10-12T02:00:00+00:60',
    '2026-10-12T02:00:00+24:00', '2026-10-12T02:00:60Z', '0001-01-01T00:00:00+01:00', None])
def test_no_implicit_timezone_or_lossy_original_parsing(contract, value):
    with pytest.raises(ValueError): contract.normalize_timestamp(value)


@pytest.mark.parametrize('path,value', [
    ('preparation.backup.created_at', '2026-10-09T00:00:00Z'),
    ('preparation.backup.verified_at', '2026-10-12T01:59:59Z'),
    ('preparation.prepared_at', '2026-10-12T02:00:00Z'),
    ('request.created_at', '2026-10-12T02:00:01Z'),
    ('request.expires_at', '2026-10-19T02:00:04Z'),
    ('request.expires_at', '2026-10-12T02:00:03Z'),
    ('source_stop.audit_started_at', '2026-10-12T02:00:02Z'),
    ('source_stop.verified_at', '2026-10-12T02:00:03Z'),
    ('source_stop.verified_at', '2026-10-19T02:00:03Z'),
    ('issued_at', '2026-10-12T02:00:04Z'),
    ('install_before', '2026-10-12T10:00:07Z'),
    ('install_before', '2026-10-12T02:00:06Z'),
    ('preparation.restore.backup_created_at', '2026-10-10T20:12:10+08:00'),
    ('preparation.backup.created_at', '2026-10-12T02:00:00.1Z')])
def test_time_order_and_public_utc_seconds(contract, body, path, value):
    put(body, path, value); refresh(contract, body)
    with pytest.raises(ValueError): contract.validate_body(body)


def test_request_and_first_activation_windows_have_exact_boundaries(contract, body):
    contract.require_install_window(body, now=at())
    contract.require_install_window(body, now=at(body['install_before'])-timedelta(microseconds=1))
    for now in (at()-timedelta(microseconds=1), at(body['install_before']),
            at()+timedelta(days=20), datetime(2026, 10, 12), None):
        with pytest.raises(ValueError): contract.require_install_window(body, now=now)
    body['request']['expires_at'] = '2026-10-12T03:00:00Z'
    refresh(contract, body)
    result = contract.make_body(body['request'], body['preparation'], body['source_stop'], now=at())
    assert result['install_before'] == body['request']['expires_at']
    contract.validate_body(result)
    with pytest.raises(ValueError):
        contract.make_body(body['request'], body['preparation'], body['source_stop'], now=at('2026-10-12T03:00:00Z'))


def test_callers_supply_time_and_identity_no_clock_or_random_generation(contract, body):
    with pytest.raises(TypeError):
        contract.make_body(body['request'], body['preparation'], body['source_stop'])
    with pytest.raises(TypeError): contract.require_install_window(body)
    with pytest.raises(ValueError):
        contract.make_request(body['preparation'], h(3), h(21), request_id='2'*32,
            now=datetime.max.replace(tzinfo=timezone.utc))


@pytest.mark.parametrize('change', ['unknown', 'missing', 'short', 'nonascii', 'padbits', 'whitespace', 'type'])
def test_strict_signature_envelope_shape(contract, body, change):
    value = contract.decode(envelope(contract, body))
    if change == 'unknown': value['version'] = 3
    elif change == 'missing': value.pop('signature')
    elif change == 'short': value['signature'] = base64.b64encode(bytes(63)).decode()
    elif change == 'nonascii': value['signature'] = '汉'
    elif change == 'padbits': value['signature'] = value['signature'][:-3]+'B=='
    elif change == 'whitespace': value['signature'] += '\n'
    else: value['signature'] = True
    with pytest.raises(ValueError): contract.authorization_body(contract.canonical(value), body['request'])


@pytest.mark.parametrize('key', ['context_sha256', 'registry_sha256', 'authorization_sha256',
    'activated_at', 'target_snapshot', 'network', 'operator_attestation'])
def test_record_preserves_exact_bindings(contract, body, key):
    value = record(contract, body)
    if key.endswith('_sha256'): value[key] = h(999)
    elif key == 'activated_at': value[key] = body['install_before']
    elif key == 'target_snapshot': value[key]['facts_sha256'] = h(999)
    elif key == 'network': value[key]['ERP_PORT'] = '9000'
    else: value[key]['source_isolation_confirmed'] = 1
    with pytest.raises(ValueError): contract.validate_record(value)


def test_record_constructor_does_not_invent_consent_and_does_not_expire_runtime(contract, body):
    value = record(contract, body)
    assert contract.validate_record(value) == body  # No current-time argument exists.
    for attestation in (None, {}, {key: True for key in contract.ATTESTATION}|{'extra': True},
            {'factory_cutover_authorized': True, 'source_isolation_confirmed': False}):
        with pytest.raises(ValueError):
            contract.make_record(body['request'], valid_network(), envelope(contract, body),
                activated_at=at(), operator_attestation=attestation)
    malformed = valid_network(); malformed['unexpected'] = ''
    with pytest.raises(ValueError):
        contract.make_record(body['request'], malformed, envelope(contract, body),
            activated_at=at(), operator_attestation={key: True for key in contract.ATTESTATION})


def test_request_shape_without_preparation_is_not_a_bound_authorization(contract, body):
    body['request']['preparation_sha256'] = h(999)
    assert contract.validate_request(body['request']) is body['request']
    with pytest.raises(ValueError): contract.validate_body(body)
    fake = envelope(contract, valid_body(contract))
    assert contract.authorization_body(fake, valid_body(contract)['request'])  # Shape only, not crypto.


@pytest.mark.parametrize('section', ['', 'operator_attestation'])
@pytest.mark.parametrize('change', ['extra', 'missing'])
def test_record_and_operator_confirmation_have_no_optional_keys(contract, body, section, change):
    value = record(contract, body)
    selected = get(value, section) if section else value
    if change == 'extra': selected['extra'] = None
    else: selected.pop(next(iter(selected)))
    with pytest.raises(ValueError): contract.validate_record(value)


def test_contract_isolated_stdlib_import_and_validation_without_runtime_io_or_clock(contract, body):
    code = '''
import ast, json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from desktop_assistant import current_mac_cutover_contract as c
b = c.decode(sys.stdin.buffer.read())
c.validate_body(b)  # Load stdlib strptime before denying runtime IO.
tree = ast.parse(Path(c.__file__).read_text())
assert not any(isinstance(n, ast.Attribute) and n.attr in ('now', 'utcnow', 'today') for n in ast.walk(tree))
assert not any(n.startswith(('cryptography', 'sqlite3', 'desktop_assistant.storage',
    'desktop_assistant.signing', 'desktop_assistant.manager', 'app.')) for n in sys.modules)
def deny(event, args):
    if event in ('open', 'os.listdir', 'os.scandir', 'os.system', 'subprocess.Popen') or event.startswith('socket.'):
        raise AssertionError('contract performed runtime IO')
sys.addaudithook(deny)
p = b['preparation']
p = c.make_preparation(**{k:v for k,v in p.items() if k not in ('type','prepared_at')}, now=c._time(p['prepared_at']))
r = c.make_request(p, b['request']['network_sha256'], p['publisher_sha256'],
    request_id=b['request']['request_id'], now=c._time(b['request']['created_at']))
made = c.make_body(r, p, b['source_stop'], now=c._time(b['issued_at']))
assert made == b
c.require_install_window(made, now=c._time(b['issued_at']))
assert c.normalize_timestamp('2026-10-12T10:00:00.999999+08:00') == '2026-10-12T02:00:00Z'
print('isolated stdlib contract: passed')
'''
    result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', code,
        str(Path(__file__).resolve().parents[2])], input=contract.canonical(body),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=15)
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b'isolated stdlib contract: passed\n'
