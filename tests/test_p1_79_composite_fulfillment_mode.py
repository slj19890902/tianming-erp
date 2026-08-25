from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy.orm import Session

from app.api.deliveries import (
    _actual_goods_lines,
    _customer_document_fulfillment_mode,
    _delivery_list_summary_context,
    _delivery_response,
    _delivery_summary_response,
    get_delivery_print_data,
)
from app.api.orders import OrderItemCreate, _validated_combination_provenance
from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryItem
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.product_bom import (
    RequisitionItemBomSource,
    SalesOrderItemBomComponent,
)
from app.models.production import ProductionTask
from app.models.requisition import Requisition, RequisitionItem
from app.models.user import User
from app.services.production_label_operations import (
    confirm_packaging_label_job_printed,
    prepare_composite_packaging_label_job,
)
from app.services.production_packaging_label import (
    build_composite_requisition_packaging_label_package,
)
from app.services.requisition_production_print import (
    build_composite_requisition_production_package,
)
from app.services.requisition_production_print_batch import (
    build_selected_production_print_package,
)


ROOT = Path(__file__).resolve().parents[1]


def _migration_config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _seed_label_facts(db: Session) -> tuple[Requisition, OrderItem, list[RequisitionItem]]:
    customer = Customer(
        name="组合交付测试客户",
        chinese_short_name="组合客户",
        customer_code="P179",
    )
    db.add(customer)
    db.flush()
    parent = Product(
        customer_id=customer.id,
        product_code="Z.001.000205",
        customer_material_code="Z.001.000205",
        product_name="组合父件",
        is_composite=True,
        composite_fulfillment_mode="component_delivery",
    )
    children = [
        Product(
            customer_id=customer.id,
            product_code="Z.001.000205",
            customer_material_code="Z.001.000205",
            product_name=name,
            is_internal_component=True,
            production_label_enabled=True,
            production_label_units_per_label=100,
        )
        for name in ("组合子件甲", "组合子件乙")
    ]
    db.add_all([parent, *children])
    db.flush()
    order = Order(
        order_number="P179-ORDER-001",
        customer_id=customer.id,
        order_date=date(2026, 8, 19),
        delivery_date=date(2026, 8, 25),
        status="pending_production",
        payment_status="unpaid",
        total_amount=Decimal("1800"),
    )
    db.add(order)
    db.flush()
    order_item = OrderItem(
        order_id=order.id,
        product_id=parent.id,
        quantity=1800,
        unit_price=Decimal("1"),
        subtotal=Decimal("1800"),
        material_status="received",
        requisition_status="已报料",
        snapshot_product_code=parent.product_code,
        snapshot_product_name=parent.product_name,
        snapshot_spec="组合规格",
        special_process="无",
        composite_fulfillment_mode_snapshot="component_delivery",
        parent_production_label_enabled_snapshot=None,
        parent_production_label_units_per_label_snapshot=None,
        parent_production_label_template_version_snapshot=None,
        parent_production_label_product_version_snapshot=parent.version,
    )
    db.add(order_item)
    db.flush()
    snapshots: list[SalesOrderItemBomComponent] = []
    tasks: list[ProductionTask] = []
    for index, (child, per_set) in enumerate(zip(children, (3, 4)), start=1):
        total = 1800 * per_set
        snapshot = SalesOrderItemBomComponent(
            sales_order_item_id=order_item.id,
            component_product_id=child.id,
            parent_product_version=parent.version,
            component_product_version=child.version,
            snapshot_schema_version=3,
            order_set_quantity=1800,
            quantity_per_set=Decimal(per_set),
            required_piece_quantity=Decimal(total),
            display_order=index,
            internal_component_code=f"P179-C{index}",
            is_die_cut=False,
            spare_sheet_quantity=0,
            display_mode="show_on_delivery",
            is_required=True,
            snapshot_component_product_code=child.product_code,
            snapshot_component_product_name=child.product_name,
            snapshot_component_spec=f"子件规格{index}",
            snapshot_component_box_category="normal",
            snapshot_component_default_cutting_mode="一开一",
        )
        db.add(snapshot)
        db.flush()
        snapshots.append(snapshot)
        task = ProductionTask(
            order_item_id=order_item.id,
            sales_order_item_bom_component_id=snapshot.id,
            task_role="component_internal",
            status="pending",
            planned_quantity=total,
            ordered_quantity_snapshot=total,
            material_received_quantity=total,
            output_factor=1,
            production_label_enabled_snapshot=True,
            production_label_units_per_label_snapshot=100,
            production_label_total_quantity_snapshot=total,
            production_label_count_snapshot=total // 100,
            production_label_template_version_snapshot="current_40x30_v2",
            production_label_product_version_snapshot=child.version,
            version=1,
        )
        db.add(task)
        tasks.append(task)
    requisition = Requisition(
        requisition_number="BL-P179-001",
        requisition_date=date(2026, 8, 19),
        supplier_name="组合供应商",
        status="已报料",
    )
    db.add(requisition)
    db.flush()
    requisition_items: list[RequisitionItem] = []
    for index, (snapshot, per_set) in enumerate(zip(snapshots, (3, 4)), start=1):
        total = 1800 * per_set
        row = RequisitionItem(
            requisition_id=requisition.id,
            order_item_id=order_item.id,
            inventory_deducted_qty=0,
            requisition_qty=total,
            cardboard_len=1000,
            cardboard_width=700,
            pieces_per_box=per_set,
            required_piece_qty=total,
            special_process="一开一",
            product_code_snapshot="Z.001.000205",
            product_name_snapshot=f"组合子件{'甲' if index == 1 else '乙'}",
            status="有效",
        )
        db.add(row)
        db.flush()
        requisition_items.append(row)
        db.add(
            RequisitionItemBomSource(
                requisition_item_id=row.id,
                sales_order_item_bom_component_id=snapshot.id,
                component_type="whole",
                order_set_quantity=1800,
                quantity_per_set=Decimal(per_set),
                required_piece_quantity=Decimal(total),
                demand_basis="order_sets",
                spare_sheet_quantity=0,
                calculated_purchase_quantity=Decimal(total),
                calculation_rule_version="p1-79-v1",
            )
        )
    db.flush()
    return requisition, order_item, requisition_items


def test_component_and_parent_delivery_label_plans_are_mutually_exclusive(
    tmp_path: Path,
) -> None:
    engine = create_sqlite_engine(tmp_path / "p1-79-labels.sqlite3")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            user = User(
                username="p1-79-label-admin",
                password_hash="pytest-only",
                role="admin",
                real_name="组合标签管理员",
                display_name="组合标签管理员",
                is_active=True,
                must_change_password=False,
            )
            db.add(user)
            db.flush()
            requisition, order_item, items = _seed_label_facts(db)
            component_package = build_composite_requisition_packaging_label_package(
                db,
                requisition,
                selected_item_ids={item.id for item in items},
            )
            assert component_package["review_required"] is False
            assert [plan["fulfillment_mode"] for plan in component_package["plans"]] == [
                "component_delivery",
                "component_delivery",
            ]
            assert [plan["product_name"] for plan in component_package["plans"]] == [
                "组合子件甲",
                "组合子件乙",
            ]
            assert {plan["product_code"] for plan in component_package["plans"]} == {
                "Z.001.000205"
            }

            order_item.composite_fulfillment_mode_snapshot = "parent_delivery"
            order_item.parent_production_label_enabled_snapshot = True
            order_item.parent_production_label_units_per_label_snapshot = 50
            order_item.parent_production_label_template_version_snapshot = (
                "current_40x30_v2"
            )
            parent_product = db.get(Product, order_item.product_id)
            assert parent_product is not None
            parent_product.production_label_enabled = True
            parent_product.production_label_units_per_label = 50
            parent_product.version = int(parent_product.version) + 1
            partial = build_composite_requisition_packaging_label_package(
                db,
                requisition,
                selected_item_ids={items[0].id},
            )
            assert partial["review_required"] is True
            assert "必须整组选择所有子件" in "；".join(partial["review_messages"])
            parent_package = build_composite_requisition_packaging_label_package(
                db,
                requisition,
                selected_item_ids={item.id for item in items},
            )
            assert parent_package["review_required"] is False
            assert len(parent_package["plans"]) == 1
            assert parent_package["plans"][0]["fulfillment_mode"] == "parent_delivery"
            assert parent_package["plans"][0]["units_per_label"] == 50
            assert parent_package["plans"][0]["product_version"] == int(
                parent_product.version
            )
            assert (
                parent_package["plans"][0]["label_policy_source"]
                == "product_master_current"
            )
            assert parent_package["plans"][0]["product_name"] == "组合父件"
            assert parent_package["plans"][0]["total_quantity"] == 1800
            assert parent_package["plans"][0]["label_count"] == 36
            assert len(parent_package["job_tasks"]) == 2

            prepared = prepare_composite_packaging_label_job(
                db,
                requisition=requisition,
                selected_item_ids={item.id for item in items},
                idempotency_key="p1-79-parent-label-job",
                expected_plan_fingerprint=parent_package["plan_fingerprint"],
                operator_id=user.id,
            )
            db.commit()
            confirmed = confirm_packaging_label_job_printed(
                db,
                job_id=prepared.job.id,
                confirmation_key="p1-79-parent-label-confirm",
                operator_id=user.id,
            )
            db.commit()
            assert confirmed.job.status == "printed"
            assert len(confirmed.package["job_tasks"]) == 2
    finally:
        engine.dispose()


def test_reported_composite_components_keep_task_and_current_label_printing(
    tmp_path: Path,
) -> None:
    engine = create_sqlite_engine(tmp_path / "p1-79-reported-print.sqlite3")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            requisition, order_item, items = _seed_label_facts(db)
            for item in items:
                item.status = "已入库"
            tasks = db.query(ProductionTask).order_by(ProductionTask.id).all()
            for task in tasks:
                task.production_label_enabled_snapshot = False
                task.production_label_units_per_label_snapshot = None
                task.production_label_total_quantity_snapshot = 0
                task.production_label_count_snapshot = 0
            children = [
                db.get(
                    Product,
                    db.get(
                        SalesOrderItemBomComponent,
                        task.sales_order_item_bom_component_id,
                    ).component_product_id,
                )
                for task in tasks
            ]
            for product in children:
                assert product is not None
                product.production_label_enabled = True
                product.production_label_units_per_label = 75
                product.version = int(product.version) + 1
            db.flush()

            production_package = build_composite_requisition_production_package(
                db,
                requisition,
                selected_item_ids={item.id for item in items},
            )
            assert production_package["card_count"] == 2
            assert [
                card["material_requisition_item_ids"]
                for card in production_package["cards"]
            ] == [[items[0].id], [items[1].id]]
            assert all(
                card["selection_eligible"] is True
                for card in production_package["cards"]
            )
            assert [
                card["production_label_units_per_bundle"]
                for card in production_package["cards"]
            ] == [75, 75]

            first_card = production_package["cards"][0]
            selected_package = build_selected_production_print_package(
                db,
                selections=[
                    {
                        "source_type": "composite_bom_requisition",
                        "document_id": requisition.id,
                        "source_identity": first_card["source_identity"],
                        "selection_fingerprint": first_card[
                            "selection_fingerprint"
                        ],
                        "task_versions": first_card["production_task_versions"],
                    }
                ],
                orders={},
                composite_requisitions={requisition.id: requisition},
                batch_id="a" * 64,
            )
            assert selected_package["card_count"] == 1
            assert selected_package["cards"][0][
                "material_requisition_item_ids"
            ] == [items[0].id]

            component_labels = build_composite_requisition_packaging_label_package(
                db,
                requisition,
                selected_item_ids={item.id for item in items},
            )
            assert component_labels["review_required"] is False
            assert [plan["total_quantity"] for plan in component_labels["plans"]] == [
                5400,
                7200,
            ]
            assert [plan["units_per_label"] for plan in component_labels["plans"]] == [
                75,
                75,
            ]
            assert [plan["label_count"] for plan in component_labels["plans"]] == [
                72,
                96,
            ]

            order_item.composite_fulfillment_mode_snapshot = "parent_delivery"
            order_item.parent_production_label_enabled_snapshot = False
            parent = db.get(Product, order_item.product_id)
            assert parent is not None
            parent.production_label_enabled = True
            parent.production_label_units_per_label = 50
            parent.version = int(parent.version) + 1
            parent_labels = build_composite_requisition_packaging_label_package(
                db,
                requisition,
                selected_item_ids={item.id for item in items},
            )
            assert parent_labels["review_required"] is False
            assert len(parent_labels["plans"]) == 1
            assert parent_labels["plans"][0]["fulfillment_mode"] == "parent_delivery"
            assert parent_labels["plans"][0]["label_count"] == 36

            items[1].status = "已取消"
            with pytest.raises(ValueError, match="已取消或失效"):
                build_composite_requisition_production_package(
                    db,
                    requisition,
                    selected_item_ids={items[1].id},
                )
    finally:
        engine.dispose()


def test_order_freezes_product_default_but_allows_one_order_override(
    tmp_path: Path,
) -> None:
    engine = create_sqlite_engine(tmp_path / "p1-79-order-mode.sqlite3")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            customer = Customer(name="订单交付模式客户", customer_code="P179-ORDER")
            product = Product(
                customer_id=1,
                product_code="P179-KIT",
                customer_material_code="P179-KIT",
                product_name="组合父件",
                is_composite=True,
                combination_mode="parent_priced_set",
                composite_fulfillment_mode="component_delivery",
                production_label_enabled=True,
                production_label_units_per_label=50,
            )
            db.add_all([customer, product])
            db.flush()
            common = {
                "product_id": product.id,
                "quantity": 100,
                "unit_price": Decimal("1"),
                "product_code": product.product_code,
                "product_name": product.product_name,
            }
            default_mode = _validated_combination_provenance(
                db,
                customer=customer,
                item_payload=OrderItemCreate(**common),
                product=product,
                item_index=1,
            )
            overridden_mode = _validated_combination_provenance(
                db,
                customer=customer,
                item_payload=OrderItemCreate(
                    **common,
                    composite_fulfillment_mode_snapshot="parent_delivery",
                ),
                product=product,
                item_index=1,
            )
            assert default_mode["composite_fulfillment_mode_snapshot"] == (
                "component_delivery"
            )
            assert default_mode["parent_production_label_enabled_snapshot"] is False
            assert overridden_mode["composite_fulfillment_mode_snapshot"] == (
                "parent_delivery"
            )
            assert overridden_mode["parent_production_label_enabled_snapshot"] is True
            assert overridden_mode[
                "parent_production_label_units_per_label_snapshot"
            ] == 50
    finally:
        engine.dispose()


def test_parent_delivery_hides_child_lines_from_customer_documents() -> None:
    lines = _actual_goods_lines(
        order_item_id=1,
        product_code="P179-KIT",
        product_name="组合父件",
        specification="组合规格",
        parent_quantity=1800,
        component_lines=[
            {
                "component_snapshot_id": 11,
                "product_code": "P179-A",
                "product_name": "子件 A",
                "specification": "A",
                "unit": "片",
                "planned_delivery_quantity": 5400,
            },
            {
                "component_snapshot_id": 12,
                "product_code": "P179-B",
                "product_name": "子件 B",
                "specification": "B",
                "unit": "片",
                "planned_delivery_quantity": 7200,
            },
        ],
        fulfillment_mode="parent_delivery",
    )
    assert [(line["line_type"], line["product_code"], line["quantity"]) for line in lines] == [
        ("parent", "P179-KIT", 1800)
    ]


def test_customer_document_parent_delivery_is_a_one_way_override() -> None:
    assert _customer_document_fulfillment_mode(
        frozen_order_mode="component_delivery",
        current_product_mode="parent_delivery",
    ) == "parent_delivery"
    assert _customer_document_fulfillment_mode(
        frozen_order_mode="parent_delivery",
        current_product_mode="component_delivery",
    ) == "parent_delivery"
    assert _customer_document_fulfillment_mode(
        frozen_order_mode="component_delivery",
        current_product_mode="component_delivery",
    ) == "component_delivery"


def test_current_parent_delivery_print_keeps_other_actual_product(
    tmp_path: Path,
) -> None:
    engine = create_sqlite_engine(tmp_path / "p0-21-parent-delivery-print.sqlite3")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            user = User(
                username="p0-21-print-admin",
                password_hash="pytest-only",
                role="admin",
                real_name="送货打印管理员",
                display_name="送货打印管理员",
                is_active=True,
                must_change_password=False,
            )
            db.add(user)
            db.flush()
            _requisition, composite_item, _items = _seed_label_facts(db)
            parent = db.get(Product, composite_item.product_id)
            assert parent is not None
            parent.composite_fulfillment_mode = "parent_delivery"
            composite_item.delivered_quantity = 500

            order = db.get(Order, composite_item.order_id)
            assert order is not None
            ordinary = Product(
                customer_id=order.customer_id,
                product_code="Z.001.000206",
                customer_material_code="Z.001.000206",
                product_name="30入装衬板6片",
                unit="PCS",
            )
            db.add(ordinary)
            db.flush()
            ordinary_item = OrderItem(
                order_id=order.id,
                product_id=ordinary.id,
                item_order_number="P0-21-ORDINARY-001",
                item_sequence=2,
                quantity=1800,
                delivered_quantity=500,
                unit_price=Decimal("1"),
                subtotal=Decimal("1800"),
                material_status="received",
                snapshot_product_code=ordinary.product_code,
                snapshot_product_name=ordinary.product_name,
                snapshot_spec="778×1139mm",
                special_process="无",
            )
            db.add(ordinary_item)
            db.flush()

            delivery = Delivery(
                delivery_number="YL-20260825-P021",
                customer_id=order.customer_id,
                delivery_date=date(2026, 8, 25),
                status="dispatched",
                total_quantity=1000,
                created_by=user.id,
            )
            db.add(delivery)
            db.flush()
            db.add_all(
                [
                    DeliveryItem(
                        delivery_id=delivery.id,
                        source_type="order",
                        order_item_id=ordinary_item.id,
                        product_code_snapshot=ordinary.product_code,
                        product_name_snapshot=ordinary.product_name,
                        specification_snapshot="778×1139mm",
                        delivered_quantity=500,
                        ordered_quantity_snapshot=1800,
                        order_remaining_snapshot=1800,
                    ),
                    DeliveryItem(
                        delivery_id=delivery.id,
                        source_type="order",
                        order_item_id=composite_item.id,
                        product_code_snapshot=parent.product_code,
                        product_name_snapshot=parent.product_name,
                        specification_snapshot="组合规格",
                        delivered_quantity=500,
                        ordered_quantity_snapshot=1800,
                        order_remaining_snapshot=1800,
                    ),
                ]
            )
            db.commit()

            payload = get_delivery_print_data(delivery.id, db=db, user=user)
            detail = _delivery_response(db, delivery.id)
            summary = _delivery_summary_response(
                delivery.id,
                context=_delivery_list_summary_context(db, [delivery.id]),
            )

            assert [
                (row["line_type"], row["product_code"], row["quantity"])
                for row in payload["actual_goods_items"]
            ] == [
                ("parent", "Z.001.000206", 500),
                ("parent", "Z.001.000205", 500),
            ]
            assert payload["total_quantity"] == 1000
            assert payload["total_actual_goods_quantity"] == 1000
            assert [
                [line["product_code"] for line in item["actual_goods_lines"]]
                for item in payload["items"]
            ] == [["Z.001.000206"], ["Z.001.000205"]]
            assert [
                [line["product_code"] for line in item["actual_goods_lines"]]
                for item in detail["items"]
            ] == [["Z.001.000206"], ["Z.001.000205"]]
            assert detail["total_actual_goods_quantity"] == 1000
            assert summary["item_count"] == 2
            assert summary["total_actual_goods_quantity"] == 1000
    finally:
        engine.dispose()


def test_reported_ui_and_label_page_expose_composite_group_contract() -> None:
    index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    label_page = (ROOT / "static" / "production-packaging-label.html").read_text(
        encoding="utf-8"
    )
    api = (ROOT / "app" / "api" / "requisition.py").read_text(encoding="utf-8")
    for marker in (
        "组合父件：",
        "撤销整组报料",
        "reportedCompositeGroupSelected",
        "选择父件标签",
        "composite_group_item_ids",
        "batch_id=",
        "item_ids=",
    ):
        assert marker in index
    assert "reported-item-action-column" not in index
    assert "compositeBatchId" in label_page
    assert "selected_item_ids:compositeItemIds" in label_page
    assert "组合 BOM 必须从父组整组撤销" in api
    assert 'Product.production_label_enabled.label(' in api
    assert "order_item_id: int | None = Query(default=None, gt=0)" in api


def test_vv30_backfills_legacy_bom_order_without_combination_role(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "vv30-legacy-composite.sqlite3"
    config = _migration_config(monkeypatch, path)
    command.upgrade(config, "uu29v8x9z18")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO customers (id, customer_code, name) "
            "VALUES (1, 'P179', 'legacy composite customer')"
        )
        connection.executemany(
            """
            INSERT INTO products (
                id, customer_id, product_code, customer_material_code,
                product_name, box_category, unit, is_composite,
                combination_mode, production_label_enabled,
                production_label_units_per_label
            ) VALUES (?, 1, ?, ?, ?, 'normal', 'PCS', ?, ?, ?, ?)
            """,
            [
                (
                    1,
                    "LEGACY-KIT",
                    "LEGACY-KIT",
                    "legacy parent",
                    1,
                    "parent_priced_set",
                    1,
                    50,
                ),
                (
                    2,
                    "LEGACY-COMP",
                    "LEGACY-COMP",
                    "legacy component",
                    0,
                    "parent_priced_set",
                    0,
                    None,
                ),
            ],
        )
        connection.execute(
            """
            INSERT INTO sales_orders (
                id, order_number, customer_id, order_date, status,
                payment_status, total_amount
            ) VALUES (
                1, 'P179-LEGACY', 1, '2026-08-19',
                'pending_production', 'unpaid', 1800
            )
            """
        )
        connection.execute(
            """
            INSERT INTO sales_order_items (
                id, order_id, product_id, quantity, unit_price, subtotal,
                material_status, snapshot_product_name, combination_role
            ) VALUES (
                1, 1, 1, 1800, 1, 1800,
                'pending', 'legacy parent', 'standalone'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO sales_order_item_bom_components (
                id, sales_order_item_id, component_product_id,
                order_set_quantity, quantity_per_set, required_piece_quantity,
                display_order, internal_component_code, is_die_cut,
                spare_sheet_quantity, display_mode, show_on_delivery, is_required,
                snapshot_component_product_code, snapshot_component_product_name,
                snapshot_component_box_category, snapshot_schema_version,
                snapshot_component_default_cutting_mode
            ) VALUES (
                1, 1, 2, 1800, 3, 5400,
                1, 'LEGACY-KIT-S01', 0, 0, 'internal_only', 0, 1,
                'LEGACY-COMP', 'legacy component', 'normal', 3, '一开一'
            )
            """
        )
        connection.commit()

    command.upgrade(config, "vv30v8x9z19")
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT composite_fulfillment_mode FROM products WHERE id = 1"
        ).fetchone()[0] == "parent_delivery"
        assert connection.execute(
            """
            SELECT composite_fulfillment_mode_snapshot,
                   parent_production_label_enabled_snapshot,
                   parent_production_label_units_per_label_snapshot
            FROM sales_order_items WHERE id = 1
            """
        ).fetchone() == ("parent_delivery", 1, 50)
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == "vv30v8x9z19"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()

    command.downgrade(config, "uu29v8x9z18")
    with sqlite3.connect(path) as connection:
        assert "composite_fulfillment_mode" not in {
            row[1] for row in connection.execute("PRAGMA table_info(products)")
        }
        assert "material_requisition_id" not in {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(production_packaging_label_print_jobs)"
            )
        }
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()

    command.upgrade(config, "vv30v8x9z19")
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == "vv30v8x9z19"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
