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
    }
    engine.dispose()


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "123456"},
    )
    assert response.status_code == 200, response.text


def test_package_keeps_split_components_and_two_half_page_layout(
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
        three_card_package = build_supplier_requisition_production_package(db, order)
        db.rollback()
        after = db.scalar(
            select(func.count()).select_from(SupplierRequisitionOrder)
        )

    assert first["card_count"] == 4
    assert first["page_count"] == 2
    assert all(page["top"] and page["bottom"] for page in first["pages"])
    assert three_card_package["card_count"] == 3
    assert three_card_package["page_count"] == 2
    assert three_card_package["pages"][-1]["top"] is not None
    assert three_card_package["pages"][-1]["bottom"] is None
    split = first["cards"][0]
    assert split["product_name"] == "天地盖测试箱"
    assert split["planned_finished_quantity"] == 200
    assert split["requisition_quantity"] == 200
    assert [row["component_label"] for row in split["components"]] == ["盖", "底"]
    assert [row["report_length_mm"] for row in split["components"]] == [700, 650]
    assert split["components"][0]["print_content"] == "单色印刷"
    assert split["components"][0]["production_task_version"] == 2
    assert set(split["components"][1]["production_notes"]) == {
        "底料要求",
        "印刷后模切",
    }
    assert first["plan_fingerprint"] == second["plan_fingerprint"]
    assert json.dumps(first, ensure_ascii=False, sort_keys=True, default=str) == json.dumps(
        second, ensure_ascii=False, sort_keys=True, default=str
    )
    assert before == after == 1


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
        assert success.json()["card_count"] == 4
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
        overflow = client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-print-package"
        )
        assert overflow.status_code == 409
        assert "超过半页容量" in overflow.text

        with production_print_app["session_factory"]() as db:
            order = db.get(SupplierRequisitionOrder, order_id)
            order.status = "voided"
            db.commit()
        assert client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-print-package"
        ).status_code == 409


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
        db.add(
            IncomingReceiptItem(
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
        )
        db.commit()
        order = db.get(
            SupplierRequisitionOrder,
            production_print_app["supplier_order_id"],
        )
        package = build_supplier_requisition_production_package(db, order)

    assert package["review_required"] is True
    assert any("已有实收" in text for text in package["review_messages"])


def test_print_page_and_erp_entry_keep_purchase_and_receipt_prints_separate():
    index_html = Path("static/index.html").read_text(encoding="utf-8")
    print_html = Path("static/requisition-production-print.html").read_text(
        encoding="utf-8"
    )
    main_source = Path("app/main.py").read_text(encoding="utf-8")

    assert "待来料任务单" in index_html
    assert "row.source_type==='supplier_order' ? '采购单' : '打印'" in index_html
    assert "row.source_type==='supplier_order' && row.status==='confirmed'" in index_html
    assert 'window.open(url, "_blank", "noopener")' in index_html
    assert "/requisition-production-print.html" in main_source
    assert "_conditional_file_endpoint(requisition_production_print_path)" in main_source
    assert "@page { size:A4 portrait;" in print_html
    assert "grid-template-rows:140.5mm 140.5mm" in print_html
    assert 'class="half-card blank"' in print_html
    assert "计划成品" in print_html and "采购纸板" in print_html
    assert 'line.product_name || "-"' in print_html
    assert "计划已变化/请核对并重打" in print_html
    assert "card.scrollHeight > card.clientHeight + 1" in print_html
    assert "任务内容超过半页容量，已停止打印" in print_html
    assert "credentials: \"include\"" in print_html
    assert "cache: \"no-store\"" in print_html
    assert "window.opener" not in print_html
    assert "method: \"POST\"" not in print_html
    assert "method: \"PUT\"" not in print_html
    assert "method: \"DELETE\"" not in print_html
    for forbidden in ("单价", "成本", "库存批次", "可用库存"):
        assert forbidden not in print_html
