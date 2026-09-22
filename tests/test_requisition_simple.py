import pytest
from fastapi.testclient import TestClient
from test_requisition_group_pool import b1_app, setup_pool
from tests.test_phase11_requisition import requisition_app


@pytest.mark.parametrize("merged", [False, True])
@pytest.mark.parametrize("partial", [False, True])
def test_full_pool_preserves_explicit_extra_purchase(b1_app, merged, partial):
    app, factory = b1_app
    with TestClient(app) as client:
        ids, reservation = setup_pool(client, factory, stocks=(100,200) if partial else (334,100))
        need = 36 if partial else 0
        selections = [dict(type='order_item', order_item_id=i, supplier_name='测试供应商',
                           retain_stock_purchase=True) for i in ids]
        if merged:
            group = client.post('/api/requisition/merge-groups', json={
                'member_item_ids':ids, 'supplier_name':'测试供应商',
                'report_length_mm':800, 'report_width_mm':600, 'cutting_mode':'一开一'})
            assert group.status_code == 201, group.text
            selections = [dict(type='merge_group', merge_group_id=group.json()['id'], retain_stock_purchase=True)]
        before = client.post('/api/requisition/supplier-orders/preview-from-pending-selection',
                             json={'selections': selections})
        assert before.status_code == 200, before.text
        response = client.post('/api/requisition/semi-inventory/reserve-safe-batch', json=reservation)
        assert response.status_code == 200, response.text
        preview = client.post('/api/requisition/supplier-orders/preview-from-pending-selection',
                              json={'selections': selections})
        assert preview.status_code == 200, preview.text
        draft = preview.json()
        from copy import deepcopy
        stale = deepcopy(before.json())
        for group in stale['supplier_groups']:
            for line in group['lines']:
                line.update(requisition_qty=50, purchase_total_sheet_qty=50,
                            order_purpose_sheet_qty=0, stock_purpose_sheet_qty=50)
        assert client.post('/api/requisition/supplier-orders/from-pending-selection', json=stale).status_code == 409
        for group in draft['supplier_groups']:
            for line in group['lines']:
                assert line['order_purpose_sheet_qty'] == need
                line.update(requisition_qty=need+50, purchase_total_sheet_qty=need+50,
                            order_purpose_sheet_qty=need, stock_purpose_sheet_qty=50)
        forged = deepcopy(draft)
        forged['supplier_groups'][0]['lines'][0].update(order_purpose_sheet_qty=need+1, stock_purpose_sheet_qty=49)
        assert client.post('/api/requisition/supplier-orders/from-pending-selection', json=forged).status_code == 409
        saved = client.post('/api/requisition/supplier-orders/from-pending-selection', json=draft)
        assert saved.status_code == 201, saved.text
        replay = client.post('/api/requisition/supplier-orders/from-pending-selection', json=draft)
        assert replay.status_code == 201, replay.text
        assert replay.json()['created_orders'] == saved.json()['created_orders']
        assert replay.json()['idempotent_replay'] is True
        from sqlalchemy import select
        from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot
        with factory() as db:
            facts = db.scalars(select(PurchasePurposeSourceSnapshot)).all()
            assert sum(row.order_purpose_sheet_qty for row in facts) == need
            assert sum(row.reserve_purpose_sheet_qty for row in facts) == 50
            assert sum(row.source_semi_reserved_piece_qty_snapshot for row in facts) == 336-need
            # New reserve facts cannot be destroyed by reverting the old positive-demand constraint.
            import importlib.util
            from pathlib import Path
            from alembic.migration import MigrationContext
            from alembic.operations import Operations
            import pytest
            path = Path(__file__).parents[1] / 'alembic/versions/dv0922_retained_material_reserve.py'
            spec = importlib.util.spec_from_file_location('reserve_migration', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if partial:
                return
            with Operations.context(MigrationContext.configure(db.connection())):
                with pytest.raises(RuntimeError, match='Retained reserve facts'):
                    module.downgrade()


def test_retained_material_receipt_does_not_create_extra_finished_goods(requisition_app, monkeypatch):
    from sqlalchemy import select
    from tests.test_p1_81_receipt_purpose_flow import (
        _seed_material_and_staging, _seed_order_semi_reservation, _use_p181_published_map_identity,
        _prepare_order_item, _login, _selection, _set_purpose_plan, _freeze_receipt_fact,
        FrozenSource, _receive, _posted_finished_quantity,
    )
    from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot, SupplierRequisitionOrderItem
    app, factory = requisition_app
    _use_p181_published_map_identity(monkeypatch)
    _seed_material_and_staging(factory)
    _prepare_order_item(factory, quantity=100)
    _seed_order_semi_reservation(factory, credited_piece_quantity=100, pieces_per_box=1)
    with TestClient(app) as client:
        _login(client, 'admin')
        selection = {**_selection(), 'retain_stock_purchase': True}
        response = client.post('/api/requisition/supplier-orders/preview-from-pending-selection', json={'selections':[selection]})
        assert response.status_code == 200, response.text
        draft = response.json()
        _set_purpose_plan(draft['supplier_groups'][0]['lines'][0], purchase_total=50, order_purpose=0, stock_purpose=50)
        response = client.post('/api/requisition/supplier-orders/from-pending-selection', json=draft)
        assert response.status_code == 201, response.text
        with factory() as db:
            snapshot = db.scalar(select(PurchasePurposeSourceSnapshot))
            item = db.get(SupplierRequisitionOrderItem, snapshot.supplier_requisition_order_item_id)
            source = FrozenSource(snapshot.source_key, f'so{item.id}', item.id, item.version,
                snapshot.id, snapshot.snapshot_version, snapshot.preview_fingerprint,
                snapshot.component_type, item.material_id, 0, 50)
        fact = _freeze_receipt_fact(client, source, idempotency_key='reserve-price')
        assert fact.status_code == 200, fact.text
        received = _receive(client, source, fact.json(), quantity=50, idempotency_key='reserve-receive')
        assert received.status_code == 200, received.text
        allocation = received.json()['purpose_allocation']
        assert allocation['order_sheet_delta'] == 0
        assert allocation['reserve_sheet_delta'] == 50
        # The pre-existing 100 reserved pieces may complete the original order;
        # the newly bought 50 sheets must never add 50 finished boxes.
        assert _posted_finished_quantity(factory) <= 100
        from app.models.warehouse_inventory import InventoryLot
        with factory() as db:
            lots = db.scalars(select(InventoryLot).where(InventoryLot.inventory_type=='semi_finished')).all()
            assert sum(row.quantity_available for row in lots) == 50
