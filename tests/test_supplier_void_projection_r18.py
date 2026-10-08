import pytest
from fastapi.testclient import TestClient
from test_phase11_requisition import requisition_app, _login, _preview_supplier_order_draft
from test_p1_80_purchase_purpose_allocation import _prepare_order_item, _selection, _single_line, _set_purpose_plan, _save_draft, _created_order_id


@pytest.mark.parametrize("purchase_count", [1, 2])
def test_void_projection_uses_current_transaction_with_production_autoflush_disabled(requisition_app, purchase_count):
    from app.models.order import OrderItem
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
    app, factory = requisition_app
    _prepare_order_item(factory, quantity=8)
    with TestClient(app) as client:
        _login(client,"admin")
        ids=[]
        for _ in range(purchase_count):
            draft=_preview_supplier_order_draft(client,[_selection()])
            quantity=8//purchase_count
            _set_purpose_plan(_single_line(draft),purchase_total=quantity,order_purpose=quantity,stock_purpose=0)
            result=_save_draft(client,draft)
            assert result.status_code==201,result.text
            ids.append(_created_order_id(result))
        factory.configure(autoflush=False)
        for index,purchase_id in enumerate(ids):
            result=client.put(f"/api/requisition/supplier-orders/{purchase_id}/void")
            assert result.status_code==200,result.text
            remaining=8-(index+1)*(8//purchase_count)
            with factory() as db:
                item=db.get(OrderItem,1)
                assert item.quantity==8
                assert db.get(SupplierRequisitionOrder,purchase_id).status=="voided"
                assert int(item.requisition_qty or 0)==remaining
                assert item.requisition_status==("已报料" if remaining else "未报料")
                if not remaining:assert item.supplier_order_number is None
            pending=client.get("/api/requisition/pending")
            assert pending.status_code==200,pending.text
            row=next(x for x in pending.json()["items"] if x.get("item_id")==1)
            assert row["already_requisitioned_qty"]==remaining
            assert row["remaining_requisition_qty"]==8-remaining
