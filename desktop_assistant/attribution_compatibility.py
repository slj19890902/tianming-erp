"""Narrow compatibility proof for the empty, additive actual-receipt table.

Existing delivery attribution facts must not prohibit this unrelated migration
when the signed previous program has exactly the same attribution reader and
writer. The receipt or finished-label exception expires as soon as its new table has facts.
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
    contracts = {'es1019ra': ('er1016pa', 'incoming_receipt_actual_specs'),
                 'et1019fl': ('es1019ra', 'finished_product_label_print_jobs')}
    if revision not in contracts:
        return False
    parent, added_table = contracts[revision]
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
        if (old['revision'] != parent or new['revision'] != revision
                or new.get('migration') != {
                    'policy': 'preserve_existing_facts_v1', 'from_revision': parent,
                    'rollback_package_sha256': release}):
            return False
        proof_sources = SOURCES + (('app/models/incoming_actual_spec.py',
            'app/services/incoming_actual_specs.py', 'app/services/incoming_receipts.py',
            'app/services/stock_preparation_processing.py',
            'alembic/versions/es1019ra_incoming_actual_specs.py') if revision == 'et1019fl' else ())
        if any(not old['files'].get(name) or old['files'][name] != new['files'].get(name)
               for name in proof_sources):
            return False
        before, after = old_schema['tables'], new_schema['tables']
        if (set(after) != set(before) | {added_table}
                or added_table in before
                or (revision == 'et1019fl' and 'incoming_receipt_actual_specs' not in before)
                or 'unordered_delivery_order_attributions' not in before
                or any(after[name] != columns for name, columns in before.items())):
            return False
        database = manager.root / 'shared/data/carton_erp.sqlite3'
        with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            db.execute('PRAGMA query_only=ON')
            return (bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (added_table,)).fetchone())
                    and db.execute('SELECT 1 FROM '+added_table+' LIMIT 1').fetchone() is None)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return False
