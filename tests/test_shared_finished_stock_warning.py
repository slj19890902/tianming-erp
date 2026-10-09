"""Warnings must read approved shared lots once, without changing ownership."""
import pytest

from app.models.shared_finished_stock import SharedFinishedGroup
from app.models.stock_replenishment import InventoryStockPolicy
from app.services.stock_replenishment import (
    current_policy_quantity, finished_product_quantity_summary, stock_policy_dict,
)
from tests.test_finished_goods_inventory_reservation import reservation_db, add_lot
from tests.test_shared_finished_stock import setup_pair, activate, reserve_for


def summary(db, product):
    return finished_product_quantity_summary(
        db, product_id=product.id, customer_id=product.customer_id,
    )


@pytest.mark.parametrize("same_code", [True, False])
def test_shared_warning_and_quick_summary_read_one_physical_pool(reservation_db, same_code):
    db, data = reservation_db
    target, item, lot = setup_pair(db, data, same_code=same_code)
    activate(db, data, target, lot)
    policies = []
    for product in (data["product"], target):
        policy = InventoryStockPolicy(
            policy_name="虚构共用预警", target_inventory_type="finished",
            product_id=product.id, customer_id=product.customer_id,
            warning_quantity=40, target_quantity=100, active=True,
        )
        db.add(policy)
        policies.append(policy)
    db.commit()
    original_owner = lot.finished_detail.owner_customer_id
    reserve_for(db, data, item, lot, 20, "warning-reserved")
    db.commit()
    before = (lot.quantity_available, lot.quantity_reserved, lot.version)
    for product, policy in zip((data["product"], target), policies):
        value = summary(db, product)
        assert value["available_quantity"] == 50
        assert value["physical_unconsumed_quantity"] == 50
        assert value["allocatable_available_quantity"] == 30
        assert value["reserved_quantity"] == 20
        assert current_policy_quantity(db, policy) == 50
        warning = stock_policy_dict(db, policy)
        assert warning["available_quantity"] == 50
        assert warning["warning_triggered"] is False
        assert warning["suggested_replenishment_quantity"] == 50
    assert (lot.quantity_available, lot.quantity_reserved, lot.version) == before
    assert lot.finished_detail.owner_customer_id == original_owner


@pytest.mark.parametrize("blocker", ["unconfirmed", "disabled", "product_changed", "lot_changed", "inactive", "wrong_customer"])
def test_warning_keeps_shared_identity_status_and_customer_gates(reservation_db, blocker):
    db, data = reservation_db
    target, _, lot = setup_pair(db, data)
    if blocker != "unconfirmed":
        result = activate(db, data, target, lot)
        if blocker == "disabled":
            db.get(SharedFinishedGroup, result["group_id"]).enabled = False
        elif blocker == "product_changed":
            target.print_content = "变更为客户专用印刷"
        elif blocker == "lot_changed":
            lot.finished_detail.physical_basis_json = None
        elif blocker == "inactive":
            lot.status = "closed"
        db.commit()
    if blocker == "wrong_customer":
        value = finished_product_quantity_summary(
            db, product_id=target.id, customer_id=data["customer"].id,
        )
    else:
        value = summary(db, target)
    assert value["available_quantity"] == 0


def test_warning_does_not_share_unapproved_same_code_lots(reservation_db):
    db, data = reservation_db
    target, _, lot = setup_pair(db, data)
    activate(db, data, target, lot)
    add_lot(db, data, quantity=17, key="dedicated-after-sharing")
    db.commit()
    assert summary(db, target)["available_quantity"] == 50
    assert summary(db, data["product"])["available_quantity"] == 67
