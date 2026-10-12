"""Disposable SQLite/files only. No restore, running instance, network or keys."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from desktop_assistant import factory_cutover as cutover
from desktop_assistant import retained_preview_audit as audit


AT = '2026-10-12T04:00:00Z'
OLD_AT = '2026-10-11T01:02:03.123456+00:00'
RECEIPT = 'a' * 64
LOG_COLUMNS = ['id', 'user_id', 'action', 'resource', 'details', 'ip_address', 'created_at',
    'username', 'role', 'entity_type', 'entity_id', 'description', 'user_agent', 'extra_json',
    'event_category', 'result', 'source', 'module_code', 'action_code', 'actor_user_id_snapshot',
    'operator_name_snapshot', 'object_ref', 'customer_id_snapshot', 'customer_name_snapshot',
    'request_id', 'batch_id', 'schema_version']


def raw(value):
    return json.dumps(value, ensure_ascii=True, indent=2).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def dbpath(shared):
    return shared / 'data/carton_erp.sqlite3'


def sql(shared, statement, parameters=()):
    with closing(sqlite3.connect(dbpath(shared))) as db, db:
        db.execute(statement, parameters)


def login(identity=2):
    result = dict.fromkeys(LOG_COLUMNS)
    result.update(id=identity, user_id=1, action='LOGIN', resource='User',
        details=json.dumps({'username': 'operator', 'role': 'admin'}, ensure_ascii=False, sort_keys=True),
        ip_address='192.168.3.10', created_at='2026-10-12 02:00:00', username='operator',
        role='admin', entity_type='user', entity_id=1, description='用户登录',
        user_agent='Synthetic browser', event_category='security', result='success', source='web',
        module_code='auth', action_code='LOGIN', actor_user_id_snapshot=1,
        operator_name_snapshot='Operator display', object_ref='operator',
        request_id='1234567812344abc8def123456789012', schema_version=1)
    return result


def insert_log(shared, value):
    sql(shared, 'INSERT INTO operation_logs (' + ','.join(LOG_COLUMNS) + ') VALUES (' +
        ','.join('?' for _ in LOG_COLUMNS) + ')', [value[k] for k in LOG_COLUMNS])


def home_receipt(before, after):
    return {'type': 'tianming.home-network-preparation.v1', 'at': OLD_AT,
        'checkpoint_files': 4, 'checkpoint_bytes': 100,
        'environment_before_sha256': sha(before), 'environment_after_sha256': sha(after),
        'changed_keys': sorted(k for k in json.loads(after) if json.loads(before).get(k) != json.loads(after)[k]),
        'state_sha256_preserved': 'c' * 64, 'restore_receipt_sha256_preserved': RECEIPT,
        'program_bytes_changed': False, 'business_database_written': False,
        'credentials_changed': False, 'server_started': False, 'factory_activated': False}


def lan_receipt(before, after):
    return {'type': 'tianming.factory-preview-lan.v1', 'at': '2026-10-12T01:00:00+00:00',
        'address': '192.168.3.102', 'port': 8000, 'source_subnet': '192.168.3.0/24',
        'upstream': '127.0.0.1:8000', 'relay_uid': 'nobody',
        'environment_before_sha256': sha(before), 'environment_after_sha256': sha(after),
        'changed_keys': sorted(['ERP_TRUSTED_HOSTS', 'ERP_ALLOWED_ORIGINS',
                              'ERP_SHELF_LABEL_ORIGIN', 'ERP_MOBILE_QR_ORIGIN']),
        'all_other_environment_values_preserved': True,
        'relay_before_sha256': '1'*64, 'relay_after_sha256': '2'*64,
        'plist_before_sha256': '3'*64, 'plist_after_sha256': '4'*64,
        'restore_receipt_sha256': RECEIPT, 'runtime_package': '5'*64,
        'supervisor_package': '6'*64, 'home_rehearsal': True, 'signed_manager_running': True,
        'factory_activated': False, 'data_restore_called': False, 'credentials_changed': False,
        'old_windows_touched': False, 'address_192_168_3_80_assigned': False,
        'business_database_directly_written': False, 'other_computer_login_verified': False}


@pytest.fixture
def case(tmp_path):
    source = tmp_path / 'baseline'; (source / 'data').mkdir(parents=True)
    (source / 'data/attachment.bin').write_bytes(b'complete synthetic attachment')
    env = {'ERP_ENVIRONMENT': 'production', 'ERP_SECRET_KEY_FILE': '${SHARED}/session.key',
           'ERP_PORT': '8000'}
    (source / 'environment.json').write_bytes(raw(env))
    with closing(sqlite3.connect(dbpath(source))) as db, db:
        db.executescript('''
            CREATE TABLE alembic_version(version_num TEXT PRIMARY KEY);
            INSERT INTO alembic_version VALUES('synthetic_head');
            CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, role TEXT,
                display_name TEXT, real_name TEXT, auth_version INTEGER, password_hash TEXT, is_active INTEGER);
            INSERT INTO users VALUES(1,'operator','admin','Operator display','Real name',1,'hash',1);
            CREATE TABLE business(id INTEGER PRIMARY KEY, quantity, note BLOB);
            INSERT INTO business VALUES(1,10,x'00ff');
            CREATE TABLE customers(id INTEGER PRIMARY KEY);
            CREATE TABLE products(id INTEGER PRIMARY KEY);
            CREATE TABLE sales_orders(id INTEGER PRIMARY KEY);
            CREATE TABLE email_intake_settings(id INTEGER PRIMARY KEY, encrypted_secret TEXT, version INTEGER);
            INSERT INTO email_intake_settings VALUES(1,'source cipher',4);
            INSERT INTO email_intake_settings VALUES(2,'preserved cipher',7);
        ''')
        integers = {'user_id', 'entity_id', 'actor_user_id_snapshot', 'customer_id_snapshot', 'schema_version'}
        fields = ['id INTEGER PRIMARY KEY AUTOINCREMENT'] + [k + (' INTEGER' if k in integers else ' TEXT') for k in LOG_COLUMNS[1:]]
        db.execute('CREATE TABLE operation_logs (' + ','.join(fields) + ')')
    old = login(1); old.update(action='LEGACY', action_code=None, schema_version=0, details='old exact bytes', request_id='c'*32)
    insert_log(source, old)
    target = tmp_path / 'target'; shutil.copytree(source, target)
    sql(target, "UPDATE email_intake_settings SET encrypted_secret='destination reference' WHERE id=1")
    home = dict(env)
    home.update(ERP_HOME_REHEARSAL='1', ERP_PRODUCTION_TRANSPORT='lan_http', ERP_BIND_HOST='127.0.0.1',
        ERP_ALLOWED_ORIGINS='http://127.0.0.1:8000,http://192.168.1.243:8000,http://192.168.163.157:8000',
        ERP_TRUSTED_HOSTS='127.0.0.1,192.168.1.243,192.168.163.157,localhost', ERP_TRUSTED_PROXY_IPS='',
        ERP_SESSION_COOKIE_SECURE='false', ERP_LAN_HTTP_ORIGIN='', ERP_ADDITIONAL_PRIVATE_HTTP_ORIGINS='',
        ERP_BROWSER_URL='http://127.0.0.1:8000/', ERP_HEALTH_URL='http://127.0.0.1:8000/api/health',
        ERP_SHELF_LABEL_ORIGIN='http://192.168.1.243:8000')
    current = dict(home)
    current.update(ERP_TRUSTED_HOSTS='127.0.0.1,192.168.3.102,localhost',
        ERP_ALLOWED_ORIGINS='http://127.0.0.1:8000,http://192.168.3.102:8000',
        ERP_SHELF_LABEL_ORIGIN='http://192.168.3.102:8000', ERP_MOBILE_QR_ORIGIN='http://192.168.3.102:8000')
    (target / 'environment.json').write_bytes(raw(current)); insert_log(target, login())
    approvals = [{'kind': 'home', 'receipt_raw': raw(home_receipt(raw(env), raw(home))), 'script_sha256': 'd'*64},
                 {'kind': 'lan', 'receipt_raw': raw(lan_receipt(raw(home), raw(current))), 'script_sha256': 'e'*64}]
    arguments = dict(baseline_snapshot=cutover.capture_snapshot(source), target_snapshot=cutover.capture_snapshot(target),
        home_environment_raw=raw(home), approved_environment_chain=approvals,
        restore_receipt_sha256=RECEIPT, prepared_at=AT)
    return source, target, arguments


def call(case):
    return audit.compare_preview(case[0], case[1], **case[2])


def refresh(case):
    case[2]['baseline_snapshot'] = cutover.capture_snapshot(case[0])
    case[2]['target_snapshot'] = cutover.capture_snapshot(case[1])


def tree(path):
    return {str(p.relative_to(path)): (p.stat().st_mode, p.read_bytes()) for p in path.rglob('*') if p.is_file()}


def test_success_preserves_every_byte_and_retains_full_private_rows(case):
    before = tree(case[0].parent)
    result = call(case)
    assert tree(case[0].parent) == before
    summary = result['summary']; private = result['private_manifest']
    assert set(summary) == {'operation_logs', 'sqlite_sequence', 'environment_chain'}
    assert summary['operation_logs']['appended_count'] == 1
    assert summary['operation_logs']['appended_sha256'] == cutover.digest(private['appended_rows'])
    assert summary['sqlite_sequence']['before'] == {'present': True, 'value': 1}
    assert summary['sqlite_sequence']['after'] == {'present': True, 'value': 2}
    assert private['credential_comparison_required'] is True
    assert len(private['appended_rows'][0]) == len(LOG_COLUMNS)
    assert 'operator' not in json.dumps(summary) and '192.168.' not in json.dumps(summary)
    change = next(x for x in private['environment_chain'][1]['changes'] if x['key'] == 'ERP_MOBILE_QR_ORIGIN')
    assert change['before'] == {'present': False, 'value': None}
    assert change['after']['present'] is True


@pytest.mark.parametrize('statement', [
    "UPDATE operation_logs SET details='changed' WHERE id=1", 'DELETE FROM operation_logs WHERE id=1',
    "UPDATE users SET auth_version=2", "UPDATE users SET role='operator'", 'UPDATE business SET quantity=11',
    "UPDATE business SET quantity='10'", "UPDATE business SET note=x'ff00'",
    "UPDATE email_intake_settings SET encrypted_secret='changed' WHERE id=2",
    'UPDATE email_intake_settings SET version=5 WHERE id=1',
    "UPDATE sqlite_sequence SET seq=20 WHERE name='operation_logs'",
    "UPDATE sqlite_sequence SET seq=20 WHERE name='users'",
    "DELETE FROM sqlite_sequence WHERE name='operation_logs'",
    "INSERT INTO sqlite_sequence VALUES('unknown',1)",
    'ALTER TABLE operation_logs ADD COLUMN unknown TEXT',
])
def test_no_ordinary_fact_old_log_or_unexplained_sequence_difference(case, statement):
    sql(case[1], statement); refresh(case); before = tree(case[0].parent)
    with pytest.raises(ValueError): call(case)
    assert tree(case[0].parent) == before


@pytest.mark.parametrize('field,value', [
    ('action', 'LOGOUT'), ('action_code', 'LOGIN_FAILED'), ('action_code', 'LOGIN_THROTTLED'),
    ('schema_version', 0), ('schema_version', '1x'), ('result', 'failed'), ('source', 'api'),
    ('user_id', 2), ('entity_id', 2), ('actor_user_id_snapshot', 2), ('username', 'other'),
    ('operator_name_snapshot', 'operator'), ('object_ref', 'other'), ('role', 'operator'),
    ('details', '{"role":"admin","username":"operator","business":1}'),
    ('details', '{"role":"admin","username":"operator","username":"operator"}'),
    ('extra_json', '{}'), ('customer_id_snapshot', 1), ('batch_id', 'x'),
    ('created_at', '2026-10-13 00:00:00'), ('created_at', 'invalid'),
    ('request_id', 'not-a-server-id'), ('ip_address', ' x '), ('user_agent', ''),
])
def test_only_exact_success_login_shape(case, field, value):
    sql(case[1], 'UPDATE operation_logs SET ' + field + '=? WHERE id=2', (value,)); refresh(case)
    with pytest.raises(ValueError): call(case)


@pytest.mark.parametrize('suffix', ['-wal', '-shm', '-journal'])
def test_even_empty_journal_sidecars_refused(case, suffix):
    Path(str(dbpath(case[1])) + suffix).touch()
    with pytest.raises(ValueError): call(case)


@pytest.mark.parametrize('fault', ['attachment', 'snapshot', 'environment', 'extra-file', 'hardlink', 'symlink'])
def test_tree_and_snapshot_fail_closed(case, fault):
    if fault == 'attachment': (case[1] / 'data/attachment.bin').write_bytes(b'drift')
    elif fault == 'snapshot': case[2]['target_snapshot']['database_sha256'] = 'f'*64
    elif fault == 'environment': (case[1] / 'environment.json').write_bytes(b'{}')
    elif fault == 'extra-file': (case[1] / 'data/extra').write_bytes(b'new')
    elif fault == 'symlink': (case[1] / 'link').symlink_to(case[1] / 'data/attachment.bin')
    else:
        import os
        os.link(case[1] / 'data/attachment.bin', case[1] / 'link')
    with pytest.raises(ValueError): call(case)


@pytest.mark.parametrize('fault', ['missing', 'unknown', 'receipt-hash', 'receipt-time', 'receipt-offset',
    'receipt-extra', 'receipt-duplicate', 'receipt-bool', 'unapproved-key', 'home-mode', 'empty-vs-missing'])
def test_original_environment_evidence_requires_exact_known_format(case, fault):
    approvals = case[2]['approved_environment_chain']
    receipt = json.loads(approvals[0]['receipt_raw'])
    if fault == 'missing': approvals.pop()
    elif fault == 'unknown': approvals[0]['unknown'] = True
    elif fault == 'receipt-hash': receipt['environment_after_sha256'] = '0'*64
    elif fault == 'receipt-time': receipt['at'] = '2026-10-11'
    elif fault == 'receipt-offset': receipt['at'] = '2026-10-11T01:02:03+08:00'
    elif fault == 'receipt-extra': receipt['new'] = True
    elif fault == 'receipt-bool': receipt['credentials_changed'] = 0
    elif fault == 'receipt-duplicate':
        approvals[0]['receipt_raw'] = approvals[0]['receipt_raw'].replace(b'"at":', b'"at":"x","at":')
    else:
        home = json.loads(case[2]['home_environment_raw'])
        home[{'unapproved-key': 'NEW_KEY', 'home-mode': 'ERP_HOME_REHEARSAL',
              'empty-vs-missing': 'ERP_MOBILE_QR_ORIGIN'}[fault]] = ''
        case[2]['home_environment_raw'] = raw(home)
        receipt['environment_after_sha256'] = sha(raw(home))
    if fault != 'receipt-duplicate': approvals[0]['receipt_raw'] = raw(receipt)
    with pytest.raises(ValueError): call(case)


def test_no_appended_rows_is_explicit_not_a_table_skip(case):
    sql(case[1], 'DELETE FROM operation_logs WHERE id=2')
    sql(case[1], "UPDATE sqlite_sequence SET seq=1 WHERE name='operation_logs'"); refresh(case)
    result = call(case)['summary']['operation_logs']
    assert result['appended_count'] == 0 and result['first_appended_id'] is None
    assert result['appended_sha256'] == cutover.digest([])


def test_mutation_during_read_is_refused_without_repair(case, monkeypatch):
    original = audit._compare_databases
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        (case[1] / 'data/attachment.bin').write_bytes(b'concurrent change')
        return result
    monkeypatch.setattr(audit, '_compare_databases', changed)
    with pytest.raises(ValueError): call(case)
    assert (case[1] / 'data/attachment.bin').read_bytes() == b'concurrent change'


@pytest.mark.parametrize('request_value', ['b'*32, '1234567812341abc8def123456789012', '1234567812344abc0def123456789012'])
def test_login_request_requires_server_uuid4(case, request_value):
    sql(case[1], 'UPDATE operation_logs SET request_id=? WHERE id=2', (request_value,)); refresh(case)
    with pytest.raises(ValueError, match='请求'): call(case)


def rebuild_without_auto(shared, all_tables=False):
    with closing(sqlite3.connect(dbpath(shared))) as db:
        lines = list(db.iterdump())
    rewritten = []
    for line in lines:
        if 'sqlite_sequence' in line:
            if all_tables or "'operation_logs'" in line: continue
        if line.startswith('CREATE TABLE') and (all_tables or 'operation_logs' in line):
            line = line.replace(' AUTOINCREMENT', '')
        rewritten.append(line)
    replacement = dbpath(shared).with_suffix('.replacement')
    with closing(sqlite3.connect(replacement)) as db, db:
        db.executescript('\n'.join(rewritten))
    replacement.replace(dbpath(shared))


@pytest.mark.parametrize('all_tables', [False, True])
def test_real_non_autoincrement_schema_does_not_invent_sequence(case, all_tables):
    for shared in case[:2]: rebuild_without_auto(shared, all_tables)
    refresh(case); before = tree(case[0].parent)
    seq = call(case)['summary']['sqlite_sequence']
    assert seq['table_present'] is (not all_tables)
    assert seq['before'] == seq['after'] == {'present': False, 'value': None}
    assert tree(case[0].parent) == before


def test_multiple_success_logins_keep_all_typed_rows(case):
    new = login(3); new['request_id'] = '2345678912344abc8def123456789012'; insert_log(case[1], new)
    refresh(case)
    result = call(case)
    assert result['summary']['operation_logs']['appended_count'] == 2
    assert [row[0] for row in result['private_manifest']['appended_rows']] == [['integer', '2'], ['integer', '3']]


@pytest.mark.parametrize('fault', ['reuse-request', 'gap', 'inactive', 'legacy-missing', 'unknown-both', 'lower-time'])
def test_additional_fail_closed_boundaries(case, fault):
    if fault == 'reuse-request': sql(case[1], "UPDATE operation_logs SET request_id=? WHERE id=2", ('c'*32,))
    elif fault == 'gap':
        sql(case[1], 'UPDATE operation_logs SET id=4 WHERE id=2')
        sql(case[1], "UPDATE sqlite_sequence SET seq=4 WHERE name='operation_logs'")
    elif fault == 'inactive':
        for shared in case[:2]: sql(shared, 'UPDATE users SET is_active=0')
    elif fault == 'legacy-missing':
        for shared in case[:2]: sql(shared, 'ALTER TABLE users DROP COLUMN display_name')
    elif fault == 'unknown-both':
        for shared in case[:2]: sql(shared, 'ALTER TABLE operation_logs ADD COLUMN mystery TEXT')
    else: sql(case[1], "UPDATE operation_logs SET created_at='2026-10-10 00:00:00' WHERE id=2")
    refresh(case)
    with pytest.raises(ValueError): call(case)


def test_nullable_request_metadata_and_original_operator_fallback(case):
    for shared in case[:2]: sql(shared, "UPDATE users SET display_name='   '")
    sql(case[1], "UPDATE operation_logs SET operator_name_snapshot='operator',ip_address=NULL,user_agent=NULL WHERE id=2")
    refresh(case)
    assert call(case)['summary']['operation_logs']['appended_count'] == 1


def test_sqlite_time_resolution_does_not_invent_subsecond_event_time(case):
    sql(case[1], "UPDATE operation_logs SET created_at='2026-10-11 01:02:03' WHERE id=2")
    refresh(case)
    assert call(case)['summary']['operation_logs']['appended_count'] == 1


def test_every_database_connection_is_read_only(case, monkeypatch):
    connect = sqlite3.connect; calls = []
    def guarded(path, *args, **kwargs):
        assert isinstance(path, str) and path.endswith('?mode=ro') and kwargs.get('uri') is True
        calls.append(path)
        db = connect(path, *args, **kwargs)
        def authorize(action, *_):
            assert action not in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_ATTACH}
            return sqlite3.SQLITE_OK
        db.set_authorizer(authorize)
        return db
    monkeypatch.setattr(sqlite3, 'connect', guarded)
    assert call(case)['summary']['operation_logs']['appended_count'] == 1 and calls


@pytest.mark.parametrize('fault', ['database', 'environment', 'new-file', 'replace-same-bytes'])
def test_mid_audit_change_preserved_and_rejected(case, monkeypatch, fault):
    original = audit._compare_databases
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        if fault == 'database': sql(case[1], 'UPDATE business SET quantity=100')
        elif fault == 'environment': (case[1] / 'environment.json').write_bytes(b'{}')
        elif fault == 'new-file': (case[1] / 'new').write_bytes(b'new')
        else:
            path = case[1] / 'data/attachment.bin'; replacement = path.with_suffix('.replace')
            replacement.write_bytes(path.read_bytes()); replacement.replace(path)
        return result
    monkeypatch.setattr(audit, '_compare_databases', changed)
    with pytest.raises(ValueError): call(case)


def test_summary_is_accepted_by_independent_v2_shape(case):
    from desktop_assistant import retained_snapshot_contract as contract
    result = call(case)
    contract.validate_preview_delta(result['summary'])


def test_expected_snapshot_rejects_bool_count_alias(case):
    case[2]['baseline_snapshot']['registered_attachments'] = False
    with pytest.raises(ValueError): call(case)


def test_pdf_complete_relative_path_is_preserved(case):
    for shared in case[:2]:
        path = shared / 'data/source.pdf'; path.write_bytes(b'pdf source')
        sql(shared, 'CREATE TABLE pdf_order_training_samples(id INTEGER PRIMARY KEY,file_path TEXT,file_sha256 TEXT)')
        sql(shared, 'INSERT INTO pdf_order_training_samples VALUES(1,?,?)', (str(path), sha(path.read_bytes())))
    refresh(case)
    assert call(case)['summary']['operation_logs']['appended_count'] == 1
    # Same basename/content in a different location is still a different fact.
    for shared in case[:2]:
        (shared / 'other').mkdir(); (shared / 'other/source.pdf').write_bytes(b'pdf source')
    sql(case[1], 'UPDATE pdf_order_training_samples SET file_path=?', (str(case[1] / 'other/source.pdf'),))
    refresh(case)
    with pytest.raises(ValueError): call(case)


@pytest.mark.parametrize('operation', ['UPDATE business SET quantity=10.0',
    "UPDATE operation_logs SET details=x'6f6c64206578616374206279746573' WHERE id=1",
    'INSERT INTO business VALUES(2,10,NULL)'])
def test_every_value_type_and_duplicate_row_multiplicity_is_preserved(case, operation):
    sql(case[1], operation); refresh(case)
    with pytest.raises(ValueError): call(case)


@pytest.mark.parametrize('fault', ['restore-identity', 'script-shape', 'home-order', 'lan-unknown', 'lan-bool', 'lan-subnet'])
def test_approval_identity_and_original_lan_boundary(case, fault):
    approvals = case[2]['approved_environment_chain']
    if fault == 'restore-identity': case[2]['restore_receipt_sha256'] = '0'*64
    elif fault == 'script-shape': approvals[0]['script_sha256'] = None
    elif fault == 'home-order': approvals.reverse()
    else:
        receipt = json.loads(approvals[1]['receipt_raw'])
        if fault == 'lan-unknown': receipt['unrecognized'] = 1
        elif fault == 'lan-bool': receipt['home_rehearsal'] = 1
        else: receipt['source_subnet'] = '192.168.0.0/16'
        approvals[1]['receipt_raw'] = raw(receipt)
    with pytest.raises(ValueError): call(case)


def test_real_sqlite_preview_to_v2_original_key_signature_and_stale_preparation(case):
    from datetime import datetime, timedelta, timezone
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from desktop_assistant import retained_snapshot_contract as contract
    from desktop_assistant import retained_cutover as codec
    from desktop_assistant.signing import public_bytes
    from desktop_assistant.release_request import public_identity

    result = call(case)
    target_identity = {'host_sha256': '1'*64, 'installation_id': '2'*32, 'service_uid': 499,
        'service_gid': 499, 'service_account': '_tianmingerp', 'root': str(case[1])}
    restore = {'backup_sha256': '3'*64, 'backup_created_at': '2026-10-10T12:00:00Z',
        'credentials_sha256': '4'*64, 'restore_package_sha256': '5'*64,
        'restore_receipt_sha256': RECEIPT, 'context_sha256': '6'*64, 'native_binding_sha256': '7'*64}
    preparation = {'type': contract.PREPARATION, 'policy': 'preview-login-only.v1',
        'prepared_at': AT, 'target': target_identity, 'package_sha256': '8'*64, 'restore': restore,
        'baseline_snapshot': deepcopy(case[2]['baseline_snapshot']),
        'target_snapshot': deepcopy(case[2]['target_snapshot']), 'preview_delta': result['summary']}
    key = Ed25519PrivateKey.generate()  # Disposable identity, never a real publisher key.
    public = public_bytes(key)
    retained = {k: restore[k] for k in ('backup_sha256', 'backup_created_at', 'credentials_sha256',
                                        'restore_package_sha256', 'restore_receipt_sha256')}
    retained['preparation_sha256'] = contract.digest(preparation)
    now = datetime(2026, 10, 12, 4, 0, 1, tzinfo=timezone.utc)
    request_value = contract.make_request(target_identity, '8'*64, '9'*64,
        public_identity(public)[1], retained, request_id='b'*32, now=now, preparation=preparation)
    original = deepcopy(case[2]['baseline_snapshot'])
    source = {'type': contract.SOURCE, 'source_sha256': 'c'*64, 'backup_sha256': restore['backup_sha256'],
        'backup_created_at': restore['backup_created_at'], 'source_package_sha256': 'd'*64,
        'credentials_sha256': restore['credentials_sha256'], 'credential_purposes': ['backup', 'mailbox'],
        'snapshot': original, 'current_snapshot': deepcopy(original),
        'audit_started_at': '2026-10-12T04:00:02Z', 'verified_at': '2026-10-12T04:00:03Z',
        'stop_proof': {'type': contract.STOP_PROOF, 'state_sha256': 'e'*64,
            'database_sha256': original['database_sha256'], 'files_sha256': original['files_sha256'],
            'environment_sha256': sha((case[0] / 'environment.json').read_bytes()),
            'credential_config_sha256': 'f'*64, 'process_record_sha256': None,
            'backup_receipt_sha256': '0'*64, 'manual_stop': True, 'process_stopped': True,
            'maintenance_lock_held': True, 'database_read_lease_held': True, 'journals_absent': True}}
    issued = now + timedelta(seconds=3)
    signed = codec.sign_authorization(request_value, source, preparation, key, now=issued)
    body = codec.verify_authorization(signed, public, request_value)
    codec.require_install_window(body, now=issued)
    assert body['preparation']['baseline_snapshot'] == case[2]['baseline_snapshot']
    assert body['preparation']['target_snapshot'] == case[2]['target_snapshot']
    assert body['source']['backup_created_at'] == '2026-10-10T12:00:00Z'
    with pytest.raises(ValueError): codec.require_install_window(body, now=issued + timedelta(hours=8))

    # Raw typed row evidence cannot be rebound beneath the already signed request.
    changed_private = deepcopy(result['private_manifest'])
    changed_private['appended_rows'][0][LOG_COLUMNS.index('details')] = ['text', 'modified']
    changed_preparation = deepcopy(preparation)
    changed_preparation['preview_delta']['operation_logs']['appended_sha256'] = contract.digest(changed_private['appended_rows'])
    with pytest.raises(ValueError): contract.validate_request(request_value, preparation=changed_preparation)

    new = login(3); new['request_id'] = '2345678912344abc8def123456789012'; insert_log(case[1], new)
    with pytest.raises(ValueError, match='快照'): call(case)  # Original exact target binding is stale.
    refresh(case); fresh = call(case)
    changed_preparation = deepcopy(preparation)
    changed_preparation.update(target_snapshot=case[2]['target_snapshot'], preview_delta=fresh['summary'])
    with pytest.raises(ValueError): contract.validate_request(request_value, preparation=changed_preparation)
