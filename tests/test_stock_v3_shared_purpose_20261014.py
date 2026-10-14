"""Protect frozen order/stock purpose splits and shared BOM demand rows."""
from datetime import date
import pytest
from fastapi.testclient import TestClient
from tests.test_stock_v3_internal_sheet_20261014 import composite_requisition_app, internal_seed, supplier_seed, BASE, _login
from app.models.order import OrderItem
from app.models.requisition import Requisition, RequisitionItem
from app.models.product_bom import RequisitionItemBomSource, SalesOrderItemBomComponent
from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot
from app.models.procurement_source import ProcurementSourceLink
from app.models.user import User
from app.api.warehouse import _manual_sheet_procurement_state, _revise_manual_internal_demand


@pytest.mark.parametrize("frozen_yield", [1, 2])
def test_linked_material_purpose_excludes_extra_stock_from_order_commitment(composite_requisition_app, frozen_yield):
    app, sessions = composite_requisition_app
    _, rid = internal_seed(sessions, qty=27)
    sid = supplier_seed(sessions, qty=27)
    with sessions() as db:
        row = db.get(RequisitionItem, rid); row.purpose_contract_status = 'frozen'
        db.add(PurchasePurposeSourceSnapshot(snapshot_key='purpose-review', allocation_group_key='review-group',
            material_requisition_item_id=rid, source_kind='order_item', source_key='order_item:1:whole',
            source_order_item_id=1, customer_id=1, customer_name_snapshot='test', component_type='whole',
            source_finished_qty_snapshot=50, pieces_per_finished_snapshot=2,
            source_required_piece_qty_snapshot=100, source_semi_reserved_piece_qty_snapshot=0,
            source_effective_piece_qty_snapshot=20*frozen_yield, yield_per_sheet_snapshot=frozen_yield,
            group_effective_piece_qty_snapshot=20*frozen_yield, group_authoritative_order_sheet_qty_snapshot=20,
            purchase_sheet_qty=27, order_purpose_sheet_qty=20, reserve_purpose_sheet_qty=7,
            calculation_rule_version='p1-80-v1', preview_fingerprint='a'*64, request_hash='b'*64, created_by=1))
        db.add(ProcurementSourceLink(supplier_item_id=sid, material_requisition_item_id=rid,
            source_quantity=27, source_snapshot_json='{}', created_by=1))
        db.commit()
    with TestClient(app) as client:
        _login(client)
        response = client.get(BASE + '/manual-inventory')
        assert response.status_code == 200, response.text
        data = response.json()
        assert data['committed_piece_qty'] == 20*frozen_yield
        assert data['remaining_required_piece_qty'] == 100-20*frozen_yield
        assert data['items'][0]['max_stock_quantity'] == 100-20*frozen_yield


@pytest.mark.parametrize('selected,remaining,expected', [(1,15,[15,30]), (2,25,[20,25]), (2,0,[20,0])])
def test_shared_bom_row_revises_only_selected_source(composite_requisition_app, selected, remaining, expected):
    _, sessions = composite_requisition_app
    with sessions() as db:
        header = Requisition(requisition_number='MERGED-REVIEW', requisition_date=date.today(), status='已报料')
        db.add(header); db.flush()
        row = RequisitionItem(requisition_id=header.id, order_item_id=1, requisition_qty=50,
            cardboard_len=1000, cardboard_width=700, special_process='一开一', required_piece_qty=50,
            product_name_snapshot='same-sheet-multi-source', status='有效', remark='retain shared note')
        db.add(row); db.flush(); sources = []
        for snapshot_id, qty in [(1,20),(2,30)]:
            snapshot = db.get(SalesOrderItemBomComponent, snapshot_id)
            source = RequisitionItemBomSource(requisition_item_id=row.id,
                sales_order_item_bom_component_id=snapshot_id, component_type='whole',
                order_set_quantity=snapshot.order_set_quantity, quantity_per_set=snapshot.quantity_per_set,
                required_piece_quantity=snapshot.required_piece_quantity, spare_sheet_quantity=0,
                calculated_purchase_quantity=qty)
            db.add(source); sources.append(source)
        db.commit()
        item = db.get(OrderItem, 1); snapshot = db.get(SalesOrderItemBomComponent, selected)
        state = _manual_sheet_procurement_state(db, item, 'whole', snapshot=snapshot)
        assert len(state['mutable_rows']) == 1
        original_revision = state['revision']
        sources[0].spare_sheet_quantity = 1
        db.flush()
        assert _manual_sheet_procurement_state(db, item, 'whole', snapshot=snapshot)['revision'] != original_revision
        sources[0].spare_sheet_quantity = 0
        db.flush()
        _revise_manual_internal_demand(db, item, 'whole', remaining, db.get(User,1),
            snapshot=snapshot, previous_remaining=20 if selected == 1 else 30)
        db.flush()
        assert row.requisition_qty == sum(expected)
        assert [int(s.calculated_purchase_quantity) for s in sources] == expected
        assert row.status == '有效' and row.remark == 'retain shared note'
        assert sources[(selected-1)^1].active_guard == 1
        assert sources[selected-1].active_guard == (None if remaining == 0 else 1)


@pytest.mark.parametrize("material_component,extra_component,supplier_quantity,expected", [("whole","whole",32,25), ("whole","cover",32,20), ("whole","cover",31,None), ("cover","whole",32,5), ("cover","whole",31,None)])
def test_supplier_own_and_linked_purposes_both_count_without_losing_material_demand(composite_requisition_app, material_component, extra_component, supplier_quantity, expected):
    app, sessions = composite_requisition_app
    _, rid = internal_seed(sessions, qty=27)
    sid = supplier_seed(sessions, qty=supplier_quantity)
    with sessions() as db:
        db.get(RequisitionItem, rid).purpose_contract_status = 'frozen'
        if material_component == 'cover':
            db.get(RequisitionItem, rid).product_name_snapshot = '内部报料-盖'
        for label, formal, amount, extra in [('material', {'material_requisition_item_id':rid}, 20, 7),
                                            ('supplier', {'supplier_requisition_order_item_id':sid}, 5, 0)]:
            db.add(PurchasePurposeSourceSnapshot(snapshot_key=label, allocation_group_key=label+'-group',
                **formal, source_kind='order_item', source_key='order_item:1:whole', source_order_item_id=1,
                customer_id=1, customer_name_snapshot='test', component_type=extra_component if label == 'supplier' else material_component,
                source_finished_qty_snapshot=50, pieces_per_finished_snapshot=2, source_required_piece_qty_snapshot=100,
                source_semi_reserved_piece_qty_snapshot=0, source_effective_piece_qty_snapshot=amount,
                yield_per_sheet_snapshot=1, group_effective_piece_qty_snapshot=amount,
                group_authoritative_order_sheet_qty_snapshot=amount, purchase_sheet_qty=amount+extra,
                order_purpose_sheet_qty=amount, reserve_purpose_sheet_qty=extra, calculation_rule_version='p1-80-v1',
                preview_fingerprint='a'*64, request_hash='b'*64, created_by=1))
        db.add(ProcurementSourceLink(supplier_item_id=sid, material_requisition_item_id=rid,
            source_quantity=27, source_snapshot_json='{}', created_by=1))
        db.commit()
    with TestClient(app) as client:
        _login(client)
        response = client.get(BASE+'/manual-inventory')
        if expected is None:
            assert response.status_code == 409 and '用途合计' in response.text
        else:
            assert response.status_code == 200, response.text
            assert response.json()['committed_piece_qty'] == expected
            assert response.json()['remaining_required_piece_qty'] == 100-expected
