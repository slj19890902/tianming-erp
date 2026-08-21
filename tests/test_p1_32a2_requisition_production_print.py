from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def production_print_app(tmp_path: Path):
    import app.models  # noqa: F401
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.requisition import router as requisition_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.production import ProductionTask
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "p1-32a2.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as db:
        admin = User(
            username="p132a2-admin",
            password_hash=hash_password("123456"),
            role="admin",
            real_name="测试管理员",
            display_name="测试管理员",
            must_change_password=False,
        )
        restricted = User(
            username="p132a2-sales",
            password_hash=hash_password("123456"),
            role="sales",
            real_name="受限账号",
            display_name="受限账号",
            customer_access_mode="selected",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=3201,
            customer_code="P132A2-A",
            name="半页任务单客户",
            chinese_short_name="半页客户",
        )
        other_customer = Customer(
            customer_number=3202,
            customer_code="P132A2-B",
            name="受限账号客户",
        )
        db.add_all([admin, restricted, customer, other_customer])
        db.flush()
        db.add_all(
            [
                UserPermissionOverride(
                    user_id=restricted.id,
                    permission_code="requisition.view",
                    is_allowed=True,
                ),
                UserCustomerScope(
                    user_id=restricted.id,
                    customer_id=other_customer.id,
                    assigned_by=admin.id,
                ),
            ]
        )
        material = Material(
            code="K=A",
            layer_count=3,
            flute_type="B",
            supplier_name="测试纸板厂",
        )
        db.add(material)
        db.flush()
        product = Product(
            customer_id=customer.id,
            product_code="P132A2-PARENT",
            customer_material_code="P132A2-PARENT",
            product_name="天地盖测试箱",
            material_id=material.id,
            legacy_material_text="K=A",
            length_mm=Decimal("400"),
            width_mm=Decimal("300"),
            height_mm=Decimal("200"),
            box_category="normal",
            box_style="A3",
        )
        db.add(product)
        db.flush()
        sales_order = Order(
            order_number="PO-P132A2-001",
            customer_po="CPO-P132A2",
            customer_id=customer.id,
            order_date=date(2026, 8, 10),
            delivery_date=date(2026, 8, 18),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("1200"),
        )
        db.add(sales_order)
        db.flush()
        order_items: list[OrderItem] = []
        for index in range(4):
            code = "P132A2-SPLIT" if index == 0 else f"P132A2-{index + 1:02d}"
            item = OrderItem(
                order_id=sales_order.id,
                product_id=product.id,
                item_order_number=f"TM-P132A2-{index + 1:03d}",
                item_sequence=index + 1,
                quantity=200 + index * 10,
                unit_price=Decimal("1.00"),
                subtotal=Decimal(str(200 + index * 10)),
                material_status="pending",
                requisition_status="已报料",
                snapshot_product_name="天地盖测试箱" if index == 0 else f"测试纸箱{index + 1}",
                snapshot_product_code=code,
                snapshot_spec="400×300×200mm",
                snapshot_material="K=A",
                snapshot_production_notes="印刷后模切",
                snapshot_report_length_mm=700 + index,
                snapshot_report_width_mm=500 + index,
                snapshot_base_report_length_mm=650,
                snapshot_base_report_width_mm=480,
                snapshot_crease_type="净",
                snapshot_base_crease_type="毛",
                snapshot_report_notes="主料要求",
                snapshot_base_report_notes="底料要求",
                layer_count=3,
                flute_type="B",
                material_id=material.id,
                drawing_file=f"drawings/{code}.pdf",
            )
            db.add(item)
            db.flush()
            order_items.append(item)
            db.add(
                ProductionTask(
                    order_item_id=item.id,
                    status="waiting_material",
                    planned_quantity=0,
                    ordered_quantity_snapshot=item.quantity,
                    version=index + 2,
                    print_content_snapshot="单色印刷",
                    printing_plate_mode_snapshot="plate",
                    printing_plate_codes_snapshot='["PLATE-01"]',
                    plate_alignment_value_mm_snapshot=Decimal("1.20"),
                    plate_mount_value_mm_snapshot=Decimal("2.30"),
                    machine_set_length_mm_snapshot=Decimal("400"),
                    machine_set_width_mm_snapshot=Decimal("300"),
                    machine_set_height_mm_snapshot=Decimal("201"),
                )
            )

        supplier_order = SupplierRequisitionOrder(
            order_number="SRO-P132A2-001",
            supplier_name="测试纸板厂",
            material_id=material.id,
            layer_count=3,
            flute_type="B",
            report_length_mm=700,
            report_width_mm=500,
            total_quantity=860,
            requisition_qty=560,
            stock_deduction_qty=0,
            required_piece_qty=860,
            status="confirmed",
            created_by=admin.id,
        )
        db.add(supplier_order)
        db.flush()
        split_item = order_items[0]
        db.add_all(
            [
                SupplierRequisitionOrderItem(
                    supplier_order_id=supplier_order.id,
                    order_item_id=split_item.id,
                    source_key=f"order_item:{split_item.id}:cover",
                    product_id=product.id,
                    material_id=material.id,
                    material_code_snapshot="K=A",
                    supplier_name_snapshot="测试纸板厂",
                    layer_count_snapshot=3,
                    flute_type_snapshot="B",
                    order_number=split_item.item_order_number,
                    product_code="P132A2-SPLIT",
                    product_name="天地盖测试箱-盖",
                    report_length_mm=700,
                    report_width_mm=500,
                    quantity=200,
                    requisition_qty=100,
                    stock_deduction_qty=0,
                    required_piece_qty=200,
                    cutting_mode="一开二",
                    pieces_per_box=1,
                    customer_name=customer.name,
                    delivery_date=sales_order.delivery_date,
                ),
                SupplierRequisitionOrderItem(
                    supplier_order_id=supplier_order.id,
                    order_item_id=split_item.id,
                    source_key=f"order_item:{split_item.id}:base",
                    product_id=product.id,
                    material_id=material.id,
                    material_code_snapshot="K=A",
                    supplier_name_snapshot="测试纸板厂",
                    layer_count_snapshot=3,
                    flute_type_snapshot="B",
                    order_number=split_item.item_order_number,
                    product_code="P132A2-SPLIT",
                    product_name="天地盖测试箱-底",
                    report_length_mm=650,
                    report_width_mm=480,
                    quantity=200,
                    requisition_qty=100,
                    stock_deduction_qty=0,
                    required_piece_qty=200,
                    cutting_mode="一开二",
                    pieces_per_box=1,
                    customer_name=customer.name,
                    delivery_date=sales_order.delivery_date,
                ),
            ]
        )
        for index, item in enumerate(order_items[1:], start=1):
            db.add(
                SupplierRequisitionOrderItem(
                    supplier_order_id=supplier_order.id,
                    order_item_id=item.id,
                    source_key=f"order_item:{item.id}",
                    product_id=product.id,
                    material_id=material.id,
                    material_code_snapshot="K=A",
                    supplier_name_snapshot="测试纸板厂",
                    layer_count_snapshot=3,
                    flute_type_snapshot="B",
                    order_number=item.item_order_number,
                    product_code=item.snapshot_product_code,
                    product_name=item.snapshot_product_name,
                    report_length_mm=700 + index,
                    report_width_mm=500 + index,
                    quantity=item.quantity,
                    requisition_qty=120,
                    stock_deduction_qty=0,
                    required_piece_qty=item.quantity,
                    cutting_mode="一开一",
                    pieces_per_box=1,
                    customer_name=customer.name,
                    delivery_date=sales_order.delivery_date,
                )
            )
        db.commit()
        supplier_order_id = supplier_order.id
        first_supplier_item_id = supplier_order.items[0].id
        order_id = sales_order.id
        order_item_id = split_item.id

    application = FastAPI()
    application.include_router(auth_router, prefix="/api/auth")
    application.include_router(requisition_router, prefix="/api/requisition")

    def override_get_db():
        with session_factory() as db:
            yield db

    application.dependency_overrides[get_db] = override_get_db
    yield {
        "app": application,
        "session_factory": session_factory,
        "supplier_order_id": supplier_order_id,
        "supplier_item_id": first_supplier_item_id,
        "sales_order_id": order_id,
        "order_item_id": order_item_id,
        "product_id": product.id,
    }
    engine.dispose()


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "123456"},
    )
    assert response.status_code == 200, response.text


def test_package_keeps_one_formal_requisition_detail_per_task_card(
    production_print_app,
):
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
    from app.services.requisition_production_print import (
        build_supplier_requisition_production_package,
    )

    session_factory = production_print_app["session_factory"]
    with session_factory() as db:
        order = db.get(
            SupplierRequisitionOrder,
            production_print_app["supplier_order_id"],
        )
        before = db.scalar(
            select(func.count()).select_from(SupplierRequisitionOrder)
        )
        first = build_supplier_requisition_production_package(db, order)
        second = build_supplier_requisition_production_package(db, order)
        order.items[-1].product_code = order.items[-2].product_code
        db.flush()
        same_code_package = build_supplier_requisition_production_package(db, order)
        db.rollback()
        after = db.scalar(
            select(func.count()).select_from(SupplierRequisitionOrder)
        )

    assert first["card_count"] == 5
    assert first["page_count"] == 3
    assert all(page["top"] and page["bottom"] for page in first["pages"][:-1])
    assert first["pages"][-1]["top"] is not None
    assert first["pages"][-1]["bottom"] is None
    assert same_code_package["card_count"] == 5
    assert same_code_package["page_count"] == 3
    cover, base = first["cards"][:2]
    assert cover["supplier_order_item_id"] != base["supplier_order_item_id"]
    assert [cover["source_identity"], base["source_identity"]] == [
        f"order_item:{production_print_app['order_item_id']}:cover",
        f"order_item:{production_print_app['order_item_id']}:base",
    ]
    assert [cover["component_label"], base["component_label"]] == ["盖", "底"]
    assert cover["customer_name"] == base["customer_name"] == "半页客户"
    assert cover["customer_pos"] == base["customer_pos"] == ["CPO-P132A2"]
    assert cover["customer_order_quantity"] == base["customer_order_quantity"] == 200
    assert cover["stock_deduction_quantity"] == base["stock_deduction_quantity"] == 0
    assert cover["layout_kind"] == base["layout_kind"] == "carton"
    assert [cover["product_name"], base["product_name"]] == [
        "天地盖测试箱-盖",
        "天地盖测试箱-底",
    ]
    assert [
        cover["planned_finished_quantity"],
        base["planned_finished_quantity"],
    ] == [200, 200]
    assert [
        cover["requisition_quantity"],
        base["requisition_quantity"],
    ] == [100, 100]
    assert [len(cover["components"]), len(base["components"])] == [1, 1]
    assert [
        cover["components"][0]["component_label"],
        base["components"][0]["component_label"],
    ] == ["盖", "底"]
    assert [
        cover["components"][0]["report_length_mm"],
        base["components"][0]["report_length_mm"],
    ] == [700, 650]
    assert cover["components"][0]["print_content"] == "单色印刷"
    assert cover["components"][0]["production_task_version"] == 2
    assert set(base["components"][0]["production_notes"]) == {
        "底料要求",
        "印刷后模切",
    }
    assert first["plan_fingerprint"] == second["plan_fingerprint"]
    assert json.dumps(first, ensure_ascii=False, sort_keys=True, default=str) == json.dumps(
        second, ensure_ascii=False, sort_keys=True, default=str
    )
    assert before == after == 1


def test_distinct_formal_details_never_merge_even_when_code_and_size_match(
    production_print_app,
):
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
    from app.services.requisition_production_print import (
        build_supplier_requisition_production_package,
    )

    with production_print_app["session_factory"]() as db:
        order = db.get(
            SupplierRequisitionOrder,
            production_print_app["supplier_order_id"],
        )
        cover, base = sorted(order.items, key=lambda row: row.id)[:2]
        base.report_length_mm = cover.report_length_mm
        base.report_width_mm = cover.report_width_mm
        package = build_supplier_requisition_production_package(db, order)
        db.rollback()

    matching_cards = [
        card
        for card in package["cards"]
        if card["product_code"] == "P132A2-SPLIT"
    ]
    assert len(matching_cards) == 2
    assert len({card["supplier_order_item_id"] for card in matching_cards}) == 2
    assert all(len(card["components"]) == 1 for card in matching_cards)
    assert [card["component_label"] for card in matching_cards] == ["盖", "底"]
    assert [
        (
            card["components"][0]["report_length_mm"],
            card["components"][0]["report_width_mm"],
        )
        for card in matching_cards
    ] == [(700, 500), (700, 500)]


def test_internal_production_print_adds_current_customer_handoff_without_changing_plan_fingerprint(
    production_print_app,
):
    from app.models.delivery import Delivery
    from app.models.finance import ReturnReceipt
    from app.models.fulfillment_reminder import FulfillmentReminder
    from app.models.product import Product
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
    from app.models.user import User
    from app.services.requisition_production_print import (
        build_supplier_requisition_production_package,
    )

    with production_print_app["session_factory"]() as db:
        supplier = db.get(
            SupplierRequisitionOrder,
            production_print_app["supplier_order_id"],
        )
        product = db.get(Product, production_print_app["product_id"])
        actor = db.query(User).filter(User.username == "p132a2-admin").one()
        before = build_supplier_requisition_production_package(db, supplier)
        delivery = Delivery(
            delivery_number="DH-P165C-PRINT",
            customer_id=product.customer_id,
            delivery_date=date(2026, 8, 15),
            status="dispatched",
            total_quantity=1,
        )
        db.add(delivery)
        db.flush()
        receipt = ReturnReceipt(
            delivery_id=delivery.id,
            actual_received_date=date(2026, 8, 16),
            signed_by="王经理",
            status="confirmed",
            created_by=actor.id,
        )
        db.add(receipt)
        db.flush()
        db.add(
            FulfillmentReminder(
                source_return_receipt_id=receipt.id,
                source_return_receipt_id_snapshot=receipt.id,
                source_delivery_id_snapshot=delivery.id,
                source_delivery_number_snapshot=delivery.delivery_number,
                source_received_date_snapshot=receipt.actual_received_date,
                source_valid=True,
                customer_id=product.customer_id,
                customer_name_snapshot="半页任务单客户",
                product_id=product.id,
                product_id_snapshot=product.id,
                product_code_snapshot=product.product_code,
                product_name_snapshot=product.product_name,
                scope_type="product",
                reminder_type="production_attention",
                content="客户回单交代：首件生产后先留样核对",
                cadence="continuous",
                remind_on=date(2026, 8, 16),
                status="active",
                version=1,
                created_by=actor.id,
                created_by_name_snapshot=actor.display_name,
            )
        )
        db.commit()
        after = build_supplier_requisition_production_package(db, supplier)
        product_id = product.id

    assert after["plan_fingerprint"] == before["plan_fingerprint"]
    projected = [
        reminder
        for card in after["cards"]
        for reminder in card["fulfillment_reminders"]
    ]
    assert projected
    assert {row["content"] for row in projected} == {
        "客户回单交代：首件生产后先留样核对"
    }
    assert {row["product_id"] for row in projected} == {product_id}


def test_package_api_is_read_only_scoped_and_fails_closed(
    production_print_app,
):
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    app = production_print_app["app"]
    order_id = production_print_app["supplier_order_id"]
    with TestClient(app) as client:
        assert client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-print-package"
        ).status_code == 401
        _login(client, "p132a2-sales")
        assert client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-print-package"
        ).status_code == 403
        client.cookies.clear()
        _login(client, "p132a2-admin")
        success = client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-print-package"
        )
        assert success.status_code == 200, success.text
        assert success.json()["card_count"] == 5
        reported = client.get(
            "/api/requisition/reported-documents",
            params={"page": 1, "page_size": 20},
        )
        assert reported.status_code == 200, reported.text
        reported_order = next(
            row
            for row in reported.json()["items"]
            if row["source_type"] == "supplier_order" and row["id"] == order_id
        )
        assert reported_order["production_print_url"] == (
            f"/requisition-production-print.html?id={order_id}"
        )
        assert client.get(
            "/api/requisition/supplier-orders/999999/production-print-package"
        ).status_code == 404

        with production_print_app["session_factory"]() as db:
            order = db.get(SupplierRequisitionOrder, order_id)
            template = db.get(
                SupplierRequisitionOrderItem,
                production_print_app["supplier_item_id"],
            )
            for offset in range(5):
                db.add(
                    SupplierRequisitionOrderItem(
                        supplier_order_id=order.id,
                        order_item_id=template.order_item_id,
                        source_key=f"order_item:{template.order_item_id}",
                        product_id=template.product_id,
                        material_id=template.material_id,
                        material_code_snapshot=template.material_code_snapshot,
                        supplier_name_snapshot=template.supplier_name_snapshot,
                        layer_count_snapshot=template.layer_count_snapshot,
                        flute_type_snapshot=template.flute_type_snapshot,
                        order_number=template.order_number,
                        product_code=template.product_code,
                        product_name=f"额外物理组件{offset + 1}",
                        report_length_mm=template.report_length_mm,
                        report_width_mm=template.report_width_mm,
                        quantity=template.quantity,
                        requisition_qty=1,
                        stock_deduction_qty=0,
                        required_piece_qty=1,
                        cutting_mode="一开一",
                        pieces_per_box=1,
                        customer_name=template.customer_name,
                        delivery_date=template.delivery_date,
                    )
                )
            db.commit()
        expanded = client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-print-package"
        )
        assert expanded.status_code == 200, expanded.text
        expanded_package = expanded.json()
        assert expanded_package["card_count"] == 10
        assert expanded_package["page_count"] == 5
        assert len(
            {
                card["supplier_order_item_id"]
                for card in expanded_package["cards"]
            }
        ) == 10
        assert all(
            len(card["components"]) == 1
            for card in expanded_package["cards"]
        )

        with production_print_app["session_factory"]() as db:
            order = db.get(SupplierRequisitionOrder, order_id)
            order.status = "voided"
            db.commit()
        assert client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-print-package"
        ).status_code == 409


def test_legacy_missing_process_snapshot_uses_current_common_box_joining_method(
    production_print_app,
):
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
    from app.services.requisition_production_print import (
        build_supplier_requisition_production_package,
    )

    with production_print_app["session_factory"]() as db:
        order = db.get(
            SupplierRequisitionOrder,
            production_print_app["supplier_order_id"],
        )
        assert order is not None
        product = db.get(Product, production_print_app["product_id"])
        item = db.get(OrderItem, production_print_app["order_item_id"])
        assert product is not None
        assert item is not None
        product.production_process = None
        item.snapshot_production_notes = None
        db.flush()
        defaulted = build_supplier_requisition_production_package(db, order)
        defaulted_card = defaulted["cards"][0]
        assert defaulted_card["joining_method"] == "无需结合"
        assert defaulted_card["joining_method_source"] == "default_no_joining"

        product.production_process = "粘贴"
        item.snapshot_production_notes = None
        db.flush()

        fallback = build_supplier_requisition_production_package(db, order)
        fallback_card = fallback["cards"][0]
        assert fallback_card["joining_method"] == "粘贴"
        assert fallback_card["joining_method_source"] == (
            "current_common_box_fallback"
        )
        assert {
            row["joining_method_source"] for row in fallback_card["components"]
        } == {"current_common_box_fallback"}

        item.snapshot_production_notes = "打钉"
        db.flush()
        frozen = build_supplier_requisition_production_package(db, order)
        frozen_card = frozen["cards"][0]
        assert frozen_card["joining_method"] == "打钉"
        assert frozen_card["joining_method_source"] == "frozen_snapshot"


def test_task_version_change_marks_preprint_for_review(production_print_app):
    from app.models.production import ProductionTask
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
    from app.services.requisition_production_print import (
        build_supplier_requisition_production_package,
    )

    with production_print_app["session_factory"]() as db:
        order = db.get(
            SupplierRequisitionOrder,
            production_print_app["supplier_order_id"],
        )
        task = db.scalar(
            select(ProductionTask).where(
                ProductionTask.order_item_id == production_print_app["order_item_id"]
            )
        )
        task.version += 1
        task.updated_at = order.created_at + timedelta(seconds=1)
        db.commit()
        package = build_supplier_requisition_production_package(db, order)

    assert package["review_required"] is True
    assert any("生产任务版本已变化" in text for text in package["review_messages"])


def test_posted_receipt_marks_preprint_for_review(production_print_app):
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
    from app.services.requisition_production_print import (
        build_receipt_production_print_package,
        build_supplier_requisition_production_package,
    )

    with production_print_app["session_factory"]() as db:
        receipt = IncomingReceipt(
            receipt_number="IR-P132A2-001",
            status="posted",
            received_at=datetime(2026, 8, 10, 10, 0, 0),
            idempotency_key="p132a2-receipt-001",
        )
        db.add(receipt)
        db.flush()
        receipt_item = IncomingReceiptItem(
                receipt_id=receipt.id,
                order_id=production_print_app["sales_order_id"],
                order_item_id=production_print_app["order_item_id"],
                supplier_order_id=production_print_app["supplier_order_id"],
                supplier_order_item_id=production_print_app["supplier_item_id"],
                planned_quantity=100,
                received_quantity=80,
                cumulative_received_quantity=80,
                variance_quantity=-20,
                variance_type="short",
                resolution_status="pending",
                resolution_action="await_supplier",
                status="posted",
            )
        db.add(receipt_item)
        db.commit()
        order = db.get(
            SupplierRequisitionOrder,
            production_print_app["supplier_order_id"],
        )
        package = build_supplier_requisition_production_package(db, order)
        actual = build_receipt_production_print_package(db, receipt_item)

    assert package["review_required"] is True
    assert any("计划版不可冒充实收版" in text for text in package["review_messages"])
    assert actual is not None
    assert actual["paper_phase"] == "actual_receipt"
    assert actual["paper_version_key"] == f"actual:receipt:{receipt_item.id}"
    assert actual["reuses_planned_card"] is False
    assert actual["reprint_required"] is True
    assert actual["cards"][0]["received_sheet_quantity"] == 80
    assert actual["cards"][0]["production_capacity_quantity"] == 80


def test_single_exact_receipt_reuses_immutable_planned_card(production_print_app):
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
    from app.services.requisition_production_print import (
        build_receipt_production_print_package,
        build_supplier_requisition_production_package,
    )

    with production_print_app["session_factory"]() as db:
        order = db.get(
            SupplierRequisitionOrder,
            production_print_app["supplier_order_id"],
        )
        before = build_supplier_requisition_production_package(db, order)
        receipt = IncomingReceipt(
            receipt_number="IR-P164A-MATCHED",
            status="posted",
            received_at=datetime(2026, 8, 17, 9, 0, 0),
            idempotency_key="p164a-matched",
        )
        db.add(receipt)
        db.flush()
        receipt_item = IncomingReceiptItem(
            receipt_id=receipt.id,
            order_id=production_print_app["sales_order_id"],
            order_item_id=production_print_app["order_item_id"],
            supplier_order_id=production_print_app["supplier_order_id"],
            supplier_order_item_id=production_print_app["supplier_item_id"],
            planned_quantity=100,
            received_quantity=100,
            cumulative_received_quantity=100,
            variance_quantity=0,
            variance_type="matched",
            resolution_status="not_required",
            status="posted",
        )
        db.add(receipt_item)
        db.commit()
        after = build_supplier_requisition_production_package(db, order)
        received = build_receipt_production_print_package(db, receipt_item)

    assert after["plan_fingerprint"] == before["plan_fingerprint"]
    target = next(
        row
        for row in after["cards"]
        if row["supplier_order_item_id"]
        == production_print_app["supplier_item_id"]
    )
    assert target["receipt_match_status"] == "matched_reuse_plan"
    assert target["review_required"] is False
    assert received is not None
    assert received["paper_phase"] == "planned"
    assert received["reuses_planned_card"] is True
    assert received["reprint_required"] is False
    assert received["paper_version_key"] == target["paper_version_key"]
    assert received["paper_fingerprint"] == after["plan_fingerprint"]
    assert "received_sheet_quantity" not in received["cards"][0]


def test_split_receipts_keep_one_actual_card_per_fact(production_print_app):
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.services.requisition_production_print import (
        build_receipt_production_print_package,
    )

    facts = []
    with production_print_app["session_factory"]() as db:
        for index, (quantity, cumulative, variance_type) in enumerate(
            ((40, 40, "short"), (60, 100, "matched")),
            start=1,
        ):
            receipt = IncomingReceipt(
                receipt_number=f"IR-P164A-SPLIT-{index}",
                status="posted",
                received_at=datetime(2026, 8, 17, 10 + index, 0, 0),
                idempotency_key=f"p164a-split-{index}",
            )
            db.add(receipt)
            db.flush()
            fact = IncomingReceiptItem(
                receipt_id=receipt.id,
                order_id=production_print_app["sales_order_id"],
                order_item_id=production_print_app["order_item_id"],
                supplier_order_id=production_print_app["supplier_order_id"],
                supplier_order_item_id=production_print_app["supplier_item_id"],
                planned_quantity=100,
                received_quantity=quantity,
                cumulative_received_quantity=cumulative,
                variance_quantity=cumulative - 100,
                variance_type=variance_type,
                resolution_status=(
                    "pending" if cumulative < 100 else "not_required"
                ),
                resolution_action=("await_supplier" if cumulative < 100 else None),
                status="posted",
            )
            db.add(fact)
            facts.append(fact)
        db.commit()
        packages = [build_receipt_production_print_package(db, fact) for fact in facts]

    assert all(package is not None for package in packages)
    assert [package["cards"][0]["received_sheet_quantity"] for package in packages] == [
        40,
        60,
    ]
    assert [package["cards"][0]["paper_phase_label"] for package in packages] == [
        "分批实收版 1/2",
        "分批实收版 2/2",
    ]
    assert [package["paper_version_key"] for package in packages] == [
        f"actual:receipt:{facts[0].id}",
        f"actual:receipt:{facts[1].id}",
    ]


def test_print_page_and_erp_entry_use_one_layout_for_plan_and_receipt_phases():
    index_html = Path("static/index.html").read_text(encoding="utf-8")
    print_html = Path("static/requisition-production-print.html").read_text(
        encoding="utf-8"
    )
    main_source = Path("app/main.py").read_text(encoding="utf-8")

    assert "待来料任务单" in index_html
    assert "reportedItemDetail.can_view_supplier_order ? '查看采购单并定位本行' : '查看原单据'" in index_html
    assert 'new Set(["supplier_order", "composite_bom_requisition"])' in index_html
    assert "row.can_print_task !== true" in index_html
    assert "/production-print-package?item_ids=" in index_html
    assert "/api/requisition/supplier-orders/${orderId}/production-print-package" in index_html
    assert 'window.open(url, "_blank", "noopener")' in index_html
    assert "/requisition-production-print.html" in main_source
    assert "_conditional_file_endpoint(requisition_production_print_path)" in main_source
    assert "incoming_production_card_path = (" in main_source
    assert '/ "requisition-production-print.html"' in main_source
    assert "receiptMode" in print_html
    assert "/api/incoming/receipt-items/${encodeURIComponent(receiptItemId)}/production-card" in print_html
    assert "待来料计划版" in print_html
    assert "每个生产任务固定半张 A4" in print_html
    assert "@page { size:A4 portrait;" in print_html
    assert "grid-template-rows:140.5mm 140.5mm" in print_html
    assert 'class="task-card half-card blank"' in print_html
    assert 'class="page batch-page"' in print_html
    assert "single-page" not in print_html
    assert "cardNeedsFullPage" not in print_html
    assert "每页上下两款" in print_html
    assert "A1 型纸箱生产任务单" in print_html
    assert "模切内盒生产任务单" in print_html
    assert "衬板生产任务单" in print_html
    assert (
        ".production-key-value,.detail-row.production-key-fact .value { font-size:12pt;"
        in print_html
    )
    assert (
        ".task-card.printing-heavy .production-key-value,\n    .task-card.printing-heavy .detail-row.production-key-fact .value { font-size:9.5pt;"
        in print_html
    )
    assert 'detailRow("压线尺寸", crease, "production-key-fact")' in print_html
    assert (
        'detailRow("结合方式", card.joining_method || "无需结合", "production-key-fact")'
        in print_html
    )
    assert 'detailRow("是否粘贴", "不需要 / 待确认")' not in print_html
    assert '["工艺待确认"]' not in print_html
    liner_layout = print_html.split('if (card.layout_kind === "liner") {', 1)[1].split(
        'if (card.layout_kind === "die_cut") {', 1
    )[0]
    assert 'detailRow("开料方式", cutting)' in liner_layout
    assert "drawingReferenceText(card)" in print_html
    assert "图号 / 图纸版本" in print_html
    assert 'class="structure-body"' not in print_html
    assert '<object data="${escapeHtml(drawing.url)}"' not in print_html
    assert "请核对后再打印" in print_html
    assert "card.scrollHeight > card.clientHeight + 1" in print_html
    assert "任务内容超过页面容量，已停止打印" in print_html
    assert 'credentials:"include"' in print_html
    assert 'cache:"no-store"' in print_html
    assert "customer-safe" in print_html
    assert "internal-only" in print_html
    assert "window.opener" not in print_html
    assert "method: \"POST\"" not in print_html
    assert "method: \"PUT\"" not in print_html
    assert "method: \"DELETE\"" not in print_html
    for forbidden in ("单价", "成本", "库存批次", "可用库存"):
        assert forbidden not in print_html


def test_package_projects_explicit_box_layout_current_mold_and_secure_drawing(
    production_print_app,
):
    from app.models.mold_tool import MoldTool
    from app.models.product import Product
    from app.models.production import ProductionTask
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
    from app.services.requisition_production_print import (
        build_supplier_requisition_production_package,
    )

    with production_print_app["session_factory"]() as db:
        product = db.get(Product, production_print_app["product_id"])
        task = db.scalar(
            select(ProductionTask).where(
                ProductionTask.order_item_id == production_print_app["order_item_id"]
            )
        )
        mold = MoldTool(
            mold_code="MD-P132A2",
            mold_name="内盒模具",
            rack_location="1F-M-R02-L2-P08",
            is_active=True,
        )
        db.add(mold)
        db.flush()
        product.box_style = "模切内盒"
        product.box_category = "die_cut"
        product.mold_tool_id = mold.id
        product.printing_colors = "黑色"
        task.printing_plate_details_snapshot = json.dumps(
            [{"plate_code": "PLATE-01", "color_name": "黑色"}],
            ensure_ascii=False,
        )
        db.commit()

        order = db.get(
            SupplierRequisitionOrder,
            production_print_app["supplier_order_id"],
        )
        die_cut = build_supplier_requisition_production_package(db, order)
        card = die_cut["cards"][0]
        component = card["components"][0]

        assert card["layout_kind"] == "die_cut"
        assert card["box_style"] == "模切内盒"
        assert card["printing_colors"] == ["黑色"]
        assert component["mold_tool_id"] == mold.id
        assert component["mold_code"] == "MD-P132A2"
        assert component["mold_location"] == "1F-M-R02-L2-P08"
        assert component["mold_location_version"] == 1
        assert component["mold_binding_basis"] == "current_product_binding"
        assert component["drawing_kind"] == "pdf"
        assert component["drawing_url"] == (
            f"/api/orders/items/{production_print_app['order_item_id']}"
            "/drawing/content/file.pdf"
        )

        product.box_style = "衬板"
        product.box_category = "normal"
        product.production_label_enabled = True
        product.production_label_units_per_label = 50
        task.production_label_enabled_snapshot = True
        task.production_label_units_per_label_snapshot = 50
        task.production_label_total_quantity_snapshot = 200
        task.production_label_count_snapshot = 4
        for supplier_line in order.items:
            if supplier_line.order_item_id == production_print_app["order_item_id"]:
                supplier_line.cutting_mode = "一开12"
        db.commit()
        liner = build_supplier_requisition_production_package(db, order)
        liner_card = liner["cards"][0]

        assert liner_card["layout_kind"] == "liner"
        assert {row["cutting_mode"] for row in liner_card["components"]} == {"一开12"}
        assert liner_card["production_label_units_per_bundle"] == 50
        assert liner_card["estimated_bundle_count"] == 4


def test_p1_67_task_sheet_prioritizes_identity_fields_and_process_order() -> None:
    source = Path("static/requisition-production-print.html").read_text(
        encoding="utf-8"
    )
    card = source[source.index("function cardHtml"):source.index("function applyMode")]
    assert card.index("<span>存货编码</span>") < card.index("<span>产品名称</span>")
    strip = card[card.index('<div class="product-strip">'):]
    assert strip.index("成品内尺寸") < strip.index("图号 / 图纸版本")
    assert strip.index("图号 / 图纸版本") < strip.index("<span class=\"field-label\">交期")
    facts = source[source.index("function orderFactsHtml"):source.index("function productionNotesHtml")]
    assert facts.index("客户订单号") < facts.index("订单数量")
    assert facts.index("订单数量") < facts.index("库存抵扣")
    assert facts.index("库存抵扣") < facts.index("计划生产")
    assert facts.index("计划生产") < facts.index("采购张数")
    assert "生产数量" not in card
    assert "产品 / 存货编码" not in card

    process_steps = source[source.index("function processSteps"):source.index("function processHtml")]
    assert process_steps.index('add("模具")') < process_steps.index('add("印刷")')
    assert process_steps.index('add("印刷")') < process_steps.index('add("粘贴")')
    process_details = source[source.index("function processDetailsHtml"):source.index("function cardHtml")]
    assert process_details.index("moldHtml(card)") < process_details.index("printingHtml(card)")
    assert process_details.index("printingHtml(card)") < process_details.index("joiningHtml(card)")
    assert "模具编号与现场位置" in source
    assert "扫描模具上的固定二维码" in source
    assert "扫码不会自动开工" in source


def test_production_packaging_labels_deduplicate_split_rows_and_keep_remainder(
    production_print_app,
):
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.production import ProductionTask
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
    from app.services.production_packaging_label import (
        build_supplier_requisition_packaging_label_package,
    )
    from app.services.requisition_production_print import (
        build_supplier_requisition_production_package,
    )

    session_factory = production_print_app["session_factory"]
    with session_factory() as db:
        task = db.scalar(
            select(ProductionTask).where(
                ProductionTask.order_item_id == production_print_app["order_item_id"]
            )
        )
        item = db.get(OrderItem, task.order_item_id)
        assert item is not None
        product = db.get(Product, item.product_id)
        assert product is not None
        product.production_label_enabled = True
        product.production_label_units_per_label = 5
        task.production_label_enabled_snapshot = True
        task.production_label_units_per_label_snapshot = 5
        task.production_label_total_quantity_snapshot = 23
        task.production_label_count_snapshot = 5
        # Keep the print-plan version stable. SQLite's second-level on-update
        # timestamp otherwise makes this test depend on crossing a clock tick.
        task.updated_at = datetime(2026, 8, 10, 9, 0, 0)
        db.commit()
        order = db.get(
            SupplierRequisitionOrder,
            production_print_app["supplier_order_id"],
        )
        before = db.scalar(select(func.count()).select_from(ProductionTask))
        first = build_supplier_requisition_packaging_label_package(db, order)
        second = build_supplier_requisition_packaging_label_package(db, order)
        after = db.scalar(select(func.count()).select_from(ProductionTask))

    # Cover/base supplier rows share one ordinary production task and must not
    # duplicate its packaging-label plan.
    assert first["production_task_count"] == 1
    assert first["label_count"] == 5
    assert [row["quantity"] for row in first["labels"]] == [5, 5, 5, 5, 3]
    assert {row["customer_code"] for row in first["labels"]} == {"P132A2-A"}
    assert {row["production_task_id"] for row in first["labels"]} == {task.id}
    assert first["plan_fingerprint"] == second["plan_fingerprint"]
    assert before == after == 4
    with session_factory() as db:
        order = db.get(
            SupplierRequisitionOrder,
            production_print_app["supplier_order_id"],
        )
        task_sheet = build_supplier_requisition_production_package(db, order)
    assert task_sheet["production_label_task_count"] == 1
    assert task_sheet["production_label_count"] == 5
    serialized = json.dumps(first, ensure_ascii=False, sort_keys=True, default=str)
    for forbidden in (
        "inventory_lot",
        "location_id",
        "available_quantity",
        "reserved_quantity",
        "unit_price",
        "cost",
    ):
        assert forbidden not in serialized


def test_production_packaging_label_api_is_read_only_and_customer_scoped(
    production_print_app,
):
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.production import ProductionTask

    order_id = production_print_app["supplier_order_id"]
    with production_print_app["session_factory"]() as db:
        task = db.scalar(
            select(ProductionTask).where(
                ProductionTask.order_item_id == production_print_app["order_item_id"]
            )
        )
        item = db.get(OrderItem, task.order_item_id)
        assert item is not None
        product = db.get(Product, item.product_id)
        assert product is not None
        product.production_label_enabled = True
        product.production_label_units_per_label = 5
        task.production_label_enabled_snapshot = True
        task.production_label_units_per_label_snapshot = 5
        task.production_label_total_quantity_snapshot = 23
        task.production_label_count_snapshot = 5
        task.updated_at = datetime(2026, 8, 10, 9, 0, 0)
        db.commit()

    with TestClient(production_print_app["app"]) as client:
        _login(client, "p132a2-admin")
        first = client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-package"
        )
        second = client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-package"
        )
        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()
        assert first.json()["label_count"] == 5

    with TestClient(production_print_app["app"]) as client:
        _login(client, "p132a2-sales")
        forbidden = client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-package"
        )
        assert forbidden.status_code == 403
