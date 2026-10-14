from contextlib import closing
import hashlib
import sqlite3

import pytest

from desktop_assistant import physical_kit_reader_contract as contract
from desktop_assistant.reader_contract import target_readers


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'carton_erp.sqlite3'
    with closing(sqlite3.connect(path)) as db:
        db.execute('CREATE TABLE inventory_lots(id INTEGER PRIMARY KEY, source_ref_type TEXT, cost_snapshot_source TEXT)')
        db.execute('CREATE TABLE finance_idempotency_records(id INTEGER PRIMARY KEY, action TEXT)')
        db.commit()
    return path


def add_fact(database, table, column, value=contract.SOURCE):
    with closing(sqlite3.connect(database)) as db:
        db.execute(f'INSERT INTO {table}({column}) VALUES (?)', (value,))
        db.commit()


@pytest.mark.parametrize('table,column', [
    ('inventory_lots', 'source_ref_type'),
    ('inventory_lots', 'cost_snapshot_source'),
    ('finance_idempotency_records', 'action'),
])
def test_any_actual_marker_rejects_old_reader_without_modifying_bytes(database, table, column):
    add_fact(database, table, column)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match='实物组套'):
        contract.require_reader(database, {})
    assert contract.require_reader(database, {'reader_capabilities': {contract.CAPABILITY: 1}})
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


@pytest.mark.parametrize('value', [True, False, '1', 1.0, 0, 2, None])
def test_reader_capability_is_exact_integer_one(database, value):
    add_fact(database, 'inventory_lots', 'cost_snapshot_source')
    with pytest.raises(ValueError):
        contract.require_reader(database, {'reader_capabilities': {contract.CAPABILITY: value}})


def test_no_marker_keeps_legacy_database_readable(database):
    add_fact(database, 'inventory_lots', 'cost_snapshot_source', 'production')
    assert contract.require_reader(database, {}) is False


def test_target_readers_uses_database_even_when_source_manifest_omits_marker(database):
    add_fact(database, 'finance_idempotency_records', 'action')
    with pytest.raises(ValueError, match='已保存事实'):
        target_readers(database, {}, {}, None, None, factory=False)
    target_readers(database, {}, {'reader_capabilities': {contract.CAPABILITY: 1}},
                   None, None, factory=False)


def test_existing_signed_capability_preserved_before_first_fact(database):
    source = {'reader_capabilities': {contract.CAPABILITY: 1}}
    with pytest.raises(ValueError):
        target_readers(database, source, {}, None, None, factory=False)


def test_missing_database_is_not_created(tmp_path):
    missing = tmp_path/'missing.sqlite3'
    with pytest.raises(ValueError):
        contract.has_records(missing)
    assert not missing.exists()


@pytest.mark.parametrize('kind', ['corrupt', 'missing-column'])
def test_unreadable_facts_fail_closed(tmp_path, kind):
    path = tmp_path/'bad.sqlite3'
    if kind == 'corrupt':
        path.write_bytes(b'not sqlite')
    else:
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE inventory_lots(id INTEGER PRIMARY KEY)')
    with pytest.raises(ValueError):
        contract.require_reader(path, {'reader_capabilities': {contract.CAPABILITY: 1}})
