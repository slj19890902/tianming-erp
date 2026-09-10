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
