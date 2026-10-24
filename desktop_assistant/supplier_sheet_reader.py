"""Reader admission for independent supplier edges and full-sheet output."""
CAPABILITY = 'supplier_sheet_edges_v1'
EXTRA_INPUT_CAPABILITY = 'sheet_extra_input_v1'
COLUMNS = {
    'products': ('supplier_edge_type', 'base_supplier_edge_type'),
    'stock_replenishment_order_items': ('supplier_edge_type',),
    'supplier_requisition_order_items': ('supplier_edge_type',),
}


def has_facts(db):
    for table, columns in COLUMNS.items():
        present = {row[1] for row in db.execute('PRAGMA table_info("'+table+'")')}
        names = [name for name in columns if name in present]
        if names and db.execute('SELECT 1 FROM "'+table+'" WHERE '+
                ' OR '.join('"'+name+'" IS NOT NULL' for name in names)+' LIMIT 1').fetchone():
            return True
    present = {row[1] for row in db.execute('PRAGMA table_info(inventory_reservations)')}
    return bool('cut_plan_json' in present and db.execute(
        "SELECT 1 FROM inventory_reservations WHERE json_valid(cut_plan_json) "
        "AND json_extract(cut_plan_json,'$.full_sheet_output_v1')=1 LIMIT 1").fetchone())


def has_extra_input_facts(db):
    present = {row[1] for row in db.execute('PRAGMA table_info(inventory_reservations)')}
    if not {'cut_plan_json', 'credited_requirement_quantity'} <= present:
        return False
    return bool(db.execute("SELECT 1 FROM inventory_reservations WHERE credited_requirement_quantity=0 "
        "AND json_valid(cut_plan_json) AND json_extract(cut_plan_json,'$.actual_sheet_input_v1')=1 "
        "AND json_extract(cut_plan_json,'$.full_sheet_output_v1')=1 LIMIT 1").fetchone())


def require_extra_input_reader(database, manifest):
    from contextlib import closing
    import sqlite3
    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        required = has_extra_input_facts(db)
    value = (manifest.get('reader_capabilities') or {}).get(EXTRA_INPUT_CAPABILITY)
    if required and not (type(value) is int and value == 1):
        raise ValueError('数据库已有额外片料加工安排，目标程序不能正确消耗与计成本，禁止启动或回退')
    return required
