import sqlite3
from contextlib import closing
from tests.desktop_assistant import test_recovery as recovery
from desktop_assistant.storage import pack_tree, write_json


def test_quotation_contract_rejects_old_writer_and_preserves_rollback_record():
    fixture=recovery.RecoveryTests();fixture.setUp()
    try:
        manager=fixture.manager
        old=manager.state['current']
        fixture.release('writer', 'r1')
        package=fixture.root/'writer-cap.zip'
        pack_tree(fixture.root/'source-writer',package,{'type':'tianming.release.v1','version':'writer',
            'revision':'r1','reader_capabilities':{'quotation_write_v1':1}},fixture.key)
        current=manager.stage_release(package)['id']
        state=dict(current=current,previous=old,schema_authority=current)
        write_json(manager.root/'state.json',state)
        assert manager.compatible(old,'r1')
        with closing(sqlite3.connect(manager.root/'shared/data/carton_erp.sqlite3')) as db:
            db.execute('CREATE TABLE quotation_mutations(id INTEGER PRIMARY KEY)')
        assert not manager.compatible(old,'r1')
        assert manager.compatible(current,'r1')
        assert manager.state == state
        # A forged cached manifest cannot grant the signed capability.
        cached=manager.manifest(old);cached['reader_capabilities']={'quotation_write_v1':1}
        write_json(manager.root/'releases'/old/'manifest.json',cached)
        assert not manager.compatible(old,'r1')
    finally:fixture.tearDown()
