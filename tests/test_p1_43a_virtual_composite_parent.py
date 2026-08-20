from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
import inspect
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect as sa_inspect, select
from sqlalchemy.orm import Session

from app.api import orders, products, requisition
from app.api.deliveries import _actual_goods_lines
from app.api.products import ProductPayload, ProductResponse
from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.mold_tool import MoldTool
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.product_bom import (
    ProductBomComponent,
    SalesOrderItemBomComponent,
)
from app.models.production import ProductionTask
from app.models.user import User
from app.services.composite_bom import (
    create_order_item_bom_snapshots,
    replace_product_bom,
)
from app.services.composite_bom_workflow import effective_component_demands
from app.services.master_data_versioning import _PRODUCT_FIELDS
from app.services.production_workflow import create_or_refresh_production_task
from app.services.requisition_quantities import normalize_cutting_mode


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "ed12v8x9z01"
TARGET_REVISION = "ee13v8x9z02"


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _product_payload(**overrides) -> ProductPayload:
    payload = {
        "customer_id": 1,
        "product_code": "KIT-001",
        "customer_material_code": "KIT-001",
        "product_name": "虚拟组合套装",
        "box_category": "die_cut",
        "box_style": "模切内盒",
        "material_id": 99,
        "mold_tool_id": 88,
        "length_mm": 780,
        "width_mm": 500,
        "height_mm": 300,
        "report_length_mm": 900,
        "report_width_mm": 800,
        "production_process": "印刷,模切",
        "production_label_enabled": True,
        "production_label_units_per_label": 5,
        "sale_unit_price": "12.50",
        "is_virtual_composite_parent": True,
    }
    payload.update(overrides)
    return ProductPayload(**payload)


def test_virtual_parent_api_marker_clears_parent_physical_facts() -> None:
    payload = _product_payload()
    assert payload.is_virtual_composite_parent is True
    assert payload.combination_mode == "parent_priced_set"
    assert payload.sale_unit_price == Decimal("12.50")
    for field in (
        "material_id",
        "mold_tool_id",
        "length_mm",
        "width_mm",
        "height_mm",
        "box_style",
        "report_length_mm",
        "report_width_mm",
        "production_process",
        "production_label_units_per_label",
    ):
        assert getattr(payload, field) is None
    assert payload.production_label_enabled is False
    assert "is_virtual_composite_parent" in ProductResponse.model_fields
    assert "is_virtual_composite_parent" in _PRODUCT_FIELDS

    with pytest.raises(ValueError, match="父件按套计价"):
        _product_payload(combination_mode="component_priced")
    with pytest.raises(ValueError, match="不能设置为外购包材或混合供货"):
        _product_payload(supply_mode="mixed_bom")


def _seed_virtual_kit(session: Session) -> tuple[User, Product, Product, Product, OrderItem]:
    user = User(
        username="p1-43a-admin",
        password_hash="test-only",
        role="admin",
        real_name="P1-43A 管理员",
        is_active=True,
        must_change_password=False,
    )
    customer = Customer(
        name="P1-43A 测试客户",
        customer_code="P143A",
        status="active",
        is_active=True,
    )
    session.add_all([user, customer])
    session.flush()
    mold = MoldTool(
        mold_code="P143A-M01",
        mold_name="组件B模具",
        rack_location="3F-M-P01",
        is_active=True,
    )
    session.add(mold)
    session.flush()
    parent = Product(
        customer_id=customer.id,
        product_code="KIT-PARENT",
        customer_material_code="KIT-PARENT",
        product_name="展示套装父件",
        box_category="normal",
        unit="套",
        sale_unit_price=Decimal("20"),
        is_virtual_composite_parent=True,
        combination_mode="parent_priced_set",
        is_active=True,
        version=1,
    )
    component_a = Product(
        customer_id=customer.id,
        product_code="KIT-A",
        customer_material_code="KIT-A",
        product_name="真实组件A",
        box_category="normal",
        unit="只",
        report_length_mm=600,
        report_width_mm=400,
        crease_type="其他",
        default_cutting_mode=normalize_cutting_mode(1),
        is_active=True,
        version=1,
    )
    component_b = Product(
        customer_id=customer.id,
        product_code="KIT-B",
        customer_material_code="KIT-B",
        product_name="真实组件B",
        box_category="die_cut",
        unit="只",
        mold_tool_id=mold.id,
        die_cut_path="drawings/kit-b.pdf",
        report_length_mm=500,
        report_width_mm=300,
        crease_type="其他",
        default_cutting_mode=normalize_cutting_mode(4),
        is_active=True,
        version=1,
    )
    session.add_all([parent, component_a, component_b])
    session.flush()
    bom = replace_product_bom(
        session,
        parent_product_id=parent.id,
        expected_version=1,
        user=user,
        change_reason="P1-43A 测试",
        components=[
            {
                "component_product_id": component_a.id,
                "quantity_per_set": 6,
                "is_die_cut": True,
                "mold_tool_id": mold.id,
                "mold_max_yield_per_sheet": 99,
                "spare_sheet_quantity": 99,
                "show_on_delivery": True,
            },
            {
                "component_product_id": component_b.id,
                "quantity_per_set": 4,
                "is_die_cut": False,
                "mold_tool_id": None,
                "mold_max_yield_per_sheet": None,
                "spare_sheet_quantity": 77,
                "show_on_delivery": False,
            },
        ],
    )
    assert bom["is_virtual_composite_parent"] is True
    relation_a, relation_b = session.scalars(
        select(ProductBomComponent)
        .where(ProductBomComponent.parent_product_id == parent.id)
        .order_by(ProductBomComponent.display_order)
    ).all()
    assert relation_a.is_die_cut is False
    assert relation_a.mold_tool_id is None
    assert relation_a.spare_sheet_quantity == 0
    assert relation_a.show_on_delivery is True
    assert relation_b.is_die_cut is True
    assert relation_b.mold_tool_id == mold.id
    assert relation_b.mold_max_yield_per_sheet == 4
    assert relation_b.spare_sheet_quantity == 0
    # 子件交付是订单级权威模式，所有必需子件都必须进入送货展示；
    # 旧的逐组件 show_on_delivery 不能再制造父/子混合口径。
    assert relation_b.show_on_delivery is True

    order = Order(
        order_number="P1-43A-ORDER",
        customer_id=customer.id,
        order_date=date(2026, 8, 11),
        status="pending_production",
        payment_status="unpaid",
        total_amount=Decimal("2000"),
        created_by=user.id,
    )
    session.add(order)
    session.flush()
    item = OrderItem(
        order_id=order.id,
        product_id=parent.id,
        quantity=100,
        unit_price=Decimal("20"),
        subtotal=Decimal("2000"),
        material_status="pending",
        snapshot_product_name=parent.product_name,
        snapshot_product_code=parent.product_code,
        supply_mode_snapshot="corrugated_production",
        is_virtual_composite_parent_snapshot=True,
        requisition_status="未报料",
        special_process=normalize_cutting_mode(1),
        combination_mode_snapshot="parent_priced_set",
        combination_role="set_parent",
    )
    session.add(item)
    session.flush()
    create_order_item_bom_snapshots(
        session,
        order_item=item,
        parent_product=parent,
    )
    return user, parent, component_a, component_b, item


def test_virtual_parent_freezes_component_demands_and_never_creates_parent_task(
    isolated_engine,
) -> None:
    Base.metadata.create_all(isolated_engine)
    with Session(isolated_engine) as session:
        user, parent, _a, _b, item = _seed_virtual_kit(session)

        legacy_payload = ProductPayload(
            customer_id=parent.customer_id,
            product_code=parent.product_code,
            customer_material_code=parent.customer_material_code,
            product_name=parent.product_name,
            box_category=parent.box_category,
            sale_unit_price=Decimal("21"),
        )
        updates = products._validated_product_versioned_updates(
            session,
            product=parent,
            payload=legacy_payload,
            user=user,
        )
        assert "is_virtual_composite_parent" not in updates
        assert updates["material_id"] is None
        assert updates["sale_unit_price"] == Decimal("21")

        explicit_disable = ProductPayload(
            customer_id=parent.customer_id,
            product_code=parent.product_code,
            customer_material_code=parent.customer_material_code,
            product_name=parent.product_name,
            box_category=parent.box_category,
            is_virtual_composite_parent=False,
        )
        with pytest.raises(Exception, match="BOM"):
            products._validated_product_versioned_updates(
                session,
                product=parent,
                payload=explicit_disable,
                user=user,
            )

        demands = effective_component_demands(session, item.id)
        assert [(row.component_code, row.required_piece_quantity) for row in demands] == [
            ("KIT-A", 600),
            ("KIT-B", 400),
        ]
        assert [row.show_on_delivery for row in demands] == [True, True]
        snapshots = session.scalars(
            select(SalesOrderItemBomComponent)
            .where(SalesOrderItemBomComponent.sales_order_item_id == item.id)
            .order_by(SalesOrderItemBomComponent.display_order)
        ).all()
        assert all(row.snapshot_schema_version == 4 for row in snapshots)
        assert [row.show_on_delivery for row in snapshots] == [True, True]

        create_or_refresh_production_task(session, item.id)
        tasks = session.scalars(
            select(ProductionTask).where(ProductionTask.order_item_id == item.id)
        ).all()
        assert len(tasks) == 2
        assert all(task.sales_order_item_bom_component_id is not None for task in tasks)

        pending = requisition._pending_requisitions_full_payload(session, user)
        assert pending["total"] == 1
        row = pending["items"][0]
        assert row["is_composite_bom"] is True
        assert row["suppress_parent_requisition"] is True
        assert row["parent_requirement"]["can_requisition"] is False
        assert {part["product_code"] for part in row["component_requirements"]} == {
            "KIT-A",
            "KIT-B",
        }


def test_delivery_visibility_filters_only_customer_goods_lines() -> None:
    component_lines = [
        {
            "component_snapshot_id": 1,
            "product_code": "KIT-A",
            "product_name": "组件A",
            "specification": "600×400",
            "unit": "PCS",
            "planned_delivery_quantity": 600,
            "show_on_delivery": True,
        },
        {
            "component_snapshot_id": 2,
            "product_code": "KIT-B",
            "product_name": "组件B",
            "specification": "500×300",
            "unit": "PCS",
            "planned_delivery_quantity": 400,
            "show_on_delivery": False,
        },
    ]
    lines = _actual_goods_lines(
        order_item_id=10,
        product_code="KIT-PARENT",
        product_name="展示套装",
        specification=None,
        parent_quantity=100,
        component_lines=component_lines,
        fulfillment_mode="component_delivery",
    )
    assert [line["product_code"] for line in lines] == ["KIT-A", "KIT-B"]
    assert all(line["line_type"] == "component" for line in lines)
    assert lines[1]["pricing_included"] is False
    assert lines[1]["independent_statement"] is False


def test_order_and_requisition_sources_freeze_and_respect_virtual_parent_marker() -> None:
    create_source = inspect.getsource(orders._create_order_impl)
    requisition_source = inspect.getsource(requisition._pending_requisitions_full_payload)
    suppression_source = inspect.getsource(
        requisition._composite_parent_requisition_is_suppressed
    )
    assert "is_virtual_composite_parent_snapshot=bool" in create_source
    assert "_composite_parent_requisition_is_suppressed" in requisition_source
    assert "is_virtual_composite_parent_snapshot" in suppression_source


def test_p1_43a_migration_upgrade_downgrade_upgrade_and_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "p1-43a-roundtrip.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, PARENT_REVISION)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO customers (id, customer_code, name) VALUES (1, 'P143A', 'legacy customer')"
        )
        connection.executemany(
            """
            INSERT INTO products (
                id, customer_id, product_code, customer_material_code,
                product_name, box_category, unit
            ) VALUES (?, 1, ?, ?, ?, 'normal', 'PCS')
            """,
            [
                (1, "LEGACY-PARENT", "LEGACY-PARENT", "legacy parent"),
                (2, "LEGACY-COMP", "LEGACY-COMP", "legacy component"),
            ],
        )
        connection.execute(
            """
            INSERT INTO product_bom_components (
                id, parent_product_id, component_product_id,
                quantity_per_set, display_order, internal_component_code,
                is_die_cut, spare_sheet_quantity, display_mode, is_required
            ) VALUES (1, 1, 2, 1, 1, 'LEGACY-PARENT-S01', 0, 0, 'internal_only', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO sales_orders (
                id, order_number, customer_id, order_date, status,
                payment_status, total_amount
            ) VALUES (1, 'P143A-LEGACY', 1, '2026-08-11', 'pending_production', 'unpaid', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO sales_order_items (
                id, order_id, product_id, quantity, unit_price, subtotal,
                material_status, snapshot_product_name
            ) VALUES (1, 1, 1, 1, 1, 1, 'pending', 'legacy parent')
            """
        )
        connection.execute(
            """
            INSERT INTO sales_order_item_bom_components (
                id, sales_order_item_id, product_bom_component_id,
                component_product_id, order_set_quantity, quantity_per_set,
                required_piece_quantity, display_order, internal_component_code,
                is_die_cut, spare_sheet_quantity, display_mode, is_required,
                snapshot_component_product_code, snapshot_component_product_name,
                snapshot_component_box_category, snapshot_schema_version,
                snapshot_component_default_cutting_mode
            ) VALUES (
                1, 1, 1, 2, 1, 1, 1, 1, 'LEGACY-PARENT-S01',
                0, 0, 'internal_only', 1, 'LEGACY-COMP', 'legacy component',
                'normal', 3, '一开一'
            )
            """
        )
        connection.commit()
    command.upgrade(config, TARGET_REVISION)
    inspector = sa_inspect(create_sqlite_engine(path))
    assert "is_virtual_composite_parent" in {
        column["name"] for column in inspector.get_columns("products")
    }
    assert "is_virtual_composite_parent_snapshot" in {
        column["name"] for column in inspector.get_columns("sales_order_items")
    }
    assert "show_on_delivery" in {
        column["name"] for column in inspector.get_columns("product_bom_components")
    }
    assert "show_on_delivery" in {
        column["name"]
        for column in inspector.get_columns("sales_order_item_bom_components")
    }
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT is_virtual_composite_parent FROM products WHERE id = 1"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT is_virtual_composite_parent_snapshot FROM sales_order_items WHERE id = 1"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT show_on_delivery FROM product_bom_components WHERE id = 1"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT show_on_delivery FROM sales_order_item_bom_components WHERE id = 1"
        ).fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE sales_order_item_bom_components SET show_on_delivery = 0 WHERE id = 1"
            )
        connection.rollback()
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    command.downgrade(config, PARENT_REVISION)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == TARGET_REVISION
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_p1_43a_migration_downgrade_fails_closed_after_virtual_fact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "p1-43a-fail-closed.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)
    engine = create_sqlite_engine(path)
    with Session(engine) as session:
        customer = Customer(name="P1-43A migration customer", customer_code="P143AM")
        session.add(customer)
        session.flush()
        session.add(
            Product(
                customer_id=customer.id,
                product_code="P143A-MIG",
                customer_material_code="P143A-MIG",
                product_name="migration virtual parent",
                box_category="normal",
                unit="套",
                is_virtual_composite_parent=True,
            )
        )
        session.commit()
    with pytest.raises(RuntimeError, match="拒绝破坏性降级"):
        command.downgrade(config, PARENT_REVISION)
