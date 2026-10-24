"""Keep independent customer prices and frozen label media across program swaps."""
CAPABILITY = 'customer_price_label_media_v1'
SOURCES = frozenset({
    'desktop_assistant/customer_price_label_reader.py', 'desktop_assistant/reader_contract.py',
    'desktop_assistant/manager.py', 'app/services/product_label_media.py',
    'app/services/shared_product_master.py', 'app/services/shared_customer_access.py',
    'app/services/customer_sharing.py', 'app/api/products.py',
    'app/services/master_data_versioning.py',
    'app/services/production_label_operations.py', 'app/services/finished_product_labels.py',
    'static/ui/product-label-media.js', 'static/production-packaging-label.html',
})


def require_sources(sources):
    if not SOURCES <= sources.keys():
        raise ValueError('客户独立售价和多纸张打印源码不完整，不能声明兼容能力')


def has_facts(db):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ('production_packaging_label_print_jobs', 'finished_product_label_print_jobs'):
        if table in tables and any(r[1] == 'payload_json' for r in db.execute(f'PRAGMA table_info("{table}")')):
            if db.execute(f"SELECT 1 FROM {table} WHERE json_valid(payload_json) AND "
                          "json_type(payload_json,'$.print_media')='object' LIMIT 1").fetchone():
                return True
    if 'shared_finished_mutations' not in tables:
        return False
    if db.execute("SELECT 1 FROM shared_finished_mutations WHERE json_valid(result_json) AND "
                  "(json_type(result_json,'$.customer_independent_sale_price_v1')='object' "
                  "OR json_type(result_json,'$.shared_product_master_v1.customer_sale_price_initializations')='object' "
                  "OR EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(result_json) THEN result_json ELSE '{}' END, '$.created_sales_references') "
                  "WHERE json_extract(value,'$.price_initialization.policy')='customer-sale-price-v1')) LIMIT 1").fetchone():
        return True
    columns = {r[1] for r in db.execute('PRAGMA table_info(products)')}
    if 'shared_finished_members' in tables and {'id', 'sale_unit_price', 'sale_unit_price_no_tax'} <= columns:
        return bool(db.execute("SELECT 1 FROM shared_finished_members a JOIN shared_finished_members b "
            "ON a.group_id=b.group_id AND a.product_id<b.product_id "
            "JOIN products p ON p.id=a.product_id JOIN products q ON q.id=b.product_id "
            "WHERE (p.sale_unit_price IS NOT q.sale_unit_price OR p.sale_unit_price_no_tax IS NOT q.sale_unit_price_no_tax) "
            "AND EXISTS (SELECT 1 FROM shared_finished_mutations m WHERE m.group_id=a.group_id "
            "AND json_valid(m.result_json) AND json_extract(m.result_json,'$.shared_product_master_v1.full_catalog')=1) LIMIT 1").fetchone())
    return False


def require_reader(database, manifest):
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        required = has_facts(db)
    value = (manifest.get('reader_capabilities') or {}).get(CAPABILITY)
    if required and not (type(value) is int and value == 1):
        raise ValueError('数据库已有客户独立售价或多纸张打印事实，禁止使用会覆盖售价或忽略纸型的旧程序')
    return required
