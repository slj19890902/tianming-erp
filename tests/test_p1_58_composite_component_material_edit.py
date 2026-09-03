from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
from fastapi import HTTPException, Response
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import incoming, requisition
from app.api.requisition import (
    PendingMaterialUpdate,
    PendingVirtualCompositeParentUpdate,
    RequisitionBatchCreate,
    RequisitionLinePayload,
)
from app.models import Base
from app.models.customer import Customer
from app.models.material import Material
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.product_bom import SalesOrderItemBomComponent
from app.models.product_bom import RequisitionItemBomSource
from app.models.production import ProductionTask
from app.models.requisition import Requisition, RequisitionItem
from app.models.supplier import Supplier
from app.models.user import User
from app.core.database import create_sqlite_engine
from app.services.composite_bom import (
    create_order_item_bom_snapshots,
    replace_product_bom,
)
from app.services.requisition_quantities import normalize_cutting_mode
from app.services.supplier_master import normalize_supplier_identity


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "ss27v8x9z16"
TARGET_REVISION = "tt28v8x9z17"


def _migration_config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _seed_composite_pending_item(session: Session):
    user = User(
        username="p1-58-admin",
        password_hash="test-only",
        role="admin",
        real_name="P1-58 管理员",
        is_active=True,
        must_change_password=False,
    )
    customer = Customer(
        name="P1-58 测试客户",
        customer_code="P158",
        status="active",
        is_active=True,
    )
    old_supplier = Supplier(
        standard_name="旧供应商",
        normalized_name=normalize_supplier_identity("旧供应商"),
        display_name="旧供应商",
        is_active=True,
        version=1,
    )
    new_supplier = Supplier(
        standard_name="新供应商",
        normalized_name=normalize_supplier_identity("新供应商"),
        display_name="新供应商",
        is_active=True,
        version=1,
    )
    session.add_all([user, customer, old_supplier, new_supplier])
    session.flush()
    old_material = Material(
        code="OLD-AB",
        layer_count=5,
        flute_type="AB",
        supplier_name="旧供应商",
        is_active=True,
    )
    new_material = Material(
        code="NEW-AB",
        layer_count=5,
        flute_type="AB",
        supplier_name="新供应商",
        is_active=True,
    )
    parent = Product(
        customer_id=customer.id,
        product_code="KIT-PRICE",
        customer_material_code="KIT-PRICE",
        product_name="只计价套装父件",
        box_category="normal",
        unit="套",
        sale_unit_price=Decimal("12.50"),
        combination_mode="parent_priced_set",
        is_active=True,
        version=1,
    )
    component = Product(
        customer_id=customer.id,
        product_code="KIT-C1",
        customer_material_code="KIT-C1",
        product_name="实体组件",
        material_id=None,
        box_category="normal",
        box_style="衬板",
        unit="片",
        report_length_mm=600,
        report_width_mm=400,
        crease_type="毛片",
        default_cutting_mode=normalize_cutting_mode(1),
        layer_count=5,
        flute_type="AB",
        is_active=True,
        version=1,
    )
    session.add_all([old_material, new_material, parent, component])
    session.flush()
    component.material_id = old_material.id
    replace_product_bom(
        session,
        parent_product_id=parent.id,
        expected_version=1,
        user=user,
        change_reason="P1-58 测试",
        components=[
            {
                "component_product_id": component.id,
                "quantity_per_set": 2,
                "is_die_cut": False,
                "mold_tool_id": None,
                "mold_max_yield_per_sheet": None,
                "spare_sheet_quantity": 0,
                "show_on_delivery": True,
            }
        ],
    )
    order = Order(
        order_number="P1-58-ORDER",
        customer_id=customer.id,
        order_date=date(2026, 8, 18),
        status="pending_production",
        payment_status="unpaid",
        total_amount=Decimal("1250"),
        created_by=user.id,
    )
    session.add(order)
    session.flush()
    item = OrderItem(
        order_id=order.id,
        product_id=parent.id,
        quantity=100,
        unit_price=Decimal("12.50"),
        subtotal=Decimal("1250"),
        material_status="pending",
        snapshot_product_name=parent.product_name,
        snapshot_product_code=parent.product_code,
        supply_mode_snapshot="corrugated_production",
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
    snapshot = session.scalar(
        select(SalesOrderItemBomComponent).where(
            SalesOrderItemBomComponent.sales_order_item_id == item.id
        )
    )
    assert snapshot is not None
    assert snapshot.snapshot_component_material_id == old_material.id
    # Mirror the real incident: common box was corrected first, while this
    # order's frozen component still held the old supplier/material.
    component.material_id = new_material.id
    component.version += 1
    session.flush()
    return user, parent, component, item, snapshot, old_material, new_material


def test_pending_component_material_updates_component_snapshot_not_parent(
    isolated_engine,
) -> None:
    Base.metadata.create_all(isolated_engine)
    with Session(isolated_engine) as session:
        user, parent, component, item, snapshot, old_material, material = (
            _seed_composite_pending_item(session)
        )
        response = requisition.update_pending_bom_component_material(
            item.id,
            snapshot.id,
            PendingMaterialUpdate(
                material_id=material.id,
                layer_count=5,
                flute_type="AB",
                sync_product=True,
                product_expected_version=component.version,
            ),
            session,
            user,
        )

        assert response["bom_snapshot_id"] == snapshot.id
        assert response["supplier_name"] == "新供应商"
        assert snapshot.snapshot_component_material_id == material.id
        assert snapshot.snapshot_component_material == "NEW-AB"
        assert snapshot.snapshot_component_supplier_name == "新供应商"
        assert snapshot.component_product_version == component.version
        assert item.material_id is None
        assert item.snapshot_material is None
        assert parent.material_id is None

        requirement = requisition._bom_snapshot_requirements(session, snapshot)
        assert requirement["product_id"] == component.id
        assert requirement["product_version"] == component.version
        assert requirement["material_id"] == material.id
        assert requirement["supplier_name"] == "新供应商"

        # The incident also put a physical supplier/material on a parent that
        # should only carry the set quantity and sale price.
        parent.material_id = old_material.id
        parent.layer_count = 5
        parent.flute_type = "AB"
        item.material_id = old_material.id
        item.snapshot_material = old_material.code
        item.snapshot_supplier_name = old_material.supplier_name
        item.layer_count = 5
        item.flute_type = "AB"
        session.flush()
        with pytest.raises(HTTPException) as preview:
            requisition.mark_pending_composite_parent_virtual(
                item.id,
                PendingVirtualCompositeParentUpdate(
                    product_expected_version=parent.version,
                    confirmed=True,
                ),
                session,
                user,
            )
        assert preview.value.status_code == 409
        confirmation_token = preview.value.detail["confirmation_token"]
        response = requisition.mark_pending_composite_parent_virtual(
            item.id,
            PendingVirtualCompositeParentUpdate(
                product_expected_version=parent.version,
                product_confirmation_token=confirmation_token,
                confirmed=True,
            ),
            session,
            user,
        )
        assert response["is_virtual_composite_parent"] is True
        assert parent.is_virtual_composite_parent is True
        assert parent.material_id is None
        assert item.is_virtual_composite_parent_snapshot is True
        assert item.material_id is None
        assert item.snapshot_material is None
        assert item.snapshot_supplier_name is None


def _component_only_batch_payload(
    session: Session,
    *,
    item: OrderItem,
    snapshot: SalesOrderItemBomComponent,
    supplier_name: str,
) -> RequisitionBatchCreate:
    requirements = requisition._bom_snapshot_requirements(session, snapshot)
    physical_group_key = str(requirements["physical_group_key"])
    source_fingerprint = str(requirements["physical_source_fingerprint"])
    assert len(physical_group_key) == 64
    assert len(source_fingerprint) == 64
    group_fingerprint = requisition._composite_physical_group_fingerprint(
        physical_group_key,
        [source_fingerprint],
    )
    purchase_sheet_quantity = int(requirements["requisition_qty"])
    return RequisitionBatchCreate(
        supplier_name=supplier_name,
        items=[
            RequisitionLinePayload(
                order_item_id=item.id,
                bom_snapshot_id=snapshot.id,
                component_type="whole",
                physical_group_key=physical_group_key,
                group_fingerprint=group_fingerprint,
                source_fingerprint=source_fingerprint,
                group_purchase_sheet_qty=purchase_sheet_quantity,
                group_order_purpose_sheet_qty=purchase_sheet_quantity,
                group_stock_purpose_sheet_qty=0,
                requisition_qty=purchase_sheet_quantity,
                purchase_total_sheet_qty=purchase_sheet_quantity,
                order_purpose_sheet_qty=purchase_sheet_quantity,
                stock_purpose_sheet_qty=0,
                purpose_plan_version=1,
                purpose_plan_fingerprint=group_fingerprint,
                cardboard_len=Decimal(str(requirements["report_length_mm"])),
                cardboard_width=Decimal(str(requirements["report_width_mm"])),
                special_process=str(requirements["cutting_mode"]),
            )
        ],
    )


def test_physical_composite_parent_still_requires_parent_board_line(
    isolated_engine,
) -> None:
    """The virtual-parent fix must not weaken ordinary composite products."""

    Base.metadata.create_all(isolated_engine)
    with Session(isolated_engine) as session:
        user, _parent, _component, item, snapshot, old_material, _new_material = (
            _seed_composite_pending_item(session)
        )
        session.commit()
        payload = _component_only_batch_payload(
            session,
            item=item,
            snapshot=snapshot,
            supplier_name=old_material.supplier_name,
        )

        with pytest.raises(HTTPException) as error:
            requisition.create_batch(payload, session, user)

        assert error.value.status_code == 400
        assert error.value.detail == "复合产品报料必须同时包含父件外包装盒"
        assert session.scalar(select(Requisition.id)) is None


def test_virtual_parent_component_only_batch_runs_through_incoming_and_is_idempotent(
    isolated_engine,
) -> None:
    """Exercise the operator path through formal save and pending incoming."""

    Base.metadata.create_all(isolated_engine)
    with Session(isolated_engine) as session:
        user, parent, _component, item, snapshot, _old_material, new_material = (
            _seed_composite_pending_item(session)
        )
        snapshot.snapshot_component_material_id = new_material.id
        snapshot.snapshot_component_material = new_material.code
        snapshot.snapshot_component_supplier_name = new_material.supplier_name
        snapshot.snapshot_component_layer_count = new_material.layer_count
        snapshot.snapshot_component_flute_type = new_material.flute_type
        item.is_virtual_composite_parent_snapshot = True
        item.material_id = None
        item.snapshot_material = None
        item.snapshot_supplier_name = None
        item.layer_count = None
        item.flute_type = None
        parent.is_virtual_composite_parent = True
        parent.material_id = None
        parent.layer_count = None
        parent.flute_type = None
        session.commit()

        pending = requisition._pending_requisitions_full_payload(session, user)
        row = next(row for row in pending["items"] if row["item_id"] == item.id)
        assert row["suppress_parent_requisition"] is True
        assert row["parent_requirement"]["can_requisition"] is False
        assert [source["snapshot_id"] for source in row["bom_requisition_sources"]] == [
            snapshot.id
        ]

        payload = _component_only_batch_payload(
            session,
            item=item,
            snapshot=snapshot,
            supplier_name=new_material.supplier_name,
        )
        before_task_count = int(
            session.scalar(
                select(func.count(ProductionTask.id)).where(
                    ProductionTask.order_item_id == item.id
                )
            )
            or 0
        )
        result = requisition.create_batch(payload, session, user)

        assert result["status"] == "已报料"
        created_items = session.scalars(
            select(RequisitionItem).where(RequisitionItem.order_item_id == item.id)
        ).all()
        assert len(created_items) == 1
        assert created_items[0].product_code_snapshot == "KIT-C1"
        assert created_items[0].product_code_snapshot != item.snapshot_product_code
        source = session.scalar(
            select(RequisitionItemBomSource).where(
                RequisitionItemBomSource.requisition_item_id == created_items[0].id
            )
        )
        assert source is not None
        assert source.sales_order_item_bom_component_id == snapshot.id
        assert source.component_type == "whole"
        assert item.requisition_status == "已报料"

        pending_incoming = incoming.pending_items(Response(), session, user)["items"]
        incoming_rows = [
            row for row in pending_incoming if row.get("order_item_id") == item.id
        ]
        assert len(incoming_rows) == 1
        assert incoming_rows[0]["product_code"] == "KIT-C1"

        with pytest.raises(HTTPException) as duplicate:
            requisition.create_batch(payload, session, user)
        assert duplicate.value.status_code == 409
        assert int(
            session.scalar(
                select(func.count(ProductionTask.id)).where(
                    ProductionTask.order_item_id == item.id
                )
            )
            or 0
        ) == before_task_count
        assert len(
            session.scalars(
                select(RequisitionItem).where(RequisitionItem.order_item_id == item.id)
            ).all()
        ) == 1


def test_tt28_migration_allows_pending_material_only_and_locks_after_fact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "p1-58-component-material.sqlite3"
    config = _migration_config(monkeypatch, path)
    engine = create_sqlite_engine(path)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        user, _parent, component, item, snapshot, old_material, new_material = (
            _seed_composite_pending_item(session)
        )
        session.commit()
        ids = {
            "user": user.id,
            "component": component.id,
            "item": item.id,
            "snapshot": snapshot.id,
            "old_material": old_material.id,
            "new_material": new_material.id,
        }
    engine.dispose()

    # Reproduce the exact parent-revision immutability contract on the current
    # model schema.  Older historic migration targets cannot be populated with
    # today's ORM because they intentionally lack later columns.
    with sqlite3.connect(path) as connection:
        columns = [
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(sales_order_item_bom_components)"
            )
            if row[1] != "product_bom_component_id"
        ]
        update_columns = ", ".join(f'"{column}"' for column in columns)
        connection.execute(
            "CREATE TRIGGER trg_sales_order_item_bom_components_immutable_update "
            f"BEFORE UPDATE OF {update_columns} "
            "ON sales_order_item_bom_components FOR EACH ROW BEGIN "
            "SELECT RAISE(ABORT, "
            "'sales_order_item_bom_components snapshots are immutable'); END"
        )
        connection.commit()
    command.stamp(config, PARENT_REVISION)

    command.upgrade(config, TARGET_REVISION)
    engine = create_sqlite_engine(path)
    with Session(engine) as session:
        user = session.get(User, ids["user"])
        component = session.get(Product, ids["component"])
        item = session.get(OrderItem, ids["item"])
        snapshot = session.get(SalesOrderItemBomComponent, ids["snapshot"])
        new_material = session.get(Material, ids["new_material"])
        assert user and component and item and snapshot and new_material
        response = requisition.update_pending_bom_component_material(
            item.id,
            snapshot.id,
            PendingMaterialUpdate(
                material_id=new_material.id,
                layer_count=5,
                flute_type="AB",
                sync_product=True,
                product_expected_version=component.version,
            ),
            session,
            user,
        )
        assert response["supplier_name"] == "新供应商"

        requisition_header = Requisition(
            requisition_number="P1-58-LOCK",
            requisition_date=date(2026, 8, 18),
            supplier_name="新供应商",
            status="reported",
            created_by=user.id,
        )
        session.add(requisition_header)
        session.flush()
        requisition_item = RequisitionItem(
            requisition_id=requisition_header.id,
            order_item_id=item.id,
            inventory_deducted_qty=0,
            requisition_qty=100,
            cardboard_len=Decimal("600"),
            cardboard_width=Decimal("400"),
            special_process="none",
            material_snapshot="NEW-AB",
            product_code_snapshot="KIT-C1",
            product_name_snapshot="实体组件",
            status="active",
        )
        session.add(requisition_item)
        session.flush()
        session.add(
            RequisitionItemBomSource(
                requisition_item_id=requisition_item.id,
                sales_order_item_bom_component_id=snapshot.id,
                component_type="whole",
                active_guard=1,
                order_set_quantity=100,
                quantity_per_set=Decimal("2"),
                required_piece_quantity=Decimal("200"),
                demand_basis="order_sets",
                spare_sheet_quantity=0,
                calculated_purchase_quantity=Decimal("100"),
            )
        )
        session.commit()

        old_material = session.get(Material, ids["old_material"])
        assert old_material is not None
        with pytest.raises(HTTPException) as error:
            requisition.update_pending_bom_component_material(
                item.id,
                snapshot.id,
                PendingMaterialUpdate(
                    material_id=old_material.id,
                    layer_count=5,
                    flute_type="AB",
                    sync_product=True,
                    product_expected_version=component.version,
                ),
                session,
                user,
            )
        assert error.value.status_code == 409
    engine.dispose()

    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="snapshots are immutable"):
            connection.execute(
                "UPDATE sales_order_item_bom_components "
                "SET snapshot_component_product_name='forbidden' WHERE id=?",
                (ids["snapshot"],),
            )
        connection.rollback()
        with pytest.raises(
            sqlite3.IntegrityError,
            match="material can only change before operational facts",
        ):
            connection.execute(
                "UPDATE sales_order_item_bom_components "
                "SET snapshot_component_material='OLD-AB' WHERE id=?",
                (ids["snapshot"],),
            )
        connection.rollback()
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    command.downgrade(config, PARENT_REVISION)
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="snapshots are immutable"):
            connection.execute(
                "UPDATE sales_order_item_bom_components "
                "SET snapshot_component_material='OLD-AB' WHERE id=?",
                (ids["snapshot"],),
            )
        connection.rollback()
    command.upgrade(config, TARGET_REVISION)


def test_frontend_exposes_component_material_edit_and_virtual_parent_marker() -> None:
    source = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    assert '@click="openRequisitionMaterialChange(row,source)"' in source
    assert (
        "`/api/requisition/pending/${form.item_id}/bom-components/"
        "${form.bom_snapshot_id}/material`"
    ) in source
    assert "更换组件供应商 / 材质" in source
    assert "父件只表示整套数量和价格，不报料、不生产" in source
    assert 'is_virtual_composite_parent: false' in source
    assert "is_virtual_composite_parent: f.is_virtual_composite_parent === true" in source
    assert '@click="markPendingCompositeParentVirtual(row)"' in source
    assert "async markPendingCompositeParentVirtual(row)" in source
