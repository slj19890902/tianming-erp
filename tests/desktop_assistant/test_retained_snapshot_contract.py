"""Pure synthetic public contracts; no real data, services or credentials."""
import importlib.util
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import json
import os
import socket
import subprocess
import time

import pytest


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


def test_explicit_retained_contract_is_available():
    assert importlib.util.find_spec('desktop_assistant.retained_snapshot_contract') is not None


@pytest.fixture
def contract():
    from desktop_assistant import retained_snapshot_contract
    return retained_snapshot_contract


@pytest.fixture
def body(contract):
    return valid_body(contract)


def set_at(value, path, replacement):
    for name in path[:-1]:
        value = value[name]
    value[path[-1]] = replacement


def rebind(body, contract):
    body['request']['retained_snapshot']['preparation_sha256'] = contract.digest(body['preparation'])


def record_for(body, contract):
    # A shape-only validator accepts a synthetic 64-byte signature. The real
    # codec must verify Ed25519 independently before any privileged consumer.
    raw = contract.canonical({'body': body, 'signature': base64.b64encode(b'x' * 64).decode()})
    return {'type': contract.TYPE, 'request': deepcopy(body['request']), 'network': valid_network(),
        'authorization': raw.decode(), 'authorization_sha256': hashlib.sha256(raw).hexdigest(),
        'context_sha256': body['preparation']['restore']['context_sha256'],
        'registry_sha256': 'f' * 64, 'activated_at': '2026-10-12T03:00:00Z',
        'target_snapshot': deepcopy(body['preparation']['target_snapshot']),
        'operator_attestation': deepcopy(contract.ATTESTATION)}


def test_valid_old_backup_new_audit_and_distinct_programs_are_preserved(contract, body):
    original = deepcopy(body)
    assert contract.validate_body(body) is body
    assert body == original
    assert body['source']['backup_created_at'] < body['request']['created_at']
    assert len({body['request']['package_sha256'], body['source']['source_package_sha256'],
        body['request']['retained_snapshot']['restore_package_sha256']}) == 3
    assert contract.validate_record(record_for(body, contract)) == body


def test_old_backup_is_not_made_new_by_reissued_request(contract, body):
    old = '2020-01-01T00:00:00Z'
    body['source']['backup_created_at'] = old
    body['preparation']['restore']['backup_created_at'] = old
    body['request']['retained_snapshot']['backup_created_at'] = old
    rebind(body, contract)
    contract.validate_body(body)
    assert body['source']['backup_created_at'] == old


def test_source_remains_strict_while_target_preview_changes_are_explicit(contract, body):
    contract.validate_body(body)
    assert body['preparation']['baseline_snapshot']['facts_sha256'] != body['preparation']['target_snapshot']['facts_sha256']
    for name in body['source']['snapshot']:
        if name == 'database_sha256':
            continue
        altered = deepcopy(body)
        current = altered['source']['current_snapshot']
        current[name] = current[name] + 1 if type(current[name]) is int else ('different' if name == 'revision' else 'f' * 64)
        with pytest.raises(ValueError):
            contract.validate_body(altered)


@pytest.mark.parametrize('path,value', [
    (('type',), 'tianming.factory-cutover.v1'),
    (('request', 'type'), 'tianming.factory-target.v1'),
    (('request', 'request_id'), 'A' * 32),
    (('request', 'target', 'host_sha256'), 'A' * 64),
    (('request', 'target', 'service_uid'), True),
    (('request', 'target', 'service_gid'), 0),
    (('request', 'target', 'service_uid'), 2**31),
    (('request', 'target', 'service_account'), 'admin'),
    (('request', 'target', 'root'), '/Library/../TianmingERP/instance'),
    (('request', 'target', 'root'), '/Library//TianmingERP/instance'),
    (('request', 'target', 'root'), '/Library/TianmingERP/instance/'),
    (('request', 'target', 'root'), '//Library/TianmingERP/instance'),
    (('request', 'target', 'root'), '/Library/TianmingERP/instance\x7f'),
    (('request', 'target', 'root'), 'C:\\ERP'),
    (('request', 'package_sha256'), None),
    (('request', 'network_sha256'), '0' * 63),
    (('request', 'publisher_sha256'), 1),
    (('request', 'retained_snapshot', 'credentials_sha256'), None),
    (('preparation', 'restore', 'credentials_sha256'), None),
    (('source', 'credentials_sha256'), None),
    (('preparation', 'policy'), 'ignore-login-table'),
    (('preparation', 'baseline_snapshot', 'tables'), True),
    (('preparation', 'baseline_snapshot', 'tables'), 0),
    (('preparation', 'baseline_snapshot', 'tables'), 2**63),
    (('preparation', 'baseline_snapshot', 'files'), -1),
    (('preparation', 'baseline_snapshot', 'registered_attachments'), 1.0),
    (('preparation', 'baseline_snapshot', 'revision'), 'unsafe.head'),
    (('preparation', 'target_snapshot', 'schema_sha256'), 'f' * 64),
    (('preparation', 'target_snapshot', 'files_sha256'), 'f' * 64),
    (('preparation', 'target_snapshot', 'pdf_sources_sha256'), 'f' * 64),
    (('preparation', 'target_snapshot', 'registered_attachments'), 105),
    (('source', 'credential_purposes'), ['mailbox']),
    (('source', 'credential_purposes'), ['backup', 'backup']),
    (('source', 'credential_purposes'), ['mailbox', 'backup']),
    (('source', 'credential_purposes'), ['backup', 'unknown']),
    (('source', 'credential_purposes'), ['backup', True]),
    (('source', 'stop_proof', 'manual_stop'), 1),
    (('source', 'stop_proof', 'process_stopped'), False),
    (('source', 'stop_proof', 'maintenance_lock_held'), False),
    (('source', 'stop_proof', 'database_read_lease_held'), False),
    (('source', 'stop_proof', 'journals_absent'), False),
    (('source', 'stop_proof', 'process_record_sha256'), ''),
    (('source', 'stop_proof', 'database_sha256'), 'f' * 64),
    (('source', 'stop_proof', 'files_sha256'), 'f' * 64),
    (('source', 'stop_proof', 'environment_sha256'), 'f' * 64),
])
def test_strict_shapes_reject_invalid_fields(contract, body, path, value):
    set_at(body, path, value)
    with pytest.raises(ValueError):
        contract.validate_body(body)


@pytest.mark.parametrize('path', [(), ('request',), ('request', 'target'),
    ('request', 'retained_snapshot'), ('preparation',), ('preparation', 'restore'),
    ('preparation', 'baseline_snapshot'), ('preparation', 'preview_delta'),
    ('preparation', 'preview_delta', 'operation_logs'),
    ('preparation', 'preview_delta', 'sqlite_sequence'),
    ('preparation', 'preview_delta', 'sqlite_sequence', 'before'),
    ('preparation', 'preview_delta', 'environment_chain', 0),
    ('source',), ('source', 'stop_proof')])
@pytest.mark.parametrize('change', ['add', 'remove'])
def test_all_objects_have_exact_fields(contract, body, path, change):
    target = body
    for key in path:
        target = target[key]
    if change == 'add':
        target['raw_personal_information'] = 'must never be accepted'
    else:
        del target[next(iter(target))]
    with pytest.raises(ValueError):
        contract.validate_body(body)


@pytest.mark.parametrize('raw', [b'', b'[]', b'null', b'"text"', b'\xff', b'{} trailing',
    b'{"x":1,"x":2}', b'{"x":{"n":1,"n":1}}', b'{"x":1,"\\u0078":2}',
    b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}', b'{"x":1e10000}',
    b'{"x":"\\ud800"}', b'{"x":' + b'[' * 1100 + b'0' + b']' * 1100 + b'}',
    b'{"x":"' + b'a' * 65536 + b'"}', b'{"x":' + b'9' * 5000 + b'}'],
    ids=lambda value: 'json-' + hashlib.sha256(value).hexdigest()[:8])
def test_strict_json_rejects_ambiguous_or_unbounded_input(contract, raw):
    with pytest.raises(ValueError):
        contract.decode(raw)


@pytest.mark.parametrize('value', [bytearray(b'{}'), '{}', None, 1])
def test_decoder_requires_bytes(contract, value):
    with pytest.raises(ValueError):
        contract.decode(value)


@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf'), '\ud800', {'unsupported'}])
def test_canonical_rejects_non_json_values(contract, value):
    with pytest.raises(ValueError):
        contract.canonical(value)


@pytest.mark.parametrize('path,value', [
    (('request', 'expires_at'), '2026-10-12T02:00:01Z'),
    (('request', 'expires_at'), '2026-10-19T02:00:02Z'),
    (('source', 'audit_started_at'), '2026-10-12T02:00:00Z'),
    (('source', 'verified_at'), '2026-10-12T02:00:01Z'),
    (('source', 'verified_at'), '2026-10-12T02:00:05Z'),
    (('issued_at',), '2026-10-12T02:00:02Z'),
    (('install_before',), '2026-10-12T02:00:04Z'),
    (('install_before',), '2026-10-12T10:00:05Z'),
    (('request', 'created_at'), '2026-10-12T02:00:01+00:00'),
    (('request', 'created_at'), '2026-10-12T02:00:01.000Z'),
    (('request', 'created_at'), '2026-10-12 02:00:01Z'),
    (('request', 'created_at'), '2026-2-12T02:00:01Z'),
    (('source', 'backup_created_at'), '2026-02-30T00:00:00Z'),
    (('source', 'backup_created_at'), '2026-10-10T12:12:60Z'),
])
def test_time_boundaries_and_fresh_audit_are_explicit(contract, body, path, value):
    set_at(body, path, value)
    with pytest.raises(ValueError):
        contract.validate_body(body)


def test_preparation_future_backup_and_future_preparation_rejected(contract, body):
    preparation = body['preparation']
    preparation['restore']['backup_created_at'] = '2026-10-13T00:00:00Z'
    with pytest.raises(ValueError):
        contract.validate_preparation(preparation)
    preparation['restore']['backup_created_at'] = body['source']['backup_created_at']
    preparation['prepared_at'] = '2026-10-12T02:00:02Z'
    rebind(body, contract)
    with pytest.raises(ValueError):
        contract.validate_body(body)


def test_no_unrequested_five_minute_gate_but_expiry_caps_window(contract, body):
    body['issued_at'] = '2026-10-12T02:06:00Z'
    body['install_before'] = '2026-10-12T10:06:00Z'
    contract.validate_body(body)
    body['request']['expires_at'] = '2026-10-12T10:05:59Z'
    with pytest.raises(ValueError):
        contract.validate_body(body)


@pytest.mark.parametrize('field', ['backup_sha256', 'backup_created_at', 'credentials_sha256',
    'restore_package_sha256', 'restore_receipt_sha256', 'preparation_sha256'])
def test_request_must_bind_exact_retained_provenance(contract, body, field):
    body['request']['retained_snapshot'][field] = '2026-10-09T00:00:00Z' if field == 'backup_created_at' else 'f' * 64
    with pytest.raises(ValueError):
        contract.validate_body(body)


@pytest.mark.parametrize('path', [('target', 'installation_id'), ('package_sha256',),
    ('restore', 'context_sha256'), ('restore', 'native_binding_sha256'),
    ('preview_delta', 'operation_logs', 'columns_sha256')])
def test_preparation_digest_binds_all_declared_evidence(contract, body, path):
    set_at(body['preparation'], path, '3' * (32 if path[-1] == 'installation_id' else 64))
    with pytest.raises(ValueError):
        contract.validate_body(body)


@pytest.mark.parametrize('path,value', [
    (('operation_logs', 'baseline_count'), True),
    (('operation_logs', 'target_count'), 7),
    (('operation_logs', 'target_count'), 2**63),
    (('operation_logs', 'appended_count'), -1),
    (('operation_logs', 'appended_count'), 2.0),
    (('operation_logs', 'first_appended_id'), None),
    (('operation_logs', 'first_appended_id'), 0),
    (('operation_logs', 'first_appended_id'), 7),
    (('operation_logs', 'last_appended_id'), 5),
    (('operation_logs', 'last_appended_id'), True),
    (('operation_logs', 'last_appended_id'), 2**63),
    (('sqlite_sequence', 'table_present'), 1),
    (('sqlite_sequence', 'table_present'), False),
    (('sqlite_sequence', 'before', 'present'), 1),
    (('sqlite_sequence', 'before', 'value'), True),
    (('sqlite_sequence', 'before', 'value'), 2**63),
    (('sqlite_sequence', 'after', 'value'), 7),
    (('sqlite_sequence', 'after'), {'present': False, 'value': None}),
    (('sqlite_sequence', 'before'), {'present': False, 'value': 0}),
    (('environment_chain', 0, 'kind'), 'lan'),
    (('environment_chain', 1, 'kind'), 'home'),
    (('environment_chain', 1, 'before_sha256'), 'f' * 64),
    (('environment_chain', 0, 'changed_fields_count'), 16),
    (('environment_chain', 1, 'changed_fields_count'), 5),
    (('environment_chain', 1, 'changed_fields_count'), True),
    (('environment_chain', 1, 'changed_fields_count'), 0),
])
def test_public_delta_shapes_and_relations(contract, body, path, value):
    preparation = body['preparation']
    set_at(preparation['preview_delta'], path, value)
    with pytest.raises(ValueError):
        contract.validate_preparation(preparation)


def zero_preview(preparation, contract):
    delta = preparation['preview_delta']
    logs = delta['operation_logs']
    logs.update(target_count=logs['baseline_count'], appended_count=0,
        target_sha256=logs['baseline_sha256'], appended_sha256=contract.digest([]),
        first_appended_id=None, last_appended_id=None)
    sequence = delta['sqlite_sequence']
    sequence.update(target_sha256=sequence['baseline_sha256'], after=deepcopy(sequence['before']))
    preparation['target_snapshot']['facts_sha256'] = preparation['baseline_snapshot']['facts_sha256']


def test_zero_login_delta_retains_exact_facts_and_sequence(contract, body):
    preparation = body['preparation']
    zero_preview(preparation, contract)
    contract.validate_preparation(preparation)
    for path, value in [(('operation_logs', 'first_appended_id'), 0),
                        (('operation_logs', 'appended_sha256'), 'f' * 64),
                        (('operation_logs', 'target_sha256'), 'f' * 64),
                        (('sqlite_sequence', 'after', 'value'), 5),
                        (('sqlite_sequence', 'target_sha256'), 'f' * 64)]:
        changed = deepcopy(preparation)
        set_at(changed['preview_delta'], path, value)
        with pytest.raises(ValueError):
            contract.validate_preparation(changed)
    preparation['target_snapshot']['facts_sha256'] = 'f' * 64
    with pytest.raises(ValueError):
        contract.validate_preparation(preparation)


@pytest.mark.parametrize('table_present', [False, True])
def test_non_autoincrement_log_table_can_have_absent_sequence(contract, body, table_present):
    sequence = body['preparation']['preview_delta']['sqlite_sequence']
    sequence.update(table_present=table_present, before={'present': False, 'value': None},
        after={'present': False, 'value': None}, target_sha256=sequence['baseline_sha256'])
    contract.validate_preparation(body['preparation'])


def test_first_autoincrement_login_may_create_operation_sequence(contract, body):
    sequence = body['preparation']['preview_delta']['sqlite_sequence']
    sequence['before'] = {'present': False, 'value': None}
    contract.validate_preparation(body['preparation'])


def test_zero_environment_changes_require_identical_hashes(contract, body):
    preparation = body['preparation']
    chain = preparation['preview_delta']['environment_chain']
    original = chain[0]['before_sha256']
    for step in chain:
        step.update(before_sha256=original, after_sha256=original,
                    changed_fields_count=0, changes_sha256=contract.digest([]))
    preparation['target_snapshot']['preserved_environment_sha256'] = preparation['baseline_snapshot']['preserved_environment_sha256']
    contract.validate_preparation(preparation)
    preparation['target_snapshot']['preserved_environment_sha256'] = 'f' * 64
    with pytest.raises(ValueError):
        contract.validate_preparation(preparation)


def test_missing_process_record_can_still_declare_checked_stop(contract, body):
    body['source']['stop_proof']['process_record_sha256'] = None
    contract.validate_body(body)


@pytest.mark.parametrize('path,value', [
    (('target_snapshot', 'database_sha256'), 'f' * 64),
    (('target_snapshot', 'facts_sha256'), 'f' * 64),
    (('target_snapshot', 'preserved_environment_sha256'), 'f' * 64),
    (('context_sha256',), 'f' * 64),
    (('authorization_sha256',), 'f' * 64),
    (('operator_attestation', 'factory_cutover_authorized'), 1),
    (('operator_attestation', 'source_isolation_confirmed'), False),
    (('activated_at',), '2026-10-12T02:00:03Z'),
    (('activated_at',), '2026-10-12T10:00:04Z'),
    (('network', 'ERP_ALLOWED_ORIGINS'), 'http://8.8.8.8:8000'),
    (('network', 'ERP_HOME_REHEARSAL'), '1'),
    (('type',), 'tianming.mac-factory-activation.v1'),
])
def test_record_binds_exact_prepared_target_and_historical_window(contract, body, path, value):
    record = record_for(body, contract)
    set_at(record, path, value)
    with pytest.raises(ValueError):
        contract.validate_record(record)


@pytest.mark.parametrize('signature', ['', 'a', base64.b64encode(b'x' * 63).decode(),
    base64.b64encode(b'x' * 65).decode(), base64.b64encode(b'x' * 64).decode() + '\n', True,
    base64.b64encode(b'x' * 64).decode()[:-3] + 'B=='])
def test_signature_is_strict_canonical_shape_but_not_verified(contract, body, signature):
    raw = contract.canonical({'body': body, 'signature': signature})
    with pytest.raises(ValueError):
        contract.authorization_body(raw, body['request'])


def test_first_install_checks_clock_but_existing_record_does_not_expire(contract, body):
    start = datetime(2026, 10, 12, 2, 0, 4, tzinfo=timezone.utc)
    end = start + timedelta(hours=8)
    contract.require_install_window(body, now=start)
    contract.require_install_window(body, now=end - timedelta(microseconds=1))
    for invalid in (start - timedelta(microseconds=1), end, end + timedelta(days=365),
                    start.replace(tzinfo=None), None):
        with pytest.raises(ValueError):
            contract.require_install_window(body, now=invalid)
    assert contract.validate_record(record_for(body, contract)) == body


def test_make_request_uses_explicit_time_and_id_and_detaches_inputs(contract, body):
    old = body['request']
    target = deepcopy(old['target']); retained = deepcopy(old['retained_snapshot'])
    now = datetime(2026, 10, 12, 10, 0, 1, 999999, tzinfo=timezone(timedelta(hours=8)))
    request = contract.make_request(target, old['package_sha256'], old['network_sha256'],
        old['publisher_sha256'], retained, request_id=old['request_id'], now=now,
        preparation=body['preparation'])
    assert request == old
    target['root'] = '/changed'; retained['credentials_sha256'] = 'f' * 64
    assert request == old
    with pytest.raises(TypeError):
        contract.make_request(old['target'], old['package_sha256'], old['network_sha256'],
            old['publisher_sha256'], old['retained_snapshot'])
    with pytest.raises(ValueError):
        contract.make_request(old['target'], old['package_sha256'], old['network_sha256'],
            old['publisher_sha256'], old['retained_snapshot'], request_id=old['request_id'],
            now=datetime.max.replace(tzinfo=timezone.utc))


def test_v1_does_not_silently_gain_retained_snapshot_exceptions(contract, body):
    from desktop_assistant import factory_target_contract as v1_request
    from desktop_assistant import factory_activation_contract as v1_activation
    with pytest.raises(ValueError):
        v1_request.validate_request(body['request'])
    with pytest.raises(ValueError):
        v1_activation._body(body)
    request = {k: v for k, v in body['request'].items() if k != 'retained_snapshot'}
    request['type'] = v1_request.REQUEST
    source = {k: body['source'][k] for k in ('source_sha256', 'backup_sha256',
        'source_package_sha256', 'backup_created_at', 'credential_purposes', 'snapshot')}
    source.update(manual_stop=True, process_stopped=True)
    with pytest.raises(ValueError):
        v1_activation._body({'type': v1_activation.AUTHORIZATION, 'request': request, 'source': source,
            'issued_at': body['issued_at'], 'install_before': body['install_before']})


def test_public_preparation_cannot_include_cyclic_request_or_private_data(contract, body):
    for field in ('request_sha256', 'authorization_sha256', 'private_manifest', 'appended_rows'):
        changed = deepcopy(body['preparation']); changed[field] = 'f' * 64
        with pytest.raises(ValueError):
            contract.validate_preparation(changed)
    step = body['preparation']['preview_delta']['environment_chain'][0]
    step['changes'] = [{'key': 'ERP_ALLOWED_ORIGINS', 'value': 'private'}]
    with pytest.raises(ValueError):
        contract.validate_preparation(body['preparation'])


def test_preview_delta_public_entry_and_explicit_nesting_limit(contract, body):
    summary = body['preparation']['preview_delta']
    assert contract.validate_preview_delta(summary) is summary
    allowed = b'{"x":' + b'[' * (contract.MAX_DEPTH - 1) + b'0' + b']' * (contract.MAX_DEPTH - 1) + b'}'
    assert contract.decode(allowed)
    with pytest.raises(ValueError):
        contract.decode(b'{"x":' + b'[' * contract.MAX_DEPTH + b'0' + b']' * contract.MAX_DEPTH + b'}')


@pytest.mark.parametrize('field', ['backup_sha256', 'backup_created_at', 'credentials_sha256'])
def test_source_must_reauthenticate_the_same_original_material(contract, body, field):
    body['source'][field] = '2026-10-09T00:00:00Z' if field == 'backup_created_at' else 'f' * 64
    with pytest.raises(ValueError):
        contract.validate_body(body)


def test_source_same_current_facts_cannot_substitute_another_baseline(contract, body):
    source = body['source']
    source['snapshot']['facts_sha256'] = source['current_snapshot']['facts_sha256'] = 'f' * 64
    contract.validate_source(source)
    with pytest.raises(ValueError):
        contract.validate_body(body)


def test_valid_source_audit_can_have_equal_request_audit_and_issue_times(contract, body):
    at = body['request']['created_at']
    body['source'].update(audit_started_at=at, verified_at=at)
    body.update(issued_at=at, install_before='2026-10-12T10:00:01Z')
    contract.validate_body(body)


def test_request_can_bind_shorter_validity_and_maximum_public_integer(contract, body):
    request = deepcopy(body['request'])
    request['expires_at'] = '2026-10-12T02:00:02Z'
    contract.validate_request(request, preparation=body['preparation'])
    preparation = deepcopy(body['preparation'])
    zero_preview(preparation, contract)
    for snapshot in (preparation['baseline_snapshot'], preparation['target_snapshot']):
        snapshot['registered_attachments'] = contract.MAX_INTEGER
    logs = preparation['preview_delta']['operation_logs']
    logs['baseline_count'] = logs['target_count'] = contract.MAX_INTEGER
    contract.validate_preparation(preparation)
    logs.update(appended_count=1, first_appended_id=contract.MAX_INTEGER,
                last_appended_id=contract.MAX_INTEGER)
    with pytest.raises(ValueError):
        contract.validate_preparation(preparation)


@pytest.mark.parametrize('change', ['missing', 'extra'])
def test_envelope_and_record_exact_shapes(contract, body, change):
    envelope = {'body': body, 'signature': base64.b64encode(b'x' * 64).decode()}
    record = record_for(body, contract)
    for value, validate in ((envelope, lambda v: contract.authorization_body(contract.canonical(v), body['request'])),
                            (record, contract.validate_record)):
        if change == 'missing':
            value.pop(next(iter(value)))
        else:
            value['unsupported'] = True
        with pytest.raises(ValueError):
            validate(value)


def test_authorization_preserves_original_encoding_and_exact_request(contract, body):
    record = record_for(body, contract)
    record['authorization'] = json.dumps(json.loads(record['authorization']), indent=2)
    with pytest.raises(ValueError):
        contract.validate_record(record)
    record['authorization_sha256'] = hashlib.sha256(record['authorization'].encode()).hexdigest()
    assert contract.validate_record(record) == body
    another = deepcopy(body['request']); another['request_id'] = '3' * 32
    with pytest.raises(ValueError):
        contract.authorization_body(record['authorization'].encode(), another)


def test_contract_operations_never_perform_io_randomness_or_read_the_clock(contract, body, monkeypatch):
    record = record_for(body, contract)
    request = body['request']
    now = datetime(2026, 10, 12, 2, 0, 4, tzinfo=timezone.utc)

    def forbidden(*args, **kwargs):
        raise AssertionError('public contract attempted an external side effect')

    with monkeypatch.context() as patch:
        patch.setattr('builtins.open', forbidden)
        for module, name in ((os, 'open'), (os, 'urandom'), (subprocess, 'run'),
                             (socket, 'socket'), (time, 'time'), (time, 'time_ns')):
            patch.setattr(module, name, forbidden)
        contract.validate_body(body)
        contract.validate_record(record)
        contract.require_install_window(body, now=now)
        contract.make_request(request['target'], request['package_sha256'], request['network_sha256'],
            request['publisher_sha256'], request['retained_snapshot'], request_id=request['request_id'],
            now=now, preparation=body['preparation'])
