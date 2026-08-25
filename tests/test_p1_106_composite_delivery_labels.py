from __future__ import annotations

import copy
import json
import sqlite3
from collections.abc import Generator
from datetime import date
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryItem, DeliveryPickTask
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.product_bom import (
    RequisitionItemBomSource,
    SalesOrderItemBomComponent,
)
from app.models.production import ProductionTask
from app.models.production_label_print import (
    ProductionPackagingLabelLayoutRevision,
    ProductionPackagingLabelPrintJob,
    ProductionPackagingLabelPrintJobTask,
)
from app.models.requisition import Requisition, RequisitionItem
from app.services.production_label_operations import (
    ProductionLabelOperationError,
    prepare_delivery_packaging_label_job,
)
from app.services.production_packaging_label import (
    ProductionPackagingLabelError,
    apply_delivery_packaging_label_print_counts,
    build_delivery_packaging_label_package,
)
from app.services.production_packaging_label_layout import (
    default_layout,
    layout_hash,
)
from app.services.requisition_production_print import (
    build_composite_requisition_production_package,
)


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "de39v8x9z28"
TARGET_REVISION = "df40v8x9z29"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _layout_element(layout: dict, element_id: str) -> dict:
    return next(row for row in layout["elements"] if row["id"] == element_id)


def _legacy_overlapping_layout() -> dict:
    """Represent a valid historical P1-66B layout that predates overlap writes."""

    layout = copy.deepcopy(default_layout())
    product_name = _layout_element(layout, "product_name")
    specification = _layout_element(layout, "specification")
    product_name.update(
        x_mm=specification["x_mm"],
        y_mm=specification["y_mm"],
        width_mm=specification["width_mm"],
        height_mm=specification["height_mm"],
    )
    return layout


def _add_layout_release(
    db: Session,
    *,
    version: int,
    layout: dict,
    operation_key: str,
) -> None:
    db.add(
        ProductionPackagingLabelLayoutRevision(
            stream="release",
            version=version,
            catalog_version=str(layout["catalog_version"]),
            payload_json=_canonical_json(layout),
            payload_hash=layout_hash(layout),
            base_release_version=max(version - 1, 0),
            operation_kind="publish",
            operation_key=operation_key,
        )
    )


@pytest.fixture()
def delivery_label_api(tmp_path: Path):
    import app.models  # noqa: F401
    from app.api.auth import router as auth_router
    from app.api.deliveries import router as deliveries_router
    from app.api.deps import get_db
    from app.api.requisition import router as requisition_router
    from app.core.security import hash_password
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "p1-106-delivery-label.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as db:
        admin = User(
            username="p1106-admin",
            password_hash=hash_password("123456"),
            role="admin",
            real_name="送货标签管理员",
            display_name="送货标签管理员",
            must_change_password=False,
        )
        sales = User(
            username="p1106-sales",
            password_hash=hash_password("123456"),
            role="sales",
            real_name="无送货权限销售",
            display_name="无送货权限销售",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=10601,
            customer_code="P1106",
            name="送货标签测试客户",
            chinese_short_name="标签客户",
        )
        db.add_all([admin, sales, customer])
        db.flush()
        product = Product(
            customer_id=customer.id,
            product_code="P1106-OLD-CODE",
            customer_material_code="P1106-OLD-CODE",
            product_name="订单历史产品名称",
            length_mm=Decimal("400"),
            width_mm=Decimal("300"),
            height_mm=Decimal("200"),
            production_label_enabled=True,
            production_label_units_per_label=50,
        )
        db.add(product)
        db.flush()
        order = Order(
            order_number="PO-P1106-001",
            customer_id=customer.id,
            customer_po="CPO-P1106",
            order_date=date(2026, 8, 25),
            delivery_date=date(2026, 8, 28),
            status="pending_delivery",
            payment_status="unpaid",
            total_amount=Decimal("103"),
        )
        db.add(order)
        db.flush()
        order_item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=103,
            unit_price=Decimal("1"),
            subtotal=Decimal("103"),
            material_status="received",
            snapshot_product_code="P1106-OLD-CODE",
            snapshot_product_name="订单历史产品名称",
            snapshot_spec="400×300×200mm",
        )
        db.add(order_item)
        db.flush()
        delivery = Delivery(
            delivery_number="DN-P1106-001",
            customer_id=customer.id,
            delivery_date=date(2026, 8, 25),
            source_mode="order",
            status="pending",
            total_quantity=103,
            created_by=admin.id,
        )
        db.add(delivery)
        db.flush()
        delivery_item = DeliveryItem(
            delivery_id=delivery.id,
            source_type="order",
            order_item_id=order_item.id,
            product_code_snapshot="P1106-OLD-CODE",
            product_name_snapshot="订单历史产品名称",
            specification_snapshot="400×300×200mm",
            delivered_quantity=103,
            ordered_quantity_snapshot=103,
            order_remaining_snapshot=103,
        )
        db.add(delivery_item)
        db.add(
            DeliveryPickTask(
                delivery_id=delivery.id,
                customer_id=customer.id,
                status="pushed",
                snapshot_version=1,
                created_by=admin.id,
            )
        )
        _add_layout_release(
            db,
            version=1,
            layout=_legacy_overlapping_layout(),
            operation_key="p1-106-overlap-release",
        )
        db.commit()
        fixture_ids = {
            "admin_id": admin.id,
            "sales_id": sales.id,
            "customer_id": customer.id,
            "product_id": product.id,
            "order_item_id": order_item.id,
            "delivery_id": delivery.id,
            "delivery_item_id": delivery_item.id,
        }

    application = FastAPI()
    application.include_router(auth_router, prefix="/api/auth")
    application.include_router(deliveries_router, prefix="/api/deliveries")
    application.include_router(requisition_router, prefix="/api/requisition")

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as db:
            yield db

    application.dependency_overrides[get_db] = override_get_db
    yield {
        "app": application,
        "session_factory": session_factory,
        **fixture_ids,
    }
    engine.dispose()


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "123456"},
    )
    assert response.status_code == 200, response.text


def _migration_config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _synthetic_delivery_package(label_count: int) -> dict:
    quantities = [10] * max(label_count - 1, 0) + [3]
    selection_key = "delivery:106:item:206:product"
    layout = {
        "version": 7,
        "layout": _legacy_overlapping_layout(),
        "layout_hash": "a" * 64,
    }
    return {
        "delivery_id": 106,
        "delivery_number": "DN-P1106-COUNT",
        "delivery_status": "pending",
        "template_version": "current_40x30_v2",
        "plan_fingerprint": "f" * 64,
        "print_plan_count": 1,
        "label_count": label_count,
        "plans": [
            {
                "selection_key": selection_key,
                "delivery_item_id": 206,
                "product_id": 306,
                "product_version": 1,
                "template_version": "current_40x30_v2",
                "total_quantity": sum(quantities),
                "units_per_label": 10,
                "label_count": label_count,
                "label_quantities": quantities,
            }
        ],
        "labels": [
            {
                "selection_key": selection_key,
                "delivery_item_id": 206,
                "label_number": number,
                "label_count": label_count,
                "quantity": quantity,
                "units_per_label": 10,
            }
            for number, quantity in enumerate(quantities, start=1)
        ],
        "label_layout": layout,
        "printable": True,
        "review_required": False,
    }


def _seed_composite_label_facts(
    db: Session,
) -> tuple[Requisition, OrderItem, list[RequisitionItem]]:
    customer = Customer(
        name="组合交付测试客户",
        chinese_short_name="组合客户",
        customer_code="P1106-COMPOSITE",
    )
    db.add(customer)
    db.flush()
    parent = Product(
        customer_id=customer.id,
        product_code="P1106-KIT",
        customer_material_code="P1106-KIT",
        product_name="组合父件",
        is_composite=True,
        composite_fulfillment_mode="component_delivery",
    )
    children = [
        Product(
            customer_id=customer.id,
            product_code=f"P1106-C{index}",
            customer_material_code=f"P1106-C{index}",
            product_name=f"组合子件{index}",
            is_internal_component=True,
            production_label_enabled=True,
            production_label_units_per_label=100,
        )
        for index in (1, 2)
    ]
    db.add_all([parent, *children])
    db.flush()
    order = Order(
        order_number="PO-P1106-COMPOSITE",
        customer_id=customer.id,
        customer_po="CPO-P1106-COMPOSITE",
        order_date=date(2026, 8, 25),
        delivery_date=date(2026, 8, 28),
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
        parent_production_label_product_version_snapshot=parent.version,
    )
    db.add(order_item)
    db.flush()

    snapshots: list[SalesOrderItemBomComponent] = []
    for index, (child, quantity_per_set) in enumerate(
        zip(children, (3, 4)),
        start=1,
    ):
        total = 1800 * quantity_per_set
        snapshot = SalesOrderItemBomComponent(
            sales_order_item_id=order_item.id,
            component_product_id=child.id,
            parent_product_version=parent.version,
            component_product_version=child.version,
            snapshot_schema_version=3,
            order_set_quantity=1800,
            quantity_per_set=Decimal(quantity_per_set),
            required_piece_quantity=Decimal(total),
            display_order=index,
            internal_component_code=f"P1106-S{index}",
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
        db.add(
            ProductionTask(
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
        )

    requisition = Requisition(
        requisition_number="BL-P1106-COMPOSITE",
        requisition_date=date(2026, 8, 25),
        supplier_name="组合供应商",
        status="已报料",
    )
    db.add(requisition)
    db.flush()
    requisition_items: list[RequisitionItem] = []
    for index, (snapshot, quantity_per_set) in enumerate(
        zip(snapshots, (3, 4)),
        start=1,
    ):
        total = 1800 * quantity_per_set
        item = RequisitionItem(
            requisition_id=requisition.id,
            order_item_id=order_item.id,
            inventory_deducted_qty=0,
            requisition_qty=total,
            cardboard_len=1000,
            cardboard_width=700,
            pieces_per_box=quantity_per_set,
            required_piece_qty=total,
            special_process="一开一",
            product_code_snapshot=f"P1106-C{index}",
            product_name_snapshot=f"组合子件{index}",
            status="有效",
        )
        db.add(item)
        db.flush()
        requisition_items.append(item)
        db.add(
            RequisitionItemBomSource(
                requisition_item_id=item.id,
                sales_order_item_bom_component_id=snapshot.id,
                component_type="whole",
                order_set_quantity=1800,
                quantity_per_set=Decimal(quantity_per_set),
                required_piece_quantity=Decimal(total),
                demand_basis="order_sets",
                spare_sheet_quantity=0,
                calculated_purchase_quantity=Decimal(total),
                calculation_rule_version="p1-106-v1",
            )
        )
    db.flush()
    return requisition, order_item, requisition_items


def test_delivery_more_menu_exposes_product_label_printing() -> None:
    page = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    assert 'data-action="print-product-labels"' in page
    assert "production-packaging-label.html?delivery_id=" in page


def test_packaging_label_job_has_an_explicit_delivery_source() -> None:
    assert "delivery_id" in ProductionPackagingLabelPrintJob.__table__.columns
    source_constraint = next(
        constraint
        for constraint in ProductionPackagingLabelPrintJob.__table__.constraints
        if constraint.name == "ck_production_packaging_label_print_jobs_source"
    )
    source_sql = str(source_constraint.sqltext)
    for source_column in (
        "supplier_order_id",
        "material_requisition_id",
        "delivery_id",
    ):
        assert source_column in source_sql


def test_parent_and_component_delivery_projection_do_not_change_child_production(
    tmp_path: Path,
) -> None:
    engine = create_sqlite_engine(tmp_path / "p1-106-composite-projection.sqlite3")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            requisition, order_item, requisition_items = _seed_composite_label_facts(db)
            parent = db.get(Product, order_item.product_id)
            assert parent is not None
            snapshots = list(
                db.scalars(
                    select(SalesOrderItemBomComponent)
                    .where(
                        SalesOrderItemBomComponent.sales_order_item_id
                        == order_item.id
                    )
                    .order_by(SalesOrderItemBomComponent.display_order)
                ).all()
            )
            children = [db.get(Product, row.component_product_id) for row in snapshots]
            assert all(child is not None for child in children)
            for child in children:
                child.production_label_enabled = True
                child.production_label_units_per_label = 30
                child.version = int(child.version) + 1
            parent.production_label_enabled = True
            parent.production_label_units_per_label = 40
            parent.composite_fulfillment_mode = "component_delivery"
            parent.version = int(parent.version) + 1

            delivery = Delivery(
                delivery_number="DN-P1106-COMPOSITE",
                customer_id=parent.customer_id,
                delivery_date=date(2026, 8, 25),
                source_mode="order",
                status="pending",
                total_quantity=101,
            )
            db.add(delivery)
            db.flush()
            db.add(
                DeliveryItem(
                    delivery_id=delivery.id,
                    source_type="order",
                    order_item_id=order_item.id,
                    product_code_snapshot=order_item.snapshot_product_code,
                    product_name_snapshot=order_item.snapshot_product_name,
                    specification_snapshot=order_item.snapshot_spec,
                    delivered_quantity=101,
                    ordered_quantity_snapshot=1800,
                    order_remaining_snapshot=1800,
                )
            )
            db.flush()

            child_package = build_delivery_packaging_label_package(db, delivery)
            assert child_package["review_required"] is False
            assert [plan["fulfillment_mode"] for plan in child_package["plans"]] == [
                "component_delivery",
                "component_delivery",
            ]
            assert [plan["component_snapshot_id"] for plan in child_package["plans"]] == [
                row.id for row in snapshots
            ]
            assert [plan["product_name"] for plan in child_package["plans"]] == [
                row.snapshot_component_product_name for row in snapshots
            ]
            assert [plan["total_quantity"] for plan in child_package["plans"]] == [
                303,
                404,
            ]
            assert [plan["label_count"] for plan in child_package["plans"]] == [
                11,
                14,
            ]
            assert [
                plan["label_quantities"][-1] for plan in child_package["plans"]
            ] == [3, 14]

            # P0-21 is intentionally one-way: the current parent mode may promote
            # an older child-mode order, and the frozen parent mode may never be
            # demoted by changing the current common-box master back to child.
            parent.composite_fulfillment_mode = "parent_delivery"
            parent_package = build_delivery_packaging_label_package(db, delivery)
            assert len(parent_package["plans"]) == 1
            assert parent_package["plans"][0]["fulfillment_mode"] == "parent_delivery"
            assert parent_package["plans"][0]["total_quantity"] == 101
            assert parent_package["plans"][0]["label_quantities"] == [40, 40, 21]

            order_item.composite_fulfillment_mode_snapshot = "parent_delivery"
            parent.composite_fulfillment_mode = "component_delivery"
            still_parent = build_delivery_packaging_label_package(db, delivery)
            assert [plan["fulfillment_mode"] for plan in still_parent["plans"]] == [
                "parent_delivery"
            ]

            # Delivery projection must not suppress the two real child production
            # cards: children are produced first even when customers receive parent sets.
            production = build_composite_requisition_production_package(
                db,
                requisition,
                selected_item_ids={row.id for row in requisition_items},
            )
            assert production["card_count"] == 2
            assert [card["product_name"] for card in production["cards"]] == [
                row.snapshot_component_product_name for row in snapshots
            ]
    finally:
        engine.dispose()


def test_legacy_order_fallback_and_unordered_delivery_identity_never_follow_renames(
    delivery_label_api,
) -> None:
    fixture = delivery_label_api
    with fixture["session_factory"]() as db:
        delivery = db.get(Delivery, fixture["delivery_id"])
        delivery_item = db.get(DeliveryItem, fixture["delivery_item_id"])
        order_item = db.get(OrderItem, fixture["order_item_id"])
        product = db.get(Product, fixture["product_id"])
        assert all(row is not None for row in (delivery, delivery_item, order_item, product))

        # Old order-sourced delivery rows may have no delivery-level display
        # snapshots.  They must fall back to the immutable OrderItem snapshots.
        delivery_item.product_code_snapshot = None
        delivery_item.product_name_snapshot = None
        delivery_item.specification_snapshot = None
        order_item.snapshot_product_code = "ORDER-SNAPSHOT-CODE"
        order_item.snapshot_product_name = "订单快照名称"
        order_item.snapshot_spec = "历史订单规格"
        product.product_code = "CURRENT-RENAMED-CODE"
        product.product_name = "当前主档改名"
        product.version = int(product.version) + 1

        unordered_product = Product(
            customer_id=fixture["customer_id"],
            product_code="CURRENT-UNORDERED-CODE",
            customer_material_code="CURRENT-UNORDERED-CODE",
            product_name="当前无订单主档名称",
            production_label_enabled=True,
            production_label_units_per_label=20,
        )
        db.add(unordered_product)
        db.flush()
        unordered_item = DeliveryItem(
            delivery_id=delivery.id,
            source_type="unordered_finished",
            product_id=unordered_product.id,
            product_code_snapshot="UNORDERED-HISTORICAL-CODE",
            product_name_snapshot="无订单历史交付名称",
            specification_snapshot="无订单历史规格",
            unit_snapshot="片",
            price_source="manual_snapshot",
            delivered_quantity=21,
        )
        db.add(unordered_item)
        delivery.source_mode = "mixed"
        delivery.total_quantity = 124
        db.flush()

        package = build_delivery_packaging_label_package(db, delivery)
        assert package["review_required"] is False
        plans = {plan["product_id"]: plan for plan in package["plans"]}
        assert plans[product.id]["product_code"] == "ORDER-SNAPSHOT-CODE"
        assert plans[product.id]["product_name"] == "订单快照名称"
        assert plans[product.id]["specification"] == "历史订单规格"
        assert plans[unordered_product.id]["product_code"] == (
            "UNORDERED-HISTORICAL-CODE"
        )
        assert plans[unordered_product.id]["product_name"] == "无订单历史交付名称"
        assert plans[unordered_product.id]["specification"] == "无订单历史规格"
        assert "CURRENT-RENAMED-CODE" not in {
            plan["product_code"] for plan in package["plans"]
        }
        assert "CURRENT-UNORDERED-CODE" not in {
            plan["product_code"] for plan in package["plans"]
        }


@pytest.mark.parametrize("label_count", (1, 2, 30, 300))
def test_delivery_count_freeze_keeps_full_tail_and_reduction_omits_the_tail(
    label_count: int,
) -> None:
    package = _synthetic_delivery_package(label_count)
    original = copy.deepcopy(package)
    selection_key = package["plans"][0]["selection_key"]

    full = apply_delivery_packaging_label_print_counts(
        package,
        {selection_key: label_count},
    )
    assert full["label_count"] == label_count
    assert full["labels"][-1]["quantity"] == 3
    assert full["plans"][0]["print_label_count"] == label_count
    assert full["label_layout"] == package["label_layout"]
    assert package == original

    if label_count > 1:
        reduced = apply_delivery_packaging_label_print_counts(
            package,
            {selection_key: label_count - 1},
        )
        assert reduced["system_label_count"] == label_count
        assert reduced["label_count"] == label_count - 1
        assert [row["quantity"] for row in reduced["labels"]] == [10] * (
            label_count - 1
        )
        assert reduced["print_selection"] == [
            {
                "selection_key": selection_key,
                "print_label_count": label_count - 1,
                "system_label_count": label_count,
            }
        ]
        assert reduced["label_layout"] == package["label_layout"]


@pytest.mark.parametrize(
    ("counts", "message"),
    [
        ({}, "清单"),
        ({"delivery:106:item:206:product": True}, "必须为整数"),
        ({"delivery:106:item:206:product": 301}, "0～300"),
    ],
)
def test_delivery_count_selection_fails_closed(counts: dict, message: str) -> None:
    with pytest.raises(ProductionPackagingLabelError, match=message):
        apply_delivery_packaging_label_print_counts(
            _synthetic_delivery_package(300),
            counts,
        )


def test_delivery_api_freezes_overlap_layout_has_no_task_links_and_is_idempotent(
    delivery_label_api,
) -> None:
    fixture = delivery_label_api
    package_url = (
        f"/api/deliveries/{fixture['delivery_id']}"
        "/production-packaging-label-package"
    )
    with TestClient(fixture["app"]) as client:
        _login(client, "p1106-admin")
        preview = client.get(package_url)
        assert preview.status_code == 200, preview.text
        preview_body = preview.json()
        assert preview_body["label_layout"]["version"] == 1
        overlap_layout = preview_body["label_layout"]["layout"]
        assert _layout_element(overlap_layout, "product_name")["x_mm"] == (
            _layout_element(overlap_layout, "specification")["x_mm"]
        )
        assert [row["quantity"] for row in preview_body["labels"]] == [50, 50, 3]
        selection_key = preview_body["plans"][0]["selection_key"]
        request = {
            "idempotency_key": "p1106-delivery-job",
            "plan_fingerprint": preview_body["plan_fingerprint"],
            "confirmed": True,
            "items": [
                {"selection_key": selection_key, "print_label_count": 2}
            ],
        }

        prepared = client.post(
            f"/api/deliveries/{fixture['delivery_id']}"
            "/production-packaging-label-jobs",
            json=request,
        )
        assert prepared.status_code == 200, prepared.text
        prepared_body = prepared.json()
        job_id = prepared_body["job_id"]
        assert prepared_body["package"]["system_label_count"] == 3
        assert prepared_body["package"]["label_count"] == 2
        assert [row["quantity"] for row in prepared_body["package"]["labels"]] == [
            50,
            50,
        ]
        assert all(
            plan.get("production_task_id") is None
            for plan in prepared_body["package"]["plans"]
        )

        replay = client.post(
            f"/api/deliveries/{fixture['delivery_id']}"
            "/production-packaging-label-jobs",
            json=request,
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["job_id"] == job_id

        changed = copy.deepcopy(request)
        changed["items"][0]["print_label_count"] = 1
        conflict = client.post(
            f"/api/deliveries/{fixture['delivery_id']}"
            "/production-packaging-label-jobs",
            json=changed,
        )
        assert conflict.status_code == 409
        assert "幂等键" in conflict.text

        # Generic historical-job reads require both product-label and delivery
        # permissions; sales has orders.view but deliberately lacks deliveries.view.
        _login(client, "p1106-sales")
        denied = client.get(
            f"/api/requisition/production-packaging-label-jobs/{job_id}"
        )
        assert denied.status_code == 403
        assert "送货单查看权限" in denied.text

        _login(client, "p1106-admin")
        confirmed = client.post(
            f"/api/requisition/production-packaging-label-jobs/{job_id}/confirm",
            json={"idempotency_key": "p1106-confirm", "confirmed": True},
        )
        assert confirmed.status_code == 200, confirmed.text
        confirm_replay = client.post(
            f"/api/requisition/production-packaging-label-jobs/{job_id}/confirm",
            json={"idempotency_key": "p1106-confirm", "confirmed": True},
        )
        assert confirm_replay.status_code == 200, confirm_replay.text
        wrong_confirmation = client.post(
            f"/api/requisition/production-packaging-label-jobs/{job_id}/confirm",
            json={"idempotency_key": "p1106-other-confirm", "confirmed": True},
        )
        assert wrong_confirmation.status_code == 409

    with fixture["session_factory"]() as db:
        assert db.scalar(
            select(func.count())
            .select_from(ProductionPackagingLabelPrintJob)
            .where(ProductionPackagingLabelPrintJob.delivery_id == fixture["delivery_id"])
        ) == 1
        assert db.scalar(
            select(func.count())
            .select_from(ProductionPackagingLabelPrintJobTask)
            .where(ProductionPackagingLabelPrintJobTask.print_job_id == job_id)
        ) == 0
        prepared_audits = list(
            db.scalars(
                select(OperationLog).where(
                    OperationLog.action_code
                    == "delivery.packaging_label_job.prepared"
                )
            ).all()
        )
        printed_audits = list(
            db.scalars(
                select(OperationLog).where(
                    OperationLog.action_code
                    == "production.packaging_label_job.printed"
                )
            ).all()
        )
        assert len(prepared_audits) == len(printed_audits) == 1
        prepared_detail = json.loads(prepared_audits[0].details or "{}")
        assert prepared_detail["label_count"] == 2
        assert prepared_detail["system_label_count"] == 3
        assert prepared_detail["lines"][0]["print_label_count"] == 2
        assert "unit_price" not in (prepared_audits[0].details or "")

        # A printed job remains the immutable reprint source after the current
        # common-box policy and released P1-104 visibility/suffix layout change.
        product = db.get(Product, fixture["product_id"])
        assert product is not None
        product.production_label_units_per_label = 25
        product.version = int(product.version) + 1
        next_layout = copy.deepcopy(default_layout())
        _layout_element(next_layout, "product_name")["visible"] = False
        _layout_element(next_layout, "quantity")["fixed_suffix"] = ""
        _add_layout_release(
            db,
            version=2,
            layout=next_layout,
            operation_key="p1-106-hidden-suffix-release",
        )
        db.commit()

    with TestClient(fixture["app"]) as client:
        _login(client, "p1106-admin")
        current = client.get(package_url)
        historical = client.get(
            f"/api/requisition/production-packaging-label-jobs/{job_id}"
        )
        assert current.status_code == 200, current.text
        assert historical.status_code == 200, historical.text
        assert current.json()["label_layout"]["version"] == 2
        assert current.json()["plans"][0]["units_per_label"] == 25
        frozen = historical.json()["package"]
        assert frozen["label_layout"]["version"] == 1
        assert frozen["plans"][0]["units_per_label"] == 50
        assert frozen["plans"][0]["print_label_count"] == 2
        assert [row["quantity"] for row in frozen["labels"]] == [50, 50]

        deleted = client.delete(f"/api/deliveries/{fixture['delivery_id']}")
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["disposition"] == "voided"

    with fixture["session_factory"]() as db:
        delivery = db.get(Delivery, fixture["delivery_id"])
        assert delivery is not None
        assert delivery.status == "voided"
        assert delivery.ever_dispatched_at is None
        assert db.get(ProductionPackagingLabelPrintJob, job_id) is not None
        assert db.scalar(
            select(func.count()).select_from(DeliveryPickTask).where(
                DeliveryPickTask.delivery_id == fixture["delivery_id"]
            )
        ) == 0
        void_audit = db.scalar(
            select(OperationLog)
            .where(
                OperationLog.resource == "Delivery",
                OperationLog.entity_id == fixture["delivery_id"],
                OperationLog.action == "VOID_WITH_LABEL_HISTORY",
            )
            .order_by(OperationLog.id.desc())
        )
        assert void_audit is not None
        assert "取消发货" not in str(void_audit.description or "")
        assert json.loads(void_audit.details or "{}")["label_job_at"]
        assert db.scalar(
            select(func.count()).select_from(OperationLog).where(
                OperationLog.resource == "Delivery",
                OperationLog.entity_id == fixture["delivery_id"],
                OperationLog.action == "VOID_AFTER_CANCEL",
            )
        ) == 0
        pick_audit = db.scalar(
            select(OperationLog)
            .where(
                OperationLog.resource == "DeliveryPickTask",
                OperationLog.action == "PICK_TASK_INVALIDATED",
            )
            .order_by(OperationLog.id.desc())
        )
        assert pick_audit is not None
        assert json.loads(pick_audit.details or "{}")["reason"] == (
            "delivery_voided_with_label_history"
        )


def test_delivery_prepare_audit_failure_rolls_back_the_entire_job(
    delivery_label_api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import deliveries as deliveries_api

    fixture = delivery_label_api
    package_url = (
        f"/api/deliveries/{fixture['delivery_id']}"
        "/production-packaging-label-package"
    )
    with TestClient(fixture["app"], raise_server_exceptions=False) as client:
        _login(client, "p1106-admin")
        preview = client.get(package_url)
        assert preview.status_code == 200, preview.text
        package = preview.json()

        def fail_audit(*_args, **_kwargs):
            raise RuntimeError("forced P1-106 audit failure")

        monkeypatch.setattr(deliveries_api, "append_audit_event", fail_audit)
        response = client.post(
            f"/api/deliveries/{fixture['delivery_id']}"
            "/production-packaging-label-jobs",
            json={
                "idempotency_key": "p1106-audit-rollback",
                "plan_fingerprint": package["plan_fingerprint"],
                "confirmed": True,
                "items": [
                    {
                        "selection_key": package["plans"][0]["selection_key"],
                        "print_label_count": 2,
                    }
                ],
            },
        )
        assert response.status_code == 500

    with fixture["session_factory"]() as db:
        assert db.scalar(
            select(func.count())
            .select_from(ProductionPackagingLabelPrintJob)
            .where(ProductionPackagingLabelPrintJob.delivery_id == fixture["delivery_id"])
        ) == 0
        delivery = db.get(Delivery, fixture["delivery_id"])
        assert delivery is not None
        assert delivery.status == "pending"
        assert delivery.ever_dispatched_at is None


def test_delivery_job_same_key_rejects_another_operator(
    delivery_label_api,
) -> None:
    fixture = delivery_label_api
    with fixture["session_factory"]() as first_db:
        first_delivery = first_db.get(Delivery, fixture["delivery_id"])
        assert first_delivery is not None
        package = build_delivery_packaging_label_package(first_db, first_delivery)
        selection_key = package["plans"][0]["selection_key"]
        first = prepare_delivery_packaging_label_job(
            first_db,
            delivery=first_delivery,
            idempotency_key="p1106-service-idempotency",
            expected_plan_fingerprint=package["plan_fingerprint"],
            requested_print_counts={selection_key: 2},
            operator_id=fixture["admin_id"],
        )
        first_db.commit()
        assert first.replayed is False
        first_job_id = first.job.id
        first_payload_hash = first.job.payload_hash

    # A second independent Session must observe the committed CAS winner and
    # replay the exact frozen payload instead of creating another source row.
    with fixture["session_factory"]() as second_db:
        second_delivery = second_db.get(Delivery, fixture["delivery_id"])
        assert second_delivery is not None
        replay = prepare_delivery_packaging_label_job(
            second_db,
            delivery=second_delivery,
            idempotency_key="p1106-service-idempotency",
            expected_plan_fingerprint=package["plan_fingerprint"],
            requested_print_counts={selection_key: 2},
            operator_id=fixture["admin_id"],
        )
        assert replay.replayed is True
        assert replay.job.id == first_job_id
        assert replay.job.payload_hash == first_payload_hash

        with pytest.raises(ProductionLabelOperationError, match="另一操作人|不同"):
            prepare_delivery_packaging_label_job(
                second_db,
                delivery=second_delivery,
                idempotency_key="p1106-service-idempotency",
                expected_plan_fingerprint=package["plan_fingerprint"],
                requested_print_counts={selection_key: 2},
                operator_id=fixture["sales_id"],
            )

    with fixture["session_factory"]() as verify_db:
        assert verify_db.scalar(
            select(func.count())
            .select_from(ProductionPackagingLabelPrintJob)
            .where(
                ProductionPackagingLabelPrintJob.idempotency_key
                == "p1106-service-idempotency"
            )
        ) == 1


def test_delivery_job_migration_roundtrips_xor_sources_and_refuses_fact_downgrade(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p1-106-delivery-label-migration.sqlite3"
    config = _migration_config(monkeypatch, database)
    command.upgrade(config, PARENT_REVISION)
    command.upgrade(config, TARGET_REVISION)

    with sqlite3.connect(database) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(production_packaging_label_print_jobs)"
            )
        }
        assert "delivery_id" in columns
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='production_packaging_label_print_jobs'"
        ).fetchone()[0]
        normalized_sql = " ".join(table_sql.lower().split())
        assert "ck_production_packaging_label_print_jobs_source" in normalized_sql
        assert normalized_sql.count("delivery_id is not null") == 1
        assert normalized_sql.count("delivery_id is null") == 2
        for source_column in (
            "supplier_order_id",
            "material_requisition_id",
            "delivery_id",
        ):
            assert source_column in normalized_sql
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(production_packaging_label_print_jobs)"
        ).fetchall()
        assert any(
            row[2] == "sales_deliveries"
            and row[3] == "delivery_id"
            and str(row[6]).upper() == "RESTRICT"
            for row in foreign_keys
        )
        indexes = {
            row[1]
            for row in connection.execute(
                "PRAGMA index_list(production_packaging_label_print_jobs)"
            )
        }
        assert "ix_production_packaging_label_print_jobs_delivery_created" in indexes

    command.downgrade(config, PARENT_REVISION)
    with sqlite3.connect(database) as connection:
        assert "delivery_id" not in {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(production_packaging_label_print_jobs)"
            )
        }
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO customers (id, customer_code, name) "
            "VALUES (1, 'P1106-MIG', 'migration customer')"
        )
        connection.execute(
            "INSERT INTO sales_deliveries "
            "(id, delivery_number, customer_id, delivery_date, source_mode, status, total_quantity) "
            "VALUES (1, 'DN-P1106-MIG', 1, '2026-08-25', 'order', 'pending', 1)"
        )
        connection.commit()

        with pytest.raises(
            sqlite3.IntegrityError,
            match="ck_production_packaging_label_print_jobs_source",
        ):
            connection.execute(
                "INSERT INTO production_packaging_label_print_jobs "
                "(idempotency_key, request_hash, template_version, plan_fingerprint, "
                "payload_json, payload_hash, status) "
                "VALUES ('p1106-no-source', ?, 'current_40x30_v2', ?, '{}', ?, 'prepared')",
                ("a" * 64, "b" * 64, "c" * 64),
            )
        connection.rollback()

        connection.execute(
            "INSERT INTO production_packaging_label_print_jobs "
            "(delivery_id, idempotency_key, request_hash, template_version, "
            "plan_fingerprint, payload_json, payload_hash, status) "
            "VALUES (1, 'p1106-delivery-source', ?, 'current_40x30_v2', ?, '{}', ?, 'prepared')",
            ("d" * 64, "e" * 64, "f" * 64),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM sales_deliveries WHERE id = 1")
        connection.rollback()
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()

    with pytest.raises(RuntimeError, match="delivery packaging-label jobs exist"):
        command.downgrade(config, PARENT_REVISION)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET_REVISION
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
