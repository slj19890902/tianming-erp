import json
import sqlite3
from contextlib import closing
import pytest
from desktop_assistant import customer_price_label_reader as reader


def database(tmp_path):
    path=tmp_path/'reader.sqlite3'
    with closing(sqlite3.connect(path)) as db:
        db.executescript('''CREATE TABLE products(id INTEGER PRIMARY KEY,sale_unit_price NUMERIC,sale_unit_price_no_tax NUMERIC);
CREATE TABLE shared_finished_members(product_id INTEGER,group_id INTEGER);
CREATE TABLE shared_finished_mutations(group_id INTEGER,result_json TEXT);
CREATE TABLE finished_product_label_print_jobs(payload_json TEXT);
CREATE TABLE production_packaging_label_print_jobs(payload_json TEXT);''')
    return path


@pytest.mark.parametrize('table,payload',[
    ('finished_product_label_print_jobs', {'print_media':{'schema_version':1,'preset':'a4_4up'}}),
    ('production_packaging_label_print_jobs', {'print_media':{'schema_version':1,'preset':'roll_40x80'}}),
    ('shared_finished_mutations', {'customer_independent_sale_price_v1':{'product_id':1}}),
    ('shared_finished_mutations', {'shared_product_master_v1':{'customer_sale_price_initializations':{}}}),
    ('shared_finished_mutations', {'created_sales_references':[{'price_initialization':{'policy':'customer-sale-price-v1'}}]}),
])
def test_saved_semantics_block_legacy_program_without_writing_database(tmp_path,table,payload):
    path=database(tmp_path)
    with closing(sqlite3.connect(path)) as db:
        column='result_json' if table=='shared_finished_mutations' else 'payload_json'
        db.execute(f'INSERT INTO {table}({column}) VALUES(?)',(json.dumps(payload),));db.commit()
    before=path.read_bytes()
    with pytest.raises(ValueError,match='独立售价或多纸张'):
        reader.require_reader(path,{})
    for bad in (True,0,2,'1'):
        with pytest.raises(ValueError):reader.require_reader(path,{'reader_capabilities':{reader.CAPABILITY:bad}})
    assert reader.require_reader(path,{'reader_capabilities':{reader.CAPABILITY:1}})
    assert path.read_bytes()==before


def test_matching_prices_do_not_erase_prior_independence_evidence(tmp_path):
    path=database(tmp_path)
    with closing(sqlite3.connect(path)) as db:
        db.executescript('''INSERT INTO products VALUES(1,5,5),(2,8,8);
INSERT INTO shared_finished_members VALUES(1,3),(2,3);
INSERT INTO shared_finished_mutations VALUES(3,'{"shared_product_master_v1":{"full_catalog":true}}');''')
        assert reader.has_facts(db)
        db.execute('INSERT INTO shared_finished_mutations VALUES(3,?)',(json.dumps({'customer_independent_sale_price_v1':{'product_id':1}}),))
        db.execute('UPDATE products SET sale_unit_price=9,sale_unit_price_no_tax=9')
        assert reader.has_facts(db)
        db.commit()
    with pytest.raises(ValueError):reader.require_reader(path,{})


def test_legacy_labels_and_pure_inventory_group_need_no_new_reader(tmp_path):
    path=database(tmp_path)
    with closing(sqlite3.connect(path)) as db:
        db.execute('INSERT INTO finished_product_label_print_jobs VALUES(?)',('{"template_version":"current_40x30_v2"}',))
        db.executescript('INSERT INTO products VALUES(1,5,5),(2,8,8);INSERT INTO shared_finished_members VALUES(1,3),(2,3);')
        db.commit()
    assert reader.require_reader(path,{}) is False


def test_capability_requires_complete_price_media_source_closure():
    complete={name:b'' for name in reader.SOURCES}
    reader.require_sources(complete)
    for name in reader.SOURCES:
        with pytest.raises(ValueError):reader.require_sources({key:value for key,value in complete.items() if key!=name})
