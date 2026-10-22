"""Read-only preservation of every signed and persisted business reader."""
from contextlib import closing
import sqlite3

from desktop_assistant import delivery_dispatch_contract as dispatch
from desktop_assistant import order_inventory_contract as inventory
from desktop_assistant import physical_kit_reader_contract as physical_kit

TABLE_READERS = {
    'quotation_mutations': 'quotation_write_v1',
    'shared_finished_groups': 'shared_finished_v1',
    'shared_finished_policies': 'shared_finished_management_v1',
    'contract_seals': 'contract_seal_v1',
    'company_profiles': 'company_profiles_v1',
    'shared_bom_members': 'shared_bom_v1',
}


SHARED_PRODUCT_MASTER_READER = 'shared_product_master_v1'


def has_shared_product_master_facts(db):
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'scrap_production_jobs' in tables and db.execute('SELECT 1 FROM scrap_production_jobs LIMIT 1').fetchone():
        return True
    columns={r[1] for r in db.execute('PRAGMA table_info(products)')}
    if 'production_variants' in columns and db.execute('SELECT 1 FROM products WHERE production_variants IS NOT NULL LIMIT 1').fetchone():
        return True
    if 'customer_sharing_members' in tables and db.execute('SELECT 1 FROM customer_sharing_members LIMIT 1').fetchone():
        return True
    return bool('shared_finished_mutations' in tables and db.execute(
        "SELECT 1 FROM shared_finished_mutations WHERE json_valid(result_json) "
        "AND json_type(result_json,'$.shared_product_master_v1')='object' LIMIT 1").fetchone())


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


MOLD_LABEL_TEXT_READER = 'mold_label_text_v3'
CUSTOMER_SPECIFICATION_READER = 'customer_specification_v1'
HISTORICAL_RECEIVABLE_READER = 'historical_delivery_receivable_v1'


def has_customer_specification_facts(db):
    if any(r[1] == 'customer_specification' for r in db.execute('PRAGMA table_info(products)')):
        if db.execute(
            'SELECT 1 FROM products WHERE customer_specification IS NOT NULL LIMIT 1'
        ).fetchone():
            return True
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='master_data_object_versions'").fetchone():
        return db.execute(
            "SELECT 1 FROM master_data_object_versions WHERE object_type='product' "
            "AND json_valid(snapshot_json) "
            "AND json_type(snapshot_json, '$.customer_specification') NOT IN ('null') LIMIT 1"
        ).fetchone() is not None
    return False


def has_mold_label_text_facts(db):
    if any(r[1] == 'label_overrides_json' for r in db.execute('PRAGMA table_info(mold_tools)')):
        if db.execute("SELECT 1 FROM mold_tools WHERE json_valid(label_overrides_json) AND json_extract(label_overrides_json, '$.v')=3 LIMIT 1").fetchone():
            return True
    if any(r[1] == 'label_layout_payload_json' for r in db.execute('PRAGMA table_info(mold_label_print_jobs)')):
        if db.execute("SELECT 1 FROM mold_label_print_jobs j, json_each(CASE WHEN json_valid(j.label_layout_payload_json) THEN j.label_layout_payload_json ELSE '{}' END, '$.content_snapshots') s WHERE json_type(s.value, '$.label_text_overrides')='object' LIMIT 1").fetchone():
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
        if 'shared_finished_mutations' in tables and db.execute(
                "SELECT 1 FROM shared_finished_mutations WHERE json_valid(result_json) AND json_type(result_json, '$.member_states') = 'object' LIMIT 1").fetchone():
            required.add('shared_customer_members_v1')
        if has_shared_product_master_facts(db): required.add(SHARED_PRODUCT_MASTER_READER)
        if has_mold_label_text_facts(db):
            required.add(MOLD_LABEL_TEXT_READER)
        else:
            required.discard(MOLD_LABEL_TEXT_READER)
        if has_supplier_trim_facts(db): required.add('supplier_sheet_trim_v1')
        if has_customer_specification_facts(db): required.add(CUSTOMER_SPECIFICATION_READER)
        if any(r[1] == 'historical_source_key' for r in db.execute('PRAGMA table_info(finance_customer_charges)')):
            if db.execute('SELECT 1 FROM finance_customer_charges WHERE historical_source_key IS NOT NULL OR historical_delivery_snapshot_json IS NOT NULL LIMIT 1').fetchone():
                required.add(HISTORICAL_RECEIVABLE_READER)
        if physical_kit.has_facts(db): required.add(physical_kit.CAPABILITY)
        from desktop_assistant.exact_sales_reader import has_facts, has_home_tool_facts, CAPABILITY
        if has_home_tool_facts(db): required.add('home_collaboration_v1')
        if has_facts(db): required.add(CAPABILITY)
    if inventory_record is not None: required.add(inventory.CAPABILITY)
    if dispatch_record is not None: required.add(dispatch.CAPABILITY)
    if not required <= set(available) or (factory and available.get('factory_cutover_v1') != 1):
        raise ValueError('目标程序缺少正式切换或已保存事实的reader能力')
