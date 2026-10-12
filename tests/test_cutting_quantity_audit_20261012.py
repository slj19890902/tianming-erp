from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from tests.test_phase11_requisition import (
    requisition_app, _add_pending_candidate, _login,
    _preview_supplier_order_draft, _save_supplier_order_draft,
)


@pytest.mark.parametrize('legacy_mode', ['一开一', '一开二'])
@pytest.mark.parametrize('quantity,expected', [(1700, 850), (1701, 851)])
def test_12752_pending_preview_and_save_share_frozen_output(requisition_app, legacy_mode, quantity, expected):
    from app.models.order import OrderItem
    from app.models.product import Product
    app, sessions = requisition_app
    item_id = _add_pending_candidate(sessions, 93, quantity=quantity,
        material_code='CUT-AUDIT-AB', layer_count=5, flute_type='AB')
    settings = {'schema_version': 2, 'whole': {'length_parts': 2,
        'width_parts': 1, 'mold_count': 1, 'is_die_cut': True}}
    with sessions() as db:
        item = db.get(OrderItem, item_id)
        product = db.get(Product, item.product_id)
        product.sheet_cutting_settings = deepcopy(settings)
        product.default_cutting_mode = '一开二'
        product.production_process = '模切'
        product.material_id = item.material_id
        product.report_length_mm = item.snapshot_report_length_mm = 375
        product.report_width_mm = item.snapshot_report_width_mm = 236
        item.sheet_cutting_settings_snapshot = deepcopy(settings)
        item.snapshot_splice_mode = 'single'
        item.snapshot_pieces_per_box = 1
        item.special_process = legacy_mode
        db.commit()
    with TestClient(app) as client:
        _login(client, 'admin')
        pending = client.get('/api/requisition/pending')
        assert pending.status_code == 200, pending.text
        row = next(r for r in pending.json()['items'] if r['item_id'] == item_id)
        assert row['suggested_cardboard_len'] == 750
        assert row['suggested_cardboard_width'] == 236
        assert row['requisition_qty'] == expected
        draft = _preview_supplier_order_draft(client, [{'type': 'order_item', 'order_item_id': item_id}])
        line = draft['supplier_groups'][0]['lines'][0]
        assert line['requisition_qty'] == expected
        assert line['sheet_cutting_snapshot']['yield_per_supplier_sheet'] == 2
        saved = _save_supplier_order_draft(client, draft)
        assert saved.status_code == 201, saved.text


def test_quantity_matrix_agrees_with_independent_physical_oracle():
    from types import SimpleNamespace
    from app.api.requisition import _ordinary_requisition_requirements, _current_requisition_requirements
    from app.services.sheet_cutting_settings import SheetCuttingSettings
    # Supplier sheets: ceil((unfinished units * physical pieces - credited
    # pieces) / (length splits * width splits * mold output)). Exercise odd
    # demands and every independent multiplier, including double splice.
    for length_parts in (1, 2, 3):
        for width_parts in (1, 2, 3):
            for mold_count in (1, 3, 7):
                settings = {'schema_version': 2, 'whole':
                    SheetCuttingSettings(length_parts, width_parts, mold_count, True).to_dict()}
                for pieces in (1, 2):
                    for quantity in (0, 1, 17, 1700, 1701):
                        item = SimpleNamespace(id=1, quantity=quantity, delivered_quantity=0,
                            special_process='一开一', sheet_cutting_settings_snapshot=settings,
                            snapshot_pieces_per_box=pieces, snapshot_splice_mode='double' if pieces == 2 else 'single')
                        output = length_parts * width_parts * mold_count
                        physical = quantity * pieces
                        assert _ordinary_requisition_requirements(item)['requisition_qty'] == (physical + output - 1) // output
                        for finished in (0, 3):
                            for semi in (0, 5, 50):
                                needed = max(max(quantity - finished, 0) * pieces - semi, 0)
                                row = _current_requisition_requirements(None, item,
                                    finished_reserved_qty=finished, semi_reserved_piece_qty=semi)
                                assert row['requisition_qty'] == (needed + output - 1) // output


def test_legacy_frozen_order_does_not_borrow_current_master_settings():
    from types import SimpleNamespace
    from app.api.requisition import _ordinary_requisition_requirements
    item = SimpleNamespace(quantity=1700, special_process='一开一',
        sheet_cutting_settings_snapshot=None, snapshot_pieces_per_box=1,
        snapshot_splice_mode='single')
    assert _ordinary_requisition_requirements(item)['requisition_qty'] == 1700


def test_12753_bom_three_pieces_per_set_then_two_by_three_sheet_output():
    from decimal import Decimal
    from types import SimpleNamespace
    from app.api.requisition import _bom_snapshot_requirements
    from tests.test_multilevel_bom_physical_contract import snapshot
    row = snapshot(quantity_per_set=Decimal(3), is_die_cut=True,
        mold_max_yield_per_sheet=3, snapshot_component_report_length_mm=375,
        snapshot_component_report_width_mm=236,
        sheet_cutting_settings_snapshot={'schema_version': 2, 'whole': {
            'length_parts': 2, 'width_parts': 1, 'mold_count': 3, 'is_die_cut': True}})
    result = _bom_snapshot_requirements(SimpleNamespace(get=lambda *args: None), row,
        effective_sets_override=1600, required_piece_quantity_override=1600 * 3,
        inventory_coverage_override={'finished_piece_quantity': 0,
            'semi_piece_quantity': 0, 'total_piece_quantity': 0})
    assert result['required_piece_quantity'] == 4800
    assert result['yield_per_sheet'] == 6
    assert result['requisition_qty'] == 800
