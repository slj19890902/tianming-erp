"""Exercise the owner-confirmed repair only inside run_task's isolated copy."""
import json
import os
import sqlite3
from decimal import Decimal
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session

from app.core.database import create_sqlite_engine
from app.models.delivery import DeliveryItem
from app.models.order import Order, OrderItem
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot, InventoryReservation
from app.services.packaging_order_repair_20261016 import preview, apply
from app.services.order_business_status import build_order_business_statuses
from app.services.external_packaging_purchase import (
    build_external_purchase_preview, list_external_purchase_routing_rows,
    ExternalPurchaseContractError,
)


def main():
    assert os.environ['ERP_ENVIRONMENT'] == 'test'
    path = Path(os.environ['ERP_DATABASE_PATH'])
    assert 'copies' in path.parts and path.is_file()
    config = Config('alembic.ini')
    command.upgrade(config, 'head')
    command.downgrade(config, 'eq1015sa')
    command.upgrade(config, 'head')
    engine = create_sqlite_engine(path)
    with Session(engine) as db:
        actor = db.get(User, 1)
        before = preview(db, actor=actor)
        assert before['delivery_version'] == 4
        assert [Decimal(line['delivery_unit_price']) for line in before['lines']] == [Decimal('9.643'), Decimal('3.077')]
        assert [Decimal(line['order_unit_price']) for line in before['lines']] == [Decimal('9.64'), Decimal('3.292')]
        db.rollback()
    with Session(engine) as db:
        actor = db.get(User, 1)
        result = apply(db, actor=actor, reviewed_hash=before['reviewed_hash'])
        db.commit()
        assert result['target_stock'] == {'product_80020549': 2700, 'product_82021188': 3600}
    with Session(engine) as db:
        actor = db.get(User, 1)
        replay = apply(db, actor=actor, reviewed_hash=before['reviewed_hash'])
        assert replay['replayed'] is True
        db.rollback()
        assert [(db.get(OrderItem, i).delivered_quantity) for i in (10628,10629)] == [100,100]
        assert db.get(Order, 9790).status == 'delivered'
        assert [(db.get(InventoryLot, i).quantity_available,db.get(InventoryLot, i).quantity_reserved) for i in (923,924,1473)] == [(0,0),(2700,0),(3600,0)]
        assert [db.get(InventoryReservation,i).status for i in (1067,1068)] == ['released','released']
        assert [db.get(DeliveryItem,i).is_current for i in (1096,1097)] == [True,True]
        assert [db.get(DeliveryItem,i).unit_price_snapshot for i in (1096,1097)] == [Decimal('9.643'), Decimal('3.077')]
        projection = build_order_business_statuses(db, [db.get(Order, 9790)])[9790]
        assert [projection['items'][i]['business_delivered_quantity'] for i in (10628,10629)] == [100,100]
        assert [projection['items'][i]['business_remaining_quantity'] for i in (10628,10629)] == [0,0]
        try:
            build_external_purchase_preview(db, 9790)
        except ExternalPurchaseContractError as error:
            assert '订单已终止' in error.message or '没有仍可继续履约' in error.message
        else:
            raise AssertionError('completed order unexpectedly offers external purchase')
        pending = list_external_purchase_routing_rows(db, visible_customer_ids={137})
        assert not {10628,10629}.intersection({r['order_item_id'] for r in pending})
    engine.dispose()
    with sqlite3.connect(path) as raw:
        assert raw.execute('PRAGMA foreign_key_check').fetchall() == []
        assert raw.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    print(json.dumps({'repair':'passed','replay':'passed','expected_stocks':[2700,3600]}))


if __name__ == '__main__':
    main()
