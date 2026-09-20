from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import event, select
from sqlalchemy.orm import sessionmaker


def _fixture(tmp_path: Path, *, ordinary_count: int):
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.production import ProductionTask
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "p1_09c_production.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        visible = Customer(
            customer_number=1,
            customer_code="VISIBLE",
            name="Visible production customer",
            payment_term_days=0,
            credit_limit=Decimal("0"),
        )
        hidden = Customer(
            customer_number=2,
            customer_code="HIDDEN",
            name="Hidden production customer",
            payment_term_days=0,
            credit_limit=Decimal("0"),
        )
        user = User(
            username="p1-09c-production-scoped",
            password_hash="not-used-by-direct-endpoint-test",
            role="workshop",
            real_name="P1 production scope",
            must_change_password=False,
            customer_access_mode="selected",
        )
        db.add_all([visible, hidden, user])
        db.flush()
        db.add(UserCustomerScope(user_id=user.id, customer_id=visible.id))
        product = Product(
            customer_id=visible.id,
            product_code="P1-09C-PROD",
            customer_material_code="P1-09C-PROD",
            product_name="Pending production box",
            box_category="normal",
        )
        hidden_product = Product(
            customer_id=hidden.id,
            product_code="P1-09C-PROD-H",
            customer_material_code="P1-09C-PROD-H",
            product_name="Hidden production box",
            box_category="normal",
        )
        db.add_all([product, hidden_product])
        db.flush()

        for index in range(ordinary_count):
            order = Order(
                order_number=f"P1-09C-PROD-{index:03d}",
                customer_id=visible.id,
                order_date=date(2026, 7, 29),
                status="pending_production",
                payment_status="unpaid",
                total_amount=Decimal("0"),
            )
            db.add(order)
            db.flush()
            item = OrderItem(
                order_id=order.id,
                product_id=product.id,
                item_order_number=f"P1-09C-I-{index:03d}",
                quantity=100,
                delivered_quantity=0,
                unit_price=Decimal("0"),
                subtotal=Decimal("0"),
                material_status="pending",
                snapshot_product_name="Pending production box",
                snapshot_product_code="P1-09C-PROD",
                snapshot_material="A=B",
                special_process="一开一",
            )
            db.add(item)
            db.flush()
            db.add(
                ProductionTask(
                    order_item_id=item.id,
                    status="pending",
                    planned_quantity=100,
                    ordered_quantity_snapshot=100,
                    material_received_quantity=0,
                    material_input_quantity=100,
                    output_factor=1,
                    version=1,
                )
            )

        hidden_order = Order(
            order_number="P1-09C-PROD-H-001",
            customer_id=hidden.id,
            order_date=date(2026, 7, 29),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("0"),
        )
        db.add(hidden_order)
        db.flush()
        hidden_item = OrderItem(
            order_id=hidden_order.id,
            product_id=hidden_product.id,
            item_order_number="P1-09C-I-H-001",
            quantity=100,
            delivered_quantity=0,
            unit_price=Decimal("0"),
            subtotal=Decimal("0"),
            material_status="pending",
            snapshot_product_name="Hidden production box",
            snapshot_product_code="P1-09C-PROD-H",
            snapshot_material="A=B",
            special_process="一开一",
        )
        db.add(hidden_item)
        db.flush()
        db.add(
            ProductionTask(
                order_item_id=hidden_item.id,
                status="pending",
                planned_quantity=100,
                ordered_quantity_snapshot=100,
                material_input_quantity=100,
                output_factor=1,
                version=1,
            )
        )
        db.commit()
        return engine, factory, user.id


def _read_and_count(factory, user_id: int):
    from app.api.production import get_production_tasks
    from app.models.user import User

    engine = factory.kw["bind"]
    statements: list[str] = []

    def record_sql(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lstrip().lower())

    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        with factory() as db:
            user = db.get(User, user_id)
            assert user is not None
            response = get_production_tasks(task_status="pending", user=user, db=db)
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)
    return response, statements


def _read_page(factory, user_id: int, *, page: int, page_size: int):
    from app.api.production import get_production_tasks
    from app.models.user import User

    with factory() as db:
        user = db.get(User, user_id)
        assert user is not None
        return get_production_tasks(
            task_status="pending",
            page=page,
            page_size=page_size,
            user=user,
            db=db,
        )


def _select_count(statements: list[str]) -> int:
    return sum(statement.startswith("select") for statement in statements)


def test_pending_production_ordinary_rows_measure_current_query_growth(
    tmp_path: Path,
) -> None:
    _engine, small_factory, small_user_id = _fixture(
        tmp_path / "small", ordinary_count=1
    )
    _engine, large_factory, large_user_id = _fixture(
        tmp_path / "large", ordinary_count=20
    )
    small, small_sql = _read_and_count(small_factory, small_user_id)
    large, large_sql = _read_and_count(large_factory, large_user_id)

    assert len(small["items"]) == 1
    assert len(large["items"]) == 20
    assert all(
        row["customer_name"] == "Visible production customer" for row in large["items"]
    )
    assert all(
        not statement.startswith(("insert", "update", "delete"))
        for statement in [*small_sql, *large_sql]
    )
    row = large["items"][0]
    assert {
        "status": "pending",
        "order_quantity": 100,
        "planned_quantity": 100,
        "material_input_quantity": 100,
        "available_material_input_quantity": 100,
        "actual_output_quantity": 0,
        "is_component_task": False,
        "customer_board_preparation_sources": [],
    }.items() <= row.items()

    # One scope/user load + task join + current fixed request-level preflight reads,
    # including the current production-label fact batch and the fulfillment
    # reminder/customer finished-storage batches. None may grow with the
    # number of task rows.
    # Ordinary rows with no workflow facts must not add per-row SELECTs.
    assert _select_count(small_sql) == 12
    assert _select_count(large_sql) == _select_count(small_sql)


def test_pending_production_observes_component_board_preparation_and_double_splice(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import app.services.production_workflow as production_workflow
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.product_bom import SalesOrderItemBomComponent
    from app.models.production import ProductionTask
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryReservation,
        SemiFinishedInventoryDetail,
        WarehouseLocation,
    )

    _engine, factory, user_id = _fixture(tmp_path, ordinary_count=1)
    with factory() as db:
        customer = db.scalar(select(Customer).where(Customer.customer_code == "VISIBLE"))
        assert customer is not None
        parent_product = db.scalar(
            select(Product).where(Product.product_code == "P1-09C-PROD")
        )
        assert parent_product is not None
        component_product = Product(
            customer_id=customer.id,
            product_code="P1-09C-COMP",
            customer_material_code="P1-09C-COMP",
            product_name="Component box",
            box_category="normal",
        )
        db.add(component_product)
        db.flush()
        component_order = Order(
            order_number="P1-09C-COMP-ORDER",
            customer_id=customer.id,
            order_date=date(2026, 7, 29),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("0"),
        )
        splice_order = Order(
            order_number="P1-09C-SPLICE-ORDER",
            customer_id=customer.id,
            order_date=date(2026, 7, 29),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("0"),
        )
        db.add_all([component_order, splice_order])
        db.flush()
        component_item = OrderItem(
            order_id=component_order.id,
            product_id=parent_product.id,
            item_order_number="P1-09C-COMP-ITEM",
            quantity=100,
            delivered_quantity=0,
            unit_price=Decimal("0"),
            subtotal=Decimal("0"),
            material_status="pending",
            snapshot_product_name="Pending production box",
            snapshot_product_code="P1-09C-PROD",
            snapshot_material="A=B",
            special_process="一开一",
        )
        splice_item = OrderItem(
            order_id=splice_order.id,
            product_id=parent_product.id,
            item_order_number="P1-09C-SPLICE-ITEM",
            quantity=100,
            delivered_quantity=0,
            unit_price=Decimal("0"),
            subtotal=Decimal("0"),
            material_status="pending",
            snapshot_product_name="Double splice box",
            snapshot_product_code="P1-09C-SPLICE",
            snapshot_material="A=B",
            special_process="一开一",
            snapshot_splice_mode="double",
            snapshot_pieces_per_box=2,
        )
        db.add_all([component_item, splice_item])
        db.flush()
        snapshot = SalesOrderItemBomComponent(
            sales_order_item_id=component_item.id,
            component_product_id=component_product.id,
            order_set_quantity=100,
            quantity_per_set=Decimal("1"),
            required_piece_quantity=Decimal("100"),
            display_order=0,
            internal_component_code="P1-09C-COMP",
            is_die_cut=False,
            spare_sheet_quantity=0,
            display_mode="internal_only",
            is_required=True,
            snapshot_component_product_code="P1-09C-COMP",
            snapshot_component_product_name="Component box",
            snapshot_component_box_category="normal",
            snapshot_component_default_cutting_mode="一开一",
        )
        db.add(snapshot)
        db.flush()
        db.add_all(
            [
                    ProductionTask(
                        order_item_id=component_item.id,
                        sales_order_item_bom_component_id=snapshot.id,
                        task_role="component_internal",
                        status="pending",
                    planned_quantity=100,
                    ordered_quantity_snapshot=100,
                    material_input_quantity=100,
                    output_factor=1,
                    version=1,
                ),
                ProductionTask(
                    order_item_id=splice_item.id,
                    status="pending",
                    planned_quantity=100,
                    ordered_quantity_snapshot=100,
                    material_input_quantity=100,
                    output_factor=1,
                    version=1,
                ),
            ]
        )
        location = WarehouseLocation(
            location_code="P1-09C-SEMI-01",
            location_name="P1 semi location",
            warehouse_type="semi_finished",
            is_active=True,
        )
        db.add(location)
        db.flush()
        lot = InventoryLot(
            lot_number="P1-09C-SEMI-LOT",
            inventory_type="semi_finished",
            warehouse_location_id=location.id,
            quantity_available=100,
            quantity_reserved=0,
            quantity_consumed=0,
            quantity_damaged=0,
            quantity_scrapped=0,
            unit="sheets",
            status="active",
            source_type="manual",
            stock_date=date(2026, 7, 29),
            stock_date_accuracy="exact",
            last_movement_at=datetime(2026, 7, 29, 9, 0),
            version=1,
        )
        db.add(lot)
        db.flush()
        db.add(
            SemiFinishedInventoryDetail(
                inventory_lot_id=lot.id,
                owner_customer_id=customer.id,
                owner_customer_name_snapshot=customer.name,
                material_code_snapshot="A=B",
                normalized_material_code="A=B",
                layer_count=3,
                flute_type="B",
                board_length_mm=500,
                board_width_mm=300,
                component_type="whole",
                pieces_per_box=1,
                stock_yield_per_sheet=1,
                sheet_type="raw_board",
            )
        )
        db.add(
            InventoryReservation(
                reservation_number="P1-09C-SEMI-RES",
                inventory_lot_id=lot.id,
                reservation_type="semi_order",
                order_id=component_order.id,
                order_item_id=component_item.id,
                sales_order_item_bom_component_id=snapshot.id,
                reserved_stock_quantity=100,
                credited_requirement_quantity=100,
                yield_factor=1,
                status="active",
                reservation_group_key="P1-09C-SEMI-GROUP",
                idempotency_key="P1-09C-SEMI-KEY",
            )
        )
        db.commit()

    response, statements = _read_and_count(factory, user_id)
    rows = {row["item_order_number"]: row for row in response["items"]}
    component = rows["P1-09C-COMP-ITEM"]
    splice = rows["P1-09C-SPLICE-ITEM"]

    assert component["is_component_task"] is True
    assert component["product_code"] == "P1-09C-COMP"
    assert component["component_required_quantity"] == 100
    assert component["customer_board_preparation_sources"] == [
        {
            "reservation_id": component["customer_board_preparation_sources"][0]["reservation_id"],
            "inventory_lot_id": component["customer_board_preparation_sources"][0]["inventory_lot_id"],
            "lot_number": "P1-09C-SEMI-LOT",
            "source_ref_type": None,
            "source_ref_id": None,
            "location_code": "P1-09C-SEMI-01",
            "location_name": "P1 semi location",
            "current_address_name": "P1 semi location",
            "employee_location_name": "P1 semi location",
            "remaining_sheet_quantity": 100,
            "remaining_product_quantity": 100,
            "stock_yield_per_sheet": 1,
            "display_name": "客户专用纸板备料",
        }
    ]
    assert splice["is_component_task"] is False
    assert splice["pieces_per_box"] == 2
    assert splice["planned_output_quantity"] == 50
    paged_items: list[dict] = []
    for page in range(1, len(response["items"]) + 1):
        paged = _read_page(factory, user_id, page=page, page_size=1)
        assert paged["total"] == len(response["items"])
        assert paged["page"] == page
        assert paged["page_size"] == 1
        paged_items.extend(paged["items"])
    assert paged_items == response["items"]
    assert all(
        not statement.startswith(("insert", "update", "delete")) for statement in statements
    )

    # The request-level preflight must be output-equivalent to the exact legacy
    # serializer, including the one ordinary row which is eligible for fast path.
    monkeypatch.setattr(
        production_workflow,
        "_pending_production_read_context",
        lambda _db, _rows: production_workflow._PendingProductionReadContext(
            frozenset()
        ),
    )
    legacy_response, legacy_statements = _read_and_count(factory, user_id)
    assert response == legacy_response
    assert all(
        not statement.startswith(("insert", "update", "delete"))
        for statement in legacy_statements
    )
