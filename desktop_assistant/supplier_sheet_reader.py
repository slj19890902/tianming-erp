"""Reader admission for independent supplier edges and full-sheet output."""
CAPABILITY = 'supplier_sheet_edges_v1'
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
