import hashlib
from pathlib import Path
import sqlite3

import pytest
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError

from tests.test_external_graph_cost_migration import migration
from tests.test_multilevel_bom_factory_compile import factory_copy
from tests.test_multilevel_bom_modes_migration import original_facts
from tests.test_p1_131_material_cost_lineage_migration import _config


def test_handoff_owns_exact_source_target_and_preserves_facts(tmp_path):
    module = migration("sm25v8x9z87_bom_source_handoffs.py")
    engine = create_engine(f"sqlite:///{tmp_path / 'handoff-constraints.sqlite3'}")
    with engine.begin() as db:
        db.exec_driver_sql("PRAGMA foreign_keys=ON")
        db.exec_driver_sql("CREATE TABLE sales_order_item_bom_components(id INTEGER PRIMARY KEY, "
            "sales_order_item_id INTEGER, component_product_id INTEGER, UNIQUE(id,sales_order_item_id,component_product_id))")
        db.exec_driver_sql("CREATE TABLE order_bom_rule_sources(snapshot_id INTEGER PRIMARY KEY, "
            "revision_id INTEGER, order_item_id INTEGER, product_id INTEGER)")
        db.exec_driver_sql("INSERT INTO sales_order_item_bom_components VALUES(10,1,100),(20,1,100),(30,1,200),(40,2,100)")
        db.exec_driver_sql("INSERT INTO order_bom_rule_sources VALUES(20,2,1,100),(30,2,1,200),(40,3,2,100)")
        with Operations.context(MigrationContext.configure(db)):
            module.upgrade()
            sql = ("INSERT INTO order_bom_source_handoffs(revision_id,source_snapshot_id,target_snapshot_id,"
                "order_item_id,product_id,source_kind,source_basis_hash,target_basis_hash) "
                "VALUES(:revision,:source,:target,:item,:product,:kind,:before,:after)")
            values = dict(revision=2, source=10, target=20, item=1, product=100,
                kind="manufactured", before="a" * 64, after="b" * 64)
            for changes in (dict(source=40), dict(source=30), dict(target=30), dict(target=40),
                    dict(revision=3), dict(item=2), dict(source=20), dict(kind="separate"),
                    dict(before="bad"), dict(after="bad"), dict(target=None)):
                with pytest.raises(IntegrityError):
                    db.exec_driver_sql(sql, {**values, **changes})
            db.exec_driver_sql(sql, values)
            with pytest.raises(IntegrityError):
                db.exec_driver_sql(sql, values)
            before = db.exec_driver_sql("SELECT * FROM order_bom_source_handoffs").all()
            for statement in ("UPDATE order_bom_source_handoffs SET source_kind='purchased'",
                              "DELETE FROM order_bom_source_handoffs"):
                with pytest.raises(IntegrityError, match="immutable"):
                    db.exec_driver_sql(statement)
            with pytest.raises(RuntimeError, match="已有BOM来源交接事实"):
                module.downgrade()
            assert db.exec_driver_sql("SELECT * FROM order_bom_source_handoffs").all() == before
            assert db.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    engine.dispose()


def test_handoff_upgrade_roundtrip_preserves_factory_copy(factory_copy, monkeypatch):
    db = factory_copy
    target = Path(db.get_bind().url.database)
    backup = target.with_name("before-upgrade.sqlite3")
    original_hash = hashlib.sha256(backup.read_bytes()).hexdigest()
    with sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True) as original:
        tables = [row[0] for row in original.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if row[0] not in {"alembic_version", "sqlite_sequence"}]
        columns = {table: [row[1] for row in original.execute(f'PRAGMA table_info("{table}")')] for table in tables}
        facts = original_facts(original, columns)
        schema = dict(original.execute("SELECT name,sql FROM sqlite_master WHERE type IN ('trigger','index') AND sql IS NOT NULL"))
    db.rollback()
    config = _config(monkeypatch, target)
    assert ScriptDirectory.from_config(config).get_heads() == ["sn26v8x9z88"]
    for destination in ("sl24v8x9z86", "sn26v8x9z88"):
        if destination == "sl24v8x9z86":
            command.downgrade(config, destination)
        else:
            command.upgrade(config, destination)
        with sqlite3.connect(target.as_uri() + "?mode=ro", uri=True) as check:
            assert check.execute("SELECT version_num FROM alembic_version").fetchall() == [(destination,)]
            assert check.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert check.execute("PRAGMA foreign_key_check").fetchall() == []
            assert original_facts(check, columns) == facts
            actual = dict(check.execute("SELECT name,sql FROM sqlite_master WHERE type IN ('trigger','index') AND sql IS NOT NULL"))
            assert all(actual.get(name) == sql for name, sql in schema.items())
            if destination == "sn26v8x9z88":
                assert check.execute("SELECT count(*) FROM order_bom_source_handoffs").fetchone() == (0,)
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == original_hash


@pytest.mark.parametrize("wrong_source_kind", [False, True])
def test_real_handoff_fact_blocks_downgrade_without_any_write(factory_copy, monkeypatch, wrong_source_kind):
    from app.models.multilevel_bom import OrderBomSourceHandoff
    from app.services.multilevel_bom_execution_boundary import _source_identity
    from app.services.multilevel_bom_rule_cutover import persist_reviewed_rule
    from app.services.multilevel_bom_rule_impact import review_current_rule_requirements
    from app.services.multilevel_bom_source_handoffs import current_source_handoffs
    from app.services.multilevel_bom_orders import read_compiled_order_bom
    from app.services.multilevel_bom_plan import BomPlanError
    from tests.test_multilevel_bom_rule_impact import frozen_order

    db = factory_copy
    command.downgrade(_config(monkeypatch, Path(db.get_bind().url.database)), "sm25v8x9z87")
    actor, item, frozen = frozen_order(db)
    review = review_current_rule_requirements(db, order_item_id=item.id, customer_id=frozen.graph.customer_id)
    # Migration fixture only: public execution is not enabled by this record.
    revision = persist_reviewed_rule(db, review=review, item=item, previous=None,
        expected_revision=0, reviewed_hash=review.checksum, request_hash="a" * 64,
        operation_key="isolated-handoff-migration", actor=actor)
    source = next(row for row in frozen.snapshots if row.component_product_id == 3771)
    target_source = next(row for row in review.proposed.snapshots if row.component_product_id == 3771)
    db.add(OrderBomSourceHandoff(revision_id=revision.id, source_snapshot_id=source.id,
        target_snapshot_id=target_source.id, order_item_id=item.id, product_id=3771,
        source_kind="purchased" if wrong_source_kind else "manufactured", source_basis_hash=_source_identity(source)["hash"],
        target_basis_hash=_source_identity(target_source)["hash"]))
    db.commit()
    compiled = read_compiled_order_bom(db, item.id)
    if wrong_source_kind:
        with pytest.raises(BomPlanError, match="生产方式或单位换算不相容"):
            current_source_handoffs(db, compiled)
    else:
        links = current_source_handoffs(db, compiled)
        assert len(links) == 1 and links[0].source_snapshot_id == source.id
        assert links[0].target_snapshot_id == target_source.id
    db.rollback()
    target = Path(db.get_bind().url.database)
    # Adding/removing an empty receipt ownership table must preserve existing
    # source handoffs, including the immutable triggers protecting those facts.
    config = _config(monkeypatch, target)
    with sqlite3.connect(target.as_uri() + "?mode=ro", uri=True) as check:
        saved_handoff = check.execute("SELECT * FROM order_bom_source_handoffs").fetchall()
    command.upgrade(config, "sn26v8x9z88")
    command.downgrade(config, "sm25v8x9z87")
    with sqlite3.connect(target.as_uri() + "?mode=ro", uri=True) as check:
        assert check.execute("SELECT * FROM order_bom_source_handoffs").fetchall() == saved_handoff
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="已有BOM来源交接事实"):
        command.downgrade(_config(monkeypatch, target), "sl24v8x9z86")
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before
    with sqlite3.connect(target.as_uri() + "?mode=ro", uri=True) as check:
        assert check.execute("SELECT count(*) FROM order_bom_source_handoffs").fetchone() == (1,)
        assert check.execute("SELECT version_num FROM alembic_version").fetchall() == [("sm25v8x9z87",)]
        assert check.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert check.execute("PRAGMA foreign_key_check").fetchall() == []
