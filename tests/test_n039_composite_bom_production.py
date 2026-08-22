from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.product_bom import SalesOrderItemBomComponent
from app.models.production import ProductionCompletion, ProductionTask
from app.models.warehouse_inventory import (
    InventoryPalletItem,
    InventoryReservation,
    WarehouseLocation,
)
from app.services.production_workflow import (
    CompletionCommand,
    ProductionWorkflowError,
    complete_production_batch,
    create_or_refresh_production_task,
    refresh_order_production_status,
    refresh_production_task,
    list_production_tasks,
    transfer_direct_completion_to_stock,
    StockTransferCommand,
)
from app.services.warehouse_twin_dashboard import (
    _parent_delivery_inventory_projections,
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _snapshot(
    *,
    item: OrderItem,
    product: Product,
    display_order: int,
    quantity_per_set: int,
    is_required: bool,
) -> SalesOrderItemBomComponent:
    sets = int(item.quantity)
    return SalesOrderItemBomComponent(
        sales_order_item_id=item.id,
        component_product_id=product.id,
        parent_product_version=1,
        component_product_version=1,
        snapshot_schema_version=2,
        order_set_quantity=sets,
        quantity_per_set=Decimal(quantity_per_set),
        required_piece_quantity=Decimal(sets * quantity_per_set),
        display_order=display_order,
        internal_component_code=f"KIT-S{display_order:02d}",
        is_die_cut=False,
        spare_sheet_quantity=0,
        display_mode="internal_only",
        is_required=is_required,
        snapshot_component_product_code=product.product_code,
        snapshot_component_product_name=product.product_name,
        snapshot_component_spec="800×600",
        snapshot_component_material="K616K / AB",
        snapshot_component_flute_type="AB",
        snapshot_component_box_category="normal",
    )


@pytest.fixture()
def composite_db(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "n039-production.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        customer = Customer(name="N039 组件客户")
        db.add(customer)
        db.flush()
        parent = Product(
            customer_id=customer.id,
            product_code="KIT-001",
            customer_material_code="KIT-001",
            product_name="组合产品",
            box_style="组合箱",
            is_composite=True,
        )
        lid = Product(
            customer_id=customer.id,
            product_code="KIT-LID",
            customer_material_code="KIT-LID",
            product_name="组合盖",
            box_style="组件",
            is_internal_component=True,
        )
        base = Product(
            customer_id=customer.id,
            product_code="KIT-BASE",
            customer_material_code="KIT-BASE",
            product_name="组合底",
            box_style="组件",
            is_internal_component=True,
        )
        optional = Product(
            customer_id=customer.id,
            product_code="KIT-CARD",
            customer_material_code="KIT-CARD",
            product_name="可选卡片",
            box_style="组件",
            is_internal_component=True,
        )
        location = WarehouseLocation(
            location_code="F12-P01",
            location_name="N039 临放位",
            warehouse_type="finished",
            is_active=True,
            is_temporary=True,
            warehouse_floor=3,
            area_code="F12",
            storage_type="temporary_aisle",
            source_version="V11",
        )
        staging_location = WarehouseLocation(
            location_code="F1-DISPATCH-01",
            location_name="一楼待送区",
            warehouse_type="finished",
            is_active=True,
            warehouse_floor=1,
            area_code="DISPATCH",
            storage_type="temporary_aisle",
            placement_status="placed",
            is_temporary=True,
            source_version="P1-25C",
        )
        db.add_all([parent, lid, base, optional, location, staging_location])
        db.flush()
        order = Order(
            order_number="N039-PRODUCTION-001",
            customer_id=customer.id,
            order_date=date.today(),
            delivery_date=date.today(),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("50"),
        )
        db.add(order)
        db.flush()
        item = OrderItem(
            order_id=order.id,
            product_id=parent.id,
            item_order_number="N039-PRODUCTION-001-001",
            item_sequence=1,
            quantity=5,
            delivered_quantity=0,
            unit_price=Decimal("10"),
            subtotal=Decimal("50"),
            material_status="received",
            material_received_at=_now(),
            snapshot_product_name=parent.product_name,
            snapshot_product_code=parent.product_code,
            snapshot_spec="组合规格",
            snapshot_material="K616K / AB",
            snapshot_production_notes="N039 测试",
            inventory_deducted_qty=0,
            requisition_qty=5,
            requisition_status="已入库",
            special_process="无",
            flute_type="AB",
        )
        db.add(item)
        db.flush()
        db.add_all(
            [
                _snapshot(item=item, product=lid, display_order=1, quantity_per_set=2, is_required=True),
                _snapshot(item=item, product=base, display_order=2, quantity_per_set=1, is_required=True),
                _snapshot(item=item, product=optional, display_order=3, quantity_per_set=1, is_required=False),
            ]
        )
        db.commit()
        yield db, item.id, {"lid": lid.id, "base": base.id, "optional": optional.id}, location.id
    engine.dispose()


def _component_tasks(db: Session, order_item_id: int) -> list[ProductionTask]:
    return list(
        db.scalars(
            select(ProductionTask)
            .where(ProductionTask.order_item_id == order_item_id)
            .order_by(ProductionTask.sales_order_item_bom_component_id)
        ).all()
    )


def test_composite_tasks_are_component_piece_tasks_and_optional_does_not_block(
    composite_db,
) -> None:
    db, item_id, _products, _location_id = composite_db
    first = create_or_refresh_production_task(db, item_id)
    tasks = _component_tasks(db, item_id)

    assert first.sales_order_item_bom_component_id is not None
    assert len(tasks) == 3
    assert all(task.sales_order_item_bom_component_id is not None for task in tasks)
    assert [task.planned_quantity for task in tasks] == [10, 5, 5]
    assert all(task.status == "pending" for task in tasks)

    direct_version = tasks[0].version
    direct = complete_production_batch(
        db,
        idempotency_key="n039-direct-lid",
        commands=[
            CompletionCommand(
                task_id=tasks[0].id,
                expected_version=direct_version,
                disposition="direct",
            )
        ],
        operator_id=None,
    )
    assert direct.completions[0].inventory_lot_id is not None
    assert complete_production_batch(
        db,
        idempotency_key="n039-direct-lid",
        commands=[
            CompletionCommand(
                task_id=tasks[0].id,
                expected_version=direct_version,
                disposition="direct",
            )
        ],
        operator_id=None,
    ).replayed is True

    stock_version = tasks[1].version
    stock = complete_production_batch(
        db,
        idempotency_key="n039-stock-base",
        commands=[
            CompletionCommand(
                task_id=tasks[1].id,
                expected_version=stock_version,
                disposition="stock",
                location_id=_location_id,
            )
        ],
        operator_id=None,
    )
    completion = stock.completions[0]
    reservation = db.scalar(
        select(InventoryReservation).where(
            InventoryReservation.inventory_lot_id == completion.inventory_lot_id
        )
    )
    assert reservation is not None
    assert reservation.order_item_id == item_id
    assert reservation.sales_order_item_bom_component_id == tasks[1].sales_order_item_bom_component_id
    from app.models.warehouse_inventory import InventoryLot

    assert db.get(InventoryLot, completion.inventory_lot_id).finished_detail.product_id == _products["base"]

    refresh_order_production_status(db, db.get(OrderItem, item_id).order_id)
    assert db.get(OrderItem, item_id).order.status == "pending_delivery"
    assert tasks[2].status == "pending"  # optional component remains informational.


def test_component_task_exposes_frozen_production_note_separately_from_process(
    composite_db,
) -> None:
    db, item_id, _products, _location_id = composite_db
    snapshot = db.scalar(
        select(SalesOrderItemBomComponent)
        .where(SalesOrderItemBomComponent.sales_order_item_id == item_id)
        .order_by(SalesOrderItemBomComponent.id)
    )
    assert snapshot is not None
    snapshot.snapshot_component_production_process = "模切"
    snapshot.snapshot_component_production_notes = "红色标识朝外，模切边缘重点检查"
    db.flush()
    create_or_refresh_production_task(db, item_id)

    row = next(
        item
        for item in list_production_tasks(db, allowed_customer_ids=None)
        if item["bom_component_snapshot_id"] == snapshot.id
    )
    assert row["production_process"] == "模切"
    assert row["production_notes"] == "红色标识朝外，模切边缘重点检查"

    mobile = (Path(__file__).resolve().parents[1] / "static" / "mobile_erp.html").read_text(
        encoding="utf-8"
    )
    assert "生产工艺：${task.production_process}" in mobile
    assert "生产备注说明：${task.production_notes}" in mobile
    assert "task.production_process || task.production_notes" not in mobile


def test_virtual_composite_component_receipts_do_not_require_parent_board(
    composite_db,
) -> None:
    from app.models.product_bom import RequisitionItemBomSource
    from app.models.requisition import Requisition, RequisitionItem
    from app.services.incoming_receipts import _all_expected_bom_sources_received

    db, item_id, _products, _location_id = composite_db
    item = db.get(OrderItem, item_id)
    item.material_status = "pending"
    item.is_virtual_composite_parent_snapshot = True
    create_or_refresh_production_task(db, item_id)
    assert all(task.status == "waiting_material" for task in _component_tasks(db, item_id))

    batch = Requisition(
        requisition_number="BL-N039-VIRTUAL-COMPONENTS",
        requisition_date=date.today(),
        supplier_name="N039 供应商",
        status="已报料",
    )
    db.add(batch)
    db.flush()
    snapshots = db.scalars(
        select(SalesOrderItemBomComponent)
        .where(SalesOrderItemBomComponent.sales_order_item_id == item_id)
        .order_by(SalesOrderItemBomComponent.id)
    ).all()
    for snapshot in snapshots:
        required = int(snapshot.required_piece_quantity)
        factor = 1
        line = RequisitionItem(
            requisition_id=batch.id,
            order_item_id=item_id,
            inventory_deducted_qty=0,
            requisition_qty=required,
            cardboard_len=1000,
            cardboard_width=700,
            pieces_per_box=1,
            required_piece_qty=required,
            special_process="一开一",
            product_code_snapshot=snapshot.snapshot_component_product_code,
            product_name_snapshot=snapshot.snapshot_component_product_name,
            status="已入库",
        )
        db.add(line)
        db.flush()
        db.add(
            RequisitionItemBomSource(
                requisition_item_id=line.id,
                sales_order_item_bom_component_id=snapshot.id,
                order_set_quantity=int(item.quantity),
                quantity_per_set=snapshot.quantity_per_set,
                required_piece_quantity=snapshot.required_piece_quantity,
                demand_basis="order_sets",
                spare_sheet_quantity=0,
                calculated_purchase_quantity=required // factor,
                calculation_rule_version="bom-demand-cutting-v2",
            )
        )
    db.flush()

    assert _all_expected_bom_sources_received(db, item) is True
    item.material_status = "received"
    refresh_production_task(db, item_id)
    assert all(task.status == "pending" for task in _component_tasks(db, item_id))


def test_component_completion_is_task_scoped_and_transfer_uses_component_product(
    composite_db,
) -> None:
    db, item_id, products, location_id = composite_db
    refresh_production_task(db, item_id, create_if_missing=True)
    task = _component_tasks(db, item_id)[0]

    with pytest.raises(ProductionWorkflowError, match="版本"):
        complete_production_batch(
            db,
            idempotency_key="n039-version-conflict",
            commands=[
                CompletionCommand(
                    task_id=task.id,
                    expected_version=task.version + 1,
                    disposition="direct",
                )
            ],
            operator_id=None,
        )

    completed = complete_production_batch(
        db,
        idempotency_key="n039-transfer-source",
        commands=[
            CompletionCommand(
                task_id=task.id,
                expected_version=task.version,
                disposition="direct",
            )
        ],
        operator_id=None,
    ).completions[0]
    transfer = transfer_direct_completion_to_stock(
        db,
        completion_id=completed.id,
        command=StockTransferCommand(
            location_id=location_id,
            idempotency_key="n039-component-transfer",
        ),
        operator_id=None,
    )
    assert transfer.replayed is False
    from app.models.warehouse_inventory import InventoryLot

    assert (
        db.get(InventoryLot, transfer.transfer.inventory_lot_id)
        .finished_detail.product_id
        == products["lid"]
    )

    with pytest.raises(ProductionWorkflowError, match="仅允许待完工|已存在完工"):
        complete_production_batch(
            db,
            idempotency_key="n039-repeat-task",
            commands=[
                CompletionCommand(
                    task_id=task.id,
                    expected_version=task.version,
                    disposition="direct",
                )
            ],
            operator_id=None,
        )


def test_parent_delivery_components_must_stock_together_on_one_pallet(
    composite_db,
) -> None:
    db, item_id, _products, location_id = composite_db
    item = db.get(OrderItem, item_id)
    item.composite_fulfillment_mode_snapshot = "parent_delivery"
    refresh_production_task(db, item_id, create_if_missing=True)
    tasks = _component_tasks(db, item_id)

    first = complete_production_batch(
        db,
        idempotency_key="n039-parent-stock-first",
        commands=[
            CompletionCommand(
                task_id=tasks[0].id,
                expected_version=tasks[0].version,
                disposition="stock",
                location_id=location_id,
            )
        ],
        operator_id=None,
    ).completions[0]
    first_pallet_id = db.scalar(
        select(InventoryPalletItem.pallet_id).where(
            InventoryPalletItem.inventory_lot_id == first.inventory_lot_id
        )
    )
    assert first_pallet_id is not None

    second = complete_production_batch(
        db,
        idempotency_key="n039-parent-stock-second",
        commands=[
            CompletionCommand(
                task_id=tasks[1].id,
                expected_version=tasks[1].version,
                disposition="stock",
                location_id=location_id,
            )
        ],
        operator_id=None,
    ).completions[0]
    second_pallet_id = db.scalar(
        select(InventoryPalletItem.pallet_id).where(
            InventoryPalletItem.inventory_lot_id == second.inventory_lot_id
        )
    )
    assert second_pallet_id == first_pallet_id
    from app.models.warehouse_inventory import InventoryLot

    lots = list(
        db.scalars(
            select(InventoryLot).where(
                InventoryLot.id.in_((first.inventory_lot_id, second.inventory_lot_id))
            )
        ).all()
    )
    projections = _parent_delivery_inventory_projections(db, lots)
    summaries = [
        row["composite_parent_summary"]
        for row in projections.values()
        if row.get("composite_parent_summary") is not None
    ]
    assert len(summaries) == 1
    assert summaries[0]["inventory_code"] == "KIT-001"
    assert summaries[0]["available_set_quantity"] == 5
    assert summaries[0]["component_lot_count"] == 2


def test_parent_delivery_rejects_direct_component_completion(composite_db) -> None:
    db, item_id, _products, _location_id = composite_db
    item = db.get(OrderItem, item_id)
    item.composite_fulfillment_mode_snapshot = "parent_delivery"
    refresh_production_task(db, item_id, create_if_missing=True)
    task = _component_tasks(db, item_id)[0]

    with pytest.raises(ProductionWorkflowError, match="同一位置齐套入库"):
        complete_production_batch(
            db,
            idempotency_key="n039-parent-direct-rejected",
            commands=[
                CompletionCommand(
                    task_id=task.id,
                    expected_version=task.version,
                    disposition="direct",
                )
            ],
            operator_id=None,
        )


def test_normal_and_a3_items_keep_one_regular_task(tmp_path: Path) -> None:
    engine = create_sqlite_engine(tmp_path / "n039-normal.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        customer = Customer(name="N039 普通客户")
        product = Product(
            customer_id=1,
            product_code="A3-NORMAL",
            customer_material_code="A3-NORMAL",
            product_name="天地盖普通产品",
            box_style="A3天地盖",
        )
        db.add(customer)
        db.flush()
        product.customer_id = customer.id
        db.add(product)
        db.flush()
        order = Order(
            order_number="N039-NORMAL-001",
            customer_id=customer.id,
            order_date=date.today(),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("1"),
        )
        db.add(order)
        db.flush()
        item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            item_order_number="N039-NORMAL-001-001",
            item_sequence=1,
            quantity=1,
            unit_price=Decimal("1"),
            subtotal=Decimal("1"),
            material_status="received",
            snapshot_product_name=product.product_name,
            snapshot_product_code=product.product_code,
            requisition_status="未报料",
            special_process="无",
        )
        db.add(item)
        db.flush()
        task = create_or_refresh_production_task(db, item.id)
        assert task.sales_order_item_bom_component_id is None
        assert len(_component_tasks(db, item.id)) == 1
    engine.dispose()
