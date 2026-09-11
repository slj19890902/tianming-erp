from decimal import Decimal

import pytest
from sqlalchemy import event, select

from app.models.order import OrderItem
from app.models.product_bom import SalesOrderItemBomComponent as Snapshot, SalesOrderItemBomDemandAdjustment
from app.services.composite_bom import get_order_item_bom_components_by_item_ids
from app.services.multilevel_bom_orders import freeze_master_order_bom
from app.services.multilevel_bom_plan import BomPlanError
from app.services.multilevel_bom_production_versions import project_complete_order_material_rows
from tests.test_multilevel_bom_orders import context
from tests.test_bom_cutover_source_read import cutover_read_fixture


def sources(db, ids):
    return list(db.scalars(select(Snapshot).where(Snapshot.sales_order_item_id.in_(ids))
                          .order_by(Snapshot.sales_order_item_id, Snapshot.display_order)))


def test_material_batch_excludes_history_but_validates_it(cutover_read_fixture):
    db, _, item, history, current, _ = cutover_read_fixture
    output = project_complete_order_material_rows(db, sources(db, [item.id]))
    assert {row.id for row in output} == {row.id for row in current}
    assert history.id not in {row.id for row in output}
    history.snapshot_component_production_notes = "changed history"
    db.commit()
    with pytest.raises(BomPlanError, match="摘要"):
        project_complete_order_material_rows(db, sources(db, [item.id]))


def test_only_legacy_input_does_not_bypass_existing_cutover(cutover_read_fixture):
    db, _, _, history, _, _ = cutover_read_fixture
    with pytest.raises(BomPlanError):
        project_complete_order_material_rows(db, [history])


def test_mixed_batch_preserves_normal_order_and_fixed_query_families(cutover_read_fixture):
    db, actor, item, history, current, _ = cutover_read_fixture
    second = OrderItem(order_id=item.order_id, product_id=item.product_id, quantity=30,
        unit_price=Decimal("5"), subtotal=Decimal("150"), snapshot_product_name="normal new order")
    db.add(second)
    db.flush()
    normal = freeze_master_order_bom(db, order_item_id=second.id, actor=actor)
    db.commit()
    for ids in [[item.id], [item.id, second.id]]:
        rows = sources(db, ids)
        statements = []
        def capture(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement)
        event.listen(db.bind, "before_cursor_execute", capture)
        try:
            result = project_complete_order_material_rows(db, rows)
        finally:
            event.remove(db.bind, "before_cursor_execute", capture)
        # Production and structural rule histories are distinct immutable
        # families. Adding a second order must not add per-order queries.
        families = ("sales_order_items", "order_bom_graph_products", "order_bom_production_revisions",
                    "order_bom_rule_revisions", "order_bom_cutover_sources")
        assert len(statements) == len(families)
        assert all(sum(f"FROM {table}" in statement for statement in statements) == 1 for table in families), [statement.split("FROM", 1)[-1] for statement in statements]
        expected = {row.id for row in current}
        if second.id in ids:
            expected.update(row.id for row in normal.snapshots)
        assert {row.id for row in result} == expected
        assert history.id not in expected


def test_order_bom_response_pairs_adjustments_by_snapshot_id(cutover_read_fixture):
    db, actor, item, history, current, _ = cutover_read_fixture
    first = min(current, key=lambda row: row.display_order)
    for row, amount in [(history,11),(first,7)]:
        db.add(SalesOrderItemBomDemandAdjustment(sales_order_item_bom_component_id=row.id,
            event_type="isolated_mapping_test", delta_order_set_quantity=0,
            delta_required_piece_quantity=amount, reason="isolated source matching",
            actor_id=actor.id, idempotency_key=f"batch-match:{row.id}"))
    db.commit()
    response = get_order_item_bom_components_by_item_ids(db, [item.id])[item.id]
    assert {row["id"] for row in response} == {row.id for row in current}
    for row in response:
        original = next(source for source in current if source.id == row["id"])
        assert row["effective_required_piece_quantity"] == original.required_piece_quantity + (7 if original.id == first.id else 0)
