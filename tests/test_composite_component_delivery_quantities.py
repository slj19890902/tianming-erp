from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.api.deliveries import (
    _composite_inventory_sources_for_order_item,
    _delivery_list_page_context,
    _delivery_list_summary_context,
    _delivery_response,
    _delivery_summary_response,
    _pick_task_response,
    get_delivery_print_data,
    pending_delivery_items,
)
from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.delivery import (
    Delivery,
    DeliveryItem,
    DeliveryPickTask,
    DeliveryPickTaskItem,
)
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.product_bom import (
    BomComponentDirectDeliveryAllocation,
    SalesOrderItemBomComponent,
    SalesOrderItemBomDemandAdjustment,
)
from app.models.production import (
    ProductionCompletion,
    ProductionCompletionBatch,
    ProductionTask,
)
from app.models.user import User
from app.services.composite_bom_workflow import (
    execute_delivery_component_consumption,
    kit_availability,
    reverse_delivery_component_allocations,
)


def _active_direct_quantity(db, snapshot_id: int) -> int:
    return int(
        db.scalar(
            select(
                func.coalesce(
                    func.sum(
                        BomComponentDirectDeliveryAllocation.consumed_quantity
                        - BomComponentDirectDeliveryAllocation.reversed_quantity
                    ),
                    0,
                )
            ).where(
                BomComponentDirectDeliveryAllocation.sales_order_item_bom_component_id
                == snapshot_id,
                BomComponentDirectDeliveryAllocation.status.in_(("active", "partial")),
            )
        )
        or 0
    )


def _assert_summary_matches_full_total(
    db: Session,
    delivery_id: int,
    expected_total: int,
) -> None:
    detail_payload = _delivery_response(db, delivery_id)
    summary_payload = _delivery_summary_response(
        delivery_id,
        context=_delivery_list_summary_context(db, [delivery_id]),
    )
    assert detail_payload["total_actual_goods_quantity"] == expected_total
    assert summary_payload["total_actual_goods_quantity"] == expected_total


def _delivery(db, *, customer_id: int, order_item_id: int, number: str, quantity: int):
    delivery = Delivery(
        delivery_number=number,
        customer_id=customer_id,
        delivery_date=date.today(),
        status="pending",
        total_quantity=quantity,
    )
    db.add(delivery)
    db.flush()
    item = DeliveryItem(
        delivery_id=delivery.id,
        order_item_id=order_item_id,
        delivered_quantity=quantity,
        ordered_quantity_snapshot=3000,
        order_remaining_snapshot=3000,
    )
    db.add(item)
    db.flush()
    return delivery, item


def test_order_component_override_caps_multi_delivery_and_cancel(
    tmp_path: Path,
) -> None:
    engine = create_sqlite_engine(tmp_path / "component-delivery.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as db:
            customer = Customer(name="组合送货匿名客户")
            db.add(customer)
            db.flush()
            user = User(
                username="component-delivery-admin",
                password_hash="pytest-only",
                role="admin",
                real_name="测试管理员",
                display_name="测试管理员",
                is_active=True,
                must_change_password=False,
            )
            db.add(user)
            parent = Product(
                customer_id=customer.id,
                product_code="KIT-PARENT",
                customer_material_code="KIT-PARENT",
                product_name="统一计价主产品",
                box_style="组合箱",
                is_composite=True,
            )
            component = Product(
                customer_id=customer.id,
                product_code="KIT-LINER",
                customer_material_code="KIT-LINER",
                product_name="订单专用内衬",
                box_style="模切内盒",
                is_internal_component=True,
            )
            db.add_all([parent, component])
            db.flush()
            order = Order(
                order_number="UAT-COMPONENT-DELIVERY-001",
                customer_id=customer.id,
                order_date=date.today(),
                delivery_date=date.today(),
                status="pending_delivery",
                payment_status="unpaid",
                total_amount=Decimal("3000"),
            )
            db.add(order)
            db.flush()
            order_item = OrderItem(
                order_id=order.id,
                product_id=parent.id,
                item_order_number="UAT-COMPONENT-DELIVERY-001-001",
                item_sequence=1,
                quantity=3000,
                delivered_quantity=0,
                unit_price=Decimal("1"),
                subtotal=Decimal("3000"),
                material_status="received",
                snapshot_product_name=parent.product_name,
                snapshot_product_code=parent.product_code,
                snapshot_spec="匿名规格",
                inventory_deducted_qty=0,
                requisition_qty=3000,
                requisition_status="已入库",
                special_process="无",
            )
            db.add(order_item)
            db.flush()
            snapshot = SalesOrderItemBomComponent(
                sales_order_item_id=order_item.id,
                component_product_id=component.id,
                parent_product_version=1,
                component_product_version=1,
                snapshot_schema_version=3,
                order_set_quantity=3000,
                quantity_per_set=Decimal("1"),
                required_piece_quantity=Decimal("3000"),
                display_order=1,
                internal_component_code="KIT-LINER-01",
                is_die_cut=False,
                spare_sheet_quantity=0,
                display_mode="show_on_delivery",
                is_required=True,
                snapshot_component_product_code=component.product_code,
                snapshot_component_product_name=component.product_name,
                snapshot_component_spec="匿名内衬规格",
                snapshot_component_box_category="die_cut_inner",
                snapshot_component_default_cutting_mode="一开一",
            )
            db.add(snapshot)
            db.flush()
            db.add(
                SalesOrderItemBomDemandAdjustment(
                    sales_order_item_bom_component_id=snapshot.id,
                    event_type="component_demand_adjusted",
                    delta_order_set_quantity=0,
                    delta_required_piece_quantity=Decimal("-300"),
                    reason="订单组件需求变更（系统记录）",
                    idempotency_key="uat-component-2700",
                )
            )
            task = ProductionTask(
                order_item_id=order_item.id,
                sales_order_item_bom_component_id=snapshot.id,
                status="completed",
                planned_quantity=2700,
                finished_coverage_snapshot=0,
                ordered_quantity_snapshot=2700,
                material_received_quantity=2700,
                material_input_quantity=2700,
                output_factor=1,
                version=1,
            )
            db.add(task)
            db.flush()
            batch = ProductionCompletionBatch(
                idempotency_key="uat-component-completion",
                request_hash="a" * 64,
                item_count=1,
                completed_at=datetime.now(),
            )
            db.add(batch)
            db.flush()
            completion = ProductionCompletion(
                batch_id=batch.id,
                task_id=task.id,
                order_item_id=order_item.id,
                expected_version=1,
                quantity=2700,
                completion_type="primary",
                material_input_quantity=2700,
                planned_output_quantity=2700,
                actual_output_quantity=2700,
                defective_quantity=0,
                order_reserved_quantity=2700,
                direct_delivery_quantity=2700,
                stock_quantity=0,
                surplus_finished_quantity=0,
                initial_disposition="direct",
                status="posted",
                completed_at=datetime.now(),
            )
            db.add(completion)
            db.commit()

            availability = kit_availability(db, order_item.id)
            assert availability["available_sets"] == 3000
            assert availability["components"][0]["remaining_required_piece_quantity"] == 2700
            planned_sources = _composite_inventory_sources_for_order_item(
                db,
                order_item=order_item,
                planned_delivery_quantity=3000,
                delivery_item_id=None,
                dispatched=False,
            )
            assert sum(row["quantity_to_pick_requirement"] for row in planned_sources) == 2700
            pending = pending_delivery_items(db=db, user=user)
            pending_component = pending["items"][0]["component_lines"][0]
            assert {
                "target_quantity": pending_component["target_quantity"],
                "delivered_quantity": pending_component["delivered_quantity"],
                "remaining_quantity": pending_component["remaining_quantity"],
                "planned_delivery_quantity": pending_component[
                    "planned_delivery_quantity"
                ],
            } == {
                "target_quantity": 2700,
                "delivered_quantity": 0,
                "remaining_quantity": 2700,
                "planned_delivery_quantity": 2700,
            }

            preview, _preview_item = _delivery(
                db,
                customer_id=customer.id,
                order_item_id=order_item.id,
                number="UAT-COMP-DELIVERY-PREVIEW",
                quantity=3000,
            )
            db.commit()
            detail_payload = _delivery_response(db, preview.id)
            list_context = _delivery_list_page_context(db, [preview.id])
            assert _delivery_response(
                db,
                preview.id,
                list_context=list_context,
            ) == detail_payload
            assert [
                line["quantity"]
                for line in detail_payload["items"][0]["actual_goods_lines"]
            ] == [3000, 2700]
            assert detail_payload["total_quantity"] == 3000
            assert detail_payload["total_actual_goods_quantity"] == 5700
            summary_context = _delivery_list_summary_context(db, [preview.id])
            summary_payload = _delivery_summary_response(
                preview.id,
                context=summary_context,
            )
            assert summary_payload["item_count"] == 1
            assert summary_payload["total_quantity"] == 3000
            assert summary_payload["total_actual_goods_quantity"] == 5700
            assert "items" not in summary_payload
            detail_component = detail_payload["items"][0]["component_lines"][0]
            assert detail_component["pricing_included"] is False
            assert detail_component["independent_return_receipt"] is False
            assert detail_component["independent_statement"] is False

            print_payload = get_delivery_print_data(
                preview.id,
                db=db,
                user=user,
            )
            assert [
                line["quantity"] for line in print_payload["actual_goods_items"]
            ] == [3000, 2700]
            assert print_payload["total_quantity"] == 3000
            assert print_payload["total_actual_goods_quantity"] == 5700
            assert (
                print_payload["actual_goods_items"][1]["pricing_included"] is False
            )
            assert (
                print_payload["actual_goods_items"][1][
                    "independent_return_receipt"
                ]
                is False
            )
            assert (
                print_payload["actual_goods_items"][1]["independent_statement"]
                is False
            )

            pick_task = DeliveryPickTask(
                delivery_id=preview.id,
                customer_id=customer.id,
                status="pushed",
                snapshot_version=1,
            )
            db.add(pick_task)
            db.flush()
            db.add(
                DeliveryPickTaskItem(
                    task_id=pick_task.id,
                    delivery_item_id=_preview_item.id,
                    order_item_id=order_item.id,
                    original_quantity=3000,
                    picked_quantity=0,
                    status="pending",
                    product_code_snapshot=parent.product_code,
                    product_name_snapshot=parent.product_name,
                    specification_snapshot="匿名规格",
                )
            )
            db.flush()
            pick_payload = _pick_task_response(db, pick_task)
            assert pick_payload["items"][0]["planned_quantity"] == 3000
            assert pick_payload["items"][0]["is_composite_bom"] is True
            assert [
                row["planned_delivery_quantity"]
                for row in pick_payload["items"][0]["component_lines"]
            ] == [2700]
            assert [
                row["pricing_included"]
                for row in pick_payload["items"][0]["component_lines"]
            ] == [False]
            assert pick_payload["location_plan_complete"] is True
            assert [group["label"] for group in pick_payload["location_groups"]] == [
                "生产区直接拿货"
            ]
            assert [
                (line["product_code"], line["pick_quantity"])
                for line in pick_payload["location_groups"][0]["lines"]
            ] == [("KIT-LINER", 2700), ("KIT-PARENT", 3000)]

            first, first_item = _delivery(
                db,
                customer_id=customer.id,
                order_item_id=order_item.id,
                number="UAT-COMP-DELIVERY-001",
                quantity=1000,
            )
            first_plan = execute_delivery_component_consumption(
                db,
                delivery_item_id=first_item.id,
                delivery_sets=1000,
                operator_id=None,
                operation_key="uat-dispatch-first",
            )
            assert first_plan[0].required_quantity == 1000
            order_item.delivered_quantity = 1000
            first.status = "dispatched"
            db.commit()
            first_detail = _delivery_response(db, first.id)
            first_list_context = _delivery_list_page_context(db, [first.id])
            assert _delivery_response(
                db,
                first.id,
                list_context=first_list_context,
            ) == first_detail
            _assert_summary_matches_full_total(db, first.id, 2000)

            second, second_item = _delivery(
                db,
                customer_id=customer.id,
                order_item_id=order_item.id,
                number="UAT-COMP-DELIVERY-002",
                quantity=2000,
            )
            second_plan = execute_delivery_component_consumption(
                db,
                delivery_item_id=second_item.id,
                delivery_sets=2000,
                operator_id=None,
                operation_key="uat-dispatch-second",
            )
            assert second_plan[0].required_quantity == 1700
            order_item.delivered_quantity = 3000
            second.status = "dispatched"
            db.commit()
            assert _active_direct_quantity(db, snapshot.id) == 2700
            _assert_summary_matches_full_total(db, second.id, 3700)

            reverse_delivery_component_allocations(
                db,
                delivery_item_id=second_item.id,
                operator_id=None,
                operation_key="uat-cancel-second",
            )
            order_item.delivered_quantity = 1000
            second.status = "pending"
            db.commit()
            assert _active_direct_quantity(db, snapshot.id) == 1000
            assert kit_availability(db, order_item.id)["available_sets"] == 2000
            _assert_summary_matches_full_total(db, second.id, 3700)

            second.status = "voided"
            db.commit()
            _assert_summary_matches_full_total(db, second.id, 2000)
            second.status = "pending"
            db.commit()

            third_plan = execute_delivery_component_consumption(
                db,
                delivery_item_id=second_item.id,
                delivery_sets=2000,
                operator_id=None,
                operation_key="uat-dispatch-third",
            )
            assert third_plan[0].required_quantity == 1700
            order_item.delivered_quantity = 3000
            second.status = "dispatched"
            db.commit()
            assert _active_direct_quantity(db, snapshot.id) == 2700
            _assert_summary_matches_full_total(db, second.id, 3700)
    finally:
        engine.dispose()
