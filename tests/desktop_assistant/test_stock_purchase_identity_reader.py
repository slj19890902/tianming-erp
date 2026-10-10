from contextlib import closing
import sqlite3
from desktop_assistant.storage import pack_tree,write_json
from tests.desktop_assistant import test_recovery as recovery


def test_frozen_purchase_requires_signed_reader_even_if_cache_is_edited():
    fixture=recovery.RecoveryTests();fixture.setUp()
    try:
        manager=fixture.manager;ids={}
        for name,caps in [('old',{}),('new',{'stock_purchase_identity_v1':1})]:
            fixture.release(name,'r1');package=fixture.root/(name+'-cap.zip')
            pack_tree(fixture.root/('source-'+name),package,dict(type='tianming.release.v1',version=name,
                revision='r1',reader_capabilities=caps),fixture.key)
            ids[name]=manager.stage_release(package)['id']
        write_json(manager.root/'state.json',dict(current=ids['new'],previous=ids['old'],schema_authority=ids['new']))
        with closing(sqlite3.connect(manager.root/'shared/data/carton_erp.sqlite3')) as db:
            db.execute('CREATE TABLE stock_replenishment_order_items(id INTEGER PRIMARY KEY,production_snapshot_json TEXT)')
        assert manager.compatible(ids['new'],'r1')
        assert not manager.compatible(ids['old'],'r1')
        manifest=manager.manifest(ids['old']);manifest['reader_capabilities']={'stock_purchase_identity_v1':1}
        write_json(manager.root/'releases'/ids['old']/'manifest.json',manifest)
        assert not manager.compatible(ids['old'],'r1')
    finally:fixture.tearDown()
