from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker


def _fixture(tmp_path: Path, *, visible_count: int):
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "p1_09c_requisition_scaling.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        visible = Customer(
            customer_number=1,
            customer_code="VISIBLE",
            name="可见客户",
            payment_term_days=0,
            credit_limit=Decimal("0"),
        )
        hidden = Customer(
            customer_number=2,
            customer_code="HIDDEN",
            name="不可见客户",
            payment_term_days=0,
            credit_limit=Decimal("0"),
        )
        user = User(
            username="scoped-sales",
            password_hash="not-used-by-direct-endpoint-test",
            role="sales",
            real_name="范围测试",
            must_change_password=False,
            customer_access_mode="selected",
        )
        db.add_all([visible, hidden, user])
        db.flush()
        db.add(UserCustomerScope(user_id=user.id, customer_id=visible.id))
        visible_product = Product(
            customer_id=visible.id,
            product_code="P1-09C-V",
            customer_material_code="P1-09C-V",
            product_name="可见待报料纸箱",
            box_category="normal",
        )
        hidden_product = Product(
            customer_id=hidden.id,
            product_code="P1-09C-H",
            customer_material_code="P1-09C-H",
            product_name="不可见待报料纸箱",
            box_category="normal",
        )
        db.add_all([visible_product, hidden_product])
        db.flush()

        for index in range(visible_count):
            order = Order(
                order_number=f"P1-09C-V-{index:03d}",
                customer_id=visible.id,
                customer_po=f"PO-V-{index:03d}",
                order_date=date(2026, 7, 29),
                status="pending_production",
                payment_status="unpaid",
                total_amount=Decimal("0"),
            )
            db.add(order)
            db.flush()
            db.add(
                OrderItem(
                    order_id=order.id,
                    product_id=visible_product.id,
                    quantity=100,
                    delivered_quantity=0,
                    unit_price=Decimal("0"),
                    subtotal=Decimal("0"),
                    material_status="pending",
                    requisition_status="未报料",
                    snapshot_product_name="可见待报料纸箱",
                    snapshot_product_code="P1-09C-V",
                    snapshot_material="A=B",
                    snapshot_report_length_mm=500,
                    snapshot_report_width_mm=300,
                )
            )

        hidden_order = Order(
            order_number="P1-09C-H-001",
            customer_id=hidden.id,
            customer_po="PO-H-001",
            order_date=date(2026, 7, 29),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("0"),
        )
        db.add(hidden_order)
        db.flush()
        db.add(
            OrderItem(
                order_id=hidden_order.id,
                product_id=hidden_product.id,
                quantity=100,
                delivered_quantity=0,
                unit_price=Decimal("0"),
                subtotal=Decimal("0"),
                material_status="pending",
                requisition_status="未报料",
                snapshot_product_name="不可见待报料纸箱",
                snapshot_product_code="P1-09C-H",
                snapshot_material="A=B",
                snapshot_report_length_mm=500,
                snapshot_report_width_mm=300,
            )
        )
        db.commit()
        return engine, factory, user.id


def _read_and_count(
    factory: sessionmaker,
    user_id: int,
    endpoint,
) -> tuple[dict, list[str]]:
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
            response = endpoint(db, user)
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)
    return response, statements


def _select_count(statements: list[str]) -> int:
    return sum(statement.startswith("select") for statement in statements)


def _add_visible_semi_requirements(factory: sessionmaker) -> None:
    from app.models.order import Order, OrderItem
    from app.models.warehouse_inventory import OrderItemSemiRequirement

    with factory() as db:
        items = db.scalars(
            select(OrderItem)
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.order_number.like("P1-09C-V-%"))
            .order_by(OrderItem.id)
        ).all()
        for item in items:
            db.add(
                OrderItemSemiRequirement(
                    order_item_id=item.id,
                    customer_id=item.order.customer_id,
                    component_type="whole",
                    board_length_mm=500,
                    board_width_mm=300,
                    material_code_snapshot="A=B",
                    normalized_material_code="A=B",
                    flute_type="B",
                    pieces_per_box=1,
                    stock_yield_per_sheet=1,
                    required_piece_quantity=100,
                )
            )
        db.commit()


def _add_visible_inventory_shape(
    factory: sessionmaker,
    *,
    matching: bool,
    include_finished: bool = False,
) -> None:
    from app.models.order import Order, OrderItem
    from app.models.warehouse_inventory import (
        FinishedGoodsInventoryDetail,
        InventoryLot,
        SemiFinishedInventoryDetail,
        SemiFinishedLotAllowedProduct,
        WarehouseLocation,
    )

    with factory() as db:
        items = db.scalars(
            select(OrderItem)
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.order_number.like("P1-09C-V-%"))
            .order_by(OrderItem.id)
        ).all()
        assert items
        product = items[0].product
        product.layer_count = 3
        for item in items:
            item.layer_count = 3
            item.flute_type = "B"
            item.snapshot_supplier_name = "测试供应商"
        location = WarehouseLocation(
            location_code=f"P1-09C-{'MATCH' if matching else 'OTHER'}",
            location_name="报料查询缩放测试位",
            warehouse_type="finished",
            is_active=True,
            warehouse_floor=3,
            area_code="P1-09C",
            storage_type="rack",
            is_temporary=False,
            source_version="P1-09C",
            sort_order=1,
        )
        db.add(location)
        db.flush()
        semi_lot = InventoryLot(
            lot_number=f"P1-09C-SEMI-{'MATCH' if matching else 'OTHER'}",
            inventory_type="semi_finished",
            warehouse_location_id=location.id,
            quantity_available=25,
            quantity_reserved=0,
            quantity_consumed=0,
            quantity_damaged=0,
            quantity_scrapped=0,
            unit="sheets",
            status="active",
            source_type="manual",
            stock_date=date(2026, 7, 29),
            last_movement_at=datetime(2026, 7, 29, 8, 0, 0),
            version=1,
        )
        db.add(semi_lot)
        db.flush()
        db.add(
            SemiFinishedInventoryDetail(
                inventory_lot_id=semi_lot.id,
                supplier_name="测试供应商",
                owner_customer_id=items[0].order.customer_id,
                owner_customer_name_snapshot="可见客户",
                material_code_snapshot="A=B" if matching else "K=K",
                normalized_material_code="A=B" if matching else "K=K",
                layer_count=3,
                flute_type="B",
                board_length_mm=500 if matching else 900,
                board_width_mm=300 if matching else 700,
                component_type="whole",
                pieces_per_box=1,
                stock_yield_per_sheet=1,
                sheet_type="raw_board",
            )
        )
        if matching:
            db.add(
                SemiFinishedLotAllowedProduct(
                    inventory_lot_id=semi_lot.id,
                    product_id=product.id,
                    confirmed_at=datetime(2026, 7, 29, 8, 0, 0),
                )
            )
        if include_finished:
            finished_lot = InventoryLot(
                lot_number="P1-09C-FINISHED-MATCH",
                inventory_type="finished",
                warehouse_location_id=location.id,
                quantity_available=20,
                quantity_reserved=0,
                quantity_consumed=0,
                quantity_damaged=0,
                quantity_scrapped=0,
                unit="boxes",
                status="active",
                source_type="manual",
                stock_date=date(2026, 7, 28),
                last_movement_at=datetime(2026, 7, 28, 8, 0, 0),
                version=1,
            )
            db.add(finished_lot)
            db.flush()
            db.add(
                FinishedGoodsInventoryDetail(
                    inventory_lot_id=finished_lot.id,
                    owner_customer_id=items[0].order.customer_id,
                    owner_customer_name_snapshot="可见客户",
                    is_general=False,
                    product_id=product.id,
                    inventory_code_snapshot=product.product_code,
                    product_name_snapshot=product.product_name,
                )
            )
        db.commit()


def _add_visible_bom_snapshots(factory: sessionmaker) -> None:
    from app.models.material import Material
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.product_bom import SalesOrderItemBomComponent

    with factory() as db:
        items = db.scalars(
            select(OrderItem)
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.order_number.like("P1-09C-V-%"))
            .order_by(OrderItem.id)
        ).all()
        assert items
        material = Material(
            code="A=B",
            layer_count=3,
            flute_type="B",
            supplier_name="测试供应商",
            is_active=True,
            version=1,
        )
        db.add(material)
        db.flush()
        component = Product(
            customer_id=items[0].order.customer_id,
            product_code="P1-09C-COMP",
            customer_material_code="P1-09C-COMP",
            product_name="复杂报料组件",
            box_category="normal",
            box_style="模切内盒",
            material_id=material.id,
        )
        db.add(component)
        db.flush()
        for item in items:
            db.add(
                SalesOrderItemBomComponent(
                    sales_order_item_id=item.id,
                    product_bom_component_id=None,
                    component_product_id=component.id,
                    order_set_quantity=100,
                    quantity_per_set=Decimal("1"),
                    required_piece_quantity=Decimal("100"),
                    display_order=1,
                    internal_component_code=f"P1-09C-COMP-{item.id}",
                    is_die_cut=False,
                    snapshot_mold_tool_id=None,
                    mold_max_yield_per_sheet=None,
                    spare_sheet_quantity=0,
                    display_mode="internal_only",
                    is_required=True,
                    snapshot_component_product_code=component.product_code,
                    snapshot_component_product_name=component.product_name,
                    snapshot_component_spec="500×300",
                    snapshot_component_material_id=material.id,
                    snapshot_component_material="A=B",
                    snapshot_component_supplier_name="测试供应商",
                    snapshot_component_layer_count=3,
                    snapshot_component_flute_type="B",
                    snapshot_component_box_category="normal",
                    snapshot_component_box_style="模切内盒",
                    snapshot_component_default_cutting_mode="一开一",
                    snapshot_component_report_length_mm=500,
                    snapshot_component_report_width_mm=300,
                )
            )
        db.commit()


def test_pending_and_merge_keep_customer_scope_and_do_not_write(tmp_path: Path) -> None:
    from app.api.requisition import merge_suggestions, pending_requisitions

    _engine, factory, user_id = _fixture(tmp_path, visible_count=2)
    pending, pending_sql = _read_and_count(factory, user_id, pending_requisitions)
    suggestions, merge_sql = _read_and_count(factory, user_id, merge_suggestions)

    assert [row["customer_name"] for row in pending["items"]] == ["可见客户"] * 2
    assert all(
        member["customer_name"] != "不可见客户"
        for row in suggestions["suggestions"]
        for member in row["members"]
    )
    assert {"item_id", "requisition_qty", "inventory_deducted_qty", "component_requirements"} <= set(pending["items"][0])
    assert not any(
        statement.startswith(("insert", "update", "delete"))
        for statement in [*pending_sql, *merge_sql]
    )


def test_pending_and_merge_query_growth_is_bounded(
    tmp_path: Path,
) -> None:
    from app.api.requisition import merge_suggestions, pending_requisitions

    _engine, small_factory, small_user_id = _fixture(tmp_path / "small", visible_count=1)
    _engine, large_factory, large_user_id = _fixture(tmp_path / "large", visible_count=20)
    _small_pending, small_pending_sql = _read_and_count(
        small_factory, small_user_id, pending_requisitions
    )
    _large_pending, large_pending_sql = _read_and_count(
        large_factory, large_user_id, pending_requisitions
    )
    _small_merge, small_merge_sql = _read_and_count(
        small_factory, small_user_id, merge_suggestions
    )
    _large_merge, large_merge_sql = _read_and_count(
        large_factory, large_user_id, merge_suggestions
    )

    # The request-scoped negative-fact context keeps ordinary 20-line reads
    # close to the one-line query count.  The bound intentionally remains
    # tight so a future per-line helper call cannot silently return.
    assert _select_count(large_pending_sql) <= _select_count(small_pending_sql) + 8
    assert _select_count(large_merge_sql) <= _select_count(small_merge_sql) + 8


def test_pending_complex_requirement_query_growth_is_bounded_and_read_only(
    tmp_path: Path,
) -> None:
    from app.api.requisition import merge_suggestions, pending_requisitions

    counts: dict[int, int] = {}
    merge_counts: dict[int, int] = {}
    for visible_count in (1, 10, 20):
        _engine, factory, user_id = _fixture(
            tmp_path / str(visible_count), visible_count=visible_count
        )
        _add_visible_semi_requirements(factory)
        response, statements = _read_and_count(
            factory, user_id, pending_requisitions
        )
        merge_response, merge_statements = _read_and_count(
            factory, user_id, merge_suggestions
        )

        assert len(response["items"]) == visible_count
        assert isinstance(merge_response["suggestions"], list)
        assert all(
            row["semi_finished_reserved_piece_qty"] == 0
            for row in response["items"]
        )
        assert not any(
            statement.startswith(("insert", "update", "delete"))
            for statement in statements
        )
        assert not any(
            statement.startswith(("insert", "update", "delete"))
            for statement in merge_statements
        )
        counts[visible_count] = _select_count(statements)
        merge_counts[visible_count] = _select_count(merge_statements)

    assert counts[10] <= counts[1] + 10
    assert counts[20] <= counts[1] + 10
    assert merge_counts[10] <= merge_counts[1] + 10
    assert merge_counts[20] <= merge_counts[1] + 10


def test_same_customer_unrelated_semi_lot_keeps_query_growth_bounded(
    tmp_path: Path,
) -> None:
    from app.api.requisition import pending_requisitions

    counts: dict[int, int] = {}
    for visible_count in (1, 10, 20):
        _engine, factory, user_id = _fixture(
            tmp_path / str(visible_count), visible_count=visible_count
        )
        _add_visible_inventory_shape(factory, matching=False)
        response, statements = _read_and_count(
            factory, user_id, pending_requisitions
        )
        assert len(response["items"]) == visible_count
        assert all(
            row["customer_board_preparation_available_piece_qty"] == 0
            and not row["can_auto_use_customer_board_preparation"]
            for row in response["items"]
        )
        counts[visible_count] = _select_count(statements)

    assert counts[10] <= counts[1] + 10
    assert counts[20] <= counts[1] + 10


def test_matching_finished_and_semi_inventory_query_growth_is_bounded(
    tmp_path: Path,
) -> None:
    from app.api.requisition import pending_requisitions

    counts: dict[int, int] = {}
    for visible_count in (1, 10, 20):
        _engine, factory, user_id = _fixture(
            tmp_path / str(visible_count), visible_count=visible_count
        )
        _add_visible_semi_requirements(factory)
        _add_visible_inventory_shape(
            factory, matching=True, include_finished=True
        )
        response, statements = _read_and_count(
            factory, user_id, pending_requisitions
        )
        assert len(response["items"]) == visible_count
        assert all(
            row["late_finished_inventory_available_qty"] == 20
            and row["late_finished_inventory_reservable_qty"] == 20
            and row["customer_board_preparation_available_piece_qty"] == 25
            for row in response["items"]
        )
        assert not any(
            statement.startswith(("insert", "update", "delete"))
            for statement in statements
        )
        counts[visible_count] = _select_count(statements)

    assert counts[10] <= counts[1] + 10
    assert counts[20] <= counts[1] + 10


def test_composite_bom_query_growth_is_bounded_and_sources_stay_complete(
    tmp_path: Path,
) -> None:
    from app.api.requisition import pending_requisitions

    counts: dict[int, int] = {}
    for visible_count in (1, 10, 20):
        _engine, factory, user_id = _fixture(
            tmp_path / str(visible_count), visible_count=visible_count
        )
        _add_visible_bom_snapshots(factory)
        response, statements = _read_and_count(
            factory, user_id, pending_requisitions
        )
        assert len(response["items"]) == visible_count
        assert all(
            row["is_composite_bom"] is True
            and len(row["bom_requisition_sources"]) == 2
            and row["component_requirements"][0]["required_piece_quantity"]
            == 100
            for row in response["items"]
        )
        assert not any(
            statement.startswith(("insert", "update", "delete"))
            for statement in statements
        )
        counts[visible_count] = _select_count(statements)

    assert counts[10] <= counts[1] + 10
    assert counts[20] <= counts[1] + 10


def test_composite_bom_batch_fields_match_established_helpers(
    tmp_path: Path,
) -> None:
    from app.api.requisition import (
        _bom_pending_component_requirements,
        _bom_pending_parent_requirement,
        _bom_snapshots_for_order_item,
        pending_requisitions,
    )
    from app.models.order import Order, OrderItem
    from app.models.user import User

    _engine, factory, user_id = _fixture(tmp_path, visible_count=1)
    _add_visible_bom_snapshots(factory)
    with factory() as db:
        item = db.scalar(
            select(OrderItem)
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.order_number == "P1-09C-V-000")
        )
        assert item is not None
        snapshots = _bom_snapshots_for_order_item(db, item.id)
        expected_components = _bom_pending_component_requirements(
            db, item, snapshots=snapshots
        )
        expected_parent = _bom_pending_parent_requirement(db, item)
        user = db.get(User, user_id)
        assert user is not None
        row = pending_requisitions(db, user)["items"][0]

    assert row["component_requirements"] == expected_components
    assert row["parent_requirement"] == expected_parent
    assert row["bom_requisition_sources"] == [
        expected_parent,
        *expected_components,
    ]


def test_complex_inventory_batch_fields_match_established_helpers(
    tmp_path: Path,
) -> None:
    from app.api.requisition import (
        _current_requisition_summary,
        _late_finished_inventory_preview,
        _safe_customer_board_preparation_options,
        pending_requisitions,
    )
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    _engine, factory, user_id = _fixture(tmp_path, visible_count=1)
    _add_visible_semi_requirements(factory)
    _add_visible_inventory_shape(factory, matching=True, include_finished=True)
    with factory() as db:
        item, order, _customer, product = db.execute(
            select(OrderItem, Order, Customer, Product)
            .join(Order, Order.id == OrderItem.order_id)
            .join(Customer, Customer.id == Order.customer_id)
            .join(Product, Product.id == OrderItem.product_id)
            .where(Order.order_number == "P1-09C-V-000")
        ).one()
        expected_requirements = _current_requisition_summary(db, item)
        expected_finished = _late_finished_inventory_preview(
            db, item=item, order=order, product=product
        )
        expected_board = _safe_customer_board_preparation_options(
            db, item=item, order=order, product=product
        )
        user = db.get(User, user_id)
        assert user is not None
        response = pending_requisitions(db, user)

    row = response["items"][0]
    for field in (
        "finished_inventory_reserved_qty",
        "production_required_qty",
        "fully_covered_by_finished_inventory",
        "requisition_qty",
        "cutting_mode",
        "cutting_factor",
        "pieces_per_box",
        "required_piece_qty",
        "semi_finished_reserved_piece_qty",
        "remaining_required_piece_qty",
        "component_requirements",
    ):
        assert row[field] == expected_requirements[field]
    assert row["late_finished_inventory"] == expected_finished
    assert row["customer_board_preparation_available_piece_qty"] == sum(
        int(option["available_piece_quantity"]) for option in expected_board
    )
    assert row["customer_board_preparation_available_sheet_qty"] == sum(
        int(option["available_sheet_quantity"]) for option in expected_board
    )
    assert row["can_auto_use_customer_board_preparation"] == bool(expected_board)


def test_complex_active_semi_reservation_matches_established_summary(
    tmp_path: Path,
) -> None:
    from app.api.requisition import (
        _current_requisition_summary,
        pending_requisitions,
    )
    from app.models.order import Order, OrderItem
    from app.models.user import User
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryReservation,
        OrderItemSemiRequirement,
    )

    _engine, factory, user_id = _fixture(tmp_path, visible_count=1)
    _add_visible_semi_requirements(factory)
    _add_visible_inventory_shape(factory, matching=True)
    with factory() as db:
        item = db.scalar(
            select(OrderItem)
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.order_number == "P1-09C-V-000")
        )
        assert item is not None
        requirement = db.scalar(
            select(OrderItemSemiRequirement).where(
                OrderItemSemiRequirement.order_item_id == item.id
            )
        )
        lot = db.scalar(
            select(InventoryLot).where(
                InventoryLot.lot_number == "P1-09C-SEMI-MATCH"
            )
        )
        assert requirement is not None and lot is not None
        lot.quantity_available = 5
        lot.quantity_reserved = 20
        db.add(
            InventoryReservation(
                reservation_number="P1-09C-SEMI-RESERVED",
                inventory_lot_id=lot.id,
                reservation_type="semi_order",
                order_id=item.order_id,
                order_item_id=item.id,
                semi_requirement_id=requirement.id,
                reserved_stock_quantity=20,
                credited_requirement_quantity=20,
                yield_factor=1,
                consumed_stock_quantity=0,
                released_stock_quantity=0,
                consumed_requirement_quantity=0,
                released_requirement_quantity=0,
                status="active",
                reserved_at=datetime(2026, 7, 29, 9, 0, 0),
                idempotency_key="p1-09c-semi-reserved",
            )
        )
        db.commit()

    with factory() as db:
        item = db.scalar(
            select(OrderItem)
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.order_number == "P1-09C-V-000")
        )
        assert item is not None
        expected = _current_requisition_summary(db, item)
        user = db.get(User, user_id)
        assert user is not None
        row = pending_requisitions(db, user)["items"][0]

    assert row["semi_finished_reserved_piece_qty"] == 20
    assert row["remaining_required_piece_qty"] == 80
    assert row["requisition_qty"] == 80
    assert row["component_requirements"] == expected["component_requirements"]
    assert row["late_finished_inventory"]["blocked_reason"] == (
        "订单已有半成品或客户专用纸板备料预占"
    )


def test_pending_complex_semi_requirement_uses_existing_calculation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import app.api.requisition as requisition_api
    from app.api.requisition import pending_requisitions
    from app.models.order import Order, OrderItem
    from app.models.warehouse_inventory import OrderItemSemiRequirement
    from app.models.user import User

    _engine, factory, user_id = _fixture(tmp_path, visible_count=1)
    with factory() as db:
        # The fixture has one visible item plus one hidden item; select the
        # scoped user's visible order item explicitly instead of relying on row
        # creation order.
        item = db.scalar(
            select(OrderItem)
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.order_number == "P1-09C-V-000")
        )
        assert item is not None
        db.add(
            OrderItemSemiRequirement(
                order_item_id=item.id,
                customer_id=item.order.customer_id,
                component_type="whole",
                board_length_mm=500,
                board_width_mm=300,
                material_code_snapshot="A=B",
                normalized_material_code="A=B",
                flute_type="B",
                pieces_per_box=1,
                stock_yield_per_sheet=1,
                required_piece_quantity=100,
            )
        )
        db.commit()

    called_item_ids: list[int] = []
    original = requisition_api._current_requisition_summary

    def spy(db, item, **kwargs):
        called_item_ids.append(item.id)
        return original(db, item, **kwargs)

    monkeypatch.setattr(requisition_api, "_current_requisition_summary", spy)
    with factory() as db:
        user = db.get(User, user_id)
        assert user is not None
        response = pending_requisitions(db, user)

    assert called_item_ids
    assert response["items"][0]["item_id"] in called_item_ids
    assert response["items"][0]["semi_finished_reserved_piece_qty"] == 0


def test_ordinary_fast_requirements_match_existing_whole_item_summary(
    tmp_path: Path,
) -> None:
    from app.api.requisition import (
        _current_requisition_summary,
        _ordinary_requisition_requirements,
    )
    from app.models.order import Order, OrderItem

    _engine, factory, _user_id = _fixture(tmp_path, visible_count=1)
    with factory() as db:
        item = db.scalar(
            select(OrderItem)
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.order_number == "P1-09C-V-000")
        )
        assert item is not None
        existing = _current_requisition_summary(db, item)
        fast = _ordinary_requisition_requirements(item)

    assert fast == existing
    assert fast["component_requirements"] == [
        {
            key: value
            for key, value in fast.items()
            if key != "component_requirements"
        }
    ]


def test_ordinary_fast_pending_fields_match_existing_helpers(
    tmp_path: Path,
) -> None:
    from app.api.requisition import (
        _current_requisition_summary,
        _late_finished_inventory_preview,
        _safe_customer_board_preparation_options,
        pending_requisitions,
    )
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    _engine, factory, user_id = _fixture(tmp_path, visible_count=1)
    with factory() as db:
        item, order, customer, product = db.execute(
            select(OrderItem, Order, Customer, Product)
            .join(Order, Order.id == OrderItem.order_id)
            .join(Customer, Customer.id == Order.customer_id)
            .join(Product, Product.id == OrderItem.product_id)
            .where(Order.order_number == "P1-09C-V-000")
        ).one()
        requirements = _current_requisition_summary(db, item)
        preview = _late_finished_inventory_preview(
            db, item=item, order=order, product=product
        )
        board_options = _safe_customer_board_preparation_options(
            db, item=item, order=order, product=product
        )
        user = db.get(User, user_id)
        assert user is not None
        response = pending_requisitions(db, user)

    row = response["items"][0]
    for field in (
        "finished_inventory_reserved_qty",
        "production_required_qty",
        "fully_covered_by_finished_inventory",
        "requisition_qty",
        "cutting_mode",
        "cutting_factor",
        "pieces_per_box",
        "required_piece_qty",
        "semi_finished_reserved_piece_qty",
        "remaining_required_piece_qty",
        "component_requirements",
    ):
        assert row[field] == requirements[field]
    assert row["late_finished_inventory"] == preview
    assert row["customer_board_preparation_available_piece_qty"] == sum(
        int(option["available_piece_quantity"]) for option in board_options
    )
    assert row["customer_board_preparation_available_sheet_qty"] == sum(
        int(option["available_sheet_quantity"]) for option in board_options
    )


def test_pending_telescoping_lid_box_falls_back_to_cover_and_base_calculation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import app.api.requisition as requisition_api
    from app.api.requisition import pending_requisitions
    from app.models.order import Order, OrderItem
    from app.models.user import User

    _engine, factory, user_id = _fixture(tmp_path, visible_count=1)
    with factory() as db:
        item = db.scalar(
            select(OrderItem)
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.order_number == "P1-09C-V-000")
        )
        assert item is not None
        item.product.box_style = "A3"
        item.snapshot_base_report_length_mm = 480
        item.snapshot_base_report_width_mm = 320
        db.commit()

    called_item_ids: list[int] = []
    original = requisition_api._current_requisition_summary

    def spy(db, item, **kwargs):
        called_item_ids.append(item.id)
        return original(db, item, **kwargs)

    monkeypatch.setattr(requisition_api, "_current_requisition_summary", spy)
    with factory() as db:
        user = db.get(User, user_id)
        assert user is not None
        response = pending_requisitions(db, user)

    assert response["items"][0]["item_id"] in called_item_ids
    assert [row["component_type"] for row in response["items"][0]["component_requirements"]] == [
        "cover",
        "base",
    ]
