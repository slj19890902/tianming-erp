from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from pydantic import ValidationError
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.products import ProductPayload, _validated_product_versioned_updates
from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.product_bom import (
    SalesOrderItemBomComponent,
    SalesOrderItemBomDemandAdjustment,
)
from app.models.production import ProductionTask
from app.models.user import User
from app.services.composite_bom_workflow import ensure_component_production_tasks
from app.services.production_label_strategy import (
    CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION,
    build_new_task_production_label_snapshot,
    production_label_quantities,
)
from app.services.production_workflow import refresh_production_task


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "dx06v8x9z95"
TARGET_REVISION = "dy07v8x9z96"


def _alembic_config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


@pytest.fixture()
def label_session(tmp_path: Path):
    import app.models  # noqa: F401

    engine = create_sqlite_engine(tmp_path / "p1-34b3.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        yield db
    engine.dispose()


def _seed_order_item(
    db: Session,
    *,
    customer_number: int,
    code: str,
    quantity: int,
    box_style: str,
    units_per_label: int,
    cutting_mode: str = "一开一",
) -> tuple[Product, OrderItem]:
    customer = Customer(
        customer_number=customer_number,
        customer_code=code,
        name=f"{code}标签客户",
    )
    db.add(customer)
    db.flush()
    product = Product(
        customer_id=customer.id,
        product_code=f"{code}-P",
        customer_material_code=f"{code}-M",
        product_name=f"{code}测试纸箱",
        box_category="normal",
        box_style=box_style,
        default_cutting_mode=cutting_mode,
        production_label_enabled=True,
        production_label_units_per_label=units_per_label,
    )
    db.add(product)
    db.flush()
    order = Order(
        order_number=f"{code}-ORDER",
        customer_id=customer.id,
        order_date=date(2026, 8, 10),
        status="pending_production",
        payment_status="unpaid",
        total_amount=Decimal(str(quantity)),
    )
    db.add(order)
    db.flush()
    item = OrderItem(
        order_id=order.id,
        product_id=product.id,
        item_order_number=f"{code}-ITEM",
        item_sequence=1,
        quantity=quantity,
        unit_price=Decimal("1"),
        subtotal=Decimal(str(quantity)),
        material_status="pending",
        snapshot_product_name=product.product_name,
        snapshot_product_code=product.product_code,
        special_process=cutting_mode,
    )
    db.add(item)
    db.flush()
    return product, item


def test_label_plan_uses_finished_units_and_handles_remainder_and_exact_multiple() -> None:
    payload = ProductPayload(
        customer_id=1,
        product_code="A1-LABEL",
        customer_material_code="A1-LABEL",
        product_name="A1标签默认值",
        box_category="normal",
        box_style="A1",
        production_label_enabled=True,
    )
    assert payload.production_label_units_per_label == 5
    product = Product(
        box_style=payload.box_style,
        production_label_enabled=True,
        production_label_units_per_label=payload.production_label_units_per_label,
    )
    remainder = ProductionTask(
        **build_new_task_production_label_snapshot(product, total_quantity=23)
    )
    exact = ProductionTask(
        **build_new_task_production_label_snapshot(product, total_quantity=20)
    )

    assert remainder.production_label_count_snapshot == 5
    assert production_label_quantities(remainder) == [5, 5, 5, 5, 3]
    assert exact.production_label_count_snapshot == 4
    assert production_label_quantities(exact) == [5, 5, 5, 5]


def test_one_to_two_cutting_mode_does_not_divide_packaging_label_units() -> None:
    payload = ProductPayload(
        customer_id=1,
        product_code="CUT-2",
        customer_material_code="CUT-2",
        product_name="一开二模切内盒",
        box_category="normal",
        box_style="模切内盒",
        default_cutting_mode="一开二",
        production_label_enabled=True,
    )
    assert payload.production_label_units_per_label == 50
    product = Product(
        box_style=payload.box_style,
        default_cutting_mode=payload.default_cutting_mode,
        production_label_enabled=True,
        production_label_units_per_label=payload.production_label_units_per_label,
    )
    task = ProductionTask(
        **build_new_task_production_label_snapshot(product, total_quantity=101)
    )
    assert production_label_quantities(task) == [50, 50, 1]


def test_unsupported_box_type_requires_explicit_units() -> None:
    with pytest.raises(ValidationError, match="没有默认每张标签数量"):
        ProductPayload(
            customer_id=1,
            product_code="OTHER",
            customer_material_code="OTHER",
            product_name="其他箱型",
            box_category="normal",
            box_style="其他",
            production_label_enabled=True,
        )


def test_regular_task_freezes_only_uncovered_finished_quantity(
    label_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product, item = _seed_order_item(
        label_session,
        customer_number=93401,
        code="REG23",
        quantity=23,
        box_style="A1",
        units_per_label=5,
    )
    monkeypatch.setattr(
        "app.services.production_workflow.active_finished_reserved_qty",
        lambda _db, _item_id: 3,
    )
    task = refresh_production_task(label_session, item.id, create_if_missing=True)
    assert task is not None
    assert task.production_label_total_quantity_snapshot == 20
    assert task.production_label_count_snapshot == 4

    product.production_label_units_per_label = 10
    item.quantity = 30
    refresh_production_task(label_session, item.id, create_if_missing=True)
    assert task.production_label_units_per_label_snapshot == 5
    assert task.production_label_total_quantity_snapshot == 20
    assert task.production_label_count_snapshot == 4
    assert (
        task.production_label_template_version_snapshot
        == CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION
    )
    assert task.production_label_product_version_snapshot == product.version


def test_fully_covered_regular_task_freezes_disabled_zero(
    label_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _product, item = _seed_order_item(
        label_session,
        customer_number=93402,
        code="REGFULL",
        quantity=20,
        box_style="A1",
        units_per_label=5,
    )
    monkeypatch.setattr(
        "app.services.production_workflow.active_finished_reserved_qty",
        lambda _db, _item_id: 20,
    )
    task = refresh_production_task(label_session, item.id, create_if_missing=True)
    assert task is not None
    assert task.production_label_enabled_snapshot is False
    assert task.production_label_units_per_label_snapshot is None
    assert task.production_label_total_quantity_snapshot == 0
    assert task.production_label_count_snapshot == 0
    assert (
        task.production_label_template_version_snapshot
        == CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION
    )
    assert task.production_label_product_version_snapshot == _product.version


def test_composite_task_freezes_adjusted_2700_minus_component_coverage(
    label_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _parent, item = _seed_order_item(
        label_session,
        customer_number=93403,
        code="BOM2700",
        quantity=3000,
        box_style="A1",
        units_per_label=5,
    )
    component = Product(
        customer_id=_parent.customer_id,
        product_code="BOM2700-C",
        customer_material_code="BOM2700-C",
        product_name="订单专用内衬",
        box_category="normal",
        box_style="模切内盒",
        default_cutting_mode="一开二",
        production_label_enabled=True,
        production_label_units_per_label=50,
    )
    label_session.add(component)
    label_session.flush()
    snapshot = SalesOrderItemBomComponent(
        sales_order_item_id=item.id,
        component_product_id=component.id,
        snapshot_schema_version=3,
        order_set_quantity=3000,
        quantity_per_set=Decimal("1"),
        required_piece_quantity=Decimal("3000"),
        display_order=1,
        internal_component_code="BOM2700-C",
        is_die_cut=False,
        spare_sheet_quantity=0,
        display_mode="internal_only",
        is_required=True,
        snapshot_component_product_code="BOM2700-C",
        snapshot_component_product_name="订单专用内衬",
        snapshot_component_box_category="normal",
        snapshot_component_box_style="模切内盒",
        snapshot_component_default_cutting_mode="一开二",
    )
    label_session.add(snapshot)
    label_session.flush()
    label_session.add(
        SalesOrderItemBomDemandAdjustment(
            sales_order_item_bom_component_id=snapshot.id,
            event_type="component_demand_adjusted",
            delta_order_set_quantity=0,
            delta_required_piece_quantity=Decimal("-300"),
            reason="客户本次少要300个",
            idempotency_key="p1-34b3-bom-2700",
        )
    )
    label_session.flush()
    monkeypatch.setattr(
        "app.services.composite_bom_workflow.component_available_quantity",
        lambda _db, _snapshot_id: 200,
    )

    tasks = ensure_component_production_tasks(label_session, item.id)
    assert len(tasks) == 1
    task = tasks[0]
    assert task.production_label_units_per_label_snapshot == 50
    assert task.production_label_total_quantity_snapshot == 2500
    assert task.production_label_count_snapshot == 50
    assert (
        task.production_label_template_version_snapshot
        == CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION
    )
    assert task.production_label_product_version_snapshot == component.version


def test_legacy_product_update_omission_preserves_label_strategy(
    label_session: Session,
) -> None:
    product, _item = _seed_order_item(
        label_session,
        customer_number=93404,
        code="LEGACY",
        quantity=20,
        box_style="A1",
        units_per_label=5,
    )
    payload = ProductPayload(
        customer_id=product.customer_id,
        product_code=product.product_code,
        customer_material_code=product.customer_material_code,
        product_name=product.product_name,
        box_category=product.box_category,
        box_style=product.box_style,
    )
    updates = _validated_product_versioned_updates(
        label_session,
        product=product,
        payload=payload,
        user=User(role="admin", username="p1-34b3-admin"),
    )
    assert "production_label_enabled" not in updates
    assert "production_label_units_per_label" not in updates


def test_migration_is_linear_defaults_old_facts_and_round_trips(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "p1-34b3-migration.sqlite3"
    config = _alembic_config(monkeypatch, path)
    command.upgrade(config, PARENT_REVISION)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO customers (
                id, name, payment_term_days, statement_cycle_start_day,
                credit_limit, delivery_method, default_tax_rate, status,
                is_active, version
            ) VALUES (1, '迁移测试客户', 0, 20, 0, '配送', 0.13, 'active', 1, 1)
            """
        )
        connection.execute(
            """
            INSERT INTO products (
                customer_id, product_code, customer_material_code, product_name,
                box_category, unit, default_cutting_mode, is_composite,
                combination_mode, is_internal_component, is_active,
                manual_modified, version
            ) VALUES (
                1, 'OLD-P', 'OLD-M', '旧常用箱', 'normal', '只', '一开一',
                0, 'parent_priced_set', 0, 1, 0, 1
            )
            """
        )
        connection.execute(
            """
            INSERT INTO sales_orders (
                id, order_number, customer_id, order_date, status,
                payment_status, requisition_strategy, total_amount
            ) VALUES (
                1, 'OLD-ORDER', 1, '2026-08-10', 'pending_production',
                'unpaid', 'normal', 20
            )
            """
        )
        connection.execute(
            """
            INSERT INTO sales_order_items (
                id, order_id, product_id, quantity, delivered_quantity,
                is_force_closed, unit_price, subtotal, material_status,
                snapshot_product_name, inventory_deducted_qty,
                requisition_status, special_process, combination_role
            ) VALUES (
                1, 1, 1, 20, 0, 0, 1, 20, 'pending', '旧常用箱',
                0, '未报料', '无', 'standalone'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO production_tasks (
                order_item_id, status, planned_quantity,
                finished_coverage_snapshot, ordered_quantity_snapshot,
                material_received_quantity, material_input_quantity,
                output_factor, version
            ) VALUES (1, 'waiting_material', 0, 0, 20, 0, 0, 1, 1)
            """
        )
        connection.commit()

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT production_label_enabled, "
            "production_label_units_per_label FROM products"
        ).fetchone() == (0, None)
        assert connection.execute(
            "SELECT production_label_enabled_snapshot, "
            "production_label_units_per_label_snapshot, "
            "production_label_total_quantity_snapshot, "
            "production_label_count_snapshot FROM production_tasks"
        ).fetchone() == (0, None, 0, 0)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    command.downgrade(config, PARENT_REVISION)
    inspector = inspect(create_sqlite_engine(path))
    assert "production_label_enabled" not in {
        column["name"] for column in inspector.get_columns("products")
    }
    command.upgrade(config, TARGET_REVISION)


def test_migration_downgrade_fails_closed_after_strategy_fact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "p1-34b3-fail-closed.sqlite3"
    config = _alembic_config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """
            INSERT INTO products (
                customer_id, product_code, customer_material_code, product_name,
                box_category, unit, default_cutting_mode, is_composite,
                combination_mode, is_internal_component, is_active,
                manual_modified, version, production_label_enabled,
                production_label_units_per_label
            ) VALUES (
                1, 'NEW-P', 'NEW-M', '新标签常用箱', 'normal', '只', '一开一',
                0, 'parent_priced_set', 0, 1, 0, 1, 1, 5
            )
            """
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="禁止破坏性降级"):
        command.downgrade(config, PARENT_REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET_REVISION
