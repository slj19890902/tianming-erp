"""Synthetic environment/payload fixtures; no installed identities or data."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3

import pytest

from desktop_assistant import uat_export_contract as c, uat_export_sanitize as s
from desktop_assistant import mac_uat_export as broker
from tests.desktop_assistant.test_controlled_uat_export import scene, do_capture, _local_broker
from tests.desktop_assistant.test_uat_export_sanitize import snapshot

SECRET = 'SYNTHETIC_PRIVATE_ENV_VALUE_7a6e298b'


def set_environment(scene, value):
    raw = c.canonical(value)
    (scene['root']/'shared/environment.json').write_bytes(raw)
    scene['source']['environment_sha256'] = c.digest(raw)
    return raw


def test_only_reviewed_paths_survive_and_effective_values_are_also_checked():
    value = {'ERP_SECRET_KEY': '  '+SECRET+'-${SHARED}  ', 'OPENAI_API_KEY': '',
        'ERP_SECRET_KEY_FILE': '${SHARED}/data/session_secret.key', 'ERP_PORT': '8000',
        'ERP_FILE_STORAGE_DIR': '${SHARED}/data/private_uploads'}
    raw = c.canonical(value)
    assert c.environment_paths(raw) == {'ERP_FILE_STORAGE_DIR': '${SHARED}/data/private_uploads'}
    variants = c.environment_credentials(raw)
    assert value['ERP_SECRET_KEY'] in variants and value['ERP_SECRET_KEY'].strip() in variants
    assert value['ERP_SECRET_KEY'].replace('${SHARED}', str(c.ROOT/'shared')).strip() in variants
    assert '' not in variants


@pytest.mark.parametrize('raw', [b'{"arbitrary":"value"}', b'{"ERP_UNKNOWN_TOKEN":"value"}',
    b'{"ERP_PORT":1}', b'[]', b'{"ERP_SECRET_KEY":"a","erp_secret_key":"b"}',
    b'{"ERP_PORT":"8000","ERP_PORT":"9000"}', b'{"ERP_SECRET_KEY":"\\ud800"}',
    b'{"erp_file_storage_dir":"somewhere"}', b'{' + b'x'*65536])
def test_unknown_or_ambiguous_environment_never_becomes_a_source(raw):
    with pytest.raises(ValueError) as error:
        c.environment_paths(raw)
    assert 'somewhere' not in str(error.value)


@pytest.mark.parametrize('reference', ['${SHARED}/data/private_uploads/credential.pdf',
    '${SHARED}/data/layout_runtime/secret.json', '/tmp/secret', '${SHARED}/data/../data/session_secret.key'])
def test_credential_file_cannot_be_read_through_the_original_or_map_channel(reference, monkeypatch):
    monkeypatch.setattr(os, 'open', lambda *a, **k: pytest.fail('pure parser must never open reference'))
    with pytest.raises(ValueError, match='非导出位置'):
        c.environment_paths(c.canonical({'ERP_SECRET_KEY_FILE': reference}))


def test_json_escaped_values_and_keys_are_checked_before_public_metadata():
    secret = SECRET+'雪'
    environment = c.canonical({'ERP_SECRET_KEY': secret})
    for payload in ({'map_relative': 'data/layout_runtime/'+secret+'.json'}, {secret: 'filename'}):
        raw = c.canonical(payload)
        assert secret.encode() not in raw
        with pytest.raises(ValueError, match='残留') as error:
            c.reject_environment_credentials(environment, raw)
        assert secret not in str(error.value)
    escaped = json.dumps(secret)
    duplicate = ('{"same":'+escaped+',"same":"overwritten"}').encode()
    with pytest.raises(ValueError): c.reject_environment_credentials(environment, duplicate)


def test_nonempty_short_whitespace_and_oversized_values_are_not_silently_ignored():
    assert c.environment_credentials(c.canonical({'ERP_SECRET_KEY': ' '})) == (' ',)
    with pytest.raises(ValueError):
        c.reject_environment_credentials(c.canonical({'ERP_SECRET_KEY': 'x'}), b'{"x":1}')
    with pytest.raises(ValueError):
        c.environment_credentials(c.canonical({'ERP_SECRET_KEY': ' '+('A'*33000)+' '}))


def test_old_policy_is_not_silently_upgraded(scene):
    plan = deepcopy(scene['plan']); plan['policy'] = 'tianming.uat-credentials.v1'
    with pytest.raises(ValueError, match='计划'):
        c.validate_plan(plan)
    with pytest.raises(ValueError, match='协议'):
        c.require_capability({'runtime_capabilities': {'controlled_uat_export_20261022_v1': 1},
                              'files': dict.fromkeys(c.FILES)})


def test_plan_cannot_publish_a_value_matching_a_recipient_field(scene, monkeypatch):
    set_environment(scene, {'ERP_SECRET_KEY': 'kimiyf'})
    plan = scene['plan']
    meta = {'context': scene['context'], **{k: plan[k] for k in
        ('context_sha256', 'publisher_sha256', 'registry_sha256')}}
    supervisor = {k: plan[k] for k in ('supervisor_package_sha256', 'control_sha256', 'plist_sha256')}
    monkeypatch.setattr(broker, '_administrator', lambda: None)
    monkeypatch.setattr(broker, 'metadata', lambda package: (meta, supervisor))
    monkeypatch.setattr(broker, 'worker', lambda *a: {'source': scene['source']})
    monkeypatch.setattr(broker.boot, 'verify_code', lambda: plan['code_manifest_sha256'])
    monkeypatch.setattr(broker.boot, '_write', lambda *a, **k: pytest.fail('plan cannot be published'))
    with pytest.raises(ValueError, match='残留'):
        broker.make_plan(plan['package_sha256'])


def test_known_inline_configuration_is_not_copied_and_source_is_unchanged(scene):
    environment = set_environment(scene, {'ERP_SECRET_KEY': SECRET})
    before = scene['db'].read_bytes()
    do_capture(scene)
    payload = c.staging(scene['plan']['attempt_id'])/'payload'
    assert scene['db'].read_bytes() == before
    assert (scene['root']/'shared/environment.json').read_bytes() == environment
    assert all(SECRET.encode() not in p.read_bytes() for p in payload.rglob('*') if p.is_file())
    receipt = json.loads((payload/'receipt.json').read_bytes())
    assert receipt['environment_credential_check'] == {'policy': c.ENVIRONMENT_POLICY,
        'environment_sha256': c.digest(environment), 'checked': True}
    assert not (payload/'environment.json').exists()


@pytest.mark.parametrize('where', ['database', 'escaped_map', 'original'])
def test_inline_value_in_business_payload_refuses_instead_of_editing_business(scene, where):
    secret = SECRET+'雪'
    set_environment(scene, {'ERP_SECRET_KEY': secret})
    if where == 'database':
        with sqlite3.connect(scene['db']) as db: db.execute('UPDATE orders SET notes=?', (secret,))
    elif where == 'escaped_map':
        (scene['root']/'shared'/scene['source']['map_relative']).write_bytes(c.canonical({'label': secret}))
    else:
        folder = scene['root']/'shared/data/pdf_training_samples'; folder.mkdir(mode=0o700)
        pdf = folder/'synthetic.pdf'; pdf.write_bytes(b'%PDF-'+secret.encode())
        with sqlite3.connect(scene['db']) as db:
            db.execute('INSERT INTO pdf_order_training_samples VALUES(1,?,?)', (str(pdf), c.digest(pdf.read_bytes())))
    before = scene['db'].read_bytes()
    with pytest.raises(ValueError): do_capture(scene)
    assert scene['db'].read_bytes() == before
    assert not (c.staging(scene['plan']['attempt_id'])/'payload/receipt.json').exists()
    assert not c.output(scene['plan']['attempt_id']).exists()


def test_environment_change_rejects_before_creating_staging(scene):
    (scene['root']/'shared/environment.json').write_bytes(c.canonical({'ERP_PORT': '9000'}))
    with pytest.raises(ValueError, match='配置身份变化'): do_capture(scene)
    assert not c.staging(scene['plan']['attempt_id']).exists()


def test_scan_detects_cross_block_raw_bytes_without_revealing_value(tmp_path):
    tmp_path.chmod(0o700); path = tmp_path/'original.bin'
    path.write_bytes(b'x'*(1024*1024-5)+SECRET.encode()+b'tail'); path.chmod(0o600)
    with pytest.raises(ValueError) as error: s.verify_credential_values_absent(path, (SECRET,))
    assert SECRET not in str(error.value)


def test_logical_sqlite_scan_detects_overflow_and_disabled_marker_is_not_exempt(tmp_path):
    source = snapshot(tmp_path)
    long_secret = 'BEGIN'+SECRET*300+'END'
    with sqlite3.connect(source) as db: db.execute('UPDATE orders SET notes=?', (long_secret,))
    clean = tmp_path/'clean.db'; s.sanitize(source, clean)
    assert long_secret.encode() not in clean.read_bytes()
    with pytest.raises(ValueError): s.verify_credential_values_absent(clean, (long_secret,), database=True)
    with pytest.raises(ValueError): s.verify_credential_values_absent(clean, (s.DISABLED_PASSWORD,), database=True)


@pytest.mark.parametrize('location', ['default_sql', 'table_name', 'view_sql'])
def test_logical_schema_scan_detects_overflow_values_and_names(tmp_path, location):
    source = snapshot(tmp_path)
    long_secret = 'BEGIN'+SECRET*300+'END'
    with sqlite3.connect(source) as db:
        if location == 'default_sql':
            db.execute("ALTER TABLE orders ADD COLUMN extra TEXT DEFAULT '"+long_secret+"'")
            db.execute('UPDATE orders SET extra=NULL')
        elif location == 'table_name':
            db.execute('CREATE TABLE "'+long_secret+'" (id INTEGER)')
        else:
            db.execute("CREATE VIEW extra_view AS SELECT '"+long_secret+"' AS label")
    clean = tmp_path/'clean.db'; s.sanitize(source, clean)
    before = clean.read_bytes()
    assert long_secret.encode() not in before
    with sqlite3.connect(clean) as db:
        assert any(long_secret in value for row in db.execute(
            'SELECT name,tbl_name,sql FROM sqlite_schema') for value in row if isinstance(value, str))
    with pytest.raises(ValueError) as error:
        s.verify_credential_values_absent(clean, (long_secret,), database=True)
    assert long_secret not in str(error.value)
    assert clean.read_bytes() == before


def test_root_independently_rejects_a_forged_worker_scan_before_any_chown(scene, monkeypatch):
    secret = SECRET+'雪'; set_environment(scene, {'ERP_SECRET_KEY': secret})
    capture = do_capture(scene); payload = c.staging(scene['plan']['attempt_id'])/'payload'
    map_file = payload/'layout.json'; map_file.write_bytes(c.canonical({'label': secret}))
    receipt = json.loads((payload/'receipt.json').read_bytes())
    receipt['files']['layout.json'] = {'sha256': c.digest(map_file.read_bytes()), 'bytes': map_file.stat().st_size}
    raw = c.canonical(receipt); (payload/'receipt.json').write_bytes(raw)
    capture = {**capture, 'receipt_sha256': c.digest(raw), 'bytes': sum(x['bytes'] for x in receipt['files'].values())}
    _local_broker(scene, monkeypatch)
    changed = []; monkeypatch.setattr(os, 'fchown', lambda *a: changed.append(a))
    with pytest.raises(ValueError): broker.deliver(scene['plan'], {'context': scene['context']}, capture)
    assert not changed and not c.output(scene['plan']['attempt_id']).exists()
    assert (c.BASE/('uat-export-delivery-'+scene['plan']['attempt_id'])/'payload').stat().st_mode & 0o077 == 0
