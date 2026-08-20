from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.orm import sessionmaker


def _build_database(tmp_path: Path):
    from app.core.database import create_sqlite_engine
    from app.models import Base

    engine = create_sqlite_engine(tmp_path / "p1_36j_production.sqlite3")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _add_customer(db, *, number: int, code: str, name: str):
    from app.models.customer import Customer

    customer = Customer(
        customer_number=number,
        customer_code=code,
        name=name,
        payment_term_days=0,
        credit_limit=Decimal("0"),
    )
    db.add(customer)
    db.flush()
    return customer


def _add_product(
    db,
    *,
    customer_id: int,
    code: str,
    name: str,
    box_category: str = "normal",
):
    from app.models.product import Product

    product = Product(
        customer_id=customer_id,
        product_code=code,
        customer_material_code=code,
        product_name=name,
        box_category=box_category,
    )
    db.add(product)
    db.flush()
    return product


def _add_task(
    db,
    *,
    customer,
    product,
    key: str,
    task_status: str = "pending",
    order_status: str = "pending_production",
    force_closed: bool = False,
    delivery_date: date | None = None,
    product_code_snapshot: str | None = None,
    splice_mode: str | None = None,
    component_product=None,
    component_code: str | None = None,
):
    from app.models.order import Order, OrderItem
    from app.models.product_bom import SalesOrderItemBomComponent
    from app.models.production import ProductionTask

    order = Order(
        order_number=f"P1-36J-{key}",
        customer_id=customer.id,
        order_date=date(2026, 8, 10),
        delivery_date=delivery_date,
        status=order_status,
        payment_status="unpaid",
        total_amount=Decimal("0"),
    )
    db.add(order)
    db.flush()
    item = OrderItem(
        order_id=order.id,
        product_id=product.id,
        item_order_number=f"P1-36J-{key}-001",
        quantity=100,
        delivered_quantity=0,
        is_force_closed=force_closed,
        unit_price=Decimal("0"),
        subtotal=Decimal("0"),
        material_status="pending",
        snapshot_product_name=f"{key} snapshot",
        snapshot_product_code=product_code_snapshot,
        snapshot_material="A=B",
        special_process="一开一",
        snapshot_splice_mode=splice_mode,
        snapshot_pieces_per_box=2 if splice_mode == "double" else 1,
    )
    db.add(item)
    db.flush()

    component_snapshot_id = None
    if component_product is not None:
        component_snapshot = SalesOrderItemBomComponent(
            sales_order_item_id=item.id,
            component_product_id=component_product.id,
            order_set_quantity=100,
            quantity_per_set=Decimal("1"),
            required_piece_quantity=Decimal("100"),
            display_order=0,
            internal_component_code=f"{key}-COMP",
            is_die_cut=False,
            spare_sheet_quantity=0,
            display_mode="internal_only",
            is_required=True,
            snapshot_component_product_code=(
                component_code or component_product.product_code
            ),
            snapshot_component_product_name=component_product.product_name,
            snapshot_component_box_category="normal",
            snapshot_component_default_cutting_mode="一开一",
        )
        db.add(component_snapshot)
        db.flush()
        component_snapshot_id = component_snapshot.id

    task = ProductionTask(
        order_item_id=item.id,
        sales_order_item_bom_component_id=component_snapshot_id,
        task_role=("component_internal" if component_snapshot_id is not None else "order_main"),
        status=task_status,
        planned_quantity=100,
        ordered_quantity_snapshot=100,
        material_received_quantity=0,
        material_input_quantity=100,
        output_factor=1,
        printing_plate_mode_snapshot="plate",
        printing_plate_codes_snapshot='["P1-36J-PLATE"]',
        version=1,
    )
    db.add(task)
    db.flush()
    return order, item, task


def _dashboard_fields(row: dict) -> dict:
    return {
        "id": row["id"],
        "customer_id": row["customer_id"],
        "customer_name": row["customer_name"],
        "order_number": row["order_number"],
        "product_code": row["product_code"],
        # The full production-page payload intentionally has no business date.
        "delivery_date": row.get("delivery_date"),
        "created_at": row.get("created_at"),
    }


def test_dashboard_projection_reuses_full_pending_filters_and_identity(
    tmp_path: Path,
) -> None:
    from app.services.production_workflow import (
        list_production_task_dashboard_rows,
        list_production_tasks,
    )

    _engine, factory = _build_database(tmp_path)
    with factory() as db:
        visible = _add_customer(
            db, number=1, code="VISIBLE", name="Visible production customer"
        )
        hidden = _add_customer(
            db, number=2, code="HIDDEN", name="Hidden production customer"
        )
        parent = _add_product(
            db,
            customer_id=visible.id,
            code="PARENT-MASTER",
            name="Parent box",
        )
        component = _add_product(
            db,
            customer_id=visible.id,
            code="COMPONENT-MASTER",
            name="Component insert",
        )
        die_cut = _add_product(
            db,
            customer_id=visible.id,
            code="DIE-MASTER",
            name="Die-cut inner box",
            box_category="die_cut",
        )
        hidden_product = _add_product(
            db,
            customer_id=hidden.id,
            code="HIDDEN-MASTER",
            name="Hidden box",
        )

        ordinary = _add_task(
            db,
            customer=visible,
            product=parent,
            key="ORDINARY",
            delivery_date=date(2026, 8, 11),
            product_code_snapshot="ORDINARY-SNAPSHOT",
        )[2]
        component_task = _add_task(
            db,
            customer=visible,
            product=parent,
            key="COMPONENT",
            delivery_date=date(2026, 8, 12),
            product_code_snapshot="PARENT-SNAPSHOT",
            component_product=component,
            component_code="COMPONENT-SNAPSHOT",
        )[2]
        double_splice = _add_task(
            db,
            customer=visible,
            product=parent,
            key="DOUBLE",
            delivery_date=date(2026, 8, 13),
            product_code_snapshot="DOUBLE-SNAPSHOT",
            splice_mode="double",
        )[2]
        die_task = _add_task(
            db,
            customer=visible,
            product=die_cut,
            key="DIE",
            delivery_date=date(2026, 8, 14),
            product_code_snapshot="DIE-SNAPSHOT",
        )[2]
        completed = _add_task(
            db,
            customer=visible,
            product=parent,
            key="COMPLETED",
            task_status="completed",
        )[2]
        forced = _add_task(
            db,
            customer=visible,
            product=parent,
            key="FORCED",
            force_closed=True,
        )[2]
        closed = _add_task(
            db,
            customer=visible,
            product=parent,
            key="CLOSED",
            order_status="closed",
        )[2]
        hidden_task = _add_task(
            db,
            customer=hidden,
            product=hidden_product,
            key="HIDDEN",
            product_code_snapshot="HIDDEN-SNAPSHOT",
        )[2]
        db.commit()

        full_rows = list_production_tasks(
            db,
            allowed_customer_ids={visible.id},
            status="pending",
        )
        projected_rows = list_production_task_dashboard_rows(
            db,
            allowed_customer_ids={visible.id},
        )

        assert projected_rows == [_dashboard_fields(row) for row in full_rows]
        assert [row["id"] for row in projected_rows] == [
            ordinary.id,
            component_task.id,
            double_splice.id,
            die_task.id,
        ]
        assert [row["product_code"] for row in projected_rows] == [
            "ORDINARY-SNAPSHOT",
            "COMPONENT-SNAPSHOT",
            "DOUBLE-SNAPSHOT",
            "DIE-SNAPSHOT",
        ]
        excluded = {completed.id, forced.id, closed.id, hidden_task.id}
        assert excluded.isdisjoint(row["id"] for row in projected_rows)

        unrestricted = list_production_task_dashboard_rows(
            db,
            allowed_customer_ids=None,
        )
        assert hidden_task.id in {row["id"] for row in unrestricted}


def _complex_projection_select_count(tmp_path: Path, *, row_count: int) -> int:
    from app.services.production_workflow import list_production_task_dashboard_rows

    engine, factory = _build_database(tmp_path)
    with factory() as db:
        customer = _add_customer(
            db,
            number=1,
            code=f"SCALE-{row_count}",
            name="Projection scale customer",
        )
        parent = _add_product(
            db,
            customer_id=customer.id,
            code=f"SCALE-PARENT-{row_count}",
            name="Scale parent",
        )
        component = _add_product(
            db,
            customer_id=customer.id,
            code=f"SCALE-COMPONENT-{row_count}",
            name="Scale component",
        )
        for index in range(row_count):
            _add_task(
                db,
                customer=customer,
                product=parent,
                key=f"SCALE-{row_count}-{index:03d}",
                delivery_date=date(2026, 8, 20),
                product_code_snapshot="PARENT-SHOULD-NOT-LEAK",
                component_product=component,
                component_code=f"COMPONENT-{index:03d}",
            )
        db.commit()

        statements: list[str] = []

        def record_sql(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement.lstrip().lower())

        event.listen(engine, "before_cursor_execute", record_sql)
        try:
            rows = list_production_task_dashboard_rows(
                db,
                allowed_customer_ids={customer.id},
            )
        finally:
            event.remove(engine, "before_cursor_execute", record_sql)

    assert len(rows) == row_count
    assert [row["product_code"] for row in rows] == [
        f"COMPONENT-{index:03d}" for index in range(row_count)
    ]
    assert not any(
        statement.startswith(("insert", "update", "delete"))
        for statement in statements
    )
    return sum(statement.startswith("select") for statement in statements)


def test_dashboard_projection_is_one_read_for_complex_rows(tmp_path: Path) -> None:
    one = _complex_projection_select_count(tmp_path / "one", row_count=1)
    twenty = _complex_projection_select_count(tmp_path / "twenty", row_count=20)
    hundred = _complex_projection_select_count(tmp_path / "hundred", row_count=100)

    assert one == twenty == hundred == 1


def test_dashboard_projection_does_not_call_full_workflow_serializers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import app.services.production_workflow as workflow

    _engine, factory = _build_database(tmp_path)
    with factory() as db:
        customer = _add_customer(
            db, number=1, code="NO-SERIALIZER", name="No serializer customer"
        )
        parent = _add_product(
            db,
            customer_id=customer.id,
            code="NO-SERIALIZER-PARENT",
            name="No serializer parent",
        )
        component = _add_product(
            db,
            customer_id=customer.id,
            code="NO-SERIALIZER-COMP",
            name="No serializer component",
        )
        task = _add_task(
            db,
            customer=customer,
            product=parent,
            key="NO-SERIALIZER",
            component_product=component,
            component_code="NO-SERIALIZER-SNAPSHOT",
        )[2]
        db.commit()

        def fail(*_args, **_kwargs):
            raise AssertionError("dashboard projection entered a full serializer")

        monkeypatch.setattr(workflow, "_pending_production_read_context", fail)
        monkeypatch.setattr(workflow, "_task_product_snapshot", fail)
        monkeypatch.setattr(workflow, "effective_component_demands", fail)
        monkeypatch.setattr(workflow, "production_ready_quantity", fail)
        monkeypatch.setattr(workflow, "_active_customer_board_preparation_sources", fail)

        assert workflow.list_production_task_dashboard_rows(
            db,
            allowed_customer_ids={customer.id},
        ) == [
            {
                "id": task.id,
                "customer_id": customer.id,
                "customer_name": "No serializer customer",
                "order_number": "P1-36J-NO-SERIALIZER",
                "product_code": "NO-SERIALIZER-SNAPSHOT",
                "delivery_date": None,
                "created_at": None,
            }
        ]
