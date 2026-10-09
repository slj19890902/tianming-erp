from contextlib import closing
import sqlite3
from desktop_assistant.storage import pack_tree, write_json
from tests.desktop_assistant import test_recovery as recovery


def test_seal_contract_requires_signed_reader_not_mutable_cache():
    fixture=recovery.RecoveryTests();fixture.setUp()
    try:
        manager=fixture.manager
        ids={}
        for name,caps in [('old',{}),('seal',{'contract_seal_v1':1})]:
            fixture.release(name,'r1')
            package=fixture.root/(name+'-cap.zip')
            pack_tree(fixture.root/('source-'+name),package,dict(type='tianming.release.v1',
                version=name,revision='r1',reader_capabilities=caps),fixture.key)
            ids[name]=manager.stage_release(package)['id']
        write_json(manager.root/'state.json',dict(current=ids['seal'],previous=ids['old'],schema_authority=ids['seal']))
        with closing(sqlite3.connect(manager.root/'shared/data/carton_erp.sqlite3')) as db:
            db.execute('CREATE TABLE contract_seals(id INTEGER PRIMARY KEY)')
        assert manager.compatible(ids['seal'],'r1')
        assert not manager.compatible(ids['old'],'r1')
        cache=manager.manifest(ids['old']);cache['reader_capabilities']={'contract_seal_v1':1}
        write_json(manager.root/'releases'/ids['old']/'manifest.json',cache)
        assert not manager.compatible(ids['old'],'r1')
    finally:
        fixture.tearDown()
