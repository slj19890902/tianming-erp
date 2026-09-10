"""An isolated what-if shape; 00205's real target remains assembled, not separate."""
from sqlalchemy import select, text
import pytest

from app.models.product import Product
from app.models.order import OrderItem
from app.models.warehouse_inventory import InventoryLot, InventoryReservation
from app.services.composite_bom import replace_product_bom
from app.services.multilevel_bom_requirements import read_graph_requirements
from tests.test_bom_cutover_api import client, URL
from tests.test_multilevel_bom_factory_compile import factory_copy


def configure_matching_shape(db, actor, matching_fixture=True):
    if matching_fixture:
        # Synthetic matching scenario only. The unmodified export has a process
        # difference and is separately proven to refuse this different mode.
        from app.models.product_bom import SalesOrderItemBomComponent
        for old in db.scalars(select(SalesOrderItemBomComponent).where(
                SalesOrderItemBomComponent.sales_order_item_id == 10050)):
            db.get(Product, old.component_product_id).production_process = old.snapshot_component_production_process
        db.commit()
    replace_product_bom(db, parent_product_id=3799, expected_version=db.get(Product, 3799).version,
        user=actor, inventory_mode="separate", material_mode="expand_children", delivery_mode="components",
        components=[dict(component_product_id=3771, quantity_per_set=3, inventory_relation="accompany"),
                    dict(component_product_id=3783, quantity_per_set=4, inventory_relation="accompany")])
    db.commit()


@pytest.mark.parametrize("matching_fixture", [True, False])
def test_full_reserved_legacy_shape_retains_parts_without_stock_creation(client, matching_fixture):
    http, db, actor = client
    configure_matching_shape(db, actor, matching_fixture)
    original = db.execute(text("SELECT id,warehouse_location_id,quantity_available,quantity_reserved,quantity_consumed FROM inventory_lots ORDER BY id")).all()
    snapshots = db.execute(text("SELECT * FROM sales_order_item_bom_components WHERE sales_order_item_id=10050 ORDER BY id")).all()
    preview = http.post(URL + "/preview", json={})
    if not matching_fixture:
        assert preview.status_code == 409 and "production_process" in preview.json()["detail"]
        assert db.execute(text("SELECT id,warehouse_location_id,quantity_available,quantity_reserved,quantity_consumed FROM inventory_lots ORDER BY id")).all() == original
        return
    assert preview.status_code == 200, preview.text
    data = preview.json()
    assert data["ready"] and data["inventory_mode"] == "separate" and data["outputs"] == []
    body = {key: data[key] for key in ("target_locations", "source_lot_versions", "reviewed_hash", "preview_hash")}
    body["operation_key"] = "isolated-retain-parts"
    saved = http.post(URL + "/execute", json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["output_lot_ids"] == saved.json()["assembly_ids"] == []
    assert http.post(URL + "/execute", json=body).json() == saved.json()
    assert db.execute(text("SELECT id,warehouse_location_id,quantity_available,quantity_reserved,quantity_consumed FROM inventory_lots ORDER BY id")).all() == original
    assert db.execute(text("SELECT * FROM sales_order_item_bom_components WHERE id IN (2,3) ORDER BY id")).all() == snapshots
    db.expire_all()
    assert db.get(OrderItem, 10050).delivered_quantity == 1500
    requirements = read_graph_requirements(db, 10050)
    assert requirements.finished_units == {3799: 0, 3771: 900, 3783: 1200}
    assert sum(row.purchase_sheets for row in requirements.plan.materials) == 0
    new = list(db.scalars(select(InventoryReservation).where(InventoryReservation.idempotency_key.like("isolated-retain-parts:retain:%"))))
    assert sorted(r.reserved_stock_quantity for r in new) == [900, 1200]
    assert [db.get(InventoryLot, lid).quantity_consumed for lid in (365, 366)] == [4500, 6000]


def test_separate_switch_audit_failure_restores_every_fact(client, monkeypatch):
    from tests.test_bom_cutover_writer import facts
    from app.services import multilevel_bom_cutover as service
    http, db, actor = client
    configure_matching_shape(db, actor)
    preview = http.post(URL + "/preview", json={})
    assert preview.status_code == 200, preview.text
    body = {key: preview.json()[key] for key in ("target_locations", "source_lot_versions", "reviewed_hash", "preview_hash")}
    body["operation_key"] = "isolated-retain-audit-retry"
    before = facts(db)
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise RuntimeError("isolated retained-stock audit failure")
        patch.setattr(service, "append_audit_event", fail)
        assert http.post(URL + "/execute", json=body).status_code == 500
    assert facts(db) == before
    saved = http.post(URL + "/execute", json=body)
    assert saved.status_code == 200, saved.text
