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


def test_rule_version_identity_chain_and_nonempty_downgrade(tmp_path):
    module = migration("si21v8x9z83_bom_rule_revisions.py")
    engine = create_engine(f"sqlite:///{tmp_path / 'rule-constraints.sqlite3'}")
    with engine.begin() as db:
        db.exec_driver_sql("PRAGMA foreign_keys=ON")
        for ddl in ("CREATE TABLE order_bom_graphs(order_item_id INTEGER PRIMARY KEY)",
                    "CREATE TABLE users(id INTEGER PRIMARY KEY)",
                    "CREATE TABLE products(id INTEGER PRIMARY KEY)",
                    "CREATE TABLE sales_order_item_bom_components(id INTEGER PRIMARY KEY, sales_order_item_id INTEGER, component_product_id INTEGER)"):
            db.exec_driver_sql(ddl)
        db.exec_driver_sql("INSERT INTO order_bom_graphs VALUES(1),(2)")
        db.exec_driver_sql("INSERT INTO users VALUES(1)")
        db.exec_driver_sql("INSERT INTO products VALUES(10),(20)")
        db.exec_driver_sql("INSERT INTO sales_order_item_bom_components VALUES(100,1,10),(101,1,20),(200,2,10)")
        with Operations.context(MigrationContext.configure(db)):
            module.upgrade()
            sql = ("INSERT INTO order_bom_rule_revisions(id,order_item_id,revision,previous_id,previous_revision,"
                   "production_revision_before,order_quantity,delivered_before,document_json,content_hash,review_hash,request_hash,idempotency_key,created_by) "
                   "VALUES(:id,:item,:rev,:prev,:prev_rev,:prod,:quantity,:delivered,'{}',:hash,:hash,:hash,:key,:actor)")
            values = dict(id=1, item=1, rev=1, prev=None, prev_rev=None, prod=0, quantity=100,
                          delivered=0, hash="a" * 64, key="rule-1", actor=1)
            for changes in (dict(item=999), dict(rev=0), dict(prev=1), dict(prev_rev=1),
                            dict(prod=-1), dict(delivered=100), dict(quantity=0), dict(hash="bad"),
                            dict(key=" "), dict(actor=999)):
                with pytest.raises(IntegrityError):
                    db.exec_driver_sql(sql, {**values, **changes})
            db.exec_driver_sql(sql, values)
            second = {**values, "id": 2, "rev": 2, "prev": 1, "prev_rev": 1, "key": "rule-2"}
            for changes in (dict(item=2), dict(prev_rev=None), dict(prev=None), dict(rev=3),
                            dict(prev=2), dict(key="rule-1")):
                with pytest.raises(IntegrityError):
                    db.exec_driver_sql(sql, {**second, **changes})
            db.exec_driver_sql(sql, second)
            product_sql = "INSERT INTO order_bom_rule_products(revision_id,product_id,order_item_id,product_version) VALUES(?,?,?,?)"
            for values in ((1,10,2,1), (1,999,1,1), (1,10,1,0)):
                with pytest.raises(IntegrityError):
                    db.exec_driver_sql(product_sql, values)
            db.exec_driver_sql(product_sql, (1,10,1,1))
            db.exec_driver_sql(product_sql, (1,20,1,1))
            source_sql = "INSERT INTO order_bom_rule_sources(snapshot_id,revision_id,product_id,order_item_id) VALUES(?,?,?,?)"
            # Same-order wrong product is also a database FK failure.
            for values in ((200,1,10,1), (100,1,20,1), (100,2,10,1)):
                with pytest.raises(IntegrityError):
                    db.exec_driver_sql(source_sql, values)
            db.exec_driver_sql(source_sql, (100,1,10,1))
            with pytest.raises(RuntimeError, match="已有BOM规则版本事实"):
                module.downgrade()
            assert db.exec_driver_sql("SELECT count(*) FROM order_bom_rule_revisions").scalar() == 2
            assert db.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    engine.dispose()


def test_v327_rule_migration_roundtrip_preserves_original_facts(factory_copy, monkeypatch):
    db = factory_copy
    target = Path(db.get_bind().url.database)
    source_backup = target.with_name("before-upgrade.sqlite3")
    original_hash = hashlib.sha256(source_backup.read_bytes()).hexdigest()
    with sqlite3.connect(source_backup.as_uri() + "?mode=ro", uri=True) as before:
        tables = [row[0] for row in before.execute("SELECT name FROM sqlite_master WHERE type='table'")
                  if row[0] not in {"alembic_version", "sqlite_sequence"}]
        columns = {table: [row[1] for row in before.execute(f'PRAGMA table_info("{table}")')] for table in tables}
        facts = original_facts(before, columns)
        triggers = dict(before.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
    db.rollback()
    config = _config(monkeypatch, target)
    assert ScriptDirectory.from_config(config).get_heads() == ["sn26v8x9z88"]
    for destination in ("sh20v8x9z82", "sn26v8x9z88"):
        if destination == "sh20v8x9z82":
            command.downgrade(config, destination)
        else:
            command.upgrade(config, destination)
        with sqlite3.connect(target.as_uri() + "?mode=ro", uri=True) as after:
            assert after.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert after.execute("PRAGMA foreign_key_check").fetchall() == []
            assert {row[0] for row in after.execute("SELECT version_num FROM alembic_version")} == (
                {destination} if destination == "sn26v8x9z88" else {destination, "rw10v8x9z71"})
            assert original_facts(after, columns) == facts
            actual = dict(after.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
            assert all(actual.get(name) == sql for name, sql in triggers.items())
    assert hashlib.sha256(source_backup.read_bytes()).hexdigest() == original_hash


def test_stored_rule_facts_refuse_downgrade_without_changing_bytes(factory_copy, monkeypatch):
    from app.models.multilevel_bom import OrderBomRuleRevision, OrderBomRuleProduct, OrderBomRuleSource
    from app.services.multilevel_bom_rule_impact import review_current_rule_requirements
    from app.services.multilevel_bom_rule_revision import prepare_rule_revision
    from tests.test_multilevel_bom_rule_impact import frozen_order

    db = factory_copy
    # Test the original 83 fact guard, before later empty schema is removed.
    command.downgrade(_config(monkeypatch, Path(db.get_bind().url.database)), "sl24v8x9z86")
    actor, item, frozen = frozen_order(db)
    review = review_current_rule_requirements(db, order_item_id=item.id, customer_id=frozen.graph.customer_id)
    db.add_all(review.proposed.snapshots)
    db.flush()
    document, checksum = prepare_rule_revision(frozen, review.proposed,
        order_quantity=100, delivered_before=0)
    row = OrderBomRuleRevision(order_item_id=item.id, revision=1, production_revision_before=0,
        order_quantity=100, delivered_before=0, document_json=document, content_hash=checksum,
        review_hash=review.checksum, request_hash="a" * 64, idempotency_key="migration-rule-facts", created_by=actor.id)
    db.add(row)
    db.flush()
    db.add_all([OrderBomRuleProduct(revision_id=row.id, order_item_id=item.id,
        product_id=node.product_id, product_version=node.version) for node in review.proposed.graph.nodes])
    db.flush()
    db.add_all([OrderBomRuleSource(snapshot_id=source.id, revision_id=row.id,
        product_id=source.component_product_id, order_item_id=item.id) for source in review.proposed.snapshots])
    db.commit()
    target = Path(db.get_bind().url.database)
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="已有BOM规则版本事实"):
        command.downgrade(_config(monkeypatch, target), "sh20v8x9z82")
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before
    with sqlite3.connect(target.as_uri() + "?mode=ro", uri=True) as check:
        assert check.execute("SELECT count(*) FROM order_bom_rule_revisions").fetchone() == (1,)
        assert check.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert check.execute("PRAGMA foreign_key_check").fetchall() == []
