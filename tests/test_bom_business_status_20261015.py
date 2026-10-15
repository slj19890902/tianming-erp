from datetime import date, datetime
import pytest
from sqlalchemy import select
from app.models.order import Order
from app.models.product_bom import SalesOrderItemBomComponent
from app.models.warehouse_inventory import InventoryLot, InventoryReservation, FinishedGoodsInventoryDetail
from app.services.order_business_status import build_order_business_statuses
from tests.test_n039_composite_bom_requisition import composite_requisition_app
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity
from tests.test_multilevel_bom_receipt_flow import seed_graph


@pytest.mark.parametrize('node,amount,release,cancelled,body,ready', [
    (1,10,0,False,False,True),
    (1,10,1,False,False,False),
    (1,10,0,True,False,False),
    (1,10,0,False,True,False),
    (2,100,0,False,False,False),
])
def test_status_uses_real_bom_delivery_capacity(composite_requisition_app, _p181_published_map_identity,
                                              node,amount,release,cancelled,body,ready):
    _,factory=composite_requisition_app
    seed_graph(factory,quantity=10)
    with factory() as db:
        snapshot=db.scalar(select(SalesOrderItemBomComponent).where(
            SalesOrderItemBomComponent.sales_order_item_id==1,
            SalesOrderItemBomComponent.component_product_id==node))
        lot=InventoryLot(lot_number='STATUS-BOM',inventory_type='finished',warehouse_location_id=1,
            quantity_available=0,quantity_reserved=amount,unit='boxes',status='active',source_type='transfer',
            stock_date=date(2026,10,15),last_movement_at=datetime(2026,10,15))
        db.add(lot);db.flush()
        db.add(FinishedGoodsInventoryDetail(inventory_lot_id=lot.id,product_id=node,
            owner_customer_id=1,is_general=False,inventory_code_snapshot='STATUS-BOM',
            product_name_snapshot='隔离状态测试',physical_basis_json='{"inventory_stage":"body"}' if body else '{}'))
        db.add(InventoryReservation(reservation_number='STATUS-BOM',inventory_lot_id=lot.id,
            reservation_type='finished_order',order_id=1,order_item_id=1,
            sales_order_item_bom_component_id=snapshot.id,reserved_stock_quantity=amount,
            credited_requirement_quantity=amount,released_requirement_quantity=release,
            released_stock_quantity=release,status='cancelled' if cancelled else 'active',
            idempotency_key='status-bom',reserved_at=datetime(2026,10,15)))
        db.flush()
        before=db.connection().connection.driver_connection.total_changes
        result=build_order_business_statuses(db,[db.get(Order,1)],include_finance=False)
        assert (result[1]['items'][1]['business_status']=='pending_delivery') is ready
        assert db.connection().connection.driver_connection.total_changes==before
