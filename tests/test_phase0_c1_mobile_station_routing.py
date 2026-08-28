from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from test_p1_21d_mobile_production_materials import _login, mobile_production_app


def _add_order_item(db, *, customer, product, suffix: str):
    from app.models.order import Order, OrderItem

    order = Order(
        order_number=f"C1-{suffix}",
        customer_id=customer.id,
        order_date=date(2026, 8, 20),
        delivery_date=date(2026, 8, 25),
        status="pending_production",
        payment_status="unpaid",
        total_amount=Decimal("10"),
    )
    db.add(order)
    db.flush()
    item = OrderItem(
        order_id=order.id,
        product_id=product.id,
        item_order_number=f"C1-{suffix}-001",
        item_sequence=1,
        quantity=10,
        delivered_quantity=0,
        unit_price=Decimal("1"),
        subtotal=Decimal("10"),
        material_status="received",
        snapshot_product_name=product.product_name,
        snapshot_product_code=product.product_code,
        snapshot_spec="400×300×200mm",
        snapshot_material="K=A",
        requisition_status="已入库",
        special_process="无",
    )
    db.add(item)
    db.flush()
    return item


def _add_regular_task(
    db,
    *,
    customer,
    suffix: str,
    box_style: str,
    print_content_snapshot: str,
    status: str = "pending",
) -> int:
    from app.models.product import Product
    from app.models.production import ProductionTask

    product = Product(
        customer_id=customer.id,
        product_code=f"C1-{suffix}",
        customer_material_code=f"C1-{suffix}",
        product_name=f"C1 {suffix}",
        box_category="normal",
        box_style=box_style,
        print_content="无印刷",
    )
    db.add(product)
    db.flush()
    item = _add_order_item(db, customer=customer, product=product, suffix=suffix)
    task = ProductionTask(
        order_item_id=item.id,
        status=status,
        planned_quantity=10,
        ordered_quantity_snapshot=10,
        material_received_quantity=10,
        material_input_quantity=10,
        output_factor=1,
        readiness_basis="material_received",
        print_content_snapshot=print_content_snapshot,
    )
    db.add(task)
    db.flush()
    return int(task.id)


def _add_component_tasks(db, *, customer, mold) -> dict[str, int]:
    from app.models.product import Product
    from app.models.product_bom import SalesOrderItemBomComponent
    from app.models.production import ProductionTask

    parent = Product(
        customer_id=customer.id,
        product_code="C1-COMP-PARENT",
        customer_material_code="C1-COMP-PARENT",
        product_name="C1 组合父项",
        box_category="normal",
        box_style="衬板",
        print_content="无印刷",
    )
    db.add(parent)
    db.flush()
    item = _add_order_item(
        db,
        customer=customer,
        product=parent,
        suffix="COMP-PARENT",
    )

    facts = (
        ("component_printing", "围板", False, "无印刷"),
        ("component_die", "衬板", True, "无印刷"),
        ("component_both", "衬板", True, "单色印刷"),
    )
    task_ids: dict[str, int] = {}
    for display_order, (key, frozen_box_style, is_die_cut, print_content) in enumerate(
        facts, start=1
    ):
        component_product = Product(
            customer_id=customer.id,
            product_code=f"C1-{key.upper()}",
            customer_material_code=f"C1-{key.upper()}",
            product_name=f"C1 {key}",
            box_category="normal",
            box_style="衬板",
            print_content="无印刷",
        )
        db.add(component_product)
        db.flush()
        snapshot = SalesOrderItemBomComponent(
            sales_order_item_id=item.id,
            component_product_id=component_product.id,
            parent_product_version=1,
            component_product_version=1,
            snapshot_schema_version=3,
            order_set_quantity=10,
            quantity_per_set=Decimal("1"),
            required_piece_quantity=Decimal("10"),
            display_order=display_order,
            internal_component_code=f"C1-C{display_order}",
            is_die_cut=is_die_cut,
            snapshot_mold_tool_id=mold.id if is_die_cut else None,
            snapshot_mold_tool_code=mold.mold_code if is_die_cut else None,
            snapshot_mold_tool_name=mold.mold_name if is_die_cut else None,
            spare_sheet_quantity=0,
            display_mode="internal_only",
            is_required=True,
            snapshot_component_product_code=component_product.product_code,
            snapshot_component_product_name=component_product.product_name,
            snapshot_component_spec="400×300",
            snapshot_component_material="K=A",
            snapshot_component_flute_type="B",
            snapshot_component_box_category=("die_cut" if is_die_cut else "normal"),
            snapshot_component_box_style=frozen_box_style,
            snapshot_component_default_cutting_mode="一开一",
        )
        db.add(snapshot)
        db.flush()
        task = ProductionTask(
            order_item_id=item.id,
            sales_order_item_bom_component_id=snapshot.id,
            task_role="component_internal",
            status="pending",
            planned_quantity=10,
            ordered_quantity_snapshot=10,
            material_received_quantity=10,
            material_input_quantity=10,
            output_factor=1,
            readiness_basis="material_received",
            print_content_snapshot=print_content,
        )
        db.add(task)
        db.flush()
        task_ids[key] = int(task.id)
    return task_ids


def test_mobile_station_query_filters_by_process_facts(mobile_production_app) -> None:
    app, factory, _ids = mobile_production_app
    from app.models.customer import Customer
    from app.models.mold_tool import MoldTool
    from app.models.order import Order, OrderItem
    from app.models.production import ProductionTask

    with factory() as db:
        customer = db.scalar(select(Customer).where(Customer.customer_code == "MW"))
        mold = db.scalar(select(MoldTool).where(MoldTool.mold_code == "MOBILE-MOLD-01"))
        assert customer is not None and mold is not None
        existing = {
            order.order_number: task
            for task, order in db.execute(
                select(ProductionTask, Order)
                .join(OrderItem, OrderItem.id == ProductionTask.order_item_id)
                .join(Order, Order.id == OrderItem.order_id)
                .where(Order.customer_id == customer.id)
            ).all()
        }
        existing["MOBILE-VISIBLE"].print_content_snapshot = "无印刷"
        existing["MOBILE-OLD"].print_content_snapshot = "单色印刷"
        die_only = int(existing["MOBILE-VISIBLE"].id)
        both = int(existing["MOBILE-OLD"].id)

        a1 = _add_regular_task(
            db,
            customer=customer,
            suffix="A1",
            box_style="A1",
            print_content_snapshot="无印刷",
        )
        liner = _add_regular_task(
            db,
            customer=customer,
            suffix="LINER",
            box_style="衬板",
            print_content_snapshot="无印刷",
        )
        printed_liner = _add_regular_task(
            db,
            customer=customer,
            suffix="PRINTED-LINER",
            box_style="衬板",
            print_content_snapshot="单色印刷",
        )
        completed_a1 = _add_regular_task(
            db,
            customer=customer,
            suffix="COMPLETED-A1",
            box_style="A1",
            print_content_snapshot="单色印刷",
            status="completed",
        )
        component = _add_component_tasks(db, customer=customer, mold=mold)
        db.commit()

    with TestClient(app) as client:
        _login(client, "mobile-workshop")
        printing_response = client.get(
            "/api/mobile/erp/production/tasks",
            params={"station": "printing", "page_size": 20},
        )
        die_response = client.get(
            "/api/mobile/erp/production/tasks",
            params={"station": "die_cut", "page_size": 20},
        )

    assert printing_response.status_code == 200, printing_response.text
    assert die_response.status_code == 200, die_response.text
    printing_ids = {row["task_id"] for row in printing_response.json()["items"]}
    die_ids = {row["task_id"] for row in die_response.json()["items"]}

    assert printing_ids == {
        both,
        a1,
        printed_liner,
        component["component_printing"],
        component["component_both"],
    }
    assert die_ids == {
        die_only,
        both,
        component["component_die"],
        component["component_both"],
    }
    assert liner not in printing_ids | die_ids
    assert completed_a1 not in printing_ids | die_ids
    assert printing_response.json()["total"] == len(printing_ids)
    assert die_response.json()["total"] == len(die_ids)
