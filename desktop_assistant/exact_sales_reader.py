"""Read-only admission for programs that preserve exact decimal sale facts."""
CAPABILITY = 'exact_sales_decimal_v1'
COLUMNS = {
    'sales_orders': ('total_amount_exact',),
    'sales_order_items': ('unit_price_exact', 'subtotal_exact'),
    'finance_statements': ('total_receivable_exact',),
    'finance_statement_items': ('unit_price_snapshot_exact', 'receivable_amount_exact'),
}


def has_facts(db):
    for table, columns in COLUMNS.items():
        existing = {r[1] for r in db.execute('PRAGMA table_info("' + table + '")')}
        present = [name for name in columns if name in existing]
        if present and db.execute('SELECT 1 FROM "' + table + '" WHERE ' +
                ' OR '.join('"' + name + '" IS NOT NULL' for name in present) + ' LIMIT 1').fetchone():
            return True
    return False


def require_reader(db, manifest):
    reader = (manifest.get('reader_capabilities') or {}).get(CAPABILITY)
    if has_facts(db) and not (type(reader) is int and reader == 1):
        raise ValueError('当前数据库已有精确销售金额，目标程序不能保留原始单价与金额；禁止启动或回退')
