import subprocess
import pytest
from fastapi.testclient import TestClient
from tests.test_multilevel_bom_receipt_flow import (
    composite_requisition_app, _p181_published_map_identity, seed_graph, purchase_sources,
    _login, _freeze_receipt_fact, _receive,
)


@pytest.mark.parametrize('extra', [0, 1])
def test_bom_source_eligible_despite_parent_summary(composite_requisition_app, _p181_published_map_identity, extra):
    from app.models.order import OrderItem
    from app.models.requisition import RequisitionItem
    from app.services.incoming_receipts import _target, IncomingReceiptError
    import pytest
    app, factory = composite_requisition_app
    mid, snapshots = seed_graph(factory)
    with TestClient(app) as client:
        _login(client)
        sources = purchase_sources(client, factory, mid, snapshots)
        with factory() as db:
            db.get(OrderItem, 1).requisition_status = "未报料"
            db.commit()
        pending = client.get('/api/incoming/pending?page=1&page_size=25')
        assert pending.status_code == 200, pending.text
        rows = pending.json()['items']
        assert rows and all(r['source_receivable'] is True for r in rows)
        assert all(r['requisition_status'] == '未报料' for r in rows)
        for i, source in enumerate(sources):
            fact = _freeze_receipt_fact(client, source, idempotency_key=f'394-price-{i}',unit_price='0.1234')
            assert fact.status_code == 200, fact.text
            result = _receive(client,source,fact.json(),quantity=source.order_purpose_sheet_qty+extra,idempotency_key=f'394-in-{i}', overrides=({'surplus_disposition':'semi_finished_reserve'} if extra else {}))
            assert result.status_code == 200, result.text
            replay = _receive(client,source,fact.json(),quantity=source.order_purpose_sheet_qty+extra,idempotency_key=f'394-in-{i}', overrides=({'surplus_disposition':'semi_finished_reserve'} if extra else {}))
            assert replay.status_code == 200, replay.text
        assert not client.get('/api/incoming/pending?page=1&page_size=25').json()['items']
        with factory() as db:
            with pytest.raises(IncomingReceiptError):
                _target(db,sources[0].route_key)


def test_button_uses_source_qualification_without_bypassing_other_gates():
    from tests.test_multilevel_bom_frontend import method
    script='const assert=require("assert/strict");const m={'+method('canReceiveIncoming')+'};\n'
    script+='''const ctx={...m,incomingReceiptExecutionIssue:()=>""};
const row={material_status:'pending',requisition_status:'未报料',purpose_status:'frozen',source_receivable:true};
assert.equal(ctx.canReceiveIncoming(row),true);
assert.equal(ctx.canReceiveIncoming({...row,source_receivable:false}),false);
assert.equal(ctx.canReceiveIncoming({...row,material_status:'received'}),false);
assert.equal(ctx.canReceiveIncoming({...row,purpose_status:'invalid'}),false);
assert.equal(ctx.canReceiveIncoming({...row,source_receivable:undefined}),false);
ctx.incomingReceiptExecutionIssue=()=>"位置不完整";assert.equal(ctx.canReceiveIncoming(row),false);'''
    r=subprocess.run(['node'],input=script,text=True,capture_output=True,encoding='utf8')
    assert r.returncode==0,r.stderr
