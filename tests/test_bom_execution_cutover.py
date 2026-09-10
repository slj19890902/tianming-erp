from types import SimpleNamespace
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3

import pytest
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from app.models.product_bom import SalesOrderItemBomComponent as Snapshot
from app.services.multilevel_bom_execution_boundary import current_snapshot_predicate, execution_window
from app.services.multilevel_bom_plan import BomPlanError
from tests.test_external_graph_cost_migration import migration
from tests.test_p1_131_material_cost_lineage_migration import _config


@pytest.fixture
def boundary_db(tmp_path):
    module = migration("sd16v8x9z78_bom_execution_cutover.py")
    engine = create_engine(f"sqlite:///{tmp_path / 'boundary.sqlite3'}")
    with engine.begin() as db:
        db.exec_driver_sql("PRAGMA foreign_keys=ON")
        db.exec_driver_sql("CREATE TABLE order_bom_graphs(order_item_id INTEGER PRIMARY KEY)")
        db.exec_driver_sql("CREATE TABLE users(id INTEGER PRIMARY KEY)")
        db.exec_driver_sql("CREATE TABLE sales_order_item_bom_components(id INTEGER PRIMARY KEY, sales_order_item_id INTEGER NOT NULL)")
        db.exec_driver_sql("INSERT INTO order_bom_graphs VALUES (1),(2)")
        db.exec_driver_sql("INSERT INTO users VALUES (1)")
        db.exec_driver_sql("INSERT INTO sales_order_item_bom_components VALUES (11,1),(12,1),(13,1),(21,2)")
        with Operations.context(MigrationContext.configure(db)):
            module.upgrade()
            yield db, module
            assert db.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    engine.dispose()


INSERT = """INSERT INTO order_bom_execution_cutovers
    (order_item_id,order_quantity,delivered_before,basis_json,basis_hash,idempotency_key,request_hash,created_by)
    VALUES(?,?,?,?,?,?,?,?)"""


def add_boundary(db):
    db.exec_driver_sql(INSERT, (1,1800,1500,"{}","a"*64,"isolated-cutover","b"*64,1))


def test_migration_identity_constraints_and_nonempty_downgrade(boundary_db):
    db, module = boundary_db
    base = [1,1800,1500,"{}","a"*64,"isolated-cutover","b"*64,1]
    for index, invalid in [(0,999),(1,0),(2,-1),(2,1800),(4,"bad"),(5," "),(6,"bad"),(7,999)]:
        values = base.copy()
        values[index] = invalid
        with pytest.raises(IntegrityError):
            db.exec_driver_sql(INSERT, tuple(values))
    add_boundary(db)
    source_sql = "INSERT INTO order_bom_cutover_sources(snapshot_id,order_item_id,role) VALUES (?,?,?)"
    for row in [(21,1,"history"),(11,2,"current"),(999,1,"history"),(11,1,"guess")]:
        with pytest.raises(IntegrityError):
            db.exec_driver_sql(source_sql, row)
    db.exec_driver_sql(source_sql, (11,1,"history"))
    with pytest.raises(IntegrityError):
        db.exec_driver_sql(source_sql, (11,1,"current"))
    with pytest.raises(IntegrityError):
        db.exec_driver_sql("DELETE FROM sales_order_item_bom_components WHERE id=11")
    with pytest.raises(IntegrityError):
        db.exec_driver_sql("UPDATE sales_order_item_bom_components SET sales_order_item_id=2 WHERE id=11")
    with pytest.raises(RuntimeError, match="已有BOM执行转换记录"):
        module.downgrade()


def test_current_selection_is_explicit_and_history_remains_addressable(boundary_db):
    db, _ = boundary_db
    def current():
        return db.execute(select(Snapshot.id).where(current_snapshot_predicate()).order_by(Snapshot.id)).scalars().all()
    assert current() == [11,12,13,21]
    add_boundary(db)
    # An incomplete mapping cannot silently fall back to legacy rows.
    assert current() == [21]
    db.exec_driver_sql("INSERT INTO order_bom_cutover_sources VALUES (11,1,'history'),(12,1,'current'),(13,1,'current')")
    assert current() == [12,13,21]
    assert db.execute(select(Snapshot.id).where(Snapshot.id == 11)).scalar_one() == 11
    alias = aliased(Snapshot)
    assert db.execute(select(alias.id).where(current_snapshot_predicate(alias)).order_by(alias.id)).scalars().all() == [12,13,21]


def test_cutover_delivery_window_counts_only_remaining_execution():
    cutover = SimpleNamespace(order_quantity=1800, delivered_before=1500)
    start = execution_window(order_quantity=1800, delivered_quantity=1500, cutover=cutover)
    assert (start.commercial_quantity,start.delivered_before,start.execution_quantity,start.delivered_since,start.remaining_quantity) == (1800,1500,300,0,300)
    later = execution_window(order_quantity=1800, delivered_quantity=1600, cutover=cutover)
    assert (later.execution_quantity,later.delivered_since,later.remaining_quantity) == (300,100,200)
    over = execution_window(order_quantity=1800, delivered_quantity=1810, cutover=cutover)
    assert (over.delivered_since,over.remaining_quantity) == (310,0)
    normal = execution_window(order_quantity=1800, delivered_quantity=1500)
    assert (normal.delivered_before,normal.execution_quantity,normal.delivered_since) == (0,1800,1500)


@pytest.mark.parametrize("quantity,delivered,boundary", [
    (1801,1500,(1800,1500)), (1800,1499,(1800,1500)), (1800,1500,(1800,1800)),
    (1800,1500,(1800,-1)), (True,0,None), (1800,-1,None), (1800,True,None),
])
def test_invalid_or_cross_boundary_execution_is_rejected(quantity, delivered, boundary):
    cutover = SimpleNamespace(order_quantity=boundary[0], delivered_before=boundary[1]) if boundary else None
    with pytest.raises(BomPlanError):
        execution_window(order_quantity=quantity, delivered_quantity=delivered, cutover=cutover)


def test_fresh_isolated_upgrade_downgrade_upgrade_preserves_all_original_facts(monkeypatch, tmp_path):
    source = Path(os.environ.get("ERP_MULTILEVEL_UAT_SOURCE", "")).resolve()
    if source.name != "order-graph-source-isolated.sqlite3" or "tm-uat" not in source.parts:
        pytest.skip("explicit isolated source required")
    digest = hashlib.sha256(source.read_bytes()).digest()
    target, backup = tmp_path / "cutover.sqlite3", tmp_path / "before-upgrade.sqlite3"
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src, sqlite3.connect(target) as dest:
        src.backup(dest)
    shutil.copy2(target, backup)
    assert hashlib.sha256(backup.read_bytes()).digest() == hashlib.sha256(target.read_bytes()).digest()
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    config = _config(monkeypatch, target)
    assert ScriptDirectory.from_config(config).get_heads() == ["se17v8x9z79"]
    command.upgrade(config, "sc15v8x9z77")
    def facts():
        with sqlite3.connect(target) as db:
            assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
            tables = ("sales_orders","sales_order_items","sales_order_item_bom_components",
                "inventory_lots","inventory_movements","inventory_reservations","warehouse_locations",
                "sales_deliveries","sales_delivery_items","production_completions")
            return ({t:db.execute(f"SELECT * FROM {t} ORDER BY id").fetchall() for t in tables},
                db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").fetchall())
    before = facts()
    command.upgrade(config, "sd16v8x9z78")
    assert facts() == before
    command.downgrade(config, "sc15v8x9z77")
    assert facts() == before
    command.upgrade(config, "sd16v8x9z78")
    assert facts() == before
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT count(*) FROM order_bom_execution_cutovers").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM order_bom_cutover_sources").fetchone() == (0,)
    assert hashlib.sha256(source.read_bytes()).digest() == digest
