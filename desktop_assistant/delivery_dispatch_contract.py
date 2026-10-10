"""Persistent managed-reader boundary for atomic delivery commands.

The marker precedes the first writable command, including the zero-record
window. Database records are a missing-marker alarm, never an activation.
"""
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat


CAPABILITY = 'delivery_dispatch_v1'
ACTION = 'delivery_dispatch_command'
STATE_KEY = 'delivery_dispatch_activation'
MARKER = 'data/delivery-dispatch-contract.json'
ERROR = '发货保护契约未通过核对，请通过最新版ERP助手核对恢复；本次未执行发货'


def _plain_file(path):
    try:
        attributes = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(attributes.st_mode) or getattr(attributes, 'st_file_attributes', 0) & 0x400:
        raise ValueError(ERROR)
    if not stat.S_ISREG(attributes.st_mode):
        raise ValueError(ERROR)
    return True


def _read_json(path, limit=65536):
    if not _plain_file(path) or path.stat().st_size > limit:
        raise ValueError(ERROR)
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode('utf-8-sig'))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ValueError(ERROR) from exc
    if not isinstance(value, dict):
        raise ValueError(ERROR)
    return value, hashlib.sha256(raw).hexdigest()


def validate_contract(value):
    if (not isinstance(value, dict) or value.get('reader_capability') != CAPABILITY
            or type(value.get('version')) is not int or value['version'] != 1):
        raise ValueError(ERROR)
    for key in ('activated_package', 'pre_activation_package'):
        if not isinstance(value.get(key), str) or not re.fullmatch(r'[0-9a-f]{64}', value[key]):
            raise ValueError(ERROR)
    if not isinstance(value.get('pre_activation_backup'), str) or not value['pre_activation_backup']:
        raise ValueError(ERROR)
    try:
        when = datetime.fromisoformat(value['activated_at'])
        if when.utcoffset() is None:
            raise ValueError(ERROR)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(ERROR) from exc
    return value


def activation(shared):
    path = Path(shared) / MARKER
    if not _plain_file(path):
        return None
    contract, digest = _read_json(path)
    validate_contract(contract)
    return {'contract': contract, 'sha256': digest}


def state_index(value):
    return {'reader_capability': CAPABILITY, 'version': 1, 'contract_sha256': value['sha256']}


def has_records(database):
    database = Path(database)
    if not database.is_file():
        return False
    try:
        with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            db.execute('PRAGMA query_only=ON')
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='finance_idempotency_records'").fetchone():
                return False
            return bool(db.execute('SELECT 1 FROM finance_idempotency_records WHERE action=? LIMIT 1', (ACTION,)).fetchone())
    except sqlite3.Error as exc:
        raise ValueError(ERROR) from exc


def signed_capability(manifest):
    value = (manifest.get('reader_capabilities') or {}).get(CAPABILITY)
    return type(value) is int and value == 1


def inspect_activation(shared, state, *, require_active=False):
    """Read-only check. A damaged/missing partner never becomes legacy mode."""
    value = activation(shared)
    index = state.get(STATE_KEY)
    if value is None:
        if index is not None or require_active or has_records(Path(shared)/'data/carton_erp.sqlite3'):
            raise ValueError(ERROR)
        return None
    if index != state_index(value):
        raise ValueError(ERROR)
    return value


def validate_recovery(shared, metadata, signed_manifest):
    """Check archive facts before moving any restored data into service."""
    value = activation(shared)
    recorded = metadata.get(STATE_KEY)
    required = signed_capability(signed_manifest) or has_records(Path(shared)/'data/carton_erp.sqlite3')
    if value is None:
        if recorded is not None or required:
            raise ValueError(ERROR)
        return None
    if recorded != value or not signed_capability(signed_manifest):
        raise ValueError(ERROR)
    return value


def require_managed_dispatch_activation(database_path, control=None):
    """No configuration switch can exempt an actual managed shared database.

    Isolated non-managed SQLite test/development databases have no installation
    state. The API supplies its actual engine path, never client input.
    """
    control = control if control is not None else os.environ.get('TM_ERP_CONTROL')
    if not database_path or str(database_path) == ':memory:':
        if control:
            raise ValueError(ERROR)
        return
    database = Path(database_path).resolve()
    inferred = (database.name == 'carton_erp.sqlite3' and database.parent.name == 'data'
                and database.parent.parent.name == 'shared')
    if control:
        root = Path(control).resolve().parent
        if Path(control).resolve().name != 'control':
            raise ValueError(ERROR)
        shared = root / 'shared'
        if database != (shared/'data/carton_erp.sqlite3').resolve():
            raise ValueError(ERROR)
    elif inferred:
        shared = database.parent.parent
        root = shared.parent
    else:
        return
    state, _ = _read_json(root/'state.json', limit=16*1024*1024)
    inspect_activation(shared, state, require_active=True)
    current = state.get('current')
    if not isinstance(current, str) or not re.fullmatch(r'[0-9a-f]{64}', current):
        raise ValueError(ERROR)
    manifest, _ = _read_json(root/'releases'/current/'manifest.json', limit=16*1024*1024)
    if not signed_capability(manifest):
        raise ValueError(ERROR)
