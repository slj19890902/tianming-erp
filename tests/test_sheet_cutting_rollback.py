import json
import sqlite3

from desktop_assistant.manager import Manager


def test_old_program_rollback_is_rejected_after_any_v2_fact(tmp_path, monkeypatch):
    import desktop_assistant.manager as module
    manager = Manager(tmp_path, b'test-public-key')
    (tmp_path / 'shared/data').mkdir()
    database = tmp_path / 'shared/data/carton_erp.sqlite3'
    (tmp_path / 'state.json').write_text(json.dumps({'schema_authority': 'new'}))
    (tmp_path / 'packages/new.zip').write_bytes(b'fixture')
    monkeypatch.setattr(manager, 'manifest', lambda release: {'revision': 'eg1008sc' if release == 'new' else 'ef1007cp'})
    monkeypatch.setattr(module, 'sha', lambda path: 'new')
    monkeypatch.setattr(module, 'signed_release_manifest', lambda *a: {'revision': 'eg1008sc',
        'migration': {'policy': 'preserve_existing_facts_v1', 'rollback_package_sha256': 'old'}})
    columns = {'products': 'sheet_cutting_settings', 'sales_order_items': 'sheet_cutting_settings_snapshot',
        'sales_order_item_bom_components': 'sheet_cutting_settings_snapshot',
        'material_requisition_items': 'sheet_cutting_snapshot', 'supplier_requisition_order_items': 'sheet_cutting_snapshot',
        'stock_replenishment_order_items': 'sheet_cutting_snapshot'}
    with sqlite3.connect(database) as db:
        for table, column in columns.items():
            db.execute(f'CREATE TABLE "{table}" ("{column}" TEXT)')
    assert manager.compatible('old', 'eg1008sc') is True
    for table, column in columns.items():
        with sqlite3.connect(database) as db:
            db.execute(f'INSERT INTO "{table}" VALUES (?)', ('{}',))
        assert manager.compatible('old', 'eg1008sc') is False
        assert manager.compatible('new', 'eg1008sc') is True
        with sqlite3.connect(database) as db:
            db.execute(f'DELETE FROM "{table}"')
