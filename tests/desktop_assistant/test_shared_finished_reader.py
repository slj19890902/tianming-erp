import sqlite3
from contextlib import closing
from tests.desktop_assistant import test_recovery as recovery
from desktop_assistant.storage import pack_tree, write_json


def test_shared_reader_contract_signed_and_no_silent_rollback():
    fixture=recovery.RecoveryTests();fixture.setUp()
    try:
        manager=fixture.manager;old=manager.state['current']
        fixture.release('shared-reader','r1')
        package=fixture.root/'shared-reader-cap.zip'
        pack_tree(fixture.root/'source-shared-reader',package,dict(type='tianming.release.v1',
            version='shared-reader',revision='r1',reader_capabilities={'shared_finished_v1':1}),fixture.key)
        current=manager.stage_release(package)['id']
        state=dict(current=current,previous=old,schema_authority=current)
        write_json(manager.root/'state.json',state)
        assert manager.compatible(old,'r1')
        with closing(sqlite3.connect(manager.root/'shared/data/carton_erp.sqlite3')) as db:
            db.execute('CREATE TABLE shared_finished_groups(id INTEGER PRIMARY KEY)')
        assert not manager.compatible(old,'r1')
        assert manager.compatible(current,'r1')
        assert manager.state==state
        cached=manager.manifest(old);cached['reader_capabilities']={'shared_finished_v1':1}
        write_json(manager.root/'releases'/old/'manifest.json',cached)
        assert not manager.compatible(old,'r1')
    finally:
        fixture.tearDown()


def test_management_contract_rejects_v580_even_with_schema_authority():
    fixture=recovery.RecoveryTests();fixture.setUp()
    try:
        manager=fixture.manager
        ids={}
        for name,capabilities in (
            ('v580',{'shared_finished_v1':1}),
            ('management',{'shared_finished_v1':1,'shared_finished_management_v1':1})):
            fixture.release(name,'r1')
            package=fixture.root/(name+'-cap.zip')
            pack_tree(fixture.root/('source-'+name),package,dict(type='tianming.release.v1',
                version=name,revision='r1',reader_capabilities=capabilities),fixture.key)
            ids[name]=manager.stage_release(package)['id']
        write_json(manager.root/'state.json',dict(current=ids['management'],previous=ids['v580'],schema_authority=ids['management']))
        with closing(sqlite3.connect(manager.root/'shared/data/carton_erp.sqlite3')) as db:
            db.execute('CREATE TABLE shared_finished_groups(id INTEGER PRIMARY KEY)')
            db.execute('CREATE TABLE shared_finished_policies(group_id INTEGER PRIMARY KEY)')
        assert manager.compatible(ids['management'],'r1')
        assert not manager.compatible(ids['v580'],'r1')
        cache=manager.manifest(ids['v580'])
        cache['reader_capabilities']['shared_finished_management_v1']=1
        write_json(manager.root/'releases'/ids['v580']/'manifest.json',cache)
        assert not manager.compatible(ids['v580'],'r1')
    finally:
        fixture.tearDown()
