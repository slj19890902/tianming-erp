import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login
from tests.test_stock_v3_internal_sheet_20261014 import internal_seed, BASE
from app.models.requisition import RequisitionItem
from app.models.warehouse_inventory import InventoryLot

NODE='/Users/kimiyf/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node'


@pytest.mark.parametrize('quantity,remaining', [(60,40),(100,0)])
def test_saved_order_and_internal_document_ui_generates_real_api_request(composite_requisition_app,quantity,remaining):
    app,sessions=composite_requisition_app;lot_id,req_id=internal_seed(sessions)
    with TestClient(app) as client:
        _login(client)
        detail=client.get('/api/orders/1');assert detail.status_code==200,detail.text
        item=next(row for row in detail.json()['items'] if row['id']==1)
        assert item['requisition_status']=='已报料'
        preview=client.get(BASE+'/manual-inventory');assert preview.status_code==200,preview.text
        documents=client.get('/api/requisition/reported-documents',params={'document_number':'BL-20261014-001','source_type':'legacy_material_requisition'})
        assert documents.status_code==200,documents.text
        document=next(row for row in documents.json()['items'] if row['document_number']=='BL-20261014-001')
        line=next(row for row in document['line_items'] if row['id']==req_id)
        run=subprocess.run([NODE,'tests/stock_v3_manual_sheet_api_ui_harness.cjs'],
            input=json.dumps({'order_item':item,'preview':preview.json(),'lot_id':lot_id,'quantity':quantity,'document_line':line}),
            text=True,capture_output=True,cwd=Path(__file__).resolve().parents[1])
        assert run.returncode==0,run.stderr
        request=json.loads(run.stdout)
        assert request['submitted']['path']==BASE+'/manual-reserve'
        assert request['submitted']['payload']['stock_quantity']==quantity
        assert request['reported'][0]['order_item_id']==1
        response=client.post(request['submitted']['path'],json=request['submitted']['payload'])
        assert response.status_code==200,response.text
        assert response.json()['remaining_required_piece_qty']==remaining
        repeated=client.post(request['submitted']['path'],json=request['submitted']['payload'])
        assert repeated.status_code==200 and repeated.json()['replayed']
        fresh=client.get(BASE+'/manual-inventory');assert fresh.status_code==200,fresh.text
        assert fresh.json()['remaining_required_piece_qty']==remaining
    with sessions() as db:
        assert db.get(RequisitionItem,req_id).requisition_qty==remaining
        lot=db.get(InventoryLot,lot_id)
        assert (lot.quantity_available,lot.quantity_reserved,lot.quantity_consumed)==(204-quantity,quantity,0)
