"""Read-only reader boundary for the finite 2026-10-14 physical kit facts.

Presence alone requires the signed reader. Malformed or orphaned facts must not
make a database appear safe for a program that predates this cost provenance.
This module does not activate a feature, repair facts, or alter the schema.
"""
from contextlib import closing
from pathlib import Path
import sqlite3


CAPABILITY = 'physical_kit_correction_20261014_v1'
SOURCE = 'physical_kit_correction_20261014'
ERROR = '实物组套库存已保存新成本来源，目标程序缺少对应读取能力；请保留现有数据并使用兼容版本'


def has_facts(db):
    """Inspect actual persisted source markers, never cached release versions."""
    try:
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table, columns in (
            ('inventory_lots', ('source_ref_type', 'cost_snapshot_source')),
            ('finance_idempotency_records', ('action',)),
        ):
            if table not in tables:
                continue
            present = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
            if not set(columns) <= present:
                raise ValueError(ERROR)
            predicate = ' OR '.join(f'"{column}"=?' for column in columns)
            if db.execute(f'SELECT 1 FROM "{table}" WHERE {predicate} LIMIT 1',
                          (SOURCE,) * len(columns)).fetchone():
                return True
        return False
    except sqlite3.Error as exc:
        raise ValueError(ERROR) from exc


def has_records(database):
    database = Path(database)
    # A missing database is not a legacy database and must never be created by
    # this compatibility check. Installation paths validate absence separately.
    try:
        with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            db.execute('PRAGMA query_only=ON')
            return has_facts(db)
    except (OSError, sqlite3.Error) as exc:
        raise ValueError(ERROR) from exc


def require_reader(database, manifest):
    required = has_records(database)
    caps = manifest.get('reader_capabilities') if isinstance(manifest, dict) else None
    value = caps.get(CAPABILITY) if isinstance(caps, dict) else None
    if required and (type(value) is not int or value != 1):
        raise ValueError(ERROR)
    return required
