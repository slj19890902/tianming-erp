from decimal import Decimal
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem, ExternalPackagingReceiptItem
from app.models.graph_material_cost import FinanceDeliveryGraphCostFact as Fact, FinanceDeliveryGraphCostPortion as Portion
from app.models.order import Order, OrderItem
from app.models.delivery import Delivery
from app.models.warehouse_inventory import InventoryLot, InventoryMovement, DeliveryInventoryAllocation
from app.services.material_cost_lineage import material_cost_coverage_report
from app.services.bom_subkits import SubkitError
from test_p1_33c3_external_packaging_purchase_confirmation import purchase_app
from tests.test_multilevel_bom_external_receipts import prepare, receive, _login, _confirm
from tests.test_p1_81_receipt_purpose_flow import _seed_material_and_staging, _p181_published_map_identity


@pytest.mark.parametrize('direct', [False, True])
def test_external_real_receipts_dispatch_cost_and_cancel(purchase_app, _p181_published_map_identity, direct, monkeypatch):
    from app.api.deliveries import router
    from app.services import graph_delivery_cost
    from app.services.multilevel_bom_cost_lineage import graph_material_sources
    purchase_app.include_router(router, prefix='/api/deliveries')
    factory = purchase_app.state.session_factory
    _seed_material_and_staging(factory)
    order_id, item_id, _ = prepare(purchase_app, direct=direct, quantity=2)
    with TestClient(purchase_app) as client:
        _login(client, 'purchase-admin')
        _confirm(client, order_id)
        with factory() as db:
            row = db.scalar(select(ExternalPackagingPurchaseItem).where(ExternalPackagingPurchaseItem.sales_order_item_id == item_id))
            purchase_id, line_id, quantity = row.purchase_order_id, row.id, row.purchase_quantity
            customer_id = db.get(Order, order_id).customer_id
        first = receive(client, purchase_id, line_id, 'first-loose', 2)
        assert first.status_code == 200, first.text
        last = receive(client, purchase_id, line_id, 'last-whole', quantity-2)
        assert last.status_code == 200, last.text
        with factory() as db:
            first_id = db.scalar(select(ExternalPackagingReceiptItem.id).where(ExternalPackagingReceiptItem.receipt_id == first.json()['receipt']['id']))
            last_id = db.scalar(select(ExternalPackagingReceiptItem.id).where(ExternalPackagingReceiptItem.receipt_id == last.json()['receipt']['id']))
            lot = db.scalar(select(InventoryLot).where(InventoryLot.quantity_reserved > 0))
            rows = graph_material_sources(db, lot)
            assert {r['external_receipt_item_id'] for r in rows} == {first_id,last_id}
            assert sum(r['amount'] for r in rows) == Decimal('59.4000' if direct else '118.8000')
            source = db.scalar(select(InventoryLot).where(InventoryLot.source_ref_type == 'bom_external_receipt'))
            detail = json.loads(source.cost_snapshot_detail_json)
            detail['customer_id'] += 1000
            source.cost_snapshot_detail_json = json.dumps(detail)
            with pytest.raises(SubkitError, match='成本身份不一致'):
                graph_material_sources(db, lot)
            db.rollback()
        created = client.post('/api/deliveries', json={'customer_id':customer_id, 'delivery_date':'2026-09-10',
            'items':[{'order_item_id':item_id, 'delivered_quantity':1}]})
        assert created.status_code == 201, created.text
        did = created.json()['id']
        with monkeypatch.context() as patch:
            def fail(**kwargs):
                raise SubkitError('模拟外购成本写入失败')
            patch.setattr(graph_delivery_cost, 'Portion', fail)
            failed = client.put(f'/api/deliveries/{did}/dispatch')
            assert failed.status_code == 409, failed.text
        with factory() as db:
            assert db.get(Delivery, did).status == 'pending'
            assert db.get(OrderItem, item_id).delivered_quantity == 0
            assert list(db.scalars(select(Fact))) == []
            assert sum(l.quantity_reserved for l in db.scalars(select(InventoryLot))) == 2
        dispatched = client.put(f'/api/deliveries/{did}/dispatch')
        assert dispatched.status_code == 200, dispatched.text
        from tests.test_multilevel_bom_external_reversal import reverse
        blocked = reverse(client, last.json()['receipt']['id'])
        assert blocked.status_code == 409, blocked.text
        with factory() as db:
            report = material_cost_coverage_report(db, month='2026-09')
            assert report['covered_delivery_lines'] == 1, report
            assert report['actual_material_cost'] == Decimal('29.70' if direct else '59.40')
            portions = list(db.scalars(select(Portion)))
            assert {p.external_receipt_item_id for p in portions} == {first_id,last_id}
            assert all(p.purchase_receipt_fact_id is None and p.purpose_allocation_id is None for p in portions)
            assert all(p.tax_included and p.tax_rate == Decimal('.13') for p in portions)
            fact_ids = [f.id for f in db.scalars(select(Fact))]
            for fact in db.scalars(select(Fact)):
                replay = graph_delivery_cost.freeze_graph_delivery_cost(db,
                    allocation=db.get(DeliveryInventoryAllocation, fact.delivery_inventory_allocation_id),
                    lot=db.get(InventoryLot, fact.inventory_lot_id), operator_id=fact.created_by)
                assert replay.id == fact.id
        cancelled = client.put(f'/api/deliveries/{did}/cancel')
        assert cancelled.status_code == 200, cancelled.text
        with factory() as db:
            assert db.get(OrderItem, item_id).delivered_quantity == 0
            assert material_cost_coverage_report(db, month='2026-09')['actual_material_cost'] == 0
            assert [f.id for f in db.scalars(select(Fact))] == fact_ids
            assert sum(l.quantity_reserved for l in db.scalars(select(InventoryLot))) == 2
        # Once dispatch is actually reversed, the receipt can unwind too.
        from app.services.bom_subkit_inventory import _only_reversed_graph_consumptions
        with factory() as db:
            output = db.scalar(select(InventoryLot).where(InventoryLot.quantity_reserved > 0))
            movements = list(db.scalars(select(InventoryMovement).where(InventoryMovement.inventory_lot_id == output.id)))
            reserved = next(m for m in movements if m.movement_type == 'reserve')
            reversed_move = next(m for m in movements if m.movement_type == 'reverse_consume')
            allocation = db.scalar(select(DeliveryInventoryAllocation))
            assert _only_reversed_graph_consumptions(db, output, ignored_reserve_id=reserved.id)
            # Matching stock balances cannot legitimize broken or partial lineage.
            for row, field, value in [(allocation, 'status', 'partial'),
                    (allocation, 'reversed_stock_quantity', 0),
                    (reversed_move, 'reversal_of_movement_id', reserved.id),
                    (reversed_move, 'related_order_item_id', None),
                    (reversed_move, 'after_reserved', 99),
                    (reversed_move, 'movement_type', 'adjust')]:
                original = getattr(row, field)
                setattr(row, field, value)
                assert not _only_reversed_graph_consumptions(db, output, ignored_reserve_id=reserved.id)
                setattr(row, field, original)
            db.rollback()
        from app.services import multilevel_bom_external_reversal as reversal_service
        from sqlalchemy import text
        def receipt_facts():
            with factory() as db:
                return {table: db.execute(text(f'SELECT * FROM {table} ORDER BY 1')).all() for table in (
                    'inventory_lots', 'inventory_movements', 'inventory_reservations',
                    'external_packaging_receipt_reversals', 'bom_assemblies', 'production_tasks', 'operation_logs')}
        before = receipt_facts()
        with monkeypatch.context() as patch:
            def late_failure(*args, **kwargs):
                raise SubkitError('撤销最终审计失败')
            patch.setattr(reversal_service, 'append_audit_event', late_failure)
            assert reverse(client, last.json()['receipt']['id']).status_code == 409
        assert receipt_facts() == before
        undone = reverse(client, last.json()['receipt']['id'])
        assert undone.status_code == 200, undone.text
        assert reverse(client, last.json()['receipt']['id']).json()['created'] is False
        with factory() as db:
            assert sum(l.quantity_reserved + l.quantity_available for l in db.scalars(select(InventoryLot))) == 0
            assert [f.id for f in db.scalars(select(Fact))] == fact_ids
