from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.user import User
from app.models.warehouse_inventory import (
    InventoryMovement,
    InventoryReservation,
    WarehouseLocation,
)
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    active_finished_reserved_qty,
    finished_inventory_candidates,
    manual_finished_in,
    release_finished_reservation,
    reserve_finished_inventory,
)


@pytest.fixture()
def reservation_db(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "finished-reservation.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="inventory-admin",
            password_hash="test",
            role="admin",
            real_name="库存管理员",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=7001,
            customer_code="FG-A",
            name="成品库存客户A",
            payment_term_days=0,
            credit_limit=0,
        )
        other_customer = Customer(
            customer_number=7002,
            customer_code="FG-B",
            name="成品库存客户B",
            payment_term_days=0,
            credit_limit=0,
        )
        db.add_all([admin, customer, other_customer])
        db.flush()
        product = Product(
            customer_id=customer.id,
            product_code="FG-P001",
            customer_material_code="FG-M001",
            product_name="成品测试箱",
            box_category="normal",
            length_mm=800,
            width_mm=200,
            height_mm=100,
            default_material_code="A416D",
            flute_type="BE",
        )
        other_product = Product(
            customer_id=customer.id,
            product_code="FG-P002",
            customer_material_code="FG-M002",
            product_name="其他产品",
            box_category="normal",
        )
        location = WarehouseLocation(
            location_code="FG-01",
            location_name="成品一号库位",
            warehouse_type="finished",
        )
        db.add_all([product, other_product, location])
        db.flush()
        order = Order(
            order_number="TM-FG-001",
            customer_id=customer.id,
            order_date=date.today(),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("100"),
        )
        db.add(order)
        db.flush()
        item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=100,
            unit_price=Decimal("1"),
            subtotal=Decimal("100"),
            material_status="pending",
            requisition_status="未报料",
            snapshot_product_code=product.product_code,
            snapshot_product_name=product.product_name,
            snapshot_spec="800×200×100mm",
            snapshot_material="A416D",
            snapshot_pieces_per_box=1,
            snapshot_report_length_mm=800,
            snapshot_report_width_mm=200,
        )
        db.add(item)
        db.flush()
        db.commit()
        yield db, {
            "admin": admin,
            "customer": customer,
            "other_customer": other_customer,
            "product": product,
            "other_product": other_product,
            "location": location,
            "order": order,
            "item": item,
        }


def add_lot(db: Session, data: dict, *, quantity: int = 50, key: str):
    return manual_finished_in(
        db,
        customer_id=data["customer"].id,
        product_id=data["product"].id,
        location_id=data["location"].id,
        quantity=quantity,
        stock_date=date.today(),
        source_type="manual",
        remarks=None,
        operator_id=data["admin"].id,
        idempotency_key=key,
    )


def reserve(db: Session, data: dict, lot, quantity: int, key: str):
    return reserve_finished_inventory(
        db,
        order_item_id=data["item"].id,
        inventory_lot_id=lot.id,
        quantity=quantity,
        expected_version=lot.version,
        operator_id=data["admin"].id,
        idempotency_key=key,
        warning_acknowledged_codes=(
            ["GENERAL_FINISHED_STOCK"] if lot.finished_detail.is_general else []
        ),
    )


def test_candidates_use_exact_product_customer_and_active_available_rules(
    reservation_db,
) -> None:
    db, data = reservation_db
    dedicated = add_lot(db, data, key="candidate-dedicated")
    general = add_lot(db, data, key="candidate-general")
    general.finished_detail.is_general = True
    general.finished_detail.owner_customer_id = None
    other_customer = add_lot(db, data, key="candidate-other-customer")
    other_customer.finished_detail.owner_customer_id = data["other_customer"].id
    frozen = add_lot(db, data, key="candidate-frozen")
    frozen.status = "frozen"
    zero = add_lot(db, data, key="candidate-zero")
    zero.quantity_available = 0
    other_product = add_lot(db, data, key="candidate-other-product")
    other_product.finished_detail.product_id = data["other_product"].id
    db.flush()

    rows = finished_inventory_candidates(db, data["item"].id)
    ids = {row.id for row in rows}
    assert ids == {dedicated.id, general.id}
    assert general.finished_detail.is_general is True


def test_general_inventory_requires_explicit_acknowledgement(reservation_db) -> None:
    db, data = reservation_db
    lot = add_lot(db, data, key="general-ack-lot")
    lot.finished_detail.is_general = True
    lot.finished_detail.owner_customer_id = None
    db.flush()
    with pytest.raises(WarehouseInventoryError, match="通用库存必须人工确认"):
        reserve_finished_inventory(
            db,
            order_item_id=data["item"].id,
            inventory_lot_id=lot.id,
            quantity=10,
            expected_version=lot.version,
            operator_id=data["admin"].id,
            idempotency_key="general-no-ack",
            warning_acknowledged_codes=[],
        )


def test_reserve_updates_balances_writes_reservation_and_movement(reservation_db) -> None:
    db, data = reservation_db
    lot = add_lot(db, data, quantity=60, key="reserve-lot")
    row = reserve(db, data, lot, 30, "reserve-30")
    db.flush()
    db.refresh(lot)
    assert data["item"].quantity == 100
    assert (lot.quantity_available, lot.quantity_reserved) == (30, 30)
    assert row.status == "active"
    assert row.credited_requirement_quantity == 30
    movement = db.scalar(
        select(InventoryMovement).where(InventoryMovement.reservation_id == row.id)
    )
    assert movement.movement_type == "reserve"
    assert (movement.before_available, movement.after_available) == (60, 30)
    assert (movement.before_reserved, movement.after_reserved) == (0, 30)


def test_reserve_guards_quantity_version_and_idempotency(reservation_db) -> None:
    db, data = reservation_db
    lot = add_lot(db, data, quantity=120, key="guard-lot")
    first = reserve(db, data, lot, 30, "same-reserve")
    repeated = reserve_finished_inventory(
        db,
        order_item_id=data["item"].id,
        inventory_lot_id=lot.id,
        quantity=30,
        expected_version=1,
        operator_id=data["admin"].id,
        idempotency_key="same-reserve",
        warning_acknowledged_codes=[],
    )
    assert repeated.id == first.id
    db.refresh(lot)
    assert lot.quantity_reserved == 30
    with pytest.raises(WarehouseInventoryError, match="刷新候选"):
        reserve_finished_inventory(
            db,
            order_item_id=data["item"].id,
            inventory_lot_id=lot.id,
            quantity=1,
            expected_version=1,
            operator_id=data["admin"].id,
            idempotency_key="wrong-version",
            warning_acknowledged_codes=[],
        )
    with pytest.raises(WarehouseInventoryError, match="剩余可抵扣"):
        reserve_finished_inventory(
            db,
            order_item_id=data["item"].id,
            inventory_lot_id=lot.id,
            quantity=71,
            expected_version=lot.version,
            operator_id=data["admin"].id,
            idempotency_key="over-order",
            warning_acknowledged_codes=[],
        )


def test_release_restores_balances_and_cannot_repeat(reservation_db) -> None:
    db, data = reservation_db
    lot = add_lot(db, data, quantity=50, key="release-lot")
    reservation = reserve(db, data, lot, 20, "reserve-release")
    released = release_finished_reservation(
        db,
        reservation_id=reservation.id,
        operator_id=data["admin"].id,
        release_reason="订单调整",
        idempotency_key="release-once",
    )
    db.flush()
    db.refresh(lot)
    assert released.status == "released"
    assert (lot.quantity_available, lot.quantity_reserved) == (50, 0)
    movement = db.scalar(
        select(InventoryMovement).where(
            InventoryMovement.reservation_id == reservation.id,
            InventoryMovement.movement_type == "release_reserve",
        )
    )
    assert movement is not None
    with pytest.raises(WarehouseInventoryError, match="不能重复释放"):
        release_finished_reservation(
            db,
            reservation_id=reservation.id,
            operator_id=data["admin"].id,
            release_reason="再次释放",
            idempotency_key="release-twice",
        )


def test_requisition_pending_uses_remaining_production_quantity(reservation_db) -> None:
    from app.api.requisition import pending_requisitions

    db, data = reservation_db
    lot = add_lot(db, data, quantity=100, key="req-lot")
    reserve(db, data, lot, 30, "req-reserve")
    result = pending_requisitions(db=db, _user=data["admin"])
    row = next(item for item in result["items"] if item["item_id"] == data["item"].id)
    assert row["quantity"] == 100
    assert row["finished_inventory_reserved_qty"] == 30
    assert row["production_required_qty"] == 70
    assert row["required_piece_qty"] == 70
    assert row["requisition_qty"] == 70
    data["item"].snapshot_pieces_per_box = 2
    result = pending_requisitions(db=db, _user=data["admin"])
    row = next(item for item in result["items"] if item["item_id"] == data["item"].id)
    assert row["required_piece_qty"] == 140


def test_requisition_batch_uses_remaining_quantity_and_keeps_order_quantity(
    reservation_db,
) -> None:
    from app.api.requisition import RequisitionBatchCreate, create_batch

    db, data = reservation_db
    lot = add_lot(db, data, quantity=100, key="batch-lot")
    reserve(db, data, lot, 30, "batch-reserve")
    result = create_batch(
        payload=RequisitionBatchCreate(
            supplier_name="测试供应商",
            items=[
                {
                    "order_item_id": data["item"].id,
                    "inventory_deducted_qty": 0,
                    "requisition_qty": 999,
                    "cardboard_len": 800,
                    "cardboard_width": 200,
                    "special_process": "一开一",
                }
            ],
        ),
        db=db,
        user=data["admin"],
    )
    assert result["items"][0]["requisition_qty"] == 70
    assert data["item"].quantity == 100


def test_supplier_draft_derives_active_reservation_without_changing_stock(
    reservation_db,
) -> None:
    from app.api.requisition import (
        PendingSupplierOrderCreatePayload,
        PendingSupplierOrderFinalizePayload,
        create_supplier_orders_from_pending_selection,
        preview_supplier_orders_from_pending_selection,
    )
    from app.models.supplier_requisition_order import SupplierRequisitionOrder

    db, data = reservation_db
    lot = add_lot(db, data, quantity=100, key="supplier-draft-lot")
    reservation = reserve(db, data, lot, 30, "supplier-draft-reserve")
    preview = preview_supplier_orders_from_pending_selection(
        payload=PendingSupplierOrderCreatePayload(
            selections=[
                {
                    "type": "order_item",
                    "order_item_id": data["item"].id,
                    "supplier_name": "测试供应商",
                    "report_length_mm": 800,
                    "report_width_mm": 200,
                    "cutting_mode": "一开一",
                }
            ]
        ),
        db=db,
        _user=data["admin"],
    )
    line = preview["supplier_groups"][0]["lines"][0]
    assert line["inventory_deducted_qty"] == 30
    assert line["production_required_qty"] == 70

    # The browser no longer submits either legacy deduction field. The server
    # must derive the active reservation again at save time.
    line.pop("inventory_deducted_qty", None)
    for source in line["source_items"]:
        source.pop("inventory_deducted_qty", None)
    result = create_supplier_orders_from_pending_selection(
        payload=PendingSupplierOrderFinalizePayload.model_validate(preview),
        db=db,
        user=data["admin"],
    )

    created = db.get(
        SupplierRequisitionOrder,
        result["created_orders"][0]["supplier_order_id"],
    )
    db.refresh(data["item"])
    db.refresh(lot)
    db.refresh(reservation)
    assert created.total_quantity == 70
    assert created.stock_deduction_qty == 30
    assert created.requisition_qty == 70
    assert data["item"].quantity == 100
    assert data["item"].delivered_quantity == 0
    assert data["item"].inventory_deducted_qty == 0
    assert reservation.status == "active"
    assert (lot.quantity_available, lot.quantity_reserved) == (70, 30)


def configure_exact_double_splice_reservation(
    db: Session,
    data: dict,
    *,
    key_prefix: str,
):
    item = data["item"]
    item.quantity = 10
    item.subtotal = Decimal("10")
    item.snapshot_pieces_per_box = 2
    item.snapshot_splice_mode = "double"
    item.special_process = "一开一"
    item.requisition_qty = 20
    lot = add_lot(db, data, quantity=20, key=f"{key_prefix}-lot")
    reservation = reserve(db, data, lot, 9, f"{key_prefix}-reservation")
    db.flush()
    return lot, reservation


def add_matching_pending_item(db: Session, data: dict, *, suffix: str) -> OrderItem:
    order = Order(
        order_number=f"TM-FG-{suffix}",
        customer_id=data["customer"].id,
        order_date=date.today(),
        status="pending_production",
        payment_status="unpaid",
        total_amount=Decimal("10"),
    )
    db.add(order)
    db.flush()
    item = OrderItem(
        order_id=order.id,
        product_id=data["product"].id,
        quantity=10,
        unit_price=Decimal("1"),
        subtotal=Decimal("10"),
        material_status="pending",
        requisition_status="未报料",
        requisition_qty=20,
        special_process="一开一",
        snapshot_product_code=data["product"].product_code,
        snapshot_product_name=data["product"].product_name,
        snapshot_spec="800×200×100mm",
        snapshot_material="A416D",
        snapshot_pieces_per_box=2,
        snapshot_splice_mode="double",
        snapshot_report_length_mm=800,
        snapshot_report_width_mm=200,
    )
    db.add(item)
    db.flush()
    return item


def test_exact_double_splice_demand_is_recomputed_at_preview_and_save(
    reservation_db,
) -> None:
    from app.api.requisition import (
        PendingSupplierOrderCreatePayload,
        PendingSupplierOrderFinalizePayload,
        create_supplier_orders_from_pending_selection,
        pending_requisitions,
        preview_supplier_orders_from_pending_selection,
    )
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    db, data = reservation_db
    lot, reservation = configure_exact_double_splice_reservation(
        db,
        data,
        key_prefix="exact-draft",
    )
    db.refresh(lot)
    lot_before = (
        lot.quantity_available,
        lot.quantity_reserved,
        lot.quantity_consumed,
        lot.version,
    )
    movement_count_before = len(db.scalars(select(InventoryMovement)).all())

    pending = pending_requisitions(db=db, _user=data["admin"])
    row = next(
        item
        for item in pending["items"]
        if item["item_id"] == data["item"].id
    )
    assert row["quantity"] == 10
    assert row["finished_inventory_reserved_qty"] == 9
    assert row["production_required_qty"] == 1
    assert row["pieces_per_box"] == 2
    assert row["required_piece_qty"] == 2
    assert row["cutting_factor"] == 1
    assert row["requisition_qty"] == 2

    preview = preview_supplier_orders_from_pending_selection(
        payload=PendingSupplierOrderCreatePayload(
            selections=[
                {
                    "type": "order_item",
                    "order_item_id": data["item"].id,
                    "supplier_name": "测试供应商",
                    "report_length_mm": 800,
                    "report_width_mm": 200,
                    "cutting_mode": "一开一",
                }
            ]
        ),
        db=db,
        _user=data["admin"],
    )
    line = preview["supplier_groups"][0]["lines"][0]
    assert line["finished_inventory_reserved_qty"] == 9
    assert line["production_required_qty"] == 1
    assert line["required_piece_qty"] == 2
    assert line["requisition_qty"] == 2
    assert line["source_items"][0]["requisition_qty"] == 2

    # Simulate a stale browser carrying the pre-reservation value. Save must
    # derive the current value again instead of persisting 20.
    line["requisition_qty"] = 20
    line.pop("inventory_deducted_qty", None)
    line.pop("finished_inventory_reserved_qty", None)
    for source in line["source_items"]:
        source["requisition_qty"] = 20
        source.pop("inventory_deducted_qty", None)
        source.pop("finished_inventory_reserved_qty", None)
    result = create_supplier_orders_from_pending_selection(
        payload=PendingSupplierOrderFinalizePayload.model_validate(preview),
        db=db,
        user=data["admin"],
    )

    created = db.get(
        SupplierRequisitionOrder,
        result["created_orders"][0]["supplier_order_id"],
    )
    created_item = db.scalar(
        select(SupplierRequisitionOrderItem).where(
            SupplierRequisitionOrderItem.supplier_order_id == created.id
        )
    )
    db.refresh(data["item"])
    db.refresh(lot)
    db.refresh(reservation)
    assert created.total_quantity == 1
    assert created.stock_deduction_qty == 9
    assert created.required_piece_qty == 2
    assert created.requisition_qty == 2
    assert created_item.quantity == 1
    assert created_item.stock_deduction_qty == 9
    assert created_item.required_piece_qty == 2
    assert created_item.requisition_qty == 2
    assert data["item"].quantity == 10
    assert data["item"].delivered_quantity == 0
    assert data["item"].inventory_deducted_qty == 0
    assert data["item"].requisition_qty == 2
    assert reservation.status == "active"
    assert reservation.credited_requirement_quantity == 9
    assert (
        lot.quantity_available,
        lot.quantity_reserved,
        lot.quantity_consumed,
        lot.version,
    ) == lot_before
    assert len(db.scalars(select(InventoryMovement)).all()) == movement_count_before


def test_exact_active_reservation_rejects_forged_deduction_at_save(
    reservation_db,
) -> None:
    from app.api.requisition import (
        PendingSupplierOrderCreatePayload,
        PendingSupplierOrderFinalizePayload,
        create_supplier_orders_from_pending_selection,
        preview_supplier_orders_from_pending_selection,
    )
    from app.models.supplier_requisition_order import SupplierRequisitionOrder

    db, data = reservation_db
    lot, reservation = configure_exact_double_splice_reservation(
        db,
        data,
        key_prefix="exact-forged",
    )
    db.commit()
    preview = preview_supplier_orders_from_pending_selection(
        payload=PendingSupplierOrderCreatePayload(
            selections=[
                {
                    "type": "order_item",
                    "order_item_id": data["item"].id,
                    "supplier_name": "测试供应商",
                    "report_length_mm": 800,
                    "report_width_mm": 200,
                    "cutting_mode": "一开一",
                }
            ]
        ),
        db=db,
        _user=data["admin"],
    )
    preview["supplier_groups"][0]["lines"][0][
        "inventory_deducted_qty"
    ] = 8
    with pytest.raises(HTTPException, match="真实库存预占"):
        create_supplier_orders_from_pending_selection(
            payload=PendingSupplierOrderFinalizePayload.model_validate(preview),
            db=db,
            user=data["admin"],
        )

    db.refresh(lot)
    db.refresh(reservation)
    assert db.scalar(select(SupplierRequisitionOrder.id)) is None
    assert reservation.status == "active"
    assert (lot.quantity_available, lot.quantity_reserved) == (11, 9)


def test_exact_double_splice_ordinary_batch_ignores_stale_requisition_qty(
    reservation_db,
) -> None:
    from app.api.requisition import RequisitionBatchCreate, create_batch
    from app.models.requisition import RequisitionItem

    db, data = reservation_db
    lot, reservation = configure_exact_double_splice_reservation(
        db,
        data,
        key_prefix="exact-batch",
    )
    db.refresh(lot)
    lot_before = (
        lot.quantity_available,
        lot.quantity_reserved,
        lot.quantity_consumed,
        lot.version,
    )
    result = create_batch(
        payload=RequisitionBatchCreate(
            supplier_name="测试供应商",
            items=[
                {
                    "order_item_id": data["item"].id,
                    "inventory_deducted_qty": 0,
                    "requisition_qty": 20,
                    "cardboard_len": 800,
                    "cardboard_width": 200,
                    "special_process": "一开一",
                }
            ],
        ),
        db=db,
        user=data["admin"],
    )
    requisition_item = db.scalar(
        select(RequisitionItem).where(
            RequisitionItem.order_item_id == data["item"].id
        )
    )
    db.refresh(lot)
    db.refresh(reservation)
    assert result["items"][0]["finished_inventory_reserved_qty"] == 9
    assert result["items"][0]["production_required_qty"] == 1
    assert result["items"][0]["required_piece_qty"] == 2
    assert result["items"][0]["requisition_qty"] == 2
    assert requisition_item.required_piece_qty == 2
    assert requisition_item.requisition_qty == 2
    assert data["item"].quantity == 10
    assert data["item"].requisition_qty == 2
    assert reservation.status == "active"
    assert (
        lot.quantity_available,
        lot.quantity_reserved,
        lot.quantity_consumed,
        lot.version,
    ) == lot_before


def test_merge_paths_recompute_exact_active_reservation_and_stale_group_rows(
    reservation_db,
) -> None:
    from app.api.requisition import (
        MergeGroupCreatePayload,
        PendingSupplierOrderCreatePayload,
        PendingSupplierOrderFinalizePayload,
        create_merge_group,
        create_supplier_orders_from_pending_selection,
        merge_suggestions,
        pending_requisitions,
        preview_supplier_orders_from_pending_selection,
    )
    from app.models.requisition import RequisitionItem
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    db, data = reservation_db
    lot, reservation = configure_exact_double_splice_reservation(
        db,
        data,
        key_prefix="exact-merged-draft",
    )
    second_item = add_matching_pending_item(db, data, suffix="MERGE-002")

    suggestions = merge_suggestions(db=db, _user=data["admin"])["suggestions"]
    suggestion = next(
        row
        for row in suggestions
        if {member["item_id"] for member in row["members"]}
        == {data["item"].id, second_item.id}
    )
    exact_member = next(
        row
        for row in suggestion["members"]
        if row["item_id"] == data["item"].id
    )
    assert exact_member["finished_inventory_reserved_qty"] == 9
    assert exact_member["production_required_qty"] == 1
    assert exact_member["required_piece_qty"] == 2
    assert exact_member["requisition_qty"] == 2

    group = create_merge_group(
        payload=MergeGroupCreatePayload(
            member_item_ids=[data["item"].id, second_item.id],
            supplier_name="测试供应商",
            report_length_mm=800,
            report_width_mm=200,
            cutting_mode="一开一",
        ),
        db=db,
        user=data["admin"],
    )
    stale_req_item = db.scalar(
        select(RequisitionItem).where(
            RequisitionItem.requisition_id == group["id"],
            RequisitionItem.order_item_id == data["item"].id,
        )
    )
    stale_req_item.required_piece_qty = 20
    stale_req_item.requisition_qty = 20
    db.commit()

    pending = pending_requisitions(db=db, _user=data["admin"])
    group_row = next(
        row
        for row in pending["items"]
        if row.get("merge_group_id") == group["id"]
    )
    exact_member = next(
        row
        for row in group_row["members"]
        if row["item_id"] == data["item"].id
    )
    assert group_row["finished_inventory_reserved_qty"] == 9
    assert group_row["production_required_qty"] == 11
    assert group_row["required_piece_qty"] == 22
    assert group_row["requisition_qty"] == 22
    assert exact_member["finished_inventory_reserved_qty"] == 9
    assert exact_member["production_required_qty"] == 1
    assert exact_member["required_piece_qty"] == 2
    assert exact_member["requisition_qty"] == 2

    preview = preview_supplier_orders_from_pending_selection(
        payload=PendingSupplierOrderCreatePayload(
            selections=[
                {
                    "type": "merge_group",
                    "merge_group_id": group["id"],
                    "supplier_name": "测试供应商",
                    "report_length_mm": 800,
                    "report_width_mm": 200,
                    "cutting_mode": "一开一",
                }
            ]
        ),
        db=db,
        _user=data["admin"],
    )
    line = preview["supplier_groups"][0]["lines"][0]
    assert line["finished_inventory_reserved_qty"] == 9
    assert line["production_required_qty"] == 11
    assert line["required_piece_qty"] == 22
    assert line["requisition_qty"] == 22
    exact_source = next(
        row
        for row in line["source_items"]
        if row["order_item_id"] == data["item"].id
    )
    assert exact_source["finished_inventory_reserved_qty"] == 9
    assert exact_source["production_required_qty"] == 1
    assert exact_source["required_piece_qty"] == 2
    assert exact_source["requisition_qty"] == 2

    line["requisition_qty"] = 40
    line.pop("inventory_deducted_qty", None)
    line.pop("finished_inventory_reserved_qty", None)
    for source in line["source_items"]:
        source["requisition_qty"] = 20
        source.pop("inventory_deducted_qty", None)
        source.pop("finished_inventory_reserved_qty", None)
    lot_before = (
        lot.quantity_available,
        lot.quantity_reserved,
        lot.quantity_consumed,
        lot.version,
    )
    result = create_supplier_orders_from_pending_selection(
        payload=PendingSupplierOrderFinalizePayload.model_validate(preview),
        db=db,
        user=data["admin"],
    )
    created = db.get(
        SupplierRequisitionOrder,
        result["created_orders"][0]["supplier_order_id"],
    )
    created_items = {
        row.order_item_id: row
        for row in db.scalars(
            select(SupplierRequisitionOrderItem).where(
                SupplierRequisitionOrderItem.supplier_order_id == created.id
            )
        ).all()
    }
    db.refresh(lot)
    db.refresh(reservation)
    assert created.total_quantity == 11
    assert created.stock_deduction_qty == 9
    assert created.required_piece_qty == 22
    assert created.requisition_qty == 22
    assert created_items[data["item"].id].quantity == 1
    assert created_items[data["item"].id].stock_deduction_qty == 9
    assert created_items[data["item"].id].required_piece_qty == 2
    assert created_items[data["item"].id].requisition_qty == 2
    assert reservation.status == "active"
    assert (
        lot.quantity_available,
        lot.quantity_reserved,
        lot.quantity_consumed,
        lot.version,
    ) == lot_before


def test_direct_merged_pending_supplier_order_recomputes_current_reservation(
    reservation_db,
) -> None:
    from app.api.requisition import (
        MergeGroupCreatePayload,
        create_merge_group,
        create_supplier_order_from_merge_group,
    )
    from app.models.requisition import RequisitionItem

    db, data = reservation_db
    lot, reservation = configure_exact_double_splice_reservation(
        db,
        data,
        key_prefix="exact-direct-merge",
    )
    second_item = add_matching_pending_item(db, data, suffix="DIRECT-002")
    group = create_merge_group(
        payload=MergeGroupCreatePayload(
            member_item_ids=[data["item"].id, second_item.id],
            supplier_name="测试供应商",
            report_length_mm=800,
            report_width_mm=200,
            cutting_mode="一开一",
        ),
        db=db,
        user=data["admin"],
    )
    stale_req_item = db.scalar(
        select(RequisitionItem).where(
            RequisitionItem.requisition_id == group["id"],
            RequisitionItem.order_item_id == data["item"].id,
        )
    )
    stale_req_item.required_piece_qty = 20
    stale_req_item.requisition_qty = 20
    db.commit()
    db.refresh(lot)
    lot_before = (
        lot.quantity_available,
        lot.quantity_reserved,
        lot.quantity_consumed,
        lot.version,
    )

    result = create_supplier_order_from_merge_group(
        group_id=group["id"],
        db=db,
        user=data["admin"],
    )
    supplier_order = result["supplier_order"]
    exact_source = next(
        row
        for row in supplier_order["source_items"]
        if row["order_item_id"] == data["item"].id
    )
    db.refresh(lot)
    db.refresh(reservation)
    assert supplier_order["total_quantity"] == 11
    assert supplier_order["stock_deduction_qty"] == 9
    assert supplier_order["required_piece_qty"] == 22
    assert supplier_order["requisition_qty"] == 22
    assert exact_source["inventory_deducted_qty"] == 9
    assert exact_source["required_piece_qty"] == 2
    assert exact_source["requisition_qty"] == 2
    assert reservation.status == "active"
    assert (
        lot.quantity_available,
        lot.quantity_reserved,
        lot.quantity_consumed,
        lot.version,
    ) == lot_before


def test_full_reservation_has_zero_requisition_and_is_delivery_eligible(
    reservation_db,
) -> None:
    from app.api.deliveries import _pending_query
    from app.api.requisition import pending_requisitions

    db, data = reservation_db
    lot = add_lot(db, data, quantity=100, key="full-lot")
    reserve(db, data, lot, 100, "full-reserve")
    result = pending_requisitions(db=db, _user=data["admin"])
    row = next(item for item in result["items"] if item["item_id"] == data["item"].id)
    assert row["fully_covered_by_finished_inventory"] is True
    assert row["production_required_qty"] == 0
    assert row["required_piece_qty"] == 0
    assert row["requisition_qty"] == 0
    pending_delivery_ids = {
        item.item_id for item in db.execute(_pending_query()).all()
    }
    assert data["item"].id in pending_delivery_ids


def test_full_reservation_cannot_generate_purchase_requisition(
    reservation_db,
) -> None:
    from app.api.requisition import RequisitionBatchCreate, create_batch

    db, data = reservation_db
    lot = add_lot(db, data, quantity=100, key="full-block-lot")
    reserve(db, data, lot, 100, "full-block-reserve")
    payload = RequisitionBatchCreate(
        supplier_name="测试供应商",
        items=[
            {
                "order_item_id": data["item"].id,
                "inventory_deducted_qty": 0,
                "cardboard_len": 800,
                "cardboard_width": 200,
                "special_process": "一开一",
            }
        ],
    )
    with pytest.raises(HTTPException, match="无需报料"):
        create_batch(payload=payload, db=db, user=data["admin"])


def test_delete_order_releases_active_finished_reservation(reservation_db) -> None:
    from app.api.orders import _delete_orders_in_transaction

    db, data = reservation_db
    lot = add_lot(db, data, quantity=100, key="delete-lot")
    reservation = reserve(db, data, lot, 40, "delete-reserve")
    _delete_orders_in_transaction(
        db,
        orders=[data["order"]],
        user=data["admin"],
    )
    assert db.get(Order, data["order"].id) is None
    released = db.get(InventoryReservation, reservation.id)
    db.refresh(lot)
    assert released.status == "released"
    assert (lot.quantity_available, lot.quantity_reserved) == (100, 0)
    assert active_finished_reserved_qty(db, data["item"].id) == 0


def test_cancel_order_releases_active_finished_reservation(reservation_db) -> None:
    from app.api.orders import OrderStatusRequest, update_order_status

    db, data = reservation_db
    lot = add_lot(db, data, quantity=100, key="cancel-lot")
    reservation = reserve(db, data, lot, 25, "cancel-reserve")
    result = update_order_status(
        order_id=data["order"].id,
        payload=OrderStatusRequest(status="cancelled", remark="客户取消订单"),
        db=db,
        user=data["admin"],
    )
    db.refresh(lot)
    assert result["status"] == "cancelled"
    assert db.get(InventoryReservation, reservation.id).status == "released"
    assert (lot.quantity_available, lot.quantity_reserved) == (100, 0)


def test_ui_contains_real_reservation_controls_and_hides_free_deduction() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "static" / "index.html"
    ).read_text(encoding="utf-8")
    warehouse = (
        Path(__file__).resolve().parents[1] / "static" / "warehouse.html"
    ).read_text(encoding="utf-8")
    for marker in (
        "已预占成品库存",
        "需生产数量",
        "查看可用成品库存",
        "人工确认抵扣",
        "取消抵扣",
        "/api/warehouse/finished/reservations",
        "/api/warehouse/reservations/",
        "成品库存已全额抵扣",
    ):
        assert marker in source
    assert 'v-model.number="line.inventory_deducted_qty"' not in source
    assert "reserve" in warehouse
    assert "release_reserve" in warehouse
    assert "已预占" in warehouse
    assert "已消耗" in warehouse
