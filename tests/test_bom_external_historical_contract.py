"""Historical readers only: fixture revision is not an authorized PO handoff."""
import pytest
from sqlalchemy import select

from app.models.user import User
from app.models.product import Product
from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem, ExternalPackagingReceiptItem
from app.services.composite_bom import get_product_bom
from app.services.multilevel_bom_external_freeze import freeze_order_procurement
from app.services.multilevel_bom_external_costs import receipt_output_cost
from app.services.multilevel_bom_external_identity import read_external_node, read_external_source_contract
from app.services.multilevel_bom_orders import read_compiled_order_bom, read_order_bom_source_contract
from app.services.multilevel_bom_plan import BomPlanError
from tests.test_bom_other_products_acceptance import factory_http
from tests.test_multilevel_bom_factory_compile import factory_copy, new_item
from tests.test_multilevel_bom_master import save
from tests.test_bom_rule_history_read import store_fixture_revision
from tests.test_p1_33c5_external_packaging_receiving import _confirm
from tests.test_multilevel_bom_external_receipts import receive


def test_removed_external_node_keeps_actual_receipt_cost_under_original_contract(factory_http):
    client, db = factory_http
    actor = db.scalar(select(User).where(User.role == "admin", User.is_active.is_(True)))
    rows = [(r["component_product_id"], int(r["quantity_per_set"]), "accompany")
        for r in get_product_bom(db, 3479)["components"]]
    purchased = {pid for pid, _, _ in rows if db.get(Product, pid).supply_mode == "external_purchase"}
    assert purchased
    save(db, actor, 3479, "manufactured", rows)
    item = new_item(db, 3479, 2)
    original = freeze_order_procurement(db, order_item_id=item.id, actor=actor)
    db.commit()
    _confirm(client, item.order_id)
    db.expire_all()
    purchases = list(db.scalars(select(ExternalPackagingPurchaseItem).where(
        ExternalPackagingPurchaseItem.sales_order_item_id == item.id)))
    assert purchases
    for line in purchases:
        response = receive(client, line.purchase_order_id, line.id,
            f"historical-cost-{line.id}", line.purchase_quantity)
        assert response.status_code == 200, response.text
    db.expire_all()
    receipts = list(db.scalars(select(ExternalPackagingReceiptItem).where(
        ExternalPackagingReceiptItem.purchase_item_id.in_([line.id for line in purchases]))))
    before = {row.id: receipt_output_cost(db, row.id) for row in receipts}
    assert before and all(detail["actual"] for detail in before.values())
    save(db, actor, 3479, "manufactured", [row for row in rows if row[0] not in purchased])
    db.commit()
    # Explicit storage fixture to exercise historical evidence. The operational
    # cutover writer must still reject an order with these procurement facts.
    revision, current_ids = store_fixture_revision(db, actor, item)
    current = read_compiled_order_bom(db, item.id)
    assert {s.id for s in current.snapshots} == current_ids
    assert not purchased & {n.product_id for n in current.graph.nodes}
    for line in purchases:
        link, historical = read_external_source_contract(db, line.order_component_id)
        assert {s.id for s in historical.snapshots} == {s.id for s in original.snapshots}
        assert link.product_id in purchased
        with pytest.raises(BomPlanError):
            read_external_node(db, line.order_component_id)
    assert {row.id: receipt_output_cost(db, row.id) for row in receipts} == before
    with pytest.raises(BomPlanError, match="不属于"):
        read_order_bom_source_contract(db, item.id, 999999999)
    revision.content_hash = "0" * 64
    db.commit()
    with pytest.raises(BomPlanError):
        receipt_output_cost(db, receipts[0].id)
