"""Preserve replenishment material coverage across program updates/rollback."""

CAPABILITY = 'replenishment_production_v1'
SOURCE_REVISION = 'fa1023oc'
REVISION = 'fb1024rs'
TABLE = 'stock_replenishment_production_jobs'
COLUMN = 'stock_replenishment_item_id'


def has_schema(db):
    return bool(db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
    ).fetchone()) or any(row[1] == COLUMN for row in db.execute(
        'PRAGMA table_info(scrap_production_jobs)'))


def has_facts(db):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if TABLE in tables and db.execute('SELECT 1 FROM ' + TABLE + ' LIMIT 1').fetchone():
        return True
    if any(row[1] == COLUMN for row in db.execute('PRAGMA table_info(scrap_production_jobs)')):
        return bool(db.execute('SELECT 1 FROM scrap_production_jobs WHERE ' + COLUMN +
                               ' IS NOT NULL LIMIT 1').fetchone())
    return False


def require_reader(database, manifest):
    from contextlib import closing
    import sqlite3
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        required = has_schema(db)
    value = (manifest.get('reader_capabilities') or {}).get(CAPABILITY)
    if required and not (type(value) is int and value == 1):
        raise ValueError('数据库已有独立补库生产结构，目标程序不能正确计算用料和采购缺口，禁止启动或回退')
    return required
