import pytest
from sqlalchemy import select, event

from app.models.multilevel_bom import (
    OrderBomGraph, OrderBomGraphProduct, OrderBomRuleRevision, OrderBomRuleProduct, OrderBomRuleSource,
)
from app.models.product_bom import SalesOrderItemBomComponent
from app.services.multilevel_bom_orders import read_compiled_order_bom, read_order_graph
from app.services.multilevel_bom_rule_impact import review_current_rule_requirements
from app.services.multilevel_bom_rule_revision import prepare_rule_revision
from app.services.multilevel_bom_production_versions import project_complete_order_material_rows
from app.services.multilevel_bom_execution_boundary import current_snapshot_predicate
from app.services.multilevel_bom_delivery_page import project_page_graph_demands, summary_graph_contracts
from app.services.multilevel_bom_plan import BomPlanError, plan_bom
from tests.test_multilevel_bom_factory_compile import factory_copy
from tests.test_multilevel_bom_rule_impact import frozen_order
from tests.test_multilevel_bom_master import save
from tests.test_multilevel_bom_delivery_page import inputs
from tests.test_bom_other_products_acceptance import factory_http


def store_fixture_revision(db, actor, item):
    """Storage fixture only; this is not the future authorized cutover writer."""
    review = review_current_rule_requirements(db, order_item_id=item.id, customer_id=item.order.customer_id)
    previous = db.scalar(select(OrderBomRuleRevision).where(OrderBomRuleRevision.order_item_id == item.id)
        .order_by(OrderBomRuleRevision.revision.desc()))
    db.add_all(review.proposed.snapshots)
    db.flush()
    document, checksum = prepare_rule_revision(review.previous, review.proposed,
        order_quantity=item.quantity, delivered_before=item.delivered_quantity or 0)
    row = OrderBomRuleRevision(order_item_id=item.id, revision=previous.revision + 1 if previous else 1,
        previous_id=previous.id if previous else None, previous_revision=previous.revision if previous else None,
        production_revision_before=0, order_quantity=item.quantity, delivered_before=item.delivered_quantity or 0,
        document_json=document, content_hash=checksum, review_hash=review.checksum, request_hash="a" * 64,
        idempotency_key=f"rule-fixture-{item.id}-{checksum[:12]}", created_by=actor.id)
    db.add(row)
    db.flush()
    for node in review.proposed.graph.nodes:
        db.add(OrderBomRuleProduct(revision_id=row.id, order_item_id=item.id,
            product_id=node.product_id, product_version=node.version))
        if db.get(OrderBomGraphProduct, (item.id, node.product_id)) is None:
            db.add(OrderBomGraphProduct(order_item_id=item.id, product_id=node.product_id,
                product_version=node.version))
    db.flush()
    db.add_all([OrderBomRuleSource(snapshot_id=source.id, revision_id=row.id,
        order_item_id=item.id, product_id=source.component_product_id) for source in review.proposed.snapshots])
    db.commit()
    return row, {source.id for source in review.proposed.snapshots}


def assert_readers_agree(db, item, expected_ids, history_ids):
    compiled = read_compiled_order_bom(db, item.id)
    assert {row.id for row in compiled.snapshots} == expected_ids
    assert compiled.history_source_ids == history_ids
    assert read_order_graph(db, item.id) == compiled.graph
    all_rows = list(db.scalars(select(SalesOrderItemBomComponent).where(
        SalesOrderItemBomComponent.sales_order_item_id == item.id)))
    assert {row.id for row in project_complete_order_material_rows(db, all_rows)} == expected_ids
    current_sql = set(db.scalars(select(SalesOrderItemBomComponent.id).where(
        SalesOrderItemBomComponent.sales_order_item_id == item.id, current_snapshot_predicate())))
    assert current_sql == expected_ids
    payload = inputs(db, [item])
    queries = []
    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        queries.append(statement)
    event.listen(db.bind, "before_cursor_execute", capture)
    try:
        roots = project_page_graph_demands(db, **payload)
    finally:
        event.remove(db.bind, "before_cursor_execute", capture)
    assert len(queries) == 5  # identity, production, rules, rule products, rule sources
    summary = summary_graph_contracts(db, [item.id])
    assert summary.roots == roots
    assert summary.history_ids == history_ids
    assert not set(summary.picks) & history_ids
    return compiled


def test_primary_material_and_delivery_readers_use_latest_sources(factory_copy):
    db = factory_copy
    actor, item, original = frozen_order(db)
    original_ids = {row.id for row in original.snapshots}
    original_header = db.get(OrderBomGraph, item.id).document_json
    save(db, actor, 3799, "assembled", [(3771, 4, "assembly"), (3783, 4, "assembly")])
    db.commit()
    _, first_ids = store_fixture_revision(db, actor, item)
    current = assert_readers_agree(db, item, first_ids, original_ids)
    assert next(row for row in current.snapshots if row.component_product_id == 3771).required_piece_quantity == 400
    save(db, actor, 3799, "assembled", [(3771, 2, "assembly"), (3783, 4, "assembly")])
    db.commit()
    _, latest_ids = store_fixture_revision(db, actor, item)
    current = assert_readers_agree(db, item, latest_ids, original_ids | first_ids)
    assert next(row for row in current.snapshots if row.component_product_id == 3771).required_piece_quantity == 200
    assert db.get(OrderBomGraph, item.id).document_json == original_header
    assert next(row for row in original.snapshots if row.component_product_id == 3771).required_piece_quantity == 300


def test_new_multilevel_products_extend_registry_without_replacing_old_identity(factory_copy):
    db = factory_copy
    actor, item, original = frozen_order(db)
    original_registry = {(row.product_id, row.product_version) for row in db.scalars(
        select(OrderBomGraphProduct).where(OrderBomGraphProduct.order_item_id == item.id))}
    save(db, actor, 3822, "assembled", [(3788, 2, "assembly"), (3789, 6, "assembly")])
    save(db, actor, 3799, "assembled", [(3771, 3, "assembly"), (3783, 4, "assembly"), (3822, 1, "assembly")])
    db.commit()
    _, current_ids = store_fixture_revision(db, actor, item)
    current = assert_readers_agree(db, item, current_ids, {row.id for row in original.snapshots})
    demand = {row.product_id: row.required_units for row in plan_bom(current.graph, 100).products}
    assert demand[3822] == 100 and demand[3788] == 200 and demand[3789] == 600
    actual_registry = {(row.product_id, row.product_version) for row in db.scalars(
        select(OrderBomGraphProduct).where(OrderBomGraphProduct.order_item_id == item.id))}
    assert original_registry < actual_registry
    db.get(OrderBomGraphProduct, (item.id, 3822)).product_version += 1
    db.commit()
    with pytest.raises(BomPlanError, match="登记不一致"):
        read_compiled_order_bom(db, item.id)


@pytest.mark.parametrize("damage", ["source", "hash", "product"])
def test_partial_rule_facts_never_reactivate_old_sources(factory_copy, damage):
    db = factory_copy
    actor, item, _ = frozen_order(db)
    row, sources = store_fixture_revision(db, actor, item)
    if damage == "source":
        db.delete(db.get(OrderBomRuleSource, min(sources)))
    elif damage == "hash":
        row.content_hash = "0" * 64
    else:
        db.get(OrderBomRuleProduct, (row.id, 3799)).product_version += 1
    db.commit()
    with pytest.raises(BomPlanError):
        read_compiled_order_bom(db, item.id)
    with pytest.raises(BomPlanError):
        project_page_graph_demands(db, **inputs(db, [item]))


def test_revised_source_ids_continue_through_real_receipt_delivery_and_cancel(factory_http):
    """API execution after a storage fixture, not administrator UI acceptance."""
    from tests.test_bom_other_products_acceptance import paper_receipt_flow
    client, db = factory_http
    actor, item, original = frozen_order(db, quantity=2)
    old_ids = {row.id for row in original.snapshots}
    save(db, actor, 3799, "assembled", [(3771, 4, "assembly"), (3783, 4, "assembly")])
    db.commit()
    _, current_ids = store_fixture_revision(db, actor, item)
    current = read_compiled_order_bom(db, item.id)
    paper_receipt_flow(client, db, current, item.id, 3799)
    after = read_compiled_order_bom(db, item.id)
    assert {row.id for row in after.snapshots} == current_ids
    assert after.history_source_ids == old_ids
    assert next(row for row in original.snapshots if row.component_product_id == 3771).required_piece_quantity == 6


def test_rule_only_history_has_the_same_cancellation_boundary_as_legacy_cutover(factory_copy):
    """Boundary storage fixture, not evidence of an operational partial handoff."""
    from app.services.multilevel_bom_delivery_boundary import validate_cancel_execution_boundary
    db = factory_copy
    actor, item, _ = frozen_order(db)
    item.delivered_quantity = 20
    db.commit()
    store_fixture_revision(db, actor, item)
    item.delivered_quantity = 25
    db.commit()
    validate_cancel_execution_boundary(db, item=item, delivery_item_ids=[], quantity=5)
    with pytest.raises(BomPlanError, match="跨越"):
        validate_cancel_execution_boundary(db, item=item, delivery_item_ids=[], quantity=6)
    assert item.delivered_quantity == 25


def test_actual_delivery_pick_sources_remain_on_original_rule_after_fixture_switch(factory_http):
    """Actual receipt/delivery, then storage fixture: not operational handoff UAT."""
    from app.api.deliveries import _composite_inventory_sources_for_order_item
    from app.models.delivery import DeliveryItem
    from app.services.composite_bom_workflow import _delivery_graph_root_snapshot, delivery_item_component_quantities
    from tests.test_bom_other_products_acceptance import paper_receipt_flow
    client, db = factory_http
    actor, item, original = frozen_order(db, quantity=2, explicit_modes=True)
    did = paper_receipt_flow(client, db, original, item.id, 3799, stop_after_first_delivery=True)
    line = db.scalar(select(DeliveryItem).where(DeliveryItem.delivery_id == did))
    def pick_sources():
        return _composite_inventory_sources_for_order_item(db, order_item=item,
            planned_delivery_quantity=1, delivery_item_id=line.id, dispatched=True)
    before = pick_sources()
    assert before and sum(row["quantity_to_pick_requirement"] for row in before) == 1
    old_root = _delivery_graph_root_snapshot(db, line.id)
    old_quantities = delivery_item_component_quantities(db, line.id)
    assert old_quantities == {old_root: 1}
    from app.services.delivery_goods_projection import delivery_component_lines, customer_document_fulfillment_mode
    def document_lines():
        return delivery_component_lines(db, order_item=item, planned_delivery_quantity=1,
            delivery_item_id=line.id, dispatched=True)
    before_lines = document_lines()
    assert before_lines and {row["bom_delivery_mode"] for row in before_lines} == {"parent_delivery"}
    from app.api.deliveries import _delivery_list_page_context, _delivery_response
    def list_item():
        return _delivery_response(db, did, list_context=_delivery_list_page_context(db, [did]))["items"][0]
    before_list = list_item()
    from app.services.composite_bom import replace_product_bom
    from app.models.product import Product
    replace_product_bom(db, parent_product_id=3799, expected_version=db.get(Product, 3799).version,
        user=actor, inventory_mode="separate", material_mode="expand_children", delivery_mode="components",
        components=[dict(component_product_id=pid, quantity_per_set=quantity, inventory_relation="accompany")
                    for pid, quantity in [(3771, 3), (3783, 4)]])
    db.commit()
    revision, new_ids = store_fixture_revision(db, actor, item)
    assert old_root not in new_ids
    payload = inputs(db, [item])
    roots = project_page_graph_demands(db, **payload)
    assert roots[item.id] in new_ids
    assert {d.component_product_id for d in payload["demands"][item.id]} == {3771, 3783}
    assert all(d.frozen_delivery_mode == "component_delivery" for d in payload["demands"][item.id])
    assert pick_sources() == before
    assert _delivery_graph_root_snapshot(db, line.id) == old_root
    assert delivery_item_component_quantities(db, line.id) == old_quantities
    assert document_lines() == before_lines
    assert customer_document_fulfillment_mode(frozen_order_mode="component_delivery",
        current_product_mode="component_delivery", component_lines=document_lines()) == "parent_delivery"
    after_list = list_item()
    assert after_list["component_lines"] == before_list["component_lines"]
    assert after_list["actual_goods_lines"] == before_list["actual_goods_lines"]
    assert after_list["inventory_sources"] == before_list["inventory_sources"]
    revision.content_hash = "0" * 64
    db.commit()
    with pytest.raises(BomPlanError):
        pick_sources()


def test_manufactured_parent_old_reservation_is_not_credited_to_new_rule(factory_http):
    """Real output/delivery, fixture revision; original stock stays original."""
    from app.models.user import User
    from app.models.warehouse_inventory import InventoryReservation
    from app.services.multilevel_bom_orders import freeze_master_order_bom
    from app.services.composite_bom_workflow import _delivered_component_quantity, _stock_reservations
    from tests.test_multilevel_bom_factory_compile import new_item
    from tests.test_bom_other_products_acceptance import paper_receipt_flow
    client, db = factory_http
    actor = db.scalar(select(User).where(User.role == "admin", User.is_active.is_(True)))
    save(db, actor, 3479, "manufactured", [])
    item = new_item(db, 3479, 2)
    original = freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    db.commit()
    old_root = original.snapshots[0].id
    did = paper_receipt_flow(client, db, original, item.id, 3479, stop_after_first_delivery=True)
    assert _delivered_component_quantity(db, old_root) == 1
    reservations = list(db.scalars(select(InventoryReservation).where(
        InventoryReservation.order_item_id == item.id,
        InventoryReservation.reservation_type == "finished_order")))
    assert reservations and all(row.sales_order_item_bom_component_id is None for row in reservations)
    _, new_ids = store_fixture_revision(db, actor, item)
    new_root, = new_ids
    assert _delivered_component_quantity(db, old_root) == 1
    assert _delivered_component_quantity(db, new_root) == 0
    assert not _stock_reservations(db, new_root)
    from app.api.deliveries import _delivery_list_page_context
    page = _delivery_list_page_context(db, [did])
    assert page["component_delivered_by_snapshot"].get(new_root, 0) == 0
    assert page["component_delivered_by_snapshot"][old_root] == 1
    assert not page["reservations_by_snapshot"].get(new_root)
    assert page["reservations_by_snapshot"][old_root]
    from app.services.multilevel_bom_receipts import NodeReceiptContext, node_completed_quantity, own_output_lots
    current = read_compiled_order_bom(db, item.id)
    context = NodeReceiptContext(current, current.graph.nodes[0], current.snapshots[0])
    assert node_completed_quantity(db, context) == 0
    assert not own_output_lots(db, item.id)
    from app.services.multilevel_bom_requirements import read_graph_requirements
    assert read_graph_requirements(db, item.id).finished_units[3479] == 0


def test_actual_assembly_reversal_uses_original_rule_after_fixture_switch(factory_http):
    import json
    from app.models.multilevel_bom import BomAssembly, BomAssemblyInput
    from app.models.warehouse_inventory import InventoryLot
    from app.models.product import Product
    from app.services.composite_bom import replace_product_bom
    from app.services.multilevel_bom_inventory import reverse_order_assembly
    from tests.test_bom_other_products_acceptance import paper_receipt_flow
    client, db = factory_http
    actor, item, original = frozen_order(db, quantity=2, explicit_modes=True)
    did = paper_receipt_flow(client, db, original, item.id, 3799, stop_after_first_delivery=True)
    cancelled = client.put(f"/api/deliveries/{did}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    db.expire_all()
    rows = list(db.scalars(select(BomAssembly).where(BomAssembly.order_item_id == item.id).order_by(BomAssembly.id.desc())))
    assert sum(row.quantity for row in rows) == 2
    costs = {row.id: row.cost_detail_json for row in rows}
    input_ids = set(db.scalars(select(BomAssemblyInput.lot_id).where(
        BomAssemblyInput.conversion_id.in_([row.id for row in rows]))))
    replace_product_bom(db, parent_product_id=3799, expected_version=db.get(Product, 3799).version,
        user=actor, inventory_mode="separate", material_mode="expand_children", delivery_mode="components",
        components=[dict(component_product_id=pid, quantity_per_set=quantity, inventory_relation="accompany")
                    for pid, quantity in [(3771, 3), (3783, 4)]])
    db.commit()
    store_fixture_revision(db, actor, item)
    from app.services.multilevel_bom_receipts import own_output_lots
    assert not own_output_lots(db, item.id)
    for row in rows:
        reverse_order_assembly(db, order_item_id=item.id, operator_id=actor.id,
            operation_key=json.loads(row.cost_detail_json)["graph_operation"]["key"],
            source_snapshot_id=original.snapshots[0].id)
    db.commit()
    assert all(row.status == "reversed" for row in rows)
    assert {row.id: row.cost_detail_json for row in rows} == costs
    restored = {}
    for lid in input_ids:
        lot = db.get(InventoryLot, lid)
        pid = lot.finished_detail.product_id
        restored[pid] = restored.get(pid, 0) + lot.quantity_available + lot.quantity_reserved
        assert lot.quantity_consumed == 0
    # Long output is two sheets at four pieces, including two rounding extras;
    # undoing six consumed pieces must preserve those original spare pieces.
    assert restored == {3771: 8, 3783: 8}
    assert all(db.get(InventoryLot, row.output_lot_id).quantity_available == 0
               and db.get(InventoryLot, row.output_lot_id).quantity_reserved == 0
               for row in rows if row.output_lot_id is not None)
