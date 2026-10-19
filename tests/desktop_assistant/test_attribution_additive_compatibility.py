import shutil
import sqlite3
from pathlib import Path

import pytest

from tests.desktop_assistant.test_stopped_update import case
from desktop_assistant.attribution_compatibility import SOURCES, unchanged_attribution_reader
from desktop_assistant.storage import pack_tree, write_json


def programs(case, mutate=None):
    case.release('attribution-old')
    source = case.root / 'source-attribution-old'
    repo = Path(__file__).resolve().parents[2]
    for name in SOURCES:
        dest = source / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((repo / name).read_bytes())
    init = source / 'app/models/__init__.py'
    init.write_text(init.read_text() + '\nfrom app.models.unordered_delivery_order_attribution import UnorderedDeliveryOrderAttribution\n')
    old_package = case.root / 'attribution-old-signed.zip'
    pack_tree(source, old_package, dict(type='tianming.release.v1', version='old', revision='er1016pa'), case.key)
    old = case.manager.stage_release(old_package)['id']
    new_source = case.root / 'attribution-new-source'
    shutil.copytree(source, new_source)
    actual = new_source / 'app/models/incoming_actual_spec.py'
    actual.write_text('from app.models import Base\nfrom sqlalchemy.orm import Mapped, mapped_column\nclass Actual(Base):\n    __tablename__="incoming_receipt_actual_specs"\n    id: Mapped[int] = mapped_column(primary_key=True)\n')
    init = new_source / 'app/models/__init__.py'
    init.write_text(init.read_text() + '\nfrom app.models.incoming_actual_spec import Actual\n')
    metadata = dict(type='tianming.release.v1', version='new', revision='es1019ra', migration={
        'policy': 'preserve_existing_facts_v1', 'from_revision': 'er1016pa', 'rollback_package_sha256': old})
    if mutate:
        mutate(new_source, metadata)
    new_package = case.root / 'attribution-new-signed.zip'
    pack_tree(new_source, new_package, metadata, case.key)
    new = case.manager.stage_release(new_package)['id']
    write_json(case.manager.root / 'state.json', dict(current=new, schema_authority=new))
    with sqlite3.connect(case.manager.root / 'shared/data/carton_erp.sqlite3') as db:
        db.execute('create table unordered_delivery_order_attributions(id integer primary key)')
        db.executemany('insert into unordered_delivery_order_attributions values(?)', [(1,), (2,)])
        db.execute('create table incoming_receipt_actual_specs(id integer primary key)')
        db.execute("update alembic_version set version_num='es1019ra'")
    return old, new


def test_unchanged_signed_reader_recovers_with_existing_attribution_facts(case):
    old, new = programs(case)
    assert unchanged_attribution_reader(case.manager, old, 'es1019ra', new)
    assert case.manager.compatible(old, 'es1019ra')


@pytest.mark.parametrize('source', SOURCES)
def test_any_attribution_reader_writer_or_ddl_change_blocks_old(case, source):
    def mutate(root, metadata):
        p = root / source
        p.write_text(p.read_text() + '\n# changed contract\n')
    old, new = programs(case, mutate)
    assert not case.manager.compatible(old, 'es1019ra')


def test_old_table_column_change_still_blocks_old(case):
    def mutate(root, metadata):
        p = root / 'app/models/fixture.py'
        p.write_text(p.read_text().replace('    note: Mapped[str]', '    note: Mapped[str]\n    extra: Mapped[str]', 1))
    old, new = programs(case, mutate)
    assert not case.manager.compatible(old, 'es1019ra')


def test_wrong_migration_authority_still_blocks_old(case):
    old, new = programs(case, lambda root, meta: meta['migration'].update(rollback_package_sha256='0' * 64))
    assert not case.manager.compatible(old, 'es1019ra')


def test_new_actual_receipt_fact_still_blocks_old(case):
    old, new = programs(case)
    with sqlite3.connect(case.manager.root / 'shared/data/carton_erp.sqlite3') as db:
        db.execute('insert into incoming_receipt_actual_specs values(1)')
    assert not unchanged_attribution_reader(case.manager, old, 'es1019ra', new)
    assert not case.manager.compatible(old, 'es1019ra')


def test_unsigned_or_unavailable_authority_never_enables_old(case):
    old, new = programs(case)
    path = case.manager.root / 'packages' / (new + '.zip')
    path.write_bytes(path.read_bytes() + b'tampered')
    assert not case.manager.compatible(old, 'es1019ra')
    assert not unchanged_attribution_reader(case.manager, old, 'future-revision', new)
