import sqlite3,json
from pathlib import Path
from tests.desktop_assistant.test_stopped_update import case
from desktop_assistant.storage import pack_tree,write_json


def mark(case):
 with sqlite3.connect(case.manager.root/'shared/data/carton_erp.sqlite3') as db:
  db.execute('create table incoming_receipt_actual_specs(receipt_item_id integer primary key)')
  db.execute('insert into incoming_receipt_actual_specs values(1)')


def test_old_reader_cannot_run_after_actual_specs_exist(case):
 mark(case)
 assert not case.manager.compatible(case.manager.state['current'],'r1')


def test_supported_signed_reader_keeps_actual_facts(case):
 mark(case);package=case.root/'actual-reader.zip'
 pack_tree(case.root/'source-one',package,{'type':'tianming.release.v1','version':'actual-reader','revision':'r1','reader_capabilities':{'actual_receipt_specs_v1':1}},case.key)
 candidate=case.manager.stage_release(package)
 assert case.manager.compatible(candidate['id'],'r1')


def test_unsigned_cached_claim_does_not_enable_actual_reader(case):
 mark(case);identity=case.manager.state['current'];p=case.manager.root/'releases'/identity/'manifest.json'
 m=json.loads(p.read_text());m['reader_capabilities']={'actual_receipt_specs_v1':1};write_json(p,m)
 assert not case.manager.compatible(identity,'r1')
