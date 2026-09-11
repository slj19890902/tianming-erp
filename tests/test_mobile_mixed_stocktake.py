import pytest
from datetime import date, datetime
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_n035_stocktake_api import stocktake_api, _login, _submission_payload
from app.models.warehouse_inventory import InventoryLot, SemiFinishedInventoryDetail, InventoryMovement


@pytest.mark.parametrize("reserved, fail_adjust", [(False, False), (True, False), (True, True)])
def test_mobile_mixed_stocktake_includes_and_counts_sheets_once(stocktake_api, reserved, fail_adjust, monkeypatch):
    app, factory, ids = stocktake_api
    with factory() as db:
        lot=InventoryLot(lot_number='MIXED-RAW',inventory_type='semi_finished',warehouse_location_id=ids['location'],quantity_available=7,unit='sheets',status='active',source_type='manual',stock_date=date.today(),last_movement_at=datetime.now())
        db.add(lot);db.flush()
        db.add(SemiFinishedInventoryDetail(inventory_lot_id=lot.id,internal_name='通用原材料',material_code_snapshot='A1B',normalized_material_code='A1B',layer_count=3,flute_type='B',board_length_mm=817,board_width_mm=613,component_type='whole',pieces_per_box=1,stock_yield_per_sheet=1,sheet_type='raw_board'))
        lot_id=lot.id
        if reserved:
            from app.models.order import Order, OrderItem
            from app.models.warehouse_inventory import InventoryReservation
            original=db.get(InventoryLot,ids['lot1']).finished_detail
            order=Order(order_number='MIXED-COUNT-ORDER',customer_id=original.owner_customer_id,order_date=date.today(),status='pending_delivery')
            db.add(order);db.flush()
            item=OrderItem(order_id=order.id,product_id=original.product_id,quantity=13,unit_price=1,subtotal=13,snapshot_product_name='片料抵扣')
            db.add(item);db.flush()
            reservation=InventoryReservation(reservation_number='MIXED-COUNT-RES',inventory_lot_id=lot.id,reservation_type='semi_order',order_id=order.id,order_item_id=item.id,reserved_stock_quantity=7,credited_requirement_quantity=13,yield_factor=2,status='active')
            db.add(reservation);db.flush();reservation_id=reservation.id
            lot.quantity_available=0;lot.quantity_reserved=7
        db.commit()
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client,'n035-admin')
        detail=client.get(f"/api/warehouse/stocktake/locations/{ids['location']}").json()
        raw=next((r for r in detail['lots'] if r['id']==lot_id),None)
        assert raw is not None, '刚入库片料必须出现在手机盘点列表'
        assert raw['product_name']=='通用原材料'
        assert raw['unit']=='sheets' and raw['inventory_code']=='A1B'
        payload=_submission_payload(client,ids['location'],key='mixed-sheet-count',counts={lot_id:5})
        if fail_adjust:
            from app.services import stocktake
            def fail():
                raise RuntimeError("injected adjustment failure")
            monkeypatch.setattr(stocktake,"_movement_number",fail)
            assert client.post('/api/warehouse/stocktakes/confirm',json=payload).status_code==500
            with factory() as db:
                assert db.get(InventoryLot,lot_id).quantity_reserved==7
                assert db.get(InventoryReservation,reservation_id).released_stock_quantity==0
                assert not list(db.scalars(select(InventoryMovement).where(InventoryMovement.inventory_lot_id==lot_id)))
            return
        for _ in range(2):
            result=client.post('/api/warehouse/stocktakes/confirm',json=payload)
            assert result.status_code==201,result.text
    with factory() as db:
        assert db.get(InventoryLot,lot_id).quantity_available==(0 if reserved else 5)
        if reserved:
            row=db.get(InventoryReservation,reservation_id)
            assert row.released_stock_quantity==2 and row.released_requirement_quantity==3
            assert db.get(OrderItem,row.order_item_id).quantity==13
        moves=list(db.scalars(select(InventoryMovement).where(InventoryMovement.inventory_lot_id==lot_id)))
        assert len(moves)==(2 if reserved else 1)
        assert all(m.quantity==2 and m.unit=='sheets' for m in moves)


