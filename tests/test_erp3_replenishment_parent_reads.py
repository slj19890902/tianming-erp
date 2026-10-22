"""The selected replenishment sources already join their frozen parent facts."""
from tests.test_erp3_dashboard_batch_reads import _capture
from tests.test_order_replenishment_supply import seed
from tests.test_stock_replenishment_flow import stock_replenishment_app


def test_selected_supplies_reuse_joined_parent_without_per_source_reads(stock_replenishment_app):
    from app.models.stock_replenishment import StockReplenishmentOrder, StockReplenishmentOrderItem
    from app.services.order_replenishment_supply import _supplies

    _, factory = stock_replenishment_app
    source_id, _ = seed(factory)
    with factory() as db:
        original = db.get(StockReplenishmentOrderItem, source_id)
        parent = original.order
        parent_values = {c.name: getattr(parent, c.name) for c in parent.__table__.columns
                         if c.name not in {"id", "order_number"}}
        child_values = {c.name: getattr(original, c.name) for c in original.__table__.columns
                        if c.name not in {"id", "replenishment_order_id"}}
        expected = {source_id}
        for index in range(15):
            clone = StockReplenishmentOrder(order_number=f"ERP3-PARENT-{index}", **parent_values)
            clone.items = [StockReplenishmentOrderItem(**child_values)]
            db.add(clone)
            db.flush()
            expected.add(clone.items[0].id)
        db.commit()
    # Fresh identity map matters: the seed's loaded parents hide lazy N+1 reads.
    with factory() as db:
        supplies, statements = _capture(db.get_bind(), lambda: _supplies(db))
        assert {r["source_id"] for r in supplies} == expected
        assert {r["stage"] for r in supplies} == {"pending_material"}
        assert len({r["quantity"] for r in supplies}) == 1
        assert all(r["supplier_name"] == r["source"].order.supplier_name for r in supplies)
        assert not any("from stock_replenishment_orders where stock_replenishment_orders.id =" in sql for sql in statements)
        assert not db.new and not db.dirty
