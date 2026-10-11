"""Read-only preservation of every signed and persisted business reader."""
from contextlib import closing
import sqlite3

from desktop_assistant import delivery_dispatch_contract as dispatch
from desktop_assistant import order_inventory_contract as inventory

TABLE_READERS = {
    'quotation_mutations': 'quotation_write_v1',
    'shared_finished_groups': 'shared_finished_v1',
    'shared_finished_policies': 'shared_finished_management_v1',
    'contract_seals': 'contract_seal_v1',
    'company_profiles': 'company_profiles_v1',
    'shared_bom_members': 'shared_bom_v1',
}


def has_supplier_trim_facts(db):
    for table, column in (
        ('products', 'sheet_cutting_settings'),
        ('sales_order_items', 'sheet_cutting_settings_snapshot'),
        ('sales_order_item_bom_components', 'sheet_cutting_settings_snapshot'),
        ('material_requisition_items', 'sheet_cutting_snapshot'),
        ('supplier_requisition_order_items', 'sheet_cutting_snapshot'),
        ('stock_replenishment_order_items', 'sheet_cutting_snapshot')):
        if any(row[1] == column for row in db.execute(f'PRAGMA table_info("{table}")')):
            if db.execute(f'SELECT 1 FROM "{table}" WHERE json_valid("{column}") AND json_extract("{column}", \'$.schema_version\') = 3 LIMIT 1').fetchone():
                return True
    return False


def capabilities(manifest):
    value = manifest.get('reader_capabilities', {})
    if (not isinstance(value, dict) or any(not isinstance(k, str) or type(v) is not int or v != 1
                                          for k, v in value.items())):
        raise ValueError('签名reader能力声明损坏')
    return value


def target_readers(database, source, target, inventory_record, dispatch_record, *, factory=True):
    required = set(capabilities(source))  # Unknown future readers are preserved too.
    available = capabilities(target)
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required.update(reader for table, reader in TABLE_READERS.items() if table in tables)
        for table, column, capability in (
            ('stock_replenishment_order_items', 'production_snapshot_json', 'stock_purchase_identity_v1'),
            ('mold_tools', 'deleted_at', 'unused_mold_deletion_v1'),
        ):
            if any(row[1] == column for row in db.execute(f'PRAGMA table_info("{table}")')):
                required.add(capability)
        if has_supplier_trim_facts(db): required.add('supplier_sheet_trim_v1')
    if inventory_record is not None: required.add(inventory.CAPABILITY)
    if dispatch_record is not None: required.add(dispatch.CAPABILITY)
    if not required <= set(available) or (factory and available.get('factory_cutover_v1') != 1):
        raise ValueError('目标程序缺少正式切换或已保存事实的reader能力')

