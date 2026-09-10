import json

import pytest
from sqlalchemy import event, select, text

from app.models.order import OrderItem
from app.models.user import User
from app.models.product import Product
from app.models.warehouse_inventory import InventoryLot, InventoryReservation
from app.services.multilevel_bom_cutover_review import review_legacy_cutover
from app.services.multilevel_bom_plan import BomPlanError, plan_bom
from tests.test_multilevel_bom_factory_compile import factory_copy
from tests.test_multilevel_bom_master import save


def prepare(db):
    actor = db.scalar(select(User).where(User.role == "admin", User.is_active.is_(True)))
    save(db, actor, 3799, "assembled", [(3771, 3, "assembly"), (3783, 4, "assembly")])
    db.commit()


def test_real_00205_review_keeps_old_rows_and_plans_only_remaining(factory_copy):
    db = factory_copy
    prepare(db)
    names = ["sales_order_items", "sales_order_item_bom_components", "inventory_lots",
             "inventory_reservations", "inventory_movements", "warehouse_locations"]
    before = {name: db.execute(text(f"SELECT * FROM {name} ORDER BY id")).all() for name in names}
    statements = []
    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
    event.listen(db.bind, "before_cursor_execute", capture)
    try:
        review = review_legacy_cutover(db, order_item_id=10050, customer_id=136)
    finally:
        event.remove(db.bind, "before_cursor_execute", capture)
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    payload = json.loads(review.document)
    assert payload["item"]["quantity"] == 1800 and payload["item"]["delivered_quantity"] == 1500
    assert payload["remaining_quantity"] == 300
    assert {row.id for row in review.history} == {2, 3}
    assert all(row.id is None and row.product_bom_component_id is None for row in review.compiled.snapshots)
    assert {row.display_order for row in review.compiled.snapshots}.isdisjoint(row.display_order for row in review.history)
    assert dict(plan_bom(review.compiled.graph, 300).picking) == {3799: 300}
    assert {row.component_product_id: row.required_piece_quantity for row in review.compiled.snapshots} == {
        3799: 300, 3771: 900, 3783: 1200}
    assert {row["id"] for row in payload["reservations"]} == {334, 335}
    assert all(row["warehouse_location_id"] == 656 for row in payload["lots"])
    costs = payload["remaining_finished_costs"]
    assert {row["lot_id"]: row["quantity"] for row in costs} == {365: 900, 366: 1200}
    assert all(row["lineage"]["actual"] is False for row in costs)
    assert {name: db.execute(text(f"SELECT * FROM {name} ORDER BY id")).all() for name in names} == before
    db.expire_all()
    assert review_legacy_cutover(db, order_item_id=10050, customer_id=136).checksum == review.checksum


@pytest.mark.parametrize("change", ["lot_version", "reservation", "master"])
def test_review_detects_relevant_facts_changed(factory_copy, change):
    db = factory_copy
    prepare(db)
    before = review_legacy_cutover(db, order_item_id=10050, customer_id=136)
    if change == "lot_version":
        db.get(InventoryLot, 365).version += 1
    elif change == "reservation":
        db.get(InventoryReservation, 334).release_reason = "changed review fact"
    else:
        db.get(Product, 3771).version += 1
    db.commit()
    assert review_legacy_cutover(db, order_item_id=10050, customer_id=136).checksum != before.checksum


def test_review_rejects_wrong_customer_and_unsaved_changes(factory_copy):
    db = factory_copy
    prepare(db)
    with pytest.raises(BomPlanError, match="客户"):
        review_legacy_cutover(db, order_item_id=10050, customer_id=999)
    item = db.get(OrderItem, 10050)
    item.snapshot_product_name = "unsaved"
    with pytest.raises(BomPlanError, match="未提交"):
        review_legacy_cutover(db, order_item_id=10050, customer_id=136)
    assert item in db.dirty
