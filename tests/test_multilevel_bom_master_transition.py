import copy

import pytest
from sqlalchemy import select, text, func

from app.models.audit import OperationLog
from app.models.product import Product
from app.models.user import User
from app.models.multilevel_bom import ProductBomProfile
from app.services.composite_bom import CompositeBOMError, get_product_bom
from app.services.multilevel_bom_master_transition import transition_master_boms
from tests.test_multilevel_bom_orders import context
from tests.test_multilevel_bom_factory_compile import factory_copy


def change(pid, mode, rows):
    return {"parent_product_id": pid, "inventory_mode": mode, "components": [
        {"component_product_id": child, "quantity_per_set": qty, "inventory_relation": relation}
        for child, qty, relation in rows]}


def simple(db, actor):
    return dict(customer_id=136, actor=actor, disable_subkits={},
        expected_versions={pid: db.get(Product, pid).version for pid in [1, 2, 3, 4]},
        changes=[change(2, "assembled", [(3, 2, "assembly"), (4, 6, "assembly")]),
                 change(1, "manufactured", [(2, 1, "accompany")])])


def test_atomic_transition_and_stale_replay(context):
    db, actor, item, _ = context
    payload = simple(db, actor)
    before = db.execute(text("SELECT * FROM sales_order_items")).all()
    result = transition_master_boms(db, **payload)
    db.commit()
    assert result["order_inventory_conversion"] is False
    assert get_product_bom(db, 1)["components"][0]["component_product_id"] == 2
    assert db.execute(text("SELECT * FROM sales_order_items")).all() == before
    count = db.scalar(select(func.count()).select_from(OperationLog))
    with pytest.raises(CompositeBOMError, match="版本已变化"):
        transition_master_boms(db, **payload)
    db.commit()
    assert db.scalar(select(func.count()).select_from(OperationLog)) == count


@pytest.mark.parametrize("fault", ["late_validation", "audit", "outer_rollback"])
def test_all_changes_rollback_together(context, monkeypatch, fault):
    import app.services.multilevel_bom_master_transition as service
    db, actor, _, _ = context
    payload = simple(db, actor)
    before = db.execute(text("SELECT * FROM products ORDER BY id")).all()
    logs = db.scalar(select(func.count()).select_from(OperationLog))
    if fault == "late_validation":
        payload["changes"][1]["components"][0]["quantity_per_set"] = 0
        with pytest.raises(CompositeBOMError):
            transition_master_boms(db, **payload)
        db.commit()
    elif fault == "audit":
        def fail(*args, **kwargs):
            raise RuntimeError("injected audit failure")
        monkeypatch.setattr(service, "append_audit_event", fail)
        with pytest.raises(RuntimeError, match="audit failure"):
            transition_master_boms(db, **payload)
        db.commit()
    else:
        transition_master_boms(db, **payload)
        db.rollback()
    assert db.execute(text("SELECT * FROM products ORDER BY id")).all() == before
    assert db.scalar(select(func.count()).select_from(ProductBomProfile)) == 0
    assert db.scalar(select(func.count()).select_from(OperationLog)) == logs


@pytest.mark.parametrize("bad", ["employee", "inactive", "customer", "leaf_version", "missing_leaf", "duplicate"])
def test_scope_and_version_guards(context, bad):
    db, actor, _, _ = context
    payload = simple(db, actor)
    if bad == "employee":
        actor.role = "workshop"
    elif bad == "inactive":
        actor.is_active = False
    elif bad == "customer":
        payload["customer_id"] = 999
    elif bad == "leaf_version":
        payload["expected_versions"][4] += 1
    elif bad == "missing_leaf":
        del payload["expected_versions"][4]
    else:
        payload["changes"].append(copy.deepcopy(payload["changes"][0]))
    db.commit()
    with pytest.raises(CompositeBOMError):
        transition_master_boms(db, **payload)
    db.commit()
    assert db.scalar(select(func.count()).select_from(ProductBomProfile)) == 0


def factory_payload(db, actor):
    return dict(customer_id=136, actor=actor, disable_subkits={3765: 1},
        expected_versions={pid: db.get(Product, pid).version
            for pid in [3765, 3822, 3788, 3789, 3799, 3771, 3783]},
        changes=[change(3822, "assembled", [(3788, 2, "assembly"), (3789, 6, "assembly")]),
            change(3765, "manufactured", [(3822, 1, "accompany")]),
            change(3799, "assembled", [(3771, 3, "assembly"), (3783, 4, "assembly")])])


def business_rows(db):
    tables = ["sales_order_items", "sales_order_item_bom_components", "inventory_lots",
              "inventory_movements", "warehouse_locations", "production_completions",
              "inventory_reservations", "sales_deliveries", "sales_delivery_items"]
    return {table: db.execute(text(f"SELECT * FROM {table} ORDER BY id")).all() for table in tables}


def test_real_factory_batch_preserves_all_order_stock_and_location_rows(factory_copy):
    from app.models.bom_subkit import ProductSubkit
    db = factory_copy
    actor = db.scalar(select(User).where(User.role == "admin", User.is_active.is_(True)))
    before = business_rows(db)
    payload = factory_payload(db, actor)
    transition_master_boms(db, **payload)
    db.commit()
    assert business_rows(db) == before
    assert not db.get(ProductSubkit, 3765).enabled
    assert db.get(Product, 3799).unit == "套"
    assert get_product_bom(db, 3765)["components"][0]["component_product_id"] == 3822
    assert [r["quantity_per_set"] for r in get_product_bom(db, 3799)["components"]] == [3, 4]
    with pytest.raises(CompositeBOMError, match="版本已变化"):
        transition_master_boms(db, **payload)
    from tests.test_multilevel_bom_factory_compile import new_item
    from app.services.multilevel_bom_orders import freeze_master_order_bom
    from app.services.multilevel_bom_plan import plan_bom
    for pid, qty, picking in [(3765, 100, {3765: 100, 3822: 100}), (3799, 300, {3799: 300})]:
        item = new_item(db, pid, qty)
        frozen = freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
        plan = plan_bom(frozen.graph, qty)
        assert dict(plan.picking) == picking
        if pid == 3799:
            assert {p.product_id: p.required_units for p in plan.products} == {3799: 300, 3771: 900, 3783: 1200}
    db.commit()


def test_referenced_old_master_edge_cannot_be_silently_removed(factory_copy):
    from app.models.user import User
    db = factory_copy
    actor = db.scalar(select(User).where(User.role == "admin", User.is_active.is_(True)))
    payload = factory_payload(db, actor)
    payload["changes"][2]["components"].pop()
    before = business_rows(db)
    master = db.execute(text("SELECT * FROM products ORDER BY id")).all()
    with pytest.raises(CompositeBOMError, match="历史订单引用"):
        transition_master_boms(db, **payload)
    db.commit()
    assert business_rows(db) == before
    assert db.execute(text("SELECT * FROM products ORDER BY id")).all() == master


def test_real_sidecar_disable_and_all_master_writes_rollback_on_last_write(factory_copy, monkeypatch):
    import app.services.multilevel_bom_master_transition as service
    db = factory_copy
    actor = db.scalar(select(User).where(User.role == "admin", User.is_active.is_(True)))
    payload = factory_payload(db, actor)
    tables = ["products", "product_subkits", "product_bom_components", "operation_logs"]
    def master_rows():
        return {table: db.execute(text(f"SELECT * FROM {table}")).all() for table in tables}
    before, master_before = business_rows(db), master_rows()
    original = service.replace_product_bom
    def fail_last(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs["parent_product_id"] == 3799:
            raise RuntimeError("after last real master write")
        return result
    monkeypatch.setattr(service, "replace_product_bom", fail_last)
    with pytest.raises(RuntimeError, match="last real master"):
        transition_master_boms(db, **payload)
    db.commit()
    assert business_rows(db) == before
    assert master_rows() == master_before
    monkeypatch.setattr(service, "replace_product_bom", original)
    transition_master_boms(db, **payload)
    db.commit()
    assert business_rows(db) == before
