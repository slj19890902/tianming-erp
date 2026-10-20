"""Real signed inputs/SQLite/subprocess; only OS migration admission is a fixture."""
from pathlib import Path
from types import SimpleNamespace
import json,sqlite3,sys
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from desktop_assistant import migration
from desktop_assistant.exact_sales_reader import COLUMNS,recover_empty_migration,CAPABILITY
from desktop_assistant.storage import pack_tree,sha,write_json,signed_release_manifest

@pytest.fixture
def scene(tmp_path,monkeypatch):
 root=tmp_path/'instance';shared=root/'shared';(shared/'data').mkdir(parents=True);(root/'packages').mkdir();(root/'control').mkdir();(root/'staging/migration-fixture').mkdir(parents=True)
 database=shared/'data/carton_erp.sqlite3'
 with sqlite3.connect(database) as db:
  db.execute('CREATE TABLE alembic_version(version_num TEXT)');db.execute("INSERT INTO alembic_version VALUES('ev1020hr')")
  for table in COLUMNS:
   db.execute('CREATE TABLE '+table+'(id INTEGER PRIMARY KEY, amount TEXT)');db.execute('INSERT INTO '+table+" VALUES(1,'unchanged')")
  for table in ('users','customers','products'):db.execute('CREATE TABLE '+table+'(id INTEGER PRIMARY KEY)')
  db.execute('CREATE TABLE unordered_delivery_order_attributions(id INTEGER PRIMARY KEY)');db.execute('INSERT INTO unordered_delivery_order_attributions VALUES(1)')
  db.execute('CREATE INDEX original_index ON sales_orders(amount)')
  db.execute('CREATE TRIGGER original_trigger BEFORE DELETE ON sales_orders BEGIN SELECT RAISE(ABORT,\'original\'); END')
 before=migration.facts(database);source_schema=migration.schema(database)
 with sqlite3.connect(database) as db:
  for table,columns in COLUMNS.items():
   for name in columns:db.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' TEXT')
  db.execute("UPDATE alembic_version SET version_num='ew1020em'")
 key=Ed25519PrivateKey.generate();public=key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
 source=tmp_path/'source';source.mkdir();(source/'main.py').write_text('pass')
 old_package=tmp_path/'old.zip';pack_tree(source,old_package,dict(type='tianming.release.v1',version='old',revision='ev1020hr'),key);old=sha(old_package);old_package.rename(root/'packages'/(old+'.zip'))
 new_package=tmp_path/'new.zip';pack_tree(source,new_package,dict(type='tianming.release.v1',version='new',revision='ew1020em',reader_capabilities={CAPABILITY:1},migration=dict(policy='preserve_existing_facts_v1',from_revision='ev1020hr',rollback_package_sha256=old)),key);new=sha(new_package);new_package.rename(root/'packages'/(new+'.zip'));manifest=signed_release_manifest(root/'packages'/(new+'.zip'),public)
 report_path=root/'staging/migration-fixture/report.json'
 write_json(report_path,dict(status='passed',package_sha256=new,source_revision='ev1020hr',target_revision='ew1020em',source_schema=source_schema,result_schema=migration.schema(database),source_facts=before,schema_fingerprint_version=2))
 backup=root/'backup.tmbackup';backup.write_bytes(b'already-authenticated-test-backup')
 state=dict(operation='update',current=new,update_backup=str(backup),migration_backup_sha256=sha(backup),migration_report=str(report_path),migration_report_sha256=sha(report_path),migration_files=migration.files(shared))
 write_json(root/'state.json',state)
 driver=tmp_path/'downgrade.py';driver.write_text("import sqlite3,os\nassert __import__('sys').argv[-2:]==['downgrade','ev1020hr']\nwith sqlite3.connect(os.environ['ISOLATED_DB']) as db:\n"+''.join(' db.execute('+repr('ALTER TABLE '+t+' DROP COLUMN '+c)+')\n' for t,cs in reversed(list(COLUMNS.items())) for c in reversed(cs))+" db.execute(\"UPDATE alembic_version SET version_num='ev1020hr'\")\n")
 calls=[]
 def prepare(*args,**kw):calls.append(True);return [sys.executable,str(driver),'upgrade','ew1020em'],{'ISOLATED_DB':str(database)},False
 monkeypatch.setattr(migration,'prepare_migration',prepare)
 manager=SimpleNamespace(root=root,public_key=public,_process=lambda:None,_execution_release=lambda *a,**k:source)
 return SimpleNamespace(**locals())

def test_empty_six_columns_recover_source_schema_with_attribution_and_triggers(scene):
 c=scene;answer=recover_empty_migration(c.manager,c.new,c.old,c.manifest,c.state)
 assert answer['status']=='completed' and not answer['database_replaced']
 assert migration.schema(c.database)==c.source_schema and migration.facts(c.database)==c.before
 assert c.state['operation']=='migration_running' # Caller must verify/start old before healthy publication.
 assert c.calls

@pytest.mark.parametrize('fault',['new_fact','changed_original','changed_schema','bad_report','bad_backup','active_migration','running_candidate'])
def test_insufficient_recovery_evidence_never_starts_downgrade(scene,fault):
 c=scene
 if fault=='new_fact':
  with sqlite3.connect(c.database) as db:db.execute("UPDATE sales_orders SET total_amount_exact='6.675'")
 if fault=='changed_original':
  with sqlite3.connect(c.database) as db:db.execute("UPDATE sales_orders SET amount='changed'")
 if fault=='changed_schema':
  with sqlite3.connect(c.database) as db:db.execute('CREATE TABLE unknown_change(id INTEGER)')
 if fault=='bad_report':c.report_path.write_bytes(b'{}')
 if fault=='bad_backup':c.backup.write_bytes(b'corrupt')
 if fault=='active_migration':c.state['operation']='migration_running'
 if fault=='running_candidate':c.manager._process=lambda:('running',{})
 before=sha(c.database)
 with pytest.raises(ValueError):recover_empty_migration(c.manager,c.new,c.old,c.manifest,c.state)
 assert not c.calls and sha(c.database)==before
