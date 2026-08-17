from __future__ import annotations

import json
import shutil
import subprocess
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.printing_plate import PrintingPlate
from app.models.product import Product
from app.models.product_bom import SalesOrderItemBomComponent
from app.models.production import ProductionTask
from app.models.supplier_requisition_order import (
    SupplierRequisitionOrder,
    SupplierRequisitionOrderItem,
)
from app.services.production_workflow import (
    ProductionWorkflowError,
    _task_printing_snapshot,
    create_or_refresh_production_task,
    list_production_tasks,
)


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
TASK_PRINT = (ROOT / "static" / "requisition-production-print.html").read_text(
    encoding="utf-8"
)


def _product(
    customer_id: int,
    code: str,
    *,
    print_content: str = "无印刷",
    printing_colors: str | None = None,
    **values,
) -> Product:
    return Product(
        customer_id=customer_id,
        product_code=code,
        customer_material_code=code,
        product_name=f"{code} 长文本生产快照纸箱",
        box_category="normal",
        print_content=print_content,
        printing_colors=printing_colors,
        **values,
    )


def _order_item(
    db: Session,
    *,
    customer_id: int,
    product: Product,
    suffix: str,
    material_status: str = "received",
) -> OrderItem:
    order = Order(
        order_number=f"P1-51C-{suffix}",
        customer_id=customer_id,
        order_date=date(2026, 8, 13),
        delivery_date=date(2026, 8, 20),
        status="pending_production",
        payment_status="unpaid",
        total_amount=Decimal("10"),
    )
    db.add(order)
    db.flush()
    item = OrderItem(
        order_id=order.id,
        product_id=product.id,
        item_order_number=f"P1-51C-{suffix}-001",
        item_sequence=1,
        quantity=10,
        delivered_quantity=0,
        unit_price=Decimal("1"),
        subtotal=Decimal("10"),
        material_status=material_status,
        snapshot_product_name=product.product_name,
        snapshot_product_code=product.product_code,
        snapshot_spec="520×350×300mm",
        snapshot_material="K=A",
        requisition_status="已报料",
        special_process="无",
    )
    db.add(item)
    db.flush()
    return item


def _component_snapshot(
    item: OrderItem,
    product: Product,
    *,
    display_order: int,
) -> SalesOrderItemBomComponent:
    return SalesOrderItemBomComponent(
        sales_order_item_id=item.id,
        component_product_id=product.id,
        parent_product_version=1,
        component_product_version=1,
        snapshot_schema_version=2,
        order_set_quantity=int(item.quantity),
        quantity_per_set=Decimal("1"),
        required_piece_quantity=Decimal(int(item.quantity)),
        display_order=display_order,
        internal_component_code=f"P1-51C-C{display_order}",
        is_die_cut=False,
        spare_sheet_quantity=0,
        display_mode="internal_only",
        is_required=True,
        snapshot_component_product_code=product.product_code,
        snapshot_component_product_name=product.product_name,
        snapshot_component_spec="800×600",
        snapshot_component_material="K616K / AB",
        snapshot_component_flute_type="AB",
        snapshot_component_box_category="normal",
    )


def _supplier_order_for_item(
    db: Session,
    *,
    item: OrderItem,
    product: Product,
    suffix: str,
) -> SupplierRequisitionOrder:
    supplier = SupplierRequisitionOrder(
        order_number=f"SRO-P1-51C-{suffix}",
        supplier_name="P1-51C 测试纸板厂",
        total_quantity=int(item.quantity),
        stock_deduction_qty=0,
        requisition_qty=int(item.quantity),
        required_piece_qty=int(item.quantity),
        status="confirmed",
    )
    db.add(supplier)
    db.flush()
    db.add(
        SupplierRequisitionOrderItem(
            supplier_order_id=supplier.id,
            order_item_id=item.id,
            source_key=f"order_item:{item.id}",
            product_id=product.id,
            product_code=product.product_code,
            product_name=product.product_name,
            order_number=item.item_order_number,
            quantity=int(item.quantity),
            stock_deduction_qty=0,
            requisition_qty=int(item.quantity),
            required_piece_qty=int(item.quantity),
            cutting_mode="一开一",
            pieces_per_box=1,
            customer_name="P1-51C 快照客户",
        )
    )
    db.flush()
    return supplier


@pytest.fixture()
def snapshot_db(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "p1-51c-snapshot.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        customer = Customer(
            customer_number=5103,
            customer_code="P151C-A",
            name="P1-51C 快照客户",
        )
        other = Customer(
            customer_number=5104,
            customer_code="P151C-B",
            name="P1-51C 范围外客户",
        )
        db.add_all([customer, other])
        db.commit()
        yield db, customer.id, other.id, engine
    engine.dispose()


def test_ordinary_direct_task_freezes_ordered_colors_once(snapshot_db) -> None:
    from app.services.requisition_production_print import (
        build_supplier_requisition_production_package,
    )

    db, customer_id, _other_id, _engine = snapshot_db
    original_colors = ["PANTONE 186 C", "专蓝-超长颜色名称", "环保绿"]
    product = _product(
        customer_id,
        "DIRECT-001",
        print_content="三色印刷",
        printing_colors="＋".join(original_colors),
    )
    db.add(product)
    db.flush()
    item = _order_item(db, customer_id=customer_id, product=product, suffix="DIRECT")

    first = create_or_refresh_production_task(db, item.id)
    first_id = first.id
    first_raw = first.printing_colors_snapshot
    assert json.loads(first_raw) == original_colors
    assert _task_printing_snapshot(first)["printing_colors"] == original_colors
    assert _task_printing_snapshot(first)["printing_colors_frozen"] is True

    product.print_content = "单色印刷"
    product.printing_colors = "后来改成的黑色"
    again = create_or_refresh_production_task(db, item.id)
    assert again.id == first_id
    assert db.scalar(select(func.count()).select_from(ProductionTask)) == 1
    assert again.printing_colors_snapshot == first_raw
    assert _task_printing_snapshot(again)["printing_colors"] == original_colors
    assert _task_printing_snapshot(again)["print_content"] == "三色印刷"
    supplier = _supplier_order_for_item(
        db,
        item=item,
        product=product,
        suffix="DIRECT",
    )
    print_component = build_supplier_requisition_production_package(db, supplier)[
        "cards"
    ][0]["components"][0]
    assert print_component["printing_colors_frozen"] is True
    assert print_component["printing_colors"] == original_colors
    assert "后来改成的黑色" not in json.dumps(print_component, ensure_ascii=False)


def test_plate_task_freezes_name_color_order_and_machine_values_but_reads_current_location(
    snapshot_db,
) -> None:
    from app.services.requisition_production_print import (
        build_supplier_requisition_production_package,
    )

    db, customer_id, _other_id, engine = snapshot_db
    plates = [
        PrintingPlate(
            plate_code=f"PL-P151C-{index}",
            customer_id=customer_id,
            plate_name=f"第{index}块超长挂板内容-正唛侧唛及注意事项",
            color_name=color,
            rack_location=f"1F-PL-R01-L1-P{index}",
        )
        for index, color in enumerate(("专红", "PANTONE 300 C", "环保绿"), 1)
    ]
    db.add_all(plates)
    db.flush()
    product = _product(
        customer_id,
        "PLATE-001",
        print_content="三色印刷",
        printing_plate_mode="plate",
        printing_plate_1_id=plates[0].id,
        printing_plate_2_id=plates[1].id,
        printing_plate_3_id=plates[2].id,
        plate_alignment_value_mm=Decimal("1.25"),
        plate_mount_value_mm=Decimal("2.50"),
        machine_set_length_mm=Decimal("520"),
        machine_set_width_mm=Decimal("350"),
        machine_set_height_mm=Decimal("300.50"),
    )
    db.add(product)
    db.flush()
    item = _order_item(db, customer_id=customer_id, product=product, suffix="PLATE")
    task = create_or_refresh_production_task(db, item.id)
    frozen_details = json.loads(task.printing_plate_details_snapshot)
    assert frozen_details == [
        {
            "plate_code": plate.plate_code,
            "plate_name": plate.plate_name,
            "color_name": plate.color_name,
        }
        for plate in plates
    ]
    assert json.loads(task.printing_colors_snapshot) == [
        "专红",
        "PANTONE 300 C",
        "环保绿",
    ]

    # Product configuration and mutable plate master data must not rewrite the task.
    plates[0].plate_name = "后来改名，不得覆盖历史"
    plates[0].color_name = "后来改色"
    plates[0].rack_location = "1F-PL-R09-L9-P99"
    product.printing_plate_1_id = plates[2].id
    product.machine_set_length_mm = Decimal("999")
    task_id = task.id
    again = create_or_refresh_production_task(db, item.id)
    assert again.id == task_id
    assert json.loads(again.printing_plate_details_snapshot) == frozen_details
    assert again.machine_set_length_mm_snapshot == Decimal("520")

    plate_selects: list[str] = []

    def count_plate_selects(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().lower().startswith("select") and "printing_plates" in statement:
            plate_selects.append(statement)

    event.listen(engine, "before_cursor_execute", count_plate_selects)
    try:
        row = list_production_tasks(db, allowed_customer_ids=None)[0]
    finally:
        event.remove(engine, "before_cursor_execute", count_plate_selects)
    assert row["printing_plates"] == [
        {
            **detail,
            "current_location": (
                "1F-PL-R09-L9-P99" if index == 0 else plates[index].rack_location
            ),
        }
        for index, detail in enumerate(frozen_details)
    ]
    assert row["plate_alignment_value_mm"] == Decimal("1.25")
    assert row["plate_mount_value_mm"] == Decimal("2.50")
    assert row["machine_set_length_mm"] == Decimal("520")
    assert row["machine_set_width_mm"] == Decimal("350")
    assert row["machine_set_height_mm"] == Decimal("300.50")
    assert row["printing_situation"] == "挂板印刷（三色）"
    assert len(plate_selects) <= 1, "current plate locations must be loaded in one batch"
    supplier = _supplier_order_for_item(
        db,
        item=item,
        product=product,
        suffix="PLATE",
    )
    print_component = build_supplier_requisition_production_package(db, supplier)[
        "cards"
    ][0]["components"][0]
    assert print_component["printing_plates"] == row["printing_plates"]
    assert print_component["printing_colors"] == ["专红", "PANTONE 300 C", "环保绿"]


@pytest.mark.parametrize(
    ("print_content", "plate_slots"),
    [
        ("单色印刷", (False, False, False)),
        ("单色印刷", (True, True, False)),
        ("双色印刷", (True, False, True)),
        ("三色印刷", (True, True, False)),
    ],
)
def test_new_task_rejects_plate_count_or_order_that_does_not_match_print_content(
    snapshot_db,
    print_content: str,
    plate_slots: tuple[bool, bool, bool],
) -> None:
    db, customer_id, _other_id, _engine = snapshot_db
    plates = [
        PrintingPlate(
            plate_code=f"PL-P151C-GATE-{index}",
            customer_id=customer_id,
            plate_name=f"门禁挂板 {index}",
            color_name=f"门禁色 {index}",
            rack_location=f"1F-PL-R08-L1-P{index:02d}",
        )
        for index in range(1, 4)
    ]
    db.add_all(plates)
    db.flush()
    product = _product(
        customer_id,
        f"PLATE-GATE-{print_content}",
        print_content=print_content,
        printing_plate_mode="plate",
        printing_plate_1_id=plates[0].id if plate_slots[0] else None,
        printing_plate_2_id=plates[1].id if plate_slots[1] else None,
        printing_plate_3_id=plates[2].id if plate_slots[2] else None,
    )
    db.add(product)
    db.flush()
    item = _order_item(
        db,
        customer_id=customer_id,
        product=product,
        suffix=f"PLATE-GATE-{sum(plate_slots)}",
    )

    with pytest.raises(ProductionWorkflowError, match="印刷色数与挂板顺序不一致"):
        create_or_refresh_production_task(db, item.id)
    assert db.scalar(select(func.count()).select_from(ProductionTask)) == 0


def test_composite_bom_creation_freezes_each_component_once_and_is_idempotent(
    snapshot_db,
) -> None:
    db, customer_id, _other_id, _engine = snapshot_db
    plate = PrintingPlate(
        plate_code="PL-P151C-COMP",
        customer_id=customer_id,
        plate_name="组合底组件挂板内容",
        color_name="组合专红",
        rack_location="1F-PL-R02-L2-P08",
    )
    parent = _product(customer_id, "COMPOSITE", print_content="无印刷")
    parent.is_composite = True
    direct = _product(
        customer_id,
        "COMP-DIRECT",
        print_content="双色印刷",
        printing_colors="PANTONE 186 C＋专蓝",
    )
    direct.is_internal_component = True
    plated = _product(
        customer_id,
        "COMP-PLATE",
        print_content="单色印刷",
        printing_plate_mode="plate",
        printing_plate_1_id=None,
        plate_alignment_value_mm=Decimal("0.80"),
        plate_mount_value_mm=Decimal("1.60"),
        machine_set_length_mm=Decimal("410"),
        machine_set_width_mm=Decimal("305"),
        machine_set_height_mm=Decimal("205"),
    )
    plated.is_internal_component = True
    db.add_all([plate, parent, direct, plated])
    db.flush()
    plated.printing_plate_1_id = plate.id
    item = _order_item(db, customer_id=customer_id, product=parent, suffix="COMPOSITE")
    db.add_all(
        [
            _component_snapshot(item, direct, display_order=1),
            _component_snapshot(item, plated, display_order=2),
        ]
    )
    db.flush()

    create_or_refresh_production_task(db, item.id)
    tasks = list(
        db.scalars(
            select(ProductionTask)
            .where(ProductionTask.order_item_id == item.id)
            .order_by(ProductionTask.sales_order_item_bom_component_id)
        ).all()
    )
    assert len(tasks) == 2
    frozen = {
        task.id: (
            task.print_content_snapshot,
            task.printing_colors_snapshot,
            task.printing_plate_details_snapshot,
            task.machine_set_length_mm_snapshot,
        )
        for task in tasks
    }
    assert json.loads(tasks[0].printing_colors_snapshot) == ["PANTONE 186 C", "专蓝"]
    assert json.loads(tasks[1].printing_plate_details_snapshot) == [
        {
            "plate_code": "PL-P151C-COMP",
            "plate_name": "组合底组件挂板内容",
            "color_name": "组合专红",
        }
    ]

    direct.printing_colors = "后改红＋后改蓝"
    plate.plate_name = "后改挂板名称"
    plate.color_name = "后改挂板颜色"
    plated.machine_set_length_mm = Decimal("999")
    create_or_refresh_production_task(db, item.id)
    replayed = list(
        db.scalars(
            select(ProductionTask)
            .where(ProductionTask.order_item_id == item.id)
            .order_by(ProductionTask.sales_order_item_bom_component_id)
        ).all()
    )
    assert len(replayed) == 2
    assert {task.id for task in replayed} == set(frozen)
    assert {
        task.id: (
            task.print_content_snapshot,
            task.printing_colors_snapshot,
            task.printing_plate_details_snapshot,
            task.machine_set_length_mm_snapshot,
        )
        for task in replayed
    } == frozen


def test_composite_bom_workflow_creation_path_uses_the_same_frozen_snapshot(
    snapshot_db,
) -> None:
    from app.services.composite_bom_workflow import ensure_component_production_tasks

    db, customer_id, _other_id, _engine = snapshot_db
    parent = _product(customer_id, "ALT-COMPOSITE", print_content="无印刷")
    parent.is_composite = True
    component = _product(
        customer_id,
        "ALT-COMP-DIRECT",
        print_content="双色印刷",
        printing_colors="专紫＋PANTONE 123 C",
    )
    component.is_internal_component = True
    db.add_all([parent, component])
    db.flush()
    item = _order_item(
        db,
        customer_id=customer_id,
        product=parent,
        suffix="ALT-COMPOSITE",
    )
    db.add(_component_snapshot(item, component, display_order=1))
    db.flush()

    first = ensure_component_production_tasks(db, item.id)
    assert len(first) == 1
    task = first[0]
    frozen_id = task.id
    frozen_raw = task.printing_colors_snapshot
    assert json.loads(frozen_raw) == ["专紫", "PANTONE 123 C"]

    component.print_content = "单色印刷"
    component.printing_colors = "后来改成的绿色"
    second = ensure_component_production_tasks(db, item.id)
    assert len(second) == 1
    assert second[0].id == frozen_id
    assert second[0].printing_colors_snapshot == frozen_raw
    assert _task_printing_snapshot(second[0])["printing_colors"] == [
        "专紫",
        "PANTONE 123 C",
    ]
    assert db.scalar(
        select(func.count()).select_from(ProductionTask).where(
            ProductionTask.order_item_id == item.id
        )
    ) == 1


def test_legacy_null_never_reads_current_product_in_screen_mobile_or_requisition(
    snapshot_db,
) -> None:
    from app.api.mobile_erp import _production_station_task_payloads
    from app.services.requisition_production_print import (
        build_supplier_requisition_production_package,
    )

    db, customer_id, _other_id, _engine = snapshot_db
    product = _product(
        customer_id,
        "LEGACY-NULL",
        print_content="双色印刷",
        printing_colors="实时红＋实时蓝",
    )
    db.add(product)
    db.flush()
    item = _order_item(db, customer_id=customer_id, product=product, suffix="LEGACY")
    task = ProductionTask(
        order_item_id=item.id,
        status="pending",
        planned_quantity=10,
        ordered_quantity_snapshot=10,
        material_received_quantity=10,
        material_input_quantity=10,
        output_factor=1,
        version=1,
        print_content_snapshot="双色印刷",
        printing_plate_mode_snapshot="no_plate",
        printing_colors_snapshot=None,
        printing_plate_codes_snapshot="[]",
        printing_plate_details_snapshot="[]",
    )
    db.add(task)
    db.flush()
    supplier = SupplierRequisitionOrder(
        order_number="SRO-P1-51C-LEGACY",
        supplier_name="测试纸板厂",
        total_quantity=10,
        stock_deduction_qty=0,
        requisition_qty=10,
        required_piece_qty=10,
        status="confirmed",
    )
    db.add(supplier)
    db.flush()
    db.add(
        SupplierRequisitionOrderItem(
            supplier_order_id=supplier.id,
            order_item_id=item.id,
            source_key=f"order_item:{item.id}",
            product_id=product.id,
            product_code=product.product_code,
            product_name=product.product_name,
            order_number="P1-51C-LEGACY",
            quantity=10,
            stock_deduction_qty=0,
            requisition_qty=10,
            required_piece_qty=10,
            cutting_mode="一开一",
            pieces_per_box=1,
            customer_name="P1-51C 快照客户",
        )
    )
    db.flush()

    row = list_production_tasks(db, allowed_customer_ids=None)[0]
    assert row["printing_colors_frozen"] is False
    assert row["printing_colors"] == []
    mobile = _production_station_task_payloads(
        db,
        tasks=[row],
        station="printing",
        mold_map_allowed=False,
    )[0]
    assert mobile["printing_colors_frozen"] is False
    assert mobile["printing_colors"] == []

    package = build_supplier_requisition_production_package(db, supplier)
    component = package["cards"][0]["components"][0]
    assert component["printing_colors_frozen"] is False
    assert component["printing_colors"] == []
    assert package["cards"][0]["printing_colors"] == []
    serialized = json.dumps(
        {"screen": row, "mobile": mobile, "print": package},
        ensure_ascii=False,
        default=str,
    )
    assert "实时红" not in serialized
    assert "实时蓝" not in serialized
    assert task.printing_colors_snapshot is None


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "123456"},
    )
    assert response.status_code == 200, response.text


def test_production_task_api_keeps_permission_and_customer_scope(snapshot_db) -> None:
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.production import router as production_router
    from app.core.security import hash_password
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    from app.models.user import User

    db, customer_id, other_id, _engine = snapshot_db
    direct = _product(
        customer_id,
        "SCOPE-A",
        print_content="单色印刷",
        printing_colors="专红",
    )
    other = _product(
        other_id,
        "SCOPE-B",
        print_content="单色印刷",
        printing_colors="专蓝",
    )
    admin = User(
        username="p151c-admin",
        password_hash=hash_password("123456"),
        role="admin",
        real_name="P1-51C 管理员",
        display_name="P1-51C 管理员",
        must_change_password=False,
    )
    scoped = User(
        username="p151c-scoped",
        password_hash=hash_password("123456"),
        role="sales",
        real_name="P1-51C 范围账号",
        display_name="P1-51C 范围账号",
        customer_access_mode="selected",
        must_change_password=False,
    )
    denied = User(
        username="p151c-denied",
        password_hash=hash_password("123456"),
        role="sales",
        real_name="P1-51C 禁止账号",
        display_name="P1-51C 禁止账号",
        must_change_password=False,
    )
    db.add_all([direct, other, admin, scoped, denied])
    db.flush()
    db.add_all(
        [
            UserCustomerScope(
                user_id=scoped.id,
                customer_id=other_id,
                assigned_by=admin.id,
            ),
            UserPermissionOverride(
                user_id=scoped.id,
                permission_code="orders.view",
                is_allowed=True,
            ),
            UserPermissionOverride(
                user_id=denied.id,
                permission_code="orders.view",
                is_allowed=False,
            ),
        ]
    )
    a_item = _order_item(db, customer_id=customer_id, product=direct, suffix="SCOPE-A")
    b_item = _order_item(db, customer_id=other_id, product=other, suffix="SCOPE-B")
    create_or_refresh_production_task(db, a_item.id)
    create_or_refresh_production_task(db, b_item.id)
    db.commit()
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(production_router, prefix="/api/production")

    def override_db():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        assert client.get("/api/production/tasks").status_code == 401
        _login(client, "p151c-denied")
        assert client.get("/api/production/tasks").status_code == 403
        client.cookies.clear()
        _login(client, "p151c-scoped")
        scoped_rows = client.get("/api/production/tasks")
        assert scoped_rows.status_code == 200, scoped_rows.text
        assert [row["product_code"] for row in scoped_rows.json()["items"]] == [
            "SCOPE-B"
        ]
        assert scoped_rows.json()["items"][0]["printing_colors"] == ["专蓝"]
        client.cookies.clear()
        _login(client, "p151c-admin")
        admin_rows = client.get("/api/production/tasks")
        assert admin_rows.status_code == 200, admin_rows.text
        assert {row["product_code"] for row in admin_rows.json()["items"]} == {
            "SCOPE-A",
            "SCOPE-B",
        }


def _method_body(source: str, signature: str, next_signature: str) -> str:
    return source.split(signature, 1)[1].split(next_signature, 1)[0].rsplit("}", 1)[0]


def _run_node(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the P1-51C UI regression"
    target = tmp_path / name
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_screen_and_black_white_print_render_frozen_long_text_without_leaking_internal_fields(
    tmp_path: Path,
) -> None:
    screen_body = _method_body(
        INDEX,
        "productionPrintingSetup(row) {",
        "productionProductTitle(row) {",
    )
    component_body = _method_body(
        TASK_PRINT,
        "function printingComponentHtml(component, componentIndex, componentCount) {",
        "function printingHtml(card) {",
    )
    print_body = _method_body(
        TASK_PRINT,
        "function printingHtml(card) {",
        "function printingComplexity(card) {",
    )
    complexity_body = _method_body(
        TASK_PRINT,
        "function printingComplexity(card) {",
        "function cardNeedsFullPage(card) {",
    )
    full_page_body = _method_body(
        TASK_PRINT,
        "function cardNeedsFullPage(card) {",
        "function printPageLayouts(cards) {",
    )
    layouts_body = _method_body(
        TASK_PRINT,
        "function printPageLayouts(cards) {",
        "function detailHtml(card) {",
    )
    mobile = (ROOT / "static" / "mobile_erp.html").read_text(encoding="utf-8")
    mobile_lines_body = _method_body(
        mobile,
        "function productionPrintingSnapshotLines(task) {",
        "function productionStationCard(task) {",
    )
    script = f"""
const screen=new Function('row',{json.dumps(screen_body, ensure_ascii=False)});
const mobileLines=new Function('task',{json.dumps(mobile_lines_body, ensure_ascii=False)});
const escapeHtml=value=>String(value??'').replace(/[&<>\"]/g, c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}}[c]));
const detailRow=(label,value,extraClass='')=>`<div class="detail-row ${{extraClass}}"><span class="label">${{escapeHtml(label)}}</span><span class="value">${{escapeHtml(value||'-')}}</span></div>`;
const numberText=value=>value===null||value===undefined||value===''?'-':String(value);
const rawPrintingComponentHtml=new Function('component','componentIndex','componentCount','escapeHtml','detailRow','numberText',{json.dumps(component_body, ensure_ascii=False)});
const printingComponentHtml=(component,index,count)=>rawPrintingComponentHtml(component,index,count,escapeHtml,detailRow,numberText);
const printingHtml=new Function('card','printingComponentHtml',{json.dumps(print_body, ensure_ascii=False)});
const printingComplexity=new Function('card',{json.dumps(complexity_body, ensure_ascii=False)});
const rawCardNeedsFullPage=new Function('card','printingComplexity',{json.dumps(full_page_body, ensure_ascii=False)});
const cardNeedsFullPage=card=>rawCardNeedsFullPage(card,printingComplexity);
const rawPrintPageLayouts=new Function('cards','cardNeedsFullPage',{json.dumps(layouts_body, ensure_ascii=False)});
const printPageLayouts=cards=>rawPrintPageLayouts(cards,cardNeedsFullPage);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
const longName='PANTONE 186 C 超长专色名称必须在黑白打印中完整保留且可以自然换行';
const longPlate='正唛与侧唛以及运输警示图案超长挂板内容必须完整保留不能截断';
expect(screen({{printing_situation:'双色印刷',print_content:'双色印刷',printing_plate_mode:'no_plate',printing_colors_frozen:true,printing_colors:[longName,'专蓝']}}).includes(longName),'screen lost direct color');
expect(screen({{printing_situation:'双色印刷',printing_plate_mode:'no_plate',printing_colors_frozen:false,printing_colors:[]}})==='双色印刷｜历史颜色未冻结｜不挂板','legacy NULL wording drifted');
expect(screen({{printing_situation:'无印刷',printing_plate_mode:'no_plate',printing_colors_frozen:true,printing_colors:[]}})==='','no-print screen gained empty color fields');
const plate={{printing_situation:'挂板印刷（三色）',print_content:'三色印刷',printing_plate_mode:'plate',printing_colors_frozen:true,printing_colors:['专红','专蓝','环保绿'],printing_plates:[
  {{plate_code:'PL-001',plate_name:longPlate,color_name:'专红',current_location:'1F-PL-R01-L1-P01'}},
  {{plate_code:'PL-002',plate_name:'侧唛',color_name:'专蓝',current_location:'1F-PL-R01-L1-P02'}},
  {{plate_code:'PL-003',plate_name:'环保标识',color_name:'环保绿',current_location:'1F-PL-R01-L1-P03'}}
],plate_alignment_value_mm:'1.25',plate_mount_value_mm:'2.50',machine_set_length_mm:'520',machine_set_width_mm:'350',machine_set_height_mm:'300'}};
const summary=screen(plate);
expect(['第1色','第2色','第3色',longPlate,'当前位置 1F-PL-R01-L1-P01','机器设定 520×350×300'].every(value=>summary.includes(value)),'screen plate snapshot incomplete');
const mobilePlate=mobileLines(plate).join('｜');
expect(['挂板印刷（三色）','第1色',longPlate,'当前位置 1F-PL-R01-L1-P01','机设 520×350×300'].every(value=>mobilePlate.includes(value)),'mobile printing station lost plate snapshot');
expect(mobileLines({{printing_situation:'双色印刷',printing_plate_mode:'no_plate',printing_colors_frozen:false,printing_colors:[]}})[0]==='双色印刷｜历史颜色未冻结｜不挂板','mobile legacy wording drifted');
expect(mobileLines({{printing_situation:'无印刷',printing_plate_mode:'no_plate',printing_colors_frozen:true,printing_colors:[]}}).length===0,'mobile no-print gained empty fields');
const html=printingHtml({{components:[plate]}},printingComponentHtml);
expect(['printing-block','printing-plate-line','第1色','第2色','第3色','颜色：专红','挂板编号：PL-001','挂板内容：'+longPlate,'当前位置','机器设定'].every(value=>html.includes(value)),'black-white text contract incomplete');
expect(html.includes('internal-only'),'internal facts were not marked sensitive');
expect(cardNeedsFullPage({{components:[plate]}})===true,'three-plate card was not promoted to a full A4 page');
expect(cardNeedsFullPage({{components:[{{printing_situation:'单色印刷',printing_plate_mode:'no_plate',printing_colors_frozen:true,printing_colors:['黑色']}}]}})===false,'simple direct-print card lost two-up layout');
const mixed={{components:[{{printing_situation:'单色印刷',printing_plate_mode:'no_plate',printing_colors_frozen:true,printing_colors:['黑色']}},plate]}};
expect(cardNeedsFullPage(mixed)===true,'multi-component printing card was not promoted to a full A4 page');
const layouts=printPageLayouts([
  {{id:'simple-1',components:[{{printing_situation:'单色印刷',printing_plate_mode:'no_plate',printing_colors_frozen:true,printing_colors:['黑色']}}]}},
  {{id:'plate',components:[plate]}},
  {{id:'simple-2',components:[{{printing_situation:'无印刷',printing_plate_mode:'no_plate',printing_colors_frozen:true,printing_colors:[]}}]}}
]);
expect(layouts.length===3 && layouts[1].fullPage===true && layouts[1].top.id==='plate','mixed print page pagination is not deterministic');
const legacyHtml=printingHtml({{components:[{{printing_situation:'双色印刷',printing_plate_mode:'no_plate',printing_colors_frozen:false,printing_colors:[]}}]}},printingComponentHtml);
expect(legacyHtml.includes('历史颜色未冻结'),'historical NULL print wording missing');
expect(printingHtml({{components:[{{printing_situation:'无印刷',printing_plate_mode:'no_plate',printing_colors_frozen:true,printing_colors:[]}}]}},printingComponentHtml)==='','no-print paper gained an empty block');
"""
    _run_node(script, tmp_path, "p1-51c-production-print-snapshot.mjs")
    compact_css = "".join(TASK_PRINT.split())
    assert ".customer-safe.internal-only{display:none!important;}" in compact_css
    assert "white-space:normal" in TASK_PRINT
    assert "overflow-wrap:anywhere" in TASK_PRINT
    assert ".single-page .task-card.printing-heavy" in TASK_PRINT
    assert "element.scrollHeight > element.clientHeight + 1" in TASK_PRINT
    assert "element.scrollWidth > element.clientWidth + 1" in TASK_PRINT
    assert "toolbarNote.textContent = receiptMode" in TASK_PRINT
    assert "fullPageCount" in TASK_PRINT
    assert "黑白打印 · ${fullPageCount} 款复杂印刷任务单独占 A4" in TASK_PRINT

    assert "task.printing_colors_frozen === false" in mobile
    assert 'colors.join("＋")' in mobile
    assert "第${index + 1}色" in mobile
    assert "item.plate_name" in mobile
    assert "当前位置 ${item.current_location}" in mobile
    assert "product.printing_colors" not in mobile
    assert "productionPrintingSnapshotLines(task).forEach" in mobile
    assert "印刷内容：${task.print_content" not in mobile
