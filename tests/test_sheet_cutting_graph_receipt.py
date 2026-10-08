from copy import deepcopy

from fastapi.testclient import TestClient

from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login
from tests.test_multilevel_bom_receipt_flow import seed_graph, read_purchase_sources
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity, _freeze_receipt_fact, _receive
from app.services.sheet_cutting_contract import SheetCuttingContract


def test_graph_receipt_closure_uses_frozen_purchase_yield_after_layout_change(composite_requisition_app, _p181_published_map_identity, monkeypatch):
    from app.models.product import Product
    from app.models.order import OrderItem
    from app.services import multilevel_bom_orders
    from app.services.multilevel_bom_receipts import graph_material_receipts_closed
    app, sessions = composite_requisition_app
    original = multilevel_bom_orders.freeze_master_order_bom
    def freeze(db, **kwargs):
        for pid in (2, 3):
            db.get(Product, pid).sheet_cutting_settings = {'schema_version': 2, 'whole': {
                'length_parts': 2, 'width_parts': 1, 'mold_count': 1, 'is_die_cut': False}}
        return original(db, **kwargs)
    monkeypatch.setattr(multilevel_bom_orders, 'freeze_master_order_bom', freeze)
    material_id, snapshots = seed_graph(sessions)
    contract = SheetCuttingContract(1000, 700, 4, 1, False, 1).to_snapshot()
    with TestClient(app) as client:
        _login(client)
        lines = []
        for sid, pid in snapshots:
            with sessions() as db:
                version = db.get(Product, pid).version
            line = dict(order_item_id=1, bom_snapshot_id=sid, component_type='whole',
                cardboard_len=4000, cardboard_width=700, special_process='一开四',
                sheet_cutting_snapshot=deepcopy(contract), expected_product_version=version)
            preview = client.post('/api/requisition/bom-sheet-cutting/preview', json=line)
            assert preview.status_code == 200, preview.text
            data = preview.json()
            for key in ('requisition_qty', 'purchase_total_sheet_qty', 'order_purpose_sheet_qty',
                        'stock_purpose_sheet_qty', 'purpose_plan_version', 'purpose_plan_fingerprint'):
                line[key] = data[key]
            lines.append(line)
        saved = client.post('/api/requisition/batches', json=dict(request_key='v2-graph-split-isolated', supplier_name='苏州纸板供应商', items=lines))
        assert saved.status_code == 201, saved.text
        sources = read_purchase_sources(sessions, material_id)
        assert sorted(source.order_purpose_sheet_qty for source in sources) == [8, 10]
        for index, source in enumerate(sources):
            fact = _freeze_receipt_fact(client, source, idempotency_key=f'v2-graph-fact-{index}')
            assert fact.status_code == 200, fact.text
            received = _receive(client, source, fact.json(), quantity=source.order_purpose_sheet_qty,
                                idempotency_key=f'v2-graph-receipt-{index}')
            assert received.status_code == 200, received.text
            with sessions() as db:
                assert graph_material_receipts_closed(db, db.get(OrderItem, 1)) == (index == len(sources) - 1)
