from copy import deepcopy
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_p1_13c_bom_a3_physical_sources import a3_surround_app, _login
from app.services.sheet_cutting_contract import SheetCuttingContract
from app.services.sheet_cutting_settings import SheetCuttingSettings


def test_bom_preview_changes_only_supplier_layout_and_formal_save_writes_defaults(a3_surround_app):
    from app.models.product import Product
    from app.models.product_bom import SalesOrderItemBomComponent
    from app.models.requisition import RequisitionItem
    from app.models.mold_tool import MoldTool
    app, sessions = a3_surround_app
    settings = {'schema_version': 2, 'cover': SheetCuttingSettings(1, 1, 2, True).to_dict(),
                'base': SheetCuttingSettings(1, 1, 3, True).to_dict()}
    with sessions() as db:
        mold = MoldTool(mold_code='CUT-BOM-MOLD', mold_name='隔离开料测试模具', rack_location='TEST')
        db.add(mold)
        db.flush()
        snapshot = db.scalar(select(SalesOrderItemBomComponent).order_by(SalesOrderItemBomComponent.id))
        snapshot.snapshot_mold_tool_id = mold.id
        snapshot.sheet_cutting_settings_snapshot = deepcopy(settings)
        snapshot.is_die_cut = True
        snapshot.mold_max_yield_per_sheet = 3
        snapshot.snapshot_component_default_cutting_mode = '一开二'
        product = db.get(Product, snapshot.component_product_id)
        product.sheet_cutting_settings = deepcopy(settings)
        product.production_process = '模切'
        product.mold_tool_id = mold.id
        product.default_cutting_mode = '一开一'
        db.commit()
        snapshot_id, product_id, version = snapshot.id, product.id, product.version
    contract = SheetCuttingContract(610, 410, 2, 2, True, 2).to_snapshot()
    line = {'order_item_id': 1, 'bom_snapshot_id': snapshot_id, 'component_type': 'cover',
            'cardboard_len': 1220, 'cardboard_width': 820, 'special_process': '一开8',
            'sheet_cutting_snapshot': contract, 'expected_product_version': version}
    with TestClient(app) as client:
        _login(client)
        response = client.post('/api/requisition/bom-sheet-cutting/preview', json=line)
        assert response.status_code == 200, response.text
        preview = response.json()
        assert preview['requisition_qty'] == 2
        assert preview['sheet_cutting_snapshot'] == contract
        with sessions() as db:
            assert db.get(Product, product_id).version == version
        line.update({key: preview[key] for key in ('purchase_total_sheet_qty', 'order_purpose_sheet_qty',
            'stock_purpose_sheet_qty', 'purpose_plan_version', 'purpose_plan_fingerprint')})
        line['requisition_qty'] = preview['requisition_qty']
        payload = {'supplier_name': '匿名供应商', 'request_key': 'cutting-bom-isolated', 'items': [line]}
        saved = client.post('/api/requisition/batches', json=payload)
        assert saved.status_code == 201, saved.text
        assert client.post('/api/requisition/batches', json=payload).status_code == 201
    with sessions() as db:
        row = db.scalar(select(RequisitionItem).where(RequisitionItem.sheet_cutting_snapshot.is_not(None)))
        assert row.sheet_cutting_snapshot == contract
        assert row.cardboard_len == Decimal('1220') and row.requisition_qty == 2
        product = db.get(Product, product_id)
        assert product.version == version + 1
        assert product.sheet_cutting_settings['cover']['length_parts'] == 2
        assert product.sheet_cutting_settings['base']['length_parts'] == 1
        assert db.get(SalesOrderItemBomComponent, snapshot_id).sheet_cutting_settings_snapshot == settings
