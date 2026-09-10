"""Real-product acceptance on disposable copies, never the factory database."""
from datetime import date
from decimal import Decimal
import hashlib
import os
from pathlib import Path
import sqlite3
import shutil

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.bom_subkit import ProductSubkit
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.user import User
from app.services.bom_subkits import save_subkit
from app.services.multilevel_bom_orders import freeze_master_order_bom
from app.services.multilevel_bom_plan import plan_bom
from tests.test_multilevel_bom_master import save


@pytest.fixture
def factory_copy(tmp_path, monkeypatch):
    source = os.environ.get("ERP_MULTILEVEL_UAT_SOURCE")
    if not source:
        pytest.skip("explicit isolated source required")
    path = Path(source).resolve()
    if path.name != "order-graph-source-isolated.sqlite3" or "tm-uat" not in path.parts:
        pytest.fail("only the prepared isolated source is allowed")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    # Real-copy stock tests must use the map paired with that export, rather
    # than silently falling back to the older map tracked in the repository.
    published_map = path.with_name("twin_layout_v1.json")
    if not published_map.is_file():
        pytest.fail("paired published twin_layout_v1.json is required beside the isolated source")
    map_before = hashlib.sha256(published_map.read_bytes()).hexdigest()
    map_target = tmp_path / "published-layout.json"
    shutil.copy2(published_map, map_target)
    from app.services import warehouse_twin_layout
    monkeypatch.setattr(warehouse_twin_layout, "TWIN_LAYOUT_RUNTIME_PATH", map_target)
    monkeypatch.setenv("ERP_TWIN_LAYOUT_RUNTIME_PATH", str(map_target))
    target = tmp_path / "multilevel-factory-test.sqlite3"
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as src, sqlite3.connect(target) as dest:
        src.backup(dest)
    backup = tmp_path / "before-upgrade.sqlite3"
    shutil.copy2(target, backup)
    assert hashlib.sha256(backup.read_bytes()).digest() == hashlib.sha256(target.read_bytes()).digest()
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    from alembic import command
    from tests.test_p1_131_material_cost_lineage_migration import _config
    command.upgrade(_config(monkeypatch, target), "head")
    engine = create_sqlite_engine(target)
    with Session(engine) as db:
        yield db
        db.rollback()
        assert db.scalar(text("PRAGMA integrity_check")) == "ok"
        assert db.execute(text("PRAGMA foreign_key_check")).all() == []
    engine.dispose()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert hashlib.sha256(published_map.read_bytes()).hexdigest() == map_before


def new_item(db, pid, qty):
    product = db.get(Product, pid)
    order = Order(order_number=f"ISOLATED-GRAPH-{pid}", customer_id=product.customer_id,
                  order_date=date(2026, 9, 9))
    db.add(order)
    db.flush()
    item = OrderItem(order_id=order.id, product_id=pid, quantity=qty,
        unit_price=Decimal("1"), subtotal=Decimal(qty), snapshot_product_name=product.product_name,
        composite_fulfillment_mode_snapshot="parent_delivery")
    db.add(item)
    db.flush()
    return item


def test_real_factory_liner_identity_and_00205_set_counts_without_inventory_changes(factory_copy):
    db = factory_copy
    inventory_before = db.execute(text("SELECT id,quantity_available,quantity_reserved,quantity_consumed,version FROM inventory_lots ORDER BY id")).all()
    actor = db.scalar(select(User).where(User.role == "admin", User.is_active.is_(True)))
    definition = db.get(ProductSubkit, 3765)
    assert definition is not None
    liner_id = definition.kit_product_id
    liner = db.get(Product, liner_id)
    save_subkit(db, parent_product_id=3765, name=liner.product_name, kits_per_parent=1,
        members=[{"product_id": 3788, "pieces_per_kit": 2}, {"product_id": 3789, "pieces_per_kit": 6}],
        expected_version=definition.version, actor=actor, enabled=False)
    save(db, actor, liner_id, "assembled", [(3788, 2, "assembly"), (3789, 6, "assembly")])
    save(db, actor, 3765, "manufactured", [(liner_id, 1, "accompany")])
    save(db, actor, 3799, "assembled", [(3771, 3, "assembly"), (3783, 4, "assembly")])
    item148 = new_item(db, 3765, 100)
    item205 = new_item(db, 3799, 300)
    frozen148 = freeze_master_order_bom(db, order_item_id=item148.id, actor=actor)
    frozen205 = freeze_master_order_bom(db, order_item_id=item205.id, actor=actor)
    db.commit()
    plan148 = plan_bom(frozen148.graph, 100)
    plan205 = plan_bom(frozen205.graph, 300)
    assert dict(plan148.picking) == {3765: 100, liner_id: 100}
    assert {m.product_id for m in plan148.materials} == {3765, 3788, 3789}
    assert dict(plan205.picking) == {3799: 300}
    assert {p.product_id: p.required_units for p in plan205.products} == {3799: 300, 3771: 900, 3783: 1200}
    assert {m.product_id: m.purchase_sheets for m in plan205.materials} == {3771: 225, 3783: 300}
    assert inventory_before == db.execute(text("SELECT id,quantity_available,quantity_reserved,quantity_consumed,version FROM inventory_lots ORDER BY id")).all()
    assert db.get(OrderItem, 10050).delivered_quantity == 1500


def test_inactive_00204_stays_protected_and_explicit_reactivation_uses_five_sets(factory_copy):
    """Existing 15/20-piece edges and the confirmed 3/4-piece set agree."""
    from app.models.product_bom import ProductBomComponent
    from app.services.multilevel_bom_master_transition import transition_master_boms
    from app.services.composite_bom import CompositeBOMError
    db = factory_copy
    actor = db.scalar(select(User).where(User.role == "admin", User.is_active.is_(True)))
    existing = {row.component_product_id: int(row.quantity_per_set) for row in db.scalars(
        select(ProductBomComponent).where(ProductBomComponent.parent_product_id == 3494))}
    assert existing == {3771: 15, 3783: 20, 3772: 6}
    original_inventory = db.execute(text("SELECT * FROM inventory_lots ORDER BY id")).all()
    ids = [3494, 3799, 3771, 3783, 3772]
    versions = {pid: db.get(Product, pid).version for pid in ids}
    changes = [
            {"parent_product_id":3799, "inventory_mode":"assembled", "components":[
                {"component_product_id":3771,"quantity_per_set":3,"inventory_relation":"assembly"},
                {"component_product_id":3783,"quantity_per_set":4,"inventory_relation":"assembly"}]},
            {"parent_product_id":3494, "inventory_mode":"manufactured", "components":[
                {"component_product_id":3799,"quantity_per_set":5,"inventory_relation":"accompany"},
                {"component_product_id":3772,"quantity_per_set":6,"inventory_relation":"accompany"}]}]
    assert db.get(Product,3494).is_active is False
    with pytest.raises(CompositeBOMError, match="失效产品"):
        transition_master_boms(db, customer_id=136, expected_versions=versions, disable_subkits={}, actor=actor, changes=changes)
    assert db.get(Product,3494).is_active is False
    # Explicit what-if fixture ONLY: never reactivate the actual factory master.
    db.get(Product,3494).is_active = True
    db.commit()
    transition_master_boms(db, customer_id=136, expected_versions=versions, disable_subkits={}, actor=actor, changes=changes)
    item = new_item(db, 3494, 10)
    graph = freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    plan = plan_bom(graph.graph, 10)
    assert dict(plan.picking) == {3494:10, 3799:50, 3772:60}
    amounts = {row.product_id: row.required_units for row in plan.products}
    assert amounts[3771] == 150 and amounts[3783] == 200
    assert {row.product_id for row in plan.materials} == {3494, 3771, 3783, 3772}
    assert db.execute(text("SELECT * FROM inventory_lots ORDER BY id")).all() == original_inventory
    assert db.get(OrderItem,10050).delivered_quantity == 1500
