import pytest
from fastapi import HTTPException

from app.api.deliveries import _cancel_delivery
from app.models.delivery import Delivery
from app.models.order import OrderItem
from app.services.multilevel_bom_delivery_boundary import validate_cancel_execution_boundary
from tests.test_multilevel_bom_orders import context
from tests.test_bom_cutover_source_read import cutover_read_fixture
from tests.test_bom_cutover_delivery import historical_delivery_fixture
from tests.test_composite_component_delivery_quantities import _delivery


@pytest.mark.parametrize("delivered", [20, 60])
def test_actual_cancel_rejects_old_boundary_or_history_and_rolls_back(cutover_read_fixture, delivered):
    db, actor, item, _, _, _ = cutover_read_fixture
    delivery, _, allocation = historical_delivery_fixture(cutover_read_fixture)
    item.delivered_quantity = delivered
    db.commit()
    # At 60, numeric remainder is still >= 20: source identity must reject it.
    with pytest.raises(HTTPException) as error:
        _cancel_delivery(delivery.id, db=db, user=actor)
    assert error.value.status_code == 409
    assert "转换" in error.value.detail
    db.expire_all()
    assert db.get(Delivery, delivery.id).status == "dispatched"
    assert db.get(OrderItem, item.id).delivered_quantity == delivered
    assert allocation.reversed_quantity == 0 and allocation.status == "active"


def test_cancel_current_quantity_cannot_cross_boundary(cutover_read_fixture):
    db, actor, item, _, _, _ = cutover_read_fixture
    item.delivered_quantity = 30
    delivery, _ = _delivery(db, customer_id=136, order_item_id=item.id, number="CUTOVER-CANCEL-BOUNDARY", quantity=12)
    delivery.status = "dispatched"
    db.commit()
    with pytest.raises(HTTPException) as error:
        _cancel_delivery(delivery.id, db=db, user=actor)
    assert error.value.status_code == 409
    db.expire_all()
    assert delivery.status == "dispatched" and item.delivered_quantity == 30


def test_current_epoch_boundary_accepts_cancel_without_source_writes(cutover_read_fixture):
    db, _, item, _, _, _ = cutover_read_fixture
    item.delivered_quantity = 25
    db.commit()
    validate_cancel_execution_boundary(db, item=item, delivery_item_ids=[], quantity=5)
    assert item.delivered_quantity == 25


def test_actual_cancel_current_sources_restores_to_boundary(cutover_read_fixture):
    db, actor, item, _, current, _ = cutover_read_fixture
    source = next(row for row in current if row.component_product_id == 2)
    delivery, _, allocation = historical_delivery_fixture(cutover_read_fixture, source=source)
    item.delivered_quantity = 40
    db.commit()
    _cancel_delivery(delivery.id, db=db, user=actor)
    db.expire_all()
    assert item.delivered_quantity == 20
    assert delivery.status == "pending"
    assert allocation.reversed_quantity == 20 and allocation.status == "reversed"


def test_actual_cancel_checks_corrupt_manifest_before_reversing(cutover_read_fixture):
    db, actor, item, _, _, cutover = cutover_read_fixture
    item.delivered_quantity = 25
    cutover.basis_hash = "0"*64
    delivery, _ = _delivery(db, customer_id=136, order_item_id=item.id, number="CUTOVER-CANCEL-BAD", quantity=1)
    delivery.status = "dispatched"
    db.commit()
    with pytest.raises(HTTPException) as error:
        _cancel_delivery(delivery.id, db=db, user=actor)
    assert error.value.status_code == 409
    db.expire_all()
    assert delivery.status == "dispatched" and item.delivered_quantity == 25
