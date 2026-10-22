"""Enforce customer-code identity only when the customer supplied a code.

No business row is changed. SQLite's original table definition is retained
verbatim apart from this one named unique constraint.
"""
import re

from alembic import op
import sqlalchemy as sa

revision = 'fa1023oc'
down_revision = 'ez1022hc'
branch_labels = None
depends_on = None

_IDENTITY = 'uq_products_customer_material_code_name'
_COLUMNS = 'customer_id, customer_material_code, product_name'
_TEMP = '_optional_customer_code_products'
_CONSTRAINT = re.compile(
    r',\s*CONSTRAINT\s+"?' + _IDENTITY + r'"?\s+UNIQUE\s*\(\s*'
    r'"?customer_id"?\s*,\s*"?customer_material_code"?\s*,\s*"?product_name"?\s*\)',
    re.IGNORECASE,
)


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _sqlite():
    bind = op.get_bind()
    if bind.dialect.name != 'sqlite':
        raise RuntimeError('Optional customer code migration requires reviewed SQLite storage')
    return bind


def _rebuild(bind, *, optional):
    # Do not disable enforcement on an active application connection. Alembic's
    # dedicated offline migration connection must arrive with FK enforcement off.
    if bind.exec_driver_sql('PRAGMA foreign_keys').scalar():
        raise RuntimeError('Products rebuild requires a dedicated FK-off migration connection')
    if bind.exec_driver_sql('PRAGMA foreign_key_check').first():
        raise RuntimeError('Existing foreign-key violations; products migration refused')
    sql = bind.execute(sa.text(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='products'"
    )).scalar_one()
    if bind.execute(sa.text(
        'SELECT 1 FROM sqlite_master WHERE name=:name'
    ), {'name': _TEMP}).first():
        raise RuntimeError('Products migration temporary object already exists')
    columns = bind.exec_driver_sql('PRAGMA table_xinfo(products)').all()
    if not columns or any(row[6] for row in columns):
        raise RuntimeError('Unexpected generated/hidden products columns; migration refused')
    if re.search(r'\bAUTOINCREMENT\b', sql, re.IGNORECASE):
        raise RuntimeError('Unexpected products sequence semantics; migration refused')
    if optional:
        sql, replacements = _CONSTRAINT.subn('', sql)
        if replacements != 1:
            raise RuntimeError('Expected exact customer-code unique constraint was not found')
    else:
        if _CONSTRAINT.search(sql) or not sql.rstrip().endswith(')'):
            raise RuntimeError('Unexpected products definition; downgrade refused')
        sql = sql.rstrip()[:-1] + f',\n CONSTRAINT {_IDENTITY} UNIQUE ({_COLUMNS})\n)'
    sql, replacements = re.subn(
        r'^CREATE TABLE\s+(?:"products"|products)(?=\s*\()',
        'CREATE TABLE ' + _quote(_TEMP), sql, count=1, flags=re.IGNORECASE,
    )
    if replacements != 1:
        raise RuntimeError('Unexpected products CREATE statement; migration refused')
    objects = bind.execute(sa.text(
        "SELECT type,name,sql FROM sqlite_master WHERE tbl_name='products' "
        "AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name"
    )).all()
    expected_index = f"CREATE UNIQUE INDEX {_IDENTITY} ON products ({_COLUMNS}) WHERE customer_material_code <> ''"
    if not optional:
        indexes = [obj for obj in objects if obj[1] == _IDENTITY]
        if len(indexes) != 1 or indexes[0][2] != expected_index:
            raise RuntimeError('Expected exact optional customer-code index was not found')
        objects = [obj for obj in objects if obj[1] != _IDENTITY]

    # sqlite3 legacy mode does not BEGIN for DDL. Ensure a real transaction also
    # covers table/trigger/index reconstruction and Alembic's version update.
    if not bind.connection.driver_connection.in_transaction:
        bind.exec_driver_sql('BEGIN IMMEDIATE')
    legacy = bind.exec_driver_sql('PRAGMA legacy_alter_table').scalar()
    bind.exec_driver_sql('PRAGMA legacy_alter_table=ON')
    try:
        bind.exec_driver_sql(sql)
        names = ','.join(_quote(row[1]) for row in columns)
        bind.exec_driver_sql(f'INSERT INTO {_quote(_TEMP)} ({names}) SELECT {names} FROM products')
        bind.exec_driver_sql('DROP TABLE products')
        bind.exec_driver_sql(f'ALTER TABLE {_quote(_TEMP)} RENAME TO products')
        for _, _, definition in objects:
            bind.exec_driver_sql(definition)
        if optional:
            bind.exec_driver_sql(expected_index)
        if bind.exec_driver_sql('PRAGMA foreign_key_check').first():
            raise RuntimeError('Products migration foreign-key validation failed')
    finally:
        bind.exec_driver_sql(f'PRAGMA legacy_alter_table={int(legacy)}')


def upgrade():
    _rebuild(_sqlite(), optional=True)


def downgrade():
    bind = _sqlite()
    # This read happens before any DDL or explicit transaction. A refusal leaves
    # both business facts and the Alembic revision untouched.
    if bind.exec_driver_sql(
        f'SELECT 1 FROM products GROUP BY {_COLUMNS} HAVING count(*) > 1 LIMIT 1'
    ).first():
        raise RuntimeError('已有空客户料号同名产品，禁止有损降级；保留现库向前修复')
    _rebuild(bind, optional=False)
