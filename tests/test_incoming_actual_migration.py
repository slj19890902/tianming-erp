import importlib.util
import sqlite3
from pathlib import Path
from unittest.mock import patch
import pytest
from sqlalchemy import create_engine
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_actual_spec_additive_roundtrip_and_no_loss_guard(tmp_path):
 path=tmp_path/'migration.sqlite3';engine=create_engine('sqlite:///'+str(path))
 with engine.begin() as db:
  db.exec_driver_sql('create table users(id integer primary key)')
  db.exec_driver_sql('create table materials(id integer primary key)')
  db.exec_driver_sql('create table incoming_receipt_items(id integer primary key)')
  db.exec_driver_sql('create trigger keep_original before delete on materials begin select raise(abort,"retain"); end')
  for table in ('users','materials','incoming_receipt_items'):db.exec_driver_sql(f'insert into {table}(id) values(1)')
 p=Path(__file__).parents[1]/'alembic/versions/es1019ra_incoming_actual_specs.py';spec=importlib.util.spec_from_file_location('migration_actual',p);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
 with engine.begin() as db:
  operations=Operations(MigrationContext.configure(db))
  with patch.object(m,'op',operations):m.upgrade();m.downgrade();m.upgrade()
  assert db.exec_driver_sql("select count(*) from sqlite_master where type='trigger' and name='keep_original'").scalar_one()==1
  for table in ('users','materials','incoming_receipt_items'):assert db.exec_driver_sql(f'select id from {table}').scalar_one()==1
  db.exec_driver_sql("insert into incoming_receipt_actual_specs(receipt_item_id,material_id,actor_id,request_json,actual_json) values(1,1,1,'{}','{}')")
  with patch.object(m,'op',operations):
   with pytest.raises(RuntimeError,match='不能有损降级'):m.downgrade()
  for sql in ('update incoming_receipt_actual_specs set actual_json=\'{"changed":true}\'','delete from incoming_receipt_actual_specs'):
   with pytest.raises(Exception,match='不可改写'):db.exec_driver_sql(sql)
