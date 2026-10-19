"""Narrow compatibility proof for the empty, additive actual-receipt table.

Existing delivery attribution facts must not prohibit this unrelated migration
when the signed previous program has exactly the same attribution reader and
writer. This exception expires as soon as the new receipt contract has facts.
"""
from contextlib import closing
import re
import sqlite3

from desktop_assistant.schema_contract import schema_contract_from_signed_release
from desktop_assistant.storage import sha


SOURCES = (
    'app/models/unordered_delivery_order_attribution.py',
    'app/services/order_business_status.py',
    'app/services/packaging_order_repair_20261016.py',
    'alembic/versions/er1016pa_unordered_delivery_order_attribution.py',
)


def unchanged_attribution_reader(manager, release, revision, authority=None):
    if revision != 'es1019ra':
        return False
    authority = authority or manager.state.get('schema_authority')
    if not all(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value)
               for value in (release, authority)):
        return False
    try:
        packages = [manager.root / 'packages' / (identity + '.zip')
                    for identity in (release, authority)]
        if any(not path.is_file() or sha(path) != identity
               for path, identity in zip(packages, (release, authority))):
            return False
        old, old_schema = schema_contract_from_signed_release(packages[0], manager.public_key)
        new, new_schema = schema_contract_from_signed_release(packages[1], manager.public_key)
        if (old['revision'] != 'er1016pa' or new['revision'] != revision
                or new.get('migration') != {
                    'policy': 'preserve_existing_facts_v1', 'from_revision': 'er1016pa',
                    'rollback_package_sha256': release}):
            return False
        if any(not old['files'].get(name) or old['files'][name] != new['files'].get(name)
               for name in SOURCES):
            return False
        before, after = old_schema['tables'], new_schema['tables']
        if (set(after) != set(before) | {'incoming_receipt_actual_specs'}
                or 'incoming_receipt_actual_specs' in before
                or 'unordered_delivery_order_attributions' not in before
                or any(after[name] != columns for name, columns in before.items())):
            return False
        database = manager.root / 'shared/data/carton_erp.sqlite3'
        with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            db.execute('PRAGMA query_only=ON')
            return (bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='incoming_receipt_actual_specs'").fetchone())
                    and db.execute('SELECT 1 FROM incoming_receipt_actual_specs LIMIT 1').fetchone() is None)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return False
