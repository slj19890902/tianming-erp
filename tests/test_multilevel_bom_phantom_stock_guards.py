from datetime import date

import pytest
from sqlalchemy import func, select

from app.models.warehouse_inventory import InventoryLot, InventoryMovement
from app.services.multilevel_bom_orders import freeze_master_order_bom
from app.services.warehouse_inventory import (
    WarehouseInventoryError, manual_finished_in, finished_inventory_candidates,
    finished_inventory_candidates_for_bom_component, reserve_finished_inventory,
    reserve_finished_inventory_for_bom_component, _frozen_product_has_stock,
)
from tests.test_multilevel_bom_orders import context
from tests.test_multilevel_bom_separate_master import configure
from tests.test_multilevel_bom_master import save


def test_manual_parent_stock_rejected_before_claiming_any_location(context):
    db, actor, _, _ = context
    configure(db, actor)
    db.commit()
    with pytest.raises(WarehouseInventoryError, match="分别选择真实子件入库"):
        manual_finished_in(db, customer_id=136, product_id=1, location_id=999,
            quantity=100, stock_date=date.today(), source_type="manual", remarks=None,
            operator_id=actor.id, idempotency_key="no-phantom-parent")
    db.commit()
    assert db.scalar(select(func.count()).select_from(InventoryLot)) == 0
    assert db.scalar(select(func.count()).select_from(InventoryMovement)) == 0


def test_both_parent_reservation_routes_reject_using_frozen_mode(context):
    db, actor, item, _ = context
    configure(db, actor)
    frozen = freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    db.commit()
    root = next(s for s in frozen.snapshots if s.component_product_id == 1)
    assert finished_inventory_candidates(db, item.id) == []
    assert finished_inventory_candidates_for_bom_component(db, order_item_id=item.id, bom_snapshot_id=root.id) == []
    common = dict(order_item_id=item.id, inventory_lot_id=999, quantity=1,
        expected_version=1, operator_id=actor.id, warning_acknowledged_codes=[])
    with pytest.raises(WarehouseInventoryError, match="分别选择子件库存抵扣"):
        reserve_finished_inventory(db, **common, idempotency_key="no-parent-reserve")
    with pytest.raises(WarehouseInventoryError, match="不能预占实体库存"):
        reserve_finished_inventory_for_bom_component(db, **common, bom_snapshot_id=root.id,
                                                    idempotency_key="no-parent-component-reserve")
    db.commit()
    assert db.scalar(select(func.count()).select_from(InventoryLot)) == 0


def test_master_edit_cannot_remove_old_frozen_physical_stock_identity(context):
    db, actor, item, _ = context
    save(db, actor, 1, "assembled", [(3, 3, "assembly"), (4, 4, "assembly")])
    freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    db.commit()
    configure(db, actor)
    db.commit()
    assert _frozen_product_has_stock(db, item.id, 1)
