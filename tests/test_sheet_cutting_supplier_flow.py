from copy import deepcopy
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select, func

from tests.test_phase11_requisition import (
    requisition_app, _add_pending_candidate, _login,
    _preview_supplier_order_draft, _save_supplier_order_draft,
)
from app.services.sheet_cutting_contract import SheetCuttingContract


def _source(session_factory, *, suffix=91, quantity=400):
    from app.models.order import OrderItem
    from app.models.product import Product
    item_id = _add_pending_candidate(session_factory, suffix, quantity=quantity,
        material_code="CUT-TEST-AB", layer_count=5, flute_type="AB")
    settings = {"schema_version": 2, "whole": {"length_parts": 1, "width_parts": 1,
        "mold_count": 2, "is_die_cut": True}}
    with session_factory() as db:
        item = db.get(OrderItem, item_id)
        product = db.get(Product, item.product_id)
        product.sheet_cutting_settings = deepcopy(settings)
        product.default_cutting_mode = "一开一"
        product.production_process = "模切"
        product.material_id = item.material_id
        product.report_length_mm = item.snapshot_report_length_mm = 340
        product.report_width_mm = item.snapshot_report_width_mm = 200
        item.sheet_cutting_settings_snapshot = deepcopy(settings)
        item.special_process = "一开二"  # Legacy sheet-output projection, not supplier cutting.
        db.commit()
        return item_id, product.id


def _preview(client, item_id, *, length_parts=2, width_parts=2):
    contract = SheetCuttingContract(340, 200, length_parts, width_parts, True, 2)
    return _preview_supplier_order_draft(client, [{"type": "order_item", "order_item_id": item_id,
        "sheet_cutting_snapshots": {"whole": contract.to_snapshot()}}])


def test_supplier_v2_quantity_dimensions_writeback_and_replay_are_atomic(requisition_app):
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.supplier_requisition_order import SupplierRequisitionOrder, SupplierRequisitionOrderItem, PurchasePurposeSourceSnapshot
    app, sessions = requisition_app
    item_id, product_id = _source(sessions)
    with TestClient(app) as client:
        _login(client, "admin")
        draft = _preview(client, item_id)
        line = draft["supplier_groups"][0]["lines"][0]
        assert line["requisition_qty"] == 50
        assert Decimal(str(line["report_length_mm"])) == 680
        assert Decimal(str(line["report_width_mm"])) == 400
        assert line["sheet_cutting_snapshot"]["mold_count"] == 2
        with sessions() as db:
            assert db.get(Product, product_id).sheet_cutting_settings["whole"]["length_parts"] == 1
        saved = _save_supplier_order_draft(client, draft)
        assert saved.status_code == 201, saved.text
        replay = _save_supplier_order_draft(client, draft)
        assert replay.status_code == 201, replay.text
    with sessions() as db:
        product = db.get(Product, product_id)
        assert product.default_cutting_mode == "一开四"
        assert product.sheet_cutting_settings["whole"] == {"length_parts": 2, "width_parts": 2, "mold_count": 2, "is_die_cut": True}
        assert product.version == 2
        item = db.get(OrderItem, item_id)
        assert item.special_process == "一开8"
        assert item.cardboard_len == 680 and item.cardboard_width == 400
        supplier_item = db.scalar(select(SupplierRequisitionOrderItem).where(SupplierRequisitionOrderItem.order_item_id == item_id))
        assert supplier_item.sheet_cutting_snapshot == line["sheet_cutting_snapshot"]
        purpose = db.scalar(select(PurchasePurposeSourceSnapshot).where(PurchasePurposeSourceSnapshot.supplier_requisition_order_item_id == supplier_item.id))
        assert purpose.yield_per_sheet_snapshot == 8
        assert purpose.purchase_sheet_qty == 50
        assert db.scalar(select(func.count(SupplierRequisitionOrder.id))) == 1


def test_stale_product_blocks_formal_order_and_default_writeback(requisition_app):
    from app.models.product import Product
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
    app, sessions = requisition_app
    item_id, product_id = _source(sessions)
    with TestClient(app) as client:
        _login(client, "admin")
        draft = _preview(client, item_id)
        with sessions() as db:
            db.get(Product, product_id).version += 1
            db.commit()
        saved = _save_supplier_order_draft(client, draft)
        assert saved.status_code == 409, saved.text
    with sessions() as db:
        assert db.scalar(select(func.count(SupplierRequisitionOrder.id))) == 0
        assert db.get(Product, product_id).sheet_cutting_settings["whole"]["length_parts"] == 1


def test_snapshot_tampering_cannot_change_mold_yield(requisition_app):
    app, sessions = requisition_app
    item_id, _ = _source(sessions)
    with TestClient(app) as client:
        _login(client, "admin")
        draft = _preview(client, item_id)
        draft["supplier_groups"][0]["lines"][0]["sheet_cutting_snapshot"] = SheetCuttingContract(340, 200, 2, 2, True, 4).to_snapshot()
        saved = _save_supplier_order_draft(client, draft)
        assert saved.status_code == 409, saved.text


def test_v2_merge_rounds_aggregate_once_and_keeps_mold_out_of_dimensions(requisition_app):
    from app.models.product import Product
    app, sessions = requisition_app
    first, first_product = _source(sessions, quantity=9)
    second, second_product = _source(sessions, suffix=92, quantity=9)
    contract = SheetCuttingContract(340, 200, 2, 1, True, 2).to_snapshot()
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post('/api/requisition/merge-groups', json={
            "member_item_ids": [first, second], "supplier_name": "苏州纸板供应商",
            "report_length_mm": 680, "report_width_mm": 200,
            "sheet_cutting_snapshot": contract})
        assert created.status_code == 201, created.text
        group = created.json()
        assert group['requisition_qty'] == 5  # ceil((9+9)/4), not ceil(9/4)*2.
        assert group['sheet_cutting_snapshot'] == contract
        draft = _preview_supplier_order_draft(client, [{"type": "merge_group", "merge_group_id": group['id']}])
        line = draft['supplier_groups'][0]['lines'][0]
        assert line['requisition_qty'] == 5
        saved = _save_supplier_order_draft(client, draft)
        assert saved.status_code == 201, saved.text
    with sessions() as db:
        for product_id in (first_product, second_product):
            assert db.get(Product, product_id).sheet_cutting_settings['whole']['length_parts'] == 2


def test_missing_product_edit_permission_rolls_back_supplier_and_defaults(requisition_app):
    from app.models.access_control import UserPermissionOverride
    from app.models.user import User
    from app.models.product import Product
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
    app, sessions = requisition_app
    item_id, product_id = _source(sessions)
    with sessions() as db:
        user = db.scalar(select(User).where(User.username == 'sales'))
        db.add(UserPermissionOverride(user_id=user.id, permission_code='products.edit', is_allowed=False))
        db.commit()
    with TestClient(app) as client:
        _login(client, 'sales')
        draft = _preview(client, item_id)
        saved = _save_supplier_order_draft(client, draft)
        assert saved.status_code == 403, saved.text
    with sessions() as db:
        assert db.scalar(select(func.count(SupplierRequisitionOrder.id))) == 0
        assert db.get(Product, product_id).sheet_cutting_settings['whole']['length_parts'] == 1


def test_cover_and_base_use_independent_directions_and_mold_counts(requisition_app):
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.services.sheet_cutting_settings import SheetCuttingSettings
    app, sessions = requisition_app
    item_id, product_id = _source(sessions, quantity=120)
    settings = {"schema_version": 2, "cover": SheetCuttingSettings(2, 1, 2, True).to_dict(),
                "base": SheetCuttingSettings(1, 2, 3, True).to_dict()}
    with sessions() as db:
        item, product = db.get(OrderItem, item_id), db.get(Product, product_id)
        product.box_style = 'A3天地盖'
        product.sheet_cutting_settings = deepcopy(settings)
        item.sheet_cutting_settings_snapshot = deepcopy(settings)
        item.snapshot_base_report_length_mm = product.base_report_length_mm = 300
        item.snapshot_base_report_width_mm = product.base_report_width_mm = 100
        item.special_process = '一开四'
        db.commit()
    with TestClient(app) as client:
        _login(client, 'admin')
        draft = _preview_supplier_order_draft(client, [{"type": "order_item", "order_item_id": item_id}])
        lines = {row['component_type']: row for row in draft['supplier_groups'][0]['lines']}
        assert lines['cover']['requisition_qty'] == 30
        assert lines['base']['requisition_qty'] == 20
        assert Decimal(str(lines['cover']['report_length_mm'])) == 680
        assert Decimal(str(lines['base']['report_length_mm'])) == 300
        assert Decimal(str(lines['base']['report_width_mm'])) == 200
        saved = _save_supplier_order_draft(client, draft)
        assert saved.status_code == 201, saved.text


def test_double_splice_expands_physical_pieces_before_inventory_and_rounding(requisition_app):
    from app.models.order import OrderItem
    from app.models.product import Product
    from tests.test_p1_81_receipt_purpose_flow import _seed_material_and_staging, _seed_order_semi_reservation
    app, sessions = requisition_app
    material_id = _seed_material_and_staging(sessions)
    settings = {'schema_version': 2, 'whole': {'length_parts': 1, 'width_parts': 1, 'mold_count': 1, 'is_die_cut': False}}
    with sessions() as db:
        item = db.get(OrderItem, 1)
        product = db.get(Product, item.product_id)
        item.material_id = product.material_id = material_id
        item.snapshot_supplier_name = '苏州纸板供应商'
        item.quantity = 400
        item.snapshot_splice_mode = product.splice_mode = 'double'
        item.snapshot_pieces_per_box = product.pieces_per_box = 2
        item.snapshot_report_length_mm = product.report_length_mm = 800
        item.snapshot_report_width_mm = product.report_width_mm = 200
        item.sheet_cutting_settings_snapshot = deepcopy(settings)
        product.sheet_cutting_settings = deepcopy(settings)
        item.special_process = product.default_cutting_mode = '一开一'
        db.commit()
    _seed_order_semi_reservation(sessions, credited_piece_quantity=30, pieces_per_box=2)
    contract = SheetCuttingContract(800, 200, 2, 2, False, 1).to_snapshot()
    with TestClient(app) as client:
        _login(client, 'admin')
        draft = _preview_supplier_order_draft(client, [{'type': 'order_item', 'order_item_id': 1,
            'sheet_cutting_snapshots': {'whole': contract}}])
        line = draft['supplier_groups'][0]['lines'][0]
        assert line['requisition_qty'] == 193  # ceil((400 * 2 - 30) / 4).
        assert line['report_length_mm'] == 1600
        saved = _save_supplier_order_draft(client, draft)
        assert saved.status_code == 201, saved.text
