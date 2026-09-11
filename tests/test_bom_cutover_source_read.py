import pytest
from sqlalchemy import select, text

from app.models.multilevel_bom import OrderBomExecutionCutover, OrderBomCutoverSource
from app.models.product_bom import SalesOrderItemBomComponent as Snapshot
from app.services.multilevel_bom_execution_boundary import make_cutover_basis
from app.services.multilevel_bom_orders import freeze_master_order_bom, read_compiled_order_bom
from app.services.multilevel_bom_plan import BomPlanError
from tests.test_multilevel_bom_orders import context
from tests.test_multilevel_bom_compile import setup_liner


@pytest.fixture
def cutover_read_fixture(context):
    """Synthetic persisted read contract, NOT an operational cutover writer."""
    db, actor, item, _ = context
    setup_liner(db, actor)
    item.quantity = 80
    item.subtotal = item.unit_price * 80
    compiled = freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    db.commit()
    history, current, cutover = seed_read_boundary(db, actor, item, compiled)
    return db, actor, item, history, current, cutover


def seed_read_boundary(db, actor, item, compiled):
    """Synthetic test data only; never import this as a production converter."""
    current = list(compiled.snapshots)
    original = next(row for row in current if row.component_product_id != compiled.graph.root_id)
    history = Snapshot(**{column.key: getattr(original, column.key)
        for column in Snapshot.__table__.columns if column.key not in {"id", "created_at"}})
    history.display_order = 0
    history.product_bom_component_id = None
    history.snapshot_schema_version = 4
    history.order_set_quantity = 100
    history.required_piece_quantity = 100 * history.quantity_per_set
    db.add(history)
    # Only fixture setup supplies the independently recorded old/new epochs.
    # No production transition or snapshot overwrite endpoint is added here.
    assert all(row.order_set_quantity == 80 for row in current)
    item.quantity = 100
    item.subtotal = item.unit_price * 100
    item.delivered_quantity = 20
    db.flush()
    document, checksum = make_cutover_basis(graph=compiled.graph, order_item_id=item.id,
        order_quantity=100, delivered_before=20, history_rows=[history], current_rows=current)
    cutover = OrderBomExecutionCutover(order_item_id=item.id, order_quantity=100,
        delivered_before=20, basis_json=document, basis_hash=checksum,
        idempotency_key="isolated-read-boundary", request_hash="a"*64, created_by=actor.id)
    db.add(cutover)
    db.flush()
    db.add_all([OrderBomCutoverSource(order_item_id=item.id, snapshot_id=row.id, role="current") for row in current])
    db.add(OrderBomCutoverSource(order_item_id=item.id, snapshot_id=history.id, role="history"))
    db.commit()
    return history, current, cutover


def test_actual_reader_returns_only_current_sources_and_keeps_history(cutover_read_fixture):
    db, _, item, history, current, _ = cutover_read_fixture
    before = db.execute(text("SELECT * FROM sales_order_item_bom_components ORDER BY id")).all()
    db.expire_all()
    compiled = read_compiled_order_bom(db, item.id)
    assert {row.id for row in compiled.snapshots} == {row.id for row in current}
    assert history.id not in {row.id for row in compiled.snapshots}
    assert compiled.execution_window.execution_quantity == 80
    assert compiled.execution_window.delivered_before == 20
    assert compiled.execution_window.delivered_since == 0
    assert db.get(Snapshot, history.id).snapshot_schema_version == 4
    assert db.execute(text("SELECT * FROM sales_order_item_bom_components ORDER BY id")).all() == before
    item.delivered_quantity = 35
    db.commit()
    later = read_compiled_order_bom(db, item.id).execution_window
    assert (later.execution_quantity,later.delivered_since,later.remaining_quantity) == (80,15,65)


@pytest.mark.parametrize("damage", ["history", "current", "hash", "document", "missing_role", "wrong_role", "old_cancel", "quantity"])
def test_reader_rejects_changed_source_or_boundary(cutover_read_fixture, damage):
    db, _, item, history, current, cutover = cutover_read_fixture
    if damage == "history":
        history.snapshot_component_production_notes = "changed old source"
    elif damage == "current":
        current[0].snapshot_component_production_notes = "changed current source"
    elif damage == "hash":
        cutover.basis_hash = "f"*64
    elif damage == "document":
        cutover.basis_json = "{}"
    elif damage == "missing_role":
        db.delete(db.get(OrderBomCutoverSource, current[0].id))
    elif damage == "wrong_role":
        db.get(OrderBomCutoverSource, history.id).role = "current"
    elif damage == "old_cancel":
        item.delivered_quantity = 19
    else:
        item.quantity = 101
    db.commit()
    with pytest.raises(BomPlanError):
        read_compiled_order_bom(db, item.id)


def test_production_projection_retains_explicit_execution_window(cutover_read_fixture):
    from app.services.multilevel_bom_production_revision import prepare_production_revision, apply_production_revision
    db, _, item, _, _, _ = cutover_read_fixture
    compiled = read_compiled_order_bom(db, item.id)
    document, checksum = prepare_production_revision(compiled, {"3": {"production_notes": "new explicit version"}})
    projected = apply_production_revision(compiled, document, expected_hash=checksum)
    assert projected.execution_window == compiled.execution_window
    original = next(row for row in compiled.snapshots if row.component_product_id == 3)
    changed = next(row for row in projected.snapshots if row.component_product_id == 3)
    assert changed.snapshot_component_production_notes == "new explicit version"
    assert original.snapshot_component_production_notes != "new explicit version"


def test_basis_rejects_overlapping_ids_and_wrong_execution_quantity(cutover_read_fixture):
    db, _, item, history, current, _ = cutover_read_fixture
    graph = read_compiled_order_bom(db, item.id).graph
    args = dict(graph=graph, order_item_id=item.id, order_quantity=100, delivered_before=20)
    assert make_cutover_basis(**args, history_rows=iter([history]), current_rows=iter(current)) == make_cutover_basis(
        **args, history_rows=[history], current_rows=current)
    with pytest.raises(BomPlanError, match="重复"):
        make_cutover_basis(graph=graph, order_item_id=item.id, order_quantity=100,
            delivered_before=20, history_rows=[history], current_rows=current + [history])
    with pytest.raises(BomPlanError, match="待执行数量"):
        make_cutover_basis(graph=graph, order_item_id=item.id, order_quantity=100,
            delivered_before=10, history_rows=[history], current_rows=current)


def test_later_master_edge_deletion_does_not_invalidate_frozen_materials(cutover_read_fixture):
    from app.models.product_bom import ProductBomComponent
    db, _, item, _, current, cutover = cutover_read_fixture
    row = next(row for row in current if row.component_product_id == 3)
    snapshot_id = row.id
    checksum = cutover.basis_hash
    db.delete(db.get(ProductBomComponent, row.product_bom_component_id))
    db.commit()
    db.expire_all()
    assert db.get(Snapshot, snapshot_id).product_bom_component_id is None
    compiled = read_compiled_order_bom(db, item.id)
    assert snapshot_id in {row.id for row in compiled.snapshots}
    assert db.get(OrderBomExecutionCutover, item.id).basis_hash == checksum


def test_cutover_reads_both_epochs_in_one_source_query(cutover_read_fixture):
    from sqlalchemy import event
    db, _, item, _, _, _ = cutover_read_fixture
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        if "FROM sales_order_item_bom_components" in statement:
            statements.append(statement)
    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        read_compiled_order_bom(db, item.id)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(statements) == 1
    assert "order_bom_cutover_sources" in statements[0]
