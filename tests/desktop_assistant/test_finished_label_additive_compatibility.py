import shutil
import sqlite3
from pathlib import Path
import pytest
from tests.desktop_assistant.test_stopped_update import case
from desktop_assistant.attribution_compatibility import SOURCES, unchanged_attribution_reader
from desktop_assistant.storage import pack_tree,write_json

EXTRA=('app/models/incoming_actual_spec.py','app/services/incoming_actual_specs.py',
       'app/services/incoming_receipts.py','app/services/stock_preparation_processing.py',
       'alembic/versions/es1019ra_incoming_actual_specs.py')


def programs(case,mutate=None):
    case.release('finished-old');source=case.root/'source-finished-old';repo=Path(__file__).resolve().parents[2]
    for name in (*SOURCES,*EXTRA):
        p=source/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes((repo/name).read_bytes())
    init=source/'app/models/__init__.py';init.write_text(init.read_text()+'\nfrom app.models.unordered_delivery_order_attribution import UnorderedDeliveryOrderAttribution\nfrom app.models.incoming_actual_spec import IncomingActualSpec\n')
    oldpath=case.root/'finished-old-signed.zip';pack_tree(source,oldpath,dict(type='tianming.release.v1',version='old',revision='es1019ra'),case.key)
    old=case.manager.stage_release(oldpath)['id'];newsource=case.root/'finished-new-source';shutil.copytree(source,newsource)
    p=newsource/'app/models/finished_product_label.py';p.write_text('from app.models import Base\nfrom sqlalchemy.orm import Mapped,mapped_column\nclass Label(Base):\n    __tablename__="finished_product_label_print_jobs"\n    id: Mapped[int] = mapped_column(primary_key=True)\n')
    init=newsource/'app/models/__init__.py';init.write_text(init.read_text()+'\nfrom app.models.finished_product_label import Label\n')
    meta=dict(type='tianming.release.v1',version='new',revision='et1019fl',migration=dict(policy='preserve_existing_facts_v1',from_revision='es1019ra',rollback_package_sha256=old))
    if mutate:mutate(newsource,meta)
    newpath=case.root/'finished-new.zip';pack_tree(newsource,newpath,meta,case.key);new=case.manager.stage_release(newpath)['id']
    write_json(case.manager.root/'state.json',dict(current=new,schema_authority=new))
    with sqlite3.connect(case.manager.root/'shared/data/carton_erp.sqlite3') as db:
        db.execute('create table unordered_delivery_order_attributions(id integer primary key)');db.execute('insert into unordered_delivery_order_attributions values(1)')
        db.execute('create table incoming_receipt_actual_specs(id integer primary key)')
        db.execute('create table finished_product_label_print_jobs(id integer primary key)')
        db.execute("update alembic_version set version_num='et1019fl'")
    return old,new


def test_same_signed_readers_and_only_empty_label_table_permit_program_fallback(case):
    old,new=programs(case)
    assert unchanged_attribution_reader(case.manager,old,'et1019fl',new)
    assert case.manager.compatible(old,'et1019fl')
    with sqlite3.connect(case.manager.root/'shared/data/carton_erp.sqlite3') as db:db.execute('insert into finished_product_label_print_jobs values(1)')
    assert not unchanged_attribution_reader(case.manager,old,'et1019fl',new)
    assert not case.manager.compatible(old,'et1019fl')


@pytest.mark.parametrize('name',(*SOURCES,*EXTRA))
def test_any_changed_attribution_or_receipt_source_blocks_previous_program(case,name):
    def mutate(root,meta):
        p=root/name;p.write_text(p.read_text()+'\n# isolated changed reader\n')
    old,new=programs(case,mutate)
    assert not case.manager.compatible(old,'et1019fl')


def test_wrong_signed_rollback_authority_blocks(case):
    old,new=programs(case,lambda root,meta:meta['migration'].update(rollback_package_sha256='0'*64))
    assert not case.manager.compatible(old,'et1019fl')
