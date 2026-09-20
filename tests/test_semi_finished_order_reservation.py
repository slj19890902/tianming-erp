from __future__ import annotations

from collections.abc import Generator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import create_sqlite_engine
from app.core.security import hash_password
from app.models.access_control import UserPermissionOverride
from app.models import Base
from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.user import User
from app.models.warehouse_inventory import (
    InventoryLot,
    InventoryReservation,
    OrderItemSemiRequirement,
    SemiFinishedMatchRuleProduct,
    WarehouseLocation,
)
from app.services.semi_finished_inventory import (
    SemiFinishedLotVersion,
    consume_semi_finished_reservation,
)
from app.services.warehouse_inventory import (
    manual_finished_in,
    manual_semi_finished_in,
    replace_semi_finished_lot_allowed_products,
)


@pytest.fixture()
def b1_app(tmp_path: Path, seed_supplier_master):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.orders import router as orders_router
    from app.api.requisition import router as requisition_router
    from app.api.warehouse import router as warehouse_router

    engine = create_sqlite_engine(tmp_path / "semi-order-b1.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    seed_supplier_master(factory, "测试供应商", "B1-TEST")
    with factory() as db:
        users = [
            User(
                username=role,
                password_hash=hash_password("RolePass123!"),
                role=role,
                real_name=role,
                display_name=role,
                must_change_password=False,
            )
            for role in ("admin", "sales")
        ]
        customer = Customer(
            customer_number=9101,
            customer_code="B1-A",
            name="B1共享库存客户",
            payment_term_days=0,
            credit_limit=0,
        )
        other_customer = Customer(
            customer_number=9102,
            customer_code="B1-B",
            name="B1其他客户",
            payment_term_days=0,
            credit_limit=0,
        )
        db.add_all([*users, customer, other_customer])
        db.flush()
        sales = next(user for user in users if user.role == "sales")
        db.add_all(
            UserPermissionOverride(
                user_id=sales.id,
                permission_code=permission_code,
                is_allowed=True,
            )
            for permission_code in (
                "requisition.view",
                "requisition.execute",
                "warehouse.view",
            )
        )
        products = []
        for index in range(1, 4):
            products.append(
                Product(
                    customer_id=customer.id,
                    product_code=f"B1-P{index}",
                    customer_material_code=f"B1-M{index}",
                    product_name=f"共享规格印刷{index}",
                    box_category="normal",
                    box_style="普通箱",
                    legacy_material_text="A416D",
                    default_material_code="A416D",
                    flute_type="B",
                    layer_count=3,
                    report_length_mm=800,
                    report_width_mm=600,
                    splice_mode="single",
                    pieces_per_box=1,
                )
            )
        for index in range(4, 6):
            products.append(
                Product(
                    customer_id=customer.id,
                    product_code=f"B1-P{index}",
                    customer_material_code=f"B1-M{index}",
                    product_name=f"双拼产品{index}",
                    box_category="normal",
                    box_style="普通箱",
                    legacy_material_text="A416D",
                    default_material_code="A416D",
                    flute_type="B",
                    layer_count=3,
                    report_length_mm=800,
                    report_width_mm=600,
                    splice_mode="double",
                    pieces_per_box=2,
                )
            )
        products.append(
            Product(
                customer_id=customer.id,
                product_code="B1-TEL",
                customer_material_code="B1-TEL-M",
                product_name="天地盖产品",
                box_category="normal",
                box_style="A3 天地盖",
                legacy_material_text="A416D",
                default_material_code="A416D",
                flute_type="B",
                layer_count=3,
                report_length_mm=400,
                report_width_mm=300,
                base_report_length_mm=375,
                base_report_width_mm=275,
                splice_mode="single",
                pieces_per_box=1,
            )
        )
        semi_location = WarehouseLocation(
            location_code="B1-SI",
            location_name="B1半成品库位",
            warehouse_type="semi_finished",
        )
        finished_location = WarehouseLocation(
            location_code="B1-FG",
            location_name="B1成品库位",
            warehouse_type="finished",
        )
        db.add_all([*products, semi_location, finished_location])
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(orders_router, prefix="/api/orders")
    app.include_router(requisition_router, prefix="/api/requisition")
    app.include_router(warehouse_router, prefix="/api/warehouse")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    return app, factory


def login(client: TestClient, role: str = "sales") -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": role, "password": "RolePass123!"},
    )
    assert response.status_code == 200


def add_semi_lot(
    factory,
    *,
    product_index: int = 1,
    quantity: int,
    key: str,
    component_type: str = "whole",
    length: int = 800,
    width: int = 600,
    pieces_per_box: int = 1,
    stock_yield_per_sheet: int = 1,
    customer_id: int | None = 1,
    allowed_product_ids: list[int] | None = None,
    bind_product: bool = True,
):
    with factory() as db:
        location = db.scalar(
            select(WarehouseLocation).where(WarehouseLocation.location_code == "B1-SI")
        )
        lot = manual_semi_finished_in(
            db,
            location_id=location.id,
            quantity=quantity,
            stock_date=date.today(),
            source_type="production_surplus",
            material_code="A416D",
            layer_count=3,
            flute_type="B",
            board_length_mm=length,
            board_width_mm=width,
            sheet_type="net_sheet",
            component_type=component_type,
            pieces_per_box=pieces_per_box,
            stock_yield_per_sheet=stock_yield_per_sheet,
            supplier_name=None,
            customer_id=customer_id,
            crease_type=None,
            crease_left_mm=None,
            crease_middle_mm=None,
            crease_right_mm=None,
            cutting_note=None,
            remarks=None,
            operator_id=1,
            idempotency_key=key,
        )
        if bind_product:
            assert customer_id is not None, "通用批次必须显式 opt-out 硬绑定"
            lot = replace_semi_finished_lot_allowed_products(
                db,
                inventory_lot_id=lot.id,
                product_ids=(
                    [product_index]
                    if allowed_product_ids is None
                    else allowed_product_ids
                ),
                expected_version=lot.version,
                operator_id=1,
            )
        db.commit()
        return lot.id, lot.version


def add_finished_lot(factory, *, product_id: int, quantity: int, key: str):
    with factory() as db:
        location = db.scalar(
            select(WarehouseLocation).where(WarehouseLocation.location_code == "B1-FG")
        )
        lot = manual_finished_in(
            db,
            customer_id=1,
            product_id=product_id,
            location_id=location.id,
            quantity=quantity,
            stock_date=date.today(),
            source_type="manual",
            remarks=None,
            operator_id=1,
            idempotency_key=key,
        )
        db.commit()
        return lot.id, lot.version


def semi_plan(lot_id: int, version: int, requested: int, component: str = "whole"):
    return {
        "lot_id": lot_id,
        "expected_version": version,
        "requested_qty": requested,
        "component_type": component,
        "recommendation_source": "signature",
        "confirmed": True,
        "quantity_available": 999999,
        "warehouse_location": "伪造库位",
    }


def finished_plan(lot_id: int, version: int, requested: int):
    return {
        "lot_id": lot_id,
        "expected_version": version,
        "requested_qty": requested,
        "recommendation_source": "dedicated",
        "confirmed": True,
        "quantity_available": 999999,
        "warehouse_location": "伪造库位",
    }


def order_item(product_id: int, quantity: int, plan: dict | None = None, line: str = "L1"):
    row = {
        "client_line_id": line,
        "product_id": product_id,
        "quantity": quantity,
        "unit_price": "1.00",
    }
    if plan is not None:
        row["reservation_plan"] = plan
    return row


def post_order(client: TestClient, items: list[dict], po: str):
    return client.post(
        "/api/orders",
        json={
            "customer_id": 1,
            "customer_po": po,
            "order_date": "2026-07-10",
            "import_integrity_status": "ok",
            "items": items,
        },
    )


@pytest.mark.parametrize("style, reserved, expected", [("衬板", 5, 5), ("普通箱", 5, 0), ("衬板", 3, 0)])
def test_liner_direct_coverage_retains_source_without_completion(b1_app, style, reserved, expected):
    from app.models.production import ProductionCompletion
    from app.services.liner_direct_delivery import liner_direct_coverage
    app, factory = b1_app
    lot_id, version = add_semi_lot(factory, quantity=12, key="liner-direct")
    with factory() as db:
        product = db.get(Product, 1)
        product.box_style = style
        product.crease_type = "净料"
        lot = db.get(InventoryLot, lot_id)
        lot.semi_finished_detail.crease_type = "净料"
        original_location = lot.warehouse_location_id
        db.commit()
    with TestClient(app) as client:
        login(client)
        saved = post_order(client, [order_item(1, 5, {"semi": [semi_plan(lot_id, version, reserved)]})], "LINER-DIRECT")
    assert saved.status_code == 201, saved.text
    with factory() as db:
        item = db.scalar(select(OrderItem))
        assert liner_direct_coverage(db, item) == expected
        lot = db.get(InventoryLot, lot_id)
        assert lot.warehouse_location_id == original_location
        assert (lot.quantity_available, lot.quantity_reserved) == (12 - reserved, reserved)
        assert db.scalar(select(ProductionCompletion.id)) is None
        if expected:
            from app.models.production import ProductionTask
            task = db.scalar(select(ProductionTask).where(ProductionTask.order_item_id == item.id))
            assert task is not None
            previous_print = task.print_content_snapshot
            task.print_content_snapshot = '需印客户图案'
            db.flush()
            assert liner_direct_coverage(db, item) == 0
            task.print_content_snapshot = previous_print
            old_type = lot.semi_finished_detail.sheet_type
            lot.semi_finished_detail.sheet_type = 'raw_board'
            db.flush()
            assert liner_direct_coverage(db, item) == 0
            lot.semi_finished_detail.sheet_type = old_type
            db.flush()
            from app.api.deliveries import _pending_query, _delivery_remaining_quantity
            # Already accepted public stock may retain a different real material.
            reservation = db.scalar(select(InventoryReservation).where(InventoryReservation.order_item_id == item.id))
            lot.semi_finished_detail.owner_customer_id = None
            lot.semi_finished_detail.normalized_material_code = 'CCC'
            lot.semi_finished_detail.material_code_snapshot = 'CCC'
            lot.semi_finished_detail.crease_type = None
            reservation.warning_acknowledged_by = None
            db.flush()
            assert liner_direct_coverage(db, item) == 0
            reservation.warning_acknowledged_by = 1
            import json
            from app.models.warehouse_goods import WarehouseGoodsProfile
            db.add(WarehouseGoodsProfile(lot_id=lot.id,data_json=json.dumps(dict(scope='public',customer_ids=[],product_ids=[],processing='cut',mold_tool_id=None,verified_material_id=None,material_code='CCC'))))
            db.flush()
            assert liner_direct_coverage(db, item) == 5
            from app.services.production_workflow import list_production_tasks
            assert list_production_tasks(db, allowed_customer_ids={1}, status='pending',task_ids=[task.id]) == []
            assert db.execute(_pending_query(db=db, order_item_id=item.id)).first() is not None
            from app.api.deliveries import _PendingDeliveryReadContext, _pending_delivery_item_payload
            pending_rows = list(db.execute(_pending_query(db=db, order_item_id=item.id)))
            context = _PendingDeliveryReadContext(db,pending_rows)
            result = _pending_delivery_item_payload(db,row=pending_rows[0],registry={},context=context)
            assert result is not None
            assert context.remaining_quantity(db,item) == 5
            assert _delivery_remaining_quantity(db, item) == 5
            from app.models.delivery import Delivery, DeliveryItem
            from app.services.production_workflow import production_ready_quantity
            from app.services.semi_finished_inventory import consume_delivery_item_inventory, reverse_delivery_item_inventory
            delivery = Delivery(delivery_number="LINER-PICK", customer_id=1, delivery_date=date.today())
            db.add(delivery)
            db.flush()
            line = DeliveryItem(delivery_id=delivery.id, order_item_id=item.id, delivered_quantity=2)
            db.add(line)
            db.flush()
            consume_delivery_item_inventory(db, delivery_item_id=line.id, delivered_quantity_after_dispatch=2,
                                            operator_id=1, operation_key="liner-dispatch")
            item.delivered_quantity = 2
            db.flush()
            assert production_ready_quantity(db, item) == 5
            assert lot.quantity_reserved == 3
            assert lot.warehouse_location_id == original_location
            reverse_delivery_item_inventory(db, delivery_item_id=line.id, delivered_quantity_after_cancel=0,
                                            operator_id=1, operation_key="liner-reverse")
            item.delivered_quantity = 0
            db.flush()
            assert (lot.quantity_available, lot.quantity_reserved) == (7, 5)
            assert lot.semi_finished_detail.material_code_snapshot == 'CCC'
            assert db.scalar(select(ProductionCompletion.id)) is None


def test_customer_generic_exact_candidate_accepts_explicit_reservation_plan(
    b1_app,
) -> None:
    app, factory = b1_app
    lot_id, version = add_semi_lot(
        factory,
        quantity=12,
        key="customer-generic-direct",
    )
    with factory() as db:
        product = db.get(Product, 1)
        lot = db.get(InventoryLot, lot_id)
        product.crease_type = "净料"
        assert lot.semi_finished_detail is not None
        lot.semi_finished_detail.customer_generic_eligible = True
        lot.semi_finished_detail.sheet_type = "net_sheet"
        lot.semi_finished_detail.crease_type = "净料"
        db.commit()
    candidate_payload = {
        "customer_id": 1,
        "board_length_mm": 800,
        "board_width_mm": 600,
        "material_code": "A416D",
        "flute_type": "B",
        "component_type": "whole",
        "pieces_per_box": 1,
        "stock_yield_per_sheet": 1,
        "layer_count": 3,
        "crease_type": "净料",
    }
    with TestClient(app) as client:
        login(client)
        candidates = client.post(
            "/api/warehouse/semi-finished/products/1/candidates",
            json=candidate_payload,
        )
        assert candidates.status_code == 200, candidates.text
        candidate = next(
            row for row in candidates.json()["items"] if row["lot_id"] == lot_id
        )
        assert candidate["source"] == "customer_generic"
        assert candidate["direct_deduction_eligible"] is True
        direct_plan = semi_plan(lot_id, version, 5)
        direct_plan.update(
            {
                "recommendation_source": "customer_generic",
                "direct_deduction": True,
                "warning_acknowledged_codes": [
                    "CUSTOMER_GENERIC_SEMI_FINISHED_STOCK"
                ],
            }
        )
        saved = post_order(
            client,
            [order_item(1, 5, {"semi": [direct_plan]})],
            "B1-CUSTOMER-GENERIC-DIRECT",
        )
    assert saved.status_code == 201, saved.text
    with factory() as db:
        lot = db.get(InventoryLot, lot_id)
        assert (lot.quantity_available, lot.quantity_reserved) == (7, 5)


@pytest.mark.parametrize("role", ["sales", "admin"])
def test_pdf_adopt_customer_generic_difference_saves_without_checkbox(b1_app, role):
    import json
    import shutil
    import subprocess

    app, factory = b1_app
    lot_id, version = add_semi_lot(factory, quantity=12, key="pdf-adopt-difference")
    with factory() as db:
        lot = db.get(InventoryLot, lot_id)
        lot.semi_finished_detail.customer_generic_eligible = True
        lot.semi_finished_detail.material_code = "B416D"
        db.commit()
    payload = dict(customer_id=1, board_length_mm=800, board_width_mm=600,
                   material_code="A416D", flute_type="B", component_type="whole",
                   pieces_per_box=1, stock_yield_per_sheet=1, layer_count=3)
    with TestClient(app) as client:
        login(client, role)
        response = client.post("/api/warehouse/semi-finished/products/1/candidates", json=payload)
        assert response.status_code == 200, response.text
        candidate = next(row for row in response.json()["items"] if row["lot_id"] == lot_id)
        assert candidate["source"] == "customer_generic"
        assert not candidate["direct_deduction_eligible"]
        run = subprocess.run([shutil.which("node"), "tests/pdf_inventory_adopt_harness.cjs"],
                             input=json.dumps(candidate), text=True, encoding="utf-8",
                             capture_output=True, cwd=Path(__file__).resolve().parents[1])
        assert run.returncode == 0, run.stderr
        plan = json.loads(run.stdout)
        forged = json.loads(run.stdout)
        forged["semi"][0]["override"] = False
        rejected = post_order(client, [order_item(1, 5, forged)], "PDF-NOT-ADOPTED")
        assert rejected.status_code == 409, rejected.text
        saved = post_order(client, [order_item(1, 5, plan)], "PDF-ADOPTED")
        assert saved.status_code == 201, saved.text
    with factory() as db:
        lot = db.get(InventoryLot, lot_id)
        assert (lot.quantity_available, lot.quantity_reserved) == (7, 5)
        assert db.scalar(select(Order.id).where(Order.customer_po == "PDF-NOT-ADOPTED")) is None


def test_direct_plan_rejects_changed_sheet_type_and_wrong_cutting_yield(
    b1_app,
) -> None:
    app, factory = b1_app
    lot_id, version = add_semi_lot(
        factory,
        quantity=12,
        key="direct-stale-physical",
        stock_yield_per_sheet=2,
    )
    with factory() as db:
        product = db.get(Product, 1)
        product.crease_type = "净料"
        product.default_cutting_mode = "一开一"
        lot = db.get(InventoryLot, lot_id)
        assert lot.semi_finished_detail is not None
        lot.semi_finished_detail.crease_type = "净料"
        db.commit()
    forged = semi_plan(lot_id, version, 5)
    forged["direct_deduction"] = True
    with TestClient(app) as client:
        login(client)
        response = post_order(
            client,
            [order_item(1, 5, {"semi": [forged]})],
            "B1-DIRECT-WRONG-YIELD",
        )
    assert response.status_code == 409
    assert "完全匹配资格" in response.json()["detail"]
    with factory() as db:
        assert db.scalar(
            select(Order.id).where(Order.customer_po == "B1-DIRECT-WRONG-YIELD")
        ) is None
        lot = db.get(InventoryLot, lot_id)
        assert (lot.quantity_available, lot.quantity_reserved, lot.version) == (
            12,
            0,
            version,
        )


def test_old_payload_and_pdf_shaped_payload_only_reserve_on_final_post(b1_app) -> None:
    app, factory = b1_app
    lot_id, version = add_semi_lot(
        factory, quantity=10, key="draft-final-lot"
    )
    with factory() as db:
        assert db.scalar(select(InventoryReservation.id)) is None
    with TestClient(app) as client:
        login(client)
        old = post_order(client, [order_item(1, 3, plan=None)], "B1-OLD")
        assert old.status_code == 201, old.text
        with factory() as db:
            assert db.scalar(select(InventoryReservation.id)) is None
        pdf_final = post_order(
            client,
            [
                order_item(
                    1,
                    5,
                    {"semi": [semi_plan(lot_id, version, 5)]},
                    line="PDF-LINE-1",
                )
            ],
            "B1-PDF",
        )
    assert pdf_final.status_code == 201, pdf_final.text
    assert pdf_final.json()["items"][0]["client_line_id"] == "PDF-LINE-1"
    with factory() as db:
        reservation = db.scalar(
            select(InventoryReservation).where(
                InventoryReservation.reservation_type == "semi_order"
            )
        )
        assert reservation.credited_requirement_quantity == 5


def test_finished_draft_candidates_are_product_customer_scoped(b1_app) -> None:
    app, factory = b1_app
    dedicated_id, dedicated_version = add_finished_lot(
        factory, product_id=1, quantity=3, key="draft-finished-dedicated"
    )
    general_id, _ = add_finished_lot(
        factory, product_id=1, quantity=2, key="draft-finished-general"
    )
    with factory() as db:
        general = db.get(InventoryLot, general_id)
        general.finished_detail.is_general = True
        general.finished_detail.owner_customer_id = None
        db.commit()
    with TestClient(app) as client:
        login(client)
        response = client.get(
            "/api/warehouse/finished/products/1/candidates?customer_id=1"
        )
        saved = post_order(
            client,
            [
                order_item(
                    1,
                    2,
                    {"finished": [finished_plan(dedicated_id, dedicated_version, 2)]},
                )
            ],
            "B1-FINISHED-EXACT",
        )
    assert response.status_code == 200
    by_id = {row["lot_id"]: row for row in response.json()["items"]}
    assert set(by_id) == {dedicated_id}
    assert by_id[dedicated_id]["warning_codes"] == []
    assert saved.status_code == 201, saved.text
    with factory() as db:
        reservation = db.scalar(
            select(InventoryReservation).where(
                InventoryReservation.reservation_type == "finished_order"
            )
        )
        assert reservation.inventory_lot_id == dedicated_id
        assert reservation.reserved_stock_quantity == 2
        assert db.get(InventoryLot, general_id).quantity_available == 2


def test_new_order_finished_reservation_ignores_reused_deleted_order_ids(
    b1_app,
) -> None:
    app, factory = b1_app
    lot_id, version = add_finished_lot(
        factory,
        product_id=1,
        quantity=5,
        key="reused-id-finished-lot",
    )
    with factory() as db:
        stale_order = Order(
            order_number="TM-LEGACY-IDEMPOTENCY",
            customer_id=1,
            customer_po="LEGACY-IDEMPOTENCY",
            order_date=date(2026, 7, 9),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("1"),
        )
        db.add(stale_order)
        db.flush()
        stale_item = OrderItem(
            order_id=stale_order.id,
            product_id=1,
            item_sequence=1,
            item_order_number="TM-LEGACY-IDEMPOTENCY-001",
            quantity=1,
            unit_price=Decimal("1"),
            subtotal=Decimal("1"),
            material_status="pending",
            requisition_status="未报料",
            snapshot_product_code="B1-P1",
            snapshot_product_name="legacy reservation owner",
        )
        db.add(stale_item)
        db.flush()
        stale_order_id = stale_order.id
        stale_item_id = stale_item.id
        legacy_key = (
            f"order-{stale_order_id}-item-{stale_item_id}-finished-1"
        )
        db.add(
            InventoryReservation(
                reservation_number="RS-LEGACY-IDEMPOTENCY",
                inventory_lot_id=lot_id,
                reservation_type="finished_order",
                order_id=stale_order_id,
                order_item_id=stale_item_id,
                reserved_stock_quantity=1,
                credited_requirement_quantity=1,
                yield_factor=1,
                released_stock_quantity=1,
                released_requirement_quantity=1,
                status="released",
                idempotency_key=legacy_key,
            )
        )
        db.commit()
        db.delete(stale_order)
        db.commit()
        historical = db.scalar(
            select(InventoryReservation).where(
                InventoryReservation.idempotency_key == legacy_key
            )
        )
        assert historical is not None
        assert historical.order_id is None
        assert historical.order_item_id is None

    with TestClient(app) as client:
        login(client)
        saved = post_order(
            client,
            [
                order_item(
                    1,
                    2,
                    {
                        "finished": [
                            finished_plan(lot_id, version, 2)
                        ]
                    },
                    line="FRESH-CLIENT-LINE",
                )
            ],
            "B1-REUSED-ID-FIRST-SAVE",
        )

    assert saved.status_code == 201, saved.text
    assert saved.json()["id"] == stale_order_id
    assert saved.json()["items"][0]["id"] == stale_item_id
    with factory() as db:
        reservations = db.scalars(
            select(InventoryReservation).order_by(InventoryReservation.id)
        ).all()
        assert [row.status for row in reservations] == ["released", "active"]
        assert reservations[0].idempotency_key == legacy_key
        assert reservations[1].idempotency_key.startswith(
            "order-create-finished-"
        )
        assert reservations[1].idempotency_key != legacy_key
        assert reservations[1].order_item_id == stale_item_id


def test_warehouse_assigns_one_semi_lot_signature_to_multiple_product_codes(
    b1_app,
) -> None:
    app, factory = b1_app
    lot_id, _version = add_semi_lot(
        factory, quantity=30, key="warehouse-assignment-api"
    )
    with factory() as db:
        product = db.get(Product, 1)
        product.die_cut_path = "三楼刀模架 A-08"
        other_product = Product(
            customer_id=2,
            product_code="B1-OTHER-P1",
            customer_material_code="B1-OTHER-M1",
            product_name="其他客户产品",
            box_category="normal",
            box_style="普通箱",
            is_active=True,
        )
        db.add(other_product)
        db.commit()
        other_product_id = other_product.id

    with TestClient(app) as client:
        login(client, "admin")
        initial = client.get(
            f"/api/warehouse/lots/{lot_id}/product-assignments"
        )
        updated = client.put(
            f"/api/warehouse/lots/{lot_id}/product-assignments",
            json={
                "expected_version": initial.json()["version"],
                "product_ids": [1, 2, 3],
            },
        )
        filtered = client.get(
            f"/api/warehouse/lots/{lot_id}/product-assignments",
            params={"q": "B1-P1"},
        )
        cross_customer = client.put(
            f"/api/warehouse/lots/{lot_id}/product-assignments",
            json={
                "expected_version": updated.json()["version"],
                "product_ids": [other_product_id],
            },
        )

    assert initial.status_code == 200, initial.text
    initial_by_code = {row["product_code"]: row for row in initial.json()["items"]}
    assert initial_by_code["B1-P1"]["recommended"] is True
    assert initial_by_code["B1-P1"]["template_location"] == "三楼刀模架 A-08"
    assert updated.status_code == 200, updated.text
    assert updated.json()["assigned_product_ids"] == [1, 2, 3]
    assert {
        row["product_code"]
        for row in updated.json()["items"]
        if row["assigned"]
    } == {"B1-P1", "B1-P2", "B1-P3"}
    assert filtered.status_code == 200
    assert [row["product_code"] for row in filtered.json()["items"]] == ["B1-P1"]
    assert cross_customer.status_code == 409
    assert "同一客户" in cross_customer.json()["detail"]


def test_warehouse_lot_keyword_searches_location_and_product_text(b1_app) -> None:
    app, factory = b1_app
    semi_id, _ = add_semi_lot(
        factory, quantity=12, key="warehouse-location-keyword"
    )
    finished_id, _ = add_finished_lot(
        factory, product_id=1, quantity=8, key="warehouse-product-keyword"
    )
    with TestClient(app) as client:
        login(client, "admin")
        by_location = client.get(
            "/api/warehouse/lots", params={"keyword": "B1半成品库位"}
        )
        by_product = client.get(
            "/api/warehouse/lots", params={"keyword": "B1-P1"}
        )
    assert by_location.status_code == 200
    assert {row["id"] for row in by_location.json()["items"]} == {semi_id}
    assert by_product.status_code == 200
    assert {row["id"] for row in by_product.json()["items"]} == {
        semi_id,
        finished_id,
    }


def test_warehouse_searches_registered_template_location(b1_app) -> None:
    app, factory = b1_app
    with factory() as db:
        product = db.get(Product, 2)
        product.die_cut_path = "二楼模板架 B-12"
        db.commit()
    with TestClient(app) as client:
        login(client, "admin")
        by_code = client.get(
            "/api/warehouse/references/template-locations", params={"q": "B1-P2"}
        )
        by_location = client.get(
            "/api/warehouse/references/template-locations", params={"q": "B-12"}
        )
        empty = client.get("/api/warehouse/references/template-locations")
    assert by_code.status_code == 200
    assert by_code.json()["items"][0]["template_location"] == "二楼模板架 B-12"
    assert by_location.status_code == 200
    assert by_location.json()["items"][0]["product_code"] == "B1-P2"
    assert empty.status_code == 200
    assert empty.json()["items"] == []


def test_forged_general_finished_plan_is_rejected(b1_app) -> None:
    app, factory = b1_app
    general_id, general_version = add_finished_lot(
        factory, product_id=1, quantity=4, key="forged-general-finished"
    )
    with factory() as db:
        general = db.get(InventoryLot, general_id)
        general.finished_detail.is_general = True
        general.finished_detail.owner_customer_id = None
        db.commit()
    plan = finished_plan(general_id, general_version, 2)
    plan.update(
        {
            "warning_acknowledged_codes": ["GENERAL_FINISHED_STOCK"],
            "is_general": False,
            "customer_id": 1,
        }
    )
    with TestClient(app) as client:
        login(client)
        response = post_order(
            client,
            [order_item(1, 2, {"finished": [plan]})],
            "B1-FORGED-GENERAL",
        )
    assert response.status_code == 409
    assert "不能使用通用成品库存" in response.json()["detail"]
    with factory() as db:
        assert db.scalar(
            select(Order.id).where(Order.customer_po == "B1-FORGED-GENERAL")
        ) is None
        assert db.scalar(
            select(InventoryReservation.id).where(
                InventoryReservation.inventory_lot_id == general_id
            )
        ) is None
        assert db.get(InventoryLot, general_id).quantity_available == 4


def test_draft_manual_inventory_browser_is_customer_and_component_scoped(
    b1_app,
) -> None:
    app, factory = b1_app
    manual_id, manual_version = add_semi_lot(
        factory,
        quantity=4,
        key="draft-manual-same-customer",
        length=900,
        width=700,
    )
    add_semi_lot(
        factory,
        quantity=4,
        key="draft-manual-other-customer",
        customer_id=2,
        length=900,
        width=700,
        bind_product=False,
    )
    add_semi_lot(
        factory,
        quantity=4,
        key="draft-manual-unowned",
        customer_id=None,
        length=900,
        width=700,
        bind_product=False,
    )
    add_semi_lot(
        factory,
        quantity=4,
        key="draft-manual-wrong-component",
        component_type="cover",
        length=900,
        width=700,
    )
    payload = {
        "customer_id": 1,
        "board_length_mm": 800,
        "board_width_mm": 600,
        "material_code": "A416D",
        "flute_type": "B",
        "component_type": "whole",
        "pieces_per_box": 1,
        "stock_yield_per_sheet": 1,
    }
    with TestClient(app) as client:
        login(client)
        recommended = client.post(
            "/api/warehouse/semi-finished/products/1/candidates",
            json=payload,
        )
        browsed = client.post(
            "/api/warehouse/semi-finished/products/1/inventory",
            json=payload,
        )
        forged = client.post(
            "/api/warehouse/semi-finished/products/1/inventory",
            json={**payload, "customer_id": 2},
        )
        forged_manual_entry = semi_plan(manual_id, manual_version, 4)
        forged_manual_entry["recommendation_source"] = "manual"
        forged_manual_entry["override"] = True
        forged_manual = post_order(
            client,
            [order_item(1, 4, {"semi": [forged_manual_entry]})],
            "B1-DRAFT-MANUAL-FORGED",
        )
        assert forged_manual.status_code == 409
        assert "签名差异警告" in forged_manual.json()["detail"]
        with factory() as db:
            assert db.scalar(
                select(Order.id).where(
                    Order.customer_po == "B1-DRAFT-MANUAL-FORGED"
                )
            ) is None
            assert db.scalar(select(InventoryReservation.id)) is None
            assert db.scalar(select(SemiFinishedMatchRuleProduct.id)) is None
            lot = db.get(InventoryLot, manual_id)
            assert (lot.quantity_available, lot.quantity_reserved, lot.version) == (
                4,
                0,
                manual_version,
            )
        manual_entry = dict(forged_manual_entry)
        manual_entry["warning_acknowledged_codes"] = [
            "SEMI_SIGNATURE_OVERRIDE"
        ]
        saved = post_order(
            client,
            [order_item(1, 4, {"semi": [manual_entry]})],
            "B1-DRAFT-MANUAL-LEARN",
        )
    assert recommended.status_code == 200
    assert recommended.json()["items"] == []
    assert browsed.status_code == 200, browsed.text
    assert [row["lot_id"] for row in browsed.json()["items"]] == [manual_id]
    row = browsed.json()["items"][0]
    assert row["version"] == manual_version
    assert row["source"] == "manual"
    assert row["component_type"] == "whole"
    assert row["warehouse_location"]["location_code"] == "B1-SI"
    assert row["stock_yield_per_sheet"] == 1
    assert row["signature_differences"]
    assert "SEMI_SIGNATURE_OVERRIDE" in row["warning_codes"]
    assert forged.status_code == 409
    assert "产品不属于所选客户" in forged.json()["detail"]
    assert saved.status_code == 201, saved.text
    with factory() as db:
        mapping = db.scalar(
            select(SemiFinishedMatchRuleProduct).where(
                SemiFinishedMatchRuleProduct.product_id == 1
            )
        )
        assert mapping is not None


def test_learned_difference_requires_client_warning_acknowledgement(
    b1_app,
) -> None:
    app, factory = b1_app
    first_id, first_version = add_semi_lot(
        factory,
        quantity=1,
        key="learned-warning-first",
        length=900,
        width=700,
    )
    manual = semi_plan(first_id, first_version, 1)
    manual.update(
        {
            "recommendation_source": "manual",
            "override": True,
            "warning_acknowledged_codes": ["SEMI_SIGNATURE_OVERRIDE"],
        }
    )
    payload = {
        "customer_id": 1,
        "board_length_mm": 800,
        "board_width_mm": 600,
        "material_code": "A416D",
        "flute_type": "B",
        "component_type": "whole",
        "pieces_per_box": 1,
        "stock_yield_per_sheet": 1,
    }
    with TestClient(app) as client:
        login(client)
        learned_first = post_order(
            client,
            [order_item(1, 1, {"semi": [manual]})],
            "B1-LEARNED-WARNING-FIRST",
        )
        assert learned_first.status_code == 201, learned_first.text
        second_id, second_version = add_semi_lot(
            factory,
            quantity=2,
            key="learned-warning-second",
            length=900,
            width=700,
        )
        candidates = client.post(
            "/api/warehouse/semi-finished/products/1/candidates",
            json=payload,
        )
        candidate = next(
            row for row in candidates.json()["items"] if row["lot_id"] == second_id
        )
        assert candidate["source"] == "learned"
        assert candidate["signature_differences"]
        learned = semi_plan(second_id, second_version, 1)
        learned.update(
            {
                "recommendation_source": "learned",
                "match_rule_id": candidate["match_rule_id"],
                "override": True,
            }
        )
        forged = post_order(
            client,
            [order_item(1, 1, {"semi": [learned]})],
            "B1-LEARNED-WARNING-FORGED",
        )
        assert forged.status_code == 409
        assert "签名差异警告" in forged.json()["detail"]
        acknowledged = dict(learned)
        acknowledged["warning_acknowledged_codes"] = [
            "SEMI_SIGNATURE_OVERRIDE"
        ]
        saved = post_order(
            client,
            [order_item(1, 1, {"semi": [acknowledged]})],
            "B1-LEARNED-WARNING-OK",
        )
    assert saved.status_code == 201, saved.text
    with factory() as db:
        assert db.scalar(
            select(Order.id).where(
                Order.customer_po == "B1-LEARNED-WARNING-FORGED"
            )
        ) is None
        second_lot = db.get(InventoryLot, second_id)
        assert (second_lot.quantity_available, second_lot.quantity_reserved) == (1, 1)


def test_shared_pool_allocates_in_payload_line_order_50_30_20(b1_app) -> None:
    app, factory = b1_app
    lot_id, version = add_semi_lot(
        factory,
        quantity=100,
        key="shared-order-100",
        allowed_product_ids=[1, 2, 3],
    )
    items = [
        order_item(
            product_id,
            quantity,
            {"semi": [semi_plan(lot_id, version, quantity)]},
            line=f"LINE-{product_id}",
        )
        for product_id, quantity in ((1, 50), (2, 30), (3, 30))
    ]
    with factory() as db:
        product_codes = [
            db.get(Product, product_id).product_code for product_id in (1, 2, 3)
        ]
        assert len(set(product_codes)) == 3
    with TestClient(app) as client:
        login(client)
        response = post_order(client, items, "B1-SHARED")
    assert response.status_code == 201, response.text
    with factory() as db:
        rows = db.scalars(
            select(InventoryReservation)
            .where(InventoryReservation.reservation_type == "semi_order")
            .order_by(InventoryReservation.order_item_id)
        ).all()
        assert [row.credited_requirement_quantity for row in rows] == [50, 30, 20]
        lot = db.get(InventoryLot, lot_id)
        assert (lot.quantity_available, lot.quantity_reserved) == (0, 100)


def test_external_version_conflict_rolls_back_order_product_rule_and_reservation(
    b1_app,
) -> None:
    app, factory = b1_app
    lot_id, version = add_semi_lot(
        factory, quantity=10, key="external-conflict-lot"
    )
    with factory() as db:
        lot = db.get(InventoryLot, lot_id)
        lot.version += 1
        db.commit()
    item = {
        "client_line_id": "AUTO-LINE",
        "is_new_product": True,
        "product_code": "AUTO-B1-CONFLICT",
        "product_name": "自动产品回滚测试",
        "specification": "800×600×200mm",
        "material": "A416D",
        "layer_count": 3,
        "flute_type": "B",
        "quantity": 5,
        "unit_price": "1",
        "reservation_plan": {"semi": [semi_plan(lot_id, version, 5)]},
    }
    with TestClient(app) as client:
        login(client)
        response = post_order(client, [item], "B1-CONFLICT")
    assert response.status_code == 409
    with factory() as db:
        assert db.scalar(select(Order.id).where(Order.customer_po == "B1-CONFLICT")) is None
        assert db.scalar(
            select(Product.id).where(Product.product_code == "AUTO-B1-CONFLICT")
        ) is None
        assert db.scalar(select(SemiFinishedMatchRuleProduct.id)) is None
        assert db.scalar(select(InventoryReservation.id)) is None


def test_late_plan_failure_rolls_back_earlier_learning_and_reservation(b1_app) -> None:
    app, factory = b1_app
    lot_id, version = add_semi_lot(
        factory, quantity=10, key="late-failure-lot"
    )
    forged_learned = semi_plan(lot_id, version, 5)
    forged_learned["recommendation_source"] = "learned"
    forged_learned["match_rule_id"] = 999999
    with TestClient(app) as client:
        login(client)
        response = post_order(
            client,
            [
                order_item(
                    1,
                    5,
                    {"semi": [semi_plan(lot_id, version, 5)]},
                    "ROLLBACK-1",
                ),
                order_item(
                    1,
                    5,
                    {"semi": [forged_learned]},
                    "ROLLBACK-2",
                ),
            ],
            "B1-LATE-ROLLBACK",
        )
    assert response.status_code == 409
    with factory() as db:
        assert db.scalar(
            select(Order.id).where(Order.customer_po == "B1-LATE-ROLLBACK")
        ) is None
        assert db.scalar(select(InventoryReservation.id)) is None
        assert db.scalar(select(SemiFinishedMatchRuleProduct.id)) is None
        lot = db.get(InventoryLot, lot_id)
        assert (lot.quantity_available, lot.quantity_reserved, lot.version) == (
            10,
            0,
            version,
        )


def test_finished_then_semi_coverage_and_cutting_formulas_are_server_calculated(
    b1_app,
) -> None:
    from app.api.requisition import _current_requisition_requirements

    app, factory = b1_app
    finished_id, finished_version = add_finished_lot(
        factory, product_id=4, quantity=9, key="formula-finished-9"
    )
    semi_id, semi_version = add_semi_lot(
        factory,
        quantity=1,
        key="formula-semi-1",
        pieces_per_box=2,
        allowed_product_ids=[4],
    )
    plan = {
        "finished": [
            {
                "lot_id": finished_id,
                "expected_version": finished_version,
                "requested_qty": 9,
                "recommendation_source": "dedicated",
                "confirmed": True,
            }
        ],
        "semi": [semi_plan(semi_id, semi_version, 1)],
    }
    with TestClient(app) as client:
        login(client)
        response = post_order(
            client, [order_item(4, 10, plan, "FORMULA")], "B1-FORMULA"
        )
        cutting_order = post_order(
            client, [order_item(1, 5, None, "CUTTING")], "B1-CUTTING"
        )
        pending = client.get("/api/requisition/pending")
    assert response.status_code == 201, response.text
    row = next(
        item for item in pending.json()["items"] if item["product_code"] == "B1-P4"
    )
    assert (
        row["finished_inventory_reserved_qty"],
        row["production_required_qty"],
        row["required_piece_qty"],
        row["semi_finished_reserved_piece_qty"],
        row["remaining_required_piece_qty"],
        row["requisition_qty"],
    ) == (9, 1, 2, 1, 1, 1)
    with factory() as db:
        item = db.get(OrderItem, cutting_order.json()["items"][0]["id"])
        one_two = _current_requisition_requirements(
            db, item, cutting_mode="一开二"
        )
        one_three = _current_requisition_requirements(
            db, item, cutting_mode="一开三"
        )
        assert one_two["requisition_qty"] == 3
        assert one_three["requisition_qty"] == 2


def test_semi_full_coverage_is_excluded_from_pending_and_preview(b1_app) -> None:
    app, factory = b1_app
    finished_id, finished_version = add_finished_lot(
        factory, product_id=5, quantity=9, key="formula-zero-finished"
    )
    semi_id, semi_version = add_semi_lot(
        factory,
        quantity=2,
        key="formula-zero-semi",
        pieces_per_box=2,
        allowed_product_ids=[5],
    )
    plan = {
        "finished": [{
            "lot_id": finished_id,
            "expected_version": finished_version,
            "requested_qty": 9,
            "recommendation_source": "dedicated",
            "confirmed": True,
        }],
        "semi": [semi_plan(semi_id, semi_version, 2)],
    }
    with TestClient(app) as client:
        login(client)
        created = post_order(client, [order_item(5, 10, plan)], "B1-ZERO")
        pending = client.get("/api/requisition/pending")
        suggestions = client.get("/api/requisition/merge-suggestions")
        item_id = created.json()["items"][0]["id"]
        preview = client.post(
            "/api/requisition/supplier-orders/preview-from-pending-selection",
            json={
                "selections": [
                    {
                        "type": "order_item",
                        "order_item_id": item_id,
                        "supplier_name": "B1-SUPPLIER",
                        "report_length_mm": 800,
                        "report_width_mm": 600,
                    }
                ]
            },
        )
        forged_save = client.post(
            "/api/requisition/supplier-orders/from-pending-selection",
            json={
                "supplier_groups": [
                        {
                            "supplier_name": "B1-SUPPLIER",
                            "request_key": "b1-full-coverage-forged-save",
                            "lines": [
                            {
                                "report_length_mm": 800,
                                "report_width_mm": 600,
                                "inventory_deducted_qty": 9,
                                "requisition_qty": 1,
                                "source_items": [
                                    {
                                        "source_type": "order_item",
                                        "order_item_id": item_id,
                                        "inventory_deducted_qty": 9,
                                        "requisition_qty": 1,
                                    }
                                ],
                            }
                        ],
                    }
                ]
            },
        )
    assert created.status_code == 201
    assert pending.status_code == 200
    assert all(row["product_code"] != "B1-P5" for row in pending.json()["items"])
    assert suggestions.status_code == 200
    assert all(
        member["product_code"] != "B1-P5"
        for suggestion in suggestions.json()["suggestions"]
        for member in suggestion["members"]
    )
    assert preview.status_code == 409
    assert "半成品库存全额抵扣" in preview.json()["detail"]
    assert forged_save.status_code == 409
    assert (
        "半成品库存全额抵扣" in forged_save.json()["detail"]
        or "采购需求已经报完" in forged_save.json()["detail"]
    )


def test_requisition_preview_rechecks_late_semi_stock_and_recalculates_purchase(
    b1_app,
) -> None:
    app, factory = b1_app
    with TestClient(app) as client:
        login(client)
        created = post_order(
            client,
            [order_item(4, 20, None, "LATE-SEMI")],
            "B1-LATE-SEMI",
        )
        assert created.status_code == 201, created.text
        item_id = created.json()["items"][0]["id"]

        # Simulate an older pending order that predates the requirement snapshot.
        with factory() as db:
            requirement = db.scalar(
                select(OrderItemSemiRequirement).where(
                    OrderItemSemiRequirement.order_item_id == item_id
                )
            )
            assert requirement is not None
            db.delete(requirement)
            db.commit()

        lot_id, version = add_semi_lot(
            factory,
            quantity=30,
            key="late-semi-after-order",
            pieces_per_box=1,
            allowed_product_ids=[4],
        )
        selection = {
            "selections": [
                {
                    "type": "order_item",
                    "order_item_id": item_id,
                    "supplier_name": "B1-SUPPLIER",
                    "report_length_mm": 800,
                    "report_width_mm": 600,
                }
            ]
        }
        preview = client.post(
            "/api/requisition/supplier-orders/preview-from-pending-selection",
            json=selection,
        )
        assert preview.status_code == 200, preview.text
        line = preview.json()["supplier_groups"][0]["lines"][0]
        option = line["source_items"][0]["late_semi_inventory_options"][0]
        assert option["requirement_id"] is None
        assert option["remaining_requirement_quantity"] == 40
        assert option["recommended_candidates"] == []
        manual = option["review_candidates"][0]
        assert manual["lot_id"] == lot_id
        assert manual["lot_number"].startswith("SI-")
        assert manual["deductible_requirement_quantity"] == 30
        assert manual["signature_differences"] == ["pieces_per_box"]
        assert manual["warehouse_location"]["location_code"] == "B1-SI"

        reserve_payload = {
            "order_item_id": item_id,
            "component_type": "whole",
            "requested_requirement_quantity": 30,
            "lots": [{"lot_id": lot_id, "expected_version": version}],
            "override": False,
            "warning_acknowledged_codes": [],
            "idempotency_key": "late-semi-reserve-1",
        }
        unconfirmed_difference = client.post(
            "/api/requisition/semi-inventory/reserve-from-pending",
            json=reserve_payload,
        )
        assert unconfirmed_difference.status_code == 409
        assert "必须明确 override" in unconfirmed_difference.json()["detail"]

        reserved = client.post(
            "/api/requisition/semi-inventory/reserve-from-pending",
            json={
                **reserve_payload,
                "override": True,
                "warning_acknowledged_codes": ["SEMI_SIGNATURE_OVERRIDE"],
            },
        )
        assert reserved.status_code == 200, reserved.text
        assert reserved.json()["allocated_requirement_quantity"] == 30
        assert reserved.json()["remaining_requirement_quantity"] == 10
        assert reserved.json()["requisition_qty"] == 10

        refreshed = client.post(
            "/api/requisition/supplier-orders/preview-from-pending-selection",
            json=selection,
        )
        assert refreshed.status_code == 200, refreshed.text
        refreshed_line = refreshed.json()["supplier_groups"][0]["lines"][0]
        assert refreshed_line["semi_finished_reserved_piece_qty"] == 30
        assert refreshed_line["remaining_required_piece_qty"] == 10
        assert refreshed_line["requisition_qty"] == 10
        empty_option = refreshed_line["source_items"][0]["late_semi_inventory_options"][0]
        assert empty_option["recommended_candidates"] == []
        assert empty_option["review_candidates"] == []

    with factory() as db:
        lot = db.get(InventoryLot, lot_id)
        assert (lot.quantity_available, lot.quantity_reserved) == (0, 30)
        requirement = db.scalar(
            select(OrderItemSemiRequirement).where(
                OrderItemSemiRequirement.order_item_id == item_id,
                OrderItemSemiRequirement.component_type == "whole",
            )
        )
        assert requirement is not None
        mapping = db.scalar(
            select(SemiFinishedMatchRuleProduct).where(
                SemiFinishedMatchRuleProduct.product_id == 4
            )
        )
        assert mapping is not None


def test_late_first_match_keeps_frozen_one_open_two_yield(b1_app) -> None:
    app, factory = b1_app
    with factory() as db:
        product = db.get(Product, 1)
        product.box_style = "模切内盒"
        product.default_cutting_mode = "一开二"
        db.commit()
    with TestClient(app) as client:
        login(client)
        one_two_item = order_item(1, 80, None, "LATE-ONE-TWO")
        one_two_item["special_process"] = "一开二"
        created = post_order(
            client,
            [one_two_item],
            "B1-LATE-ONE-TWO",
        )
        assert created.status_code == 201, created.text
        item_id = created.json()["items"][0]["id"]
        with factory() as db:
            item = db.get(OrderItem, item_id)
            assert item.special_process == "一开二"
            requirement = db.scalar(
                select(OrderItemSemiRequirement).where(
                    OrderItemSemiRequirement.order_item_id == item_id
                )
            )
            assert requirement.stock_yield_per_sheet == 2
            db.delete(requirement)
            db.commit()

        lot_id, _version = add_semi_lot(
            factory,
            quantity=45,
            key="late-one-two-board-prep",
            stock_yield_per_sheet=2,
            allowed_product_ids=[1],
        )
        preview = client.post(
            "/api/requisition/supplier-orders/preview-from-pending-selection",
            json={
                "selections": [
                    {
                        "type": "order_item",
                        "order_item_id": item_id,
                        "supplier_name": "B1-SUPPLIER",
                        "report_length_mm": 800,
                        "report_width_mm": 600,
                        "cutting_mode": "一开二",
                    }
                ]
            },
        )
        assert preview.status_code == 200, preview.text
        option = preview.json()["supplier_groups"][0]["lines"][0][
            "source_items"
        ][0]["late_semi_inventory_options"][0]
        assert option["remaining_requirement_quantity"] == 80
        assert option["recommended_candidates"][0]["lot_id"] == lot_id
        assert option["recommended_candidates"][0][
            "stock_yield_per_sheet"
        ] == 2
        assert option["recommended_candidates"][0][
            "deductible_requirement_quantity"
        ] == 90
        assert option["review_candidates"] == []


def test_telescoping_cover_and_base_are_deducted_separately(b1_app) -> None:
    app, factory = b1_app
    cover_id, cover_version = add_semi_lot(
        factory,
        quantity=1,
        key="tel-cover",
        component_type="cover",
        length=400,
        width=300,
        allowed_product_ids=[6],
    )
    base_id, base_version = add_semi_lot(
        factory,
        quantity=2,
        key="tel-base",
        component_type="base",
        length=375,
        width=275,
        allowed_product_ids=[6],
    )
    plan = {
        "semi": [
            semi_plan(cover_id, cover_version, 1, "cover"),
            semi_plan(base_id, base_version, 2, "base"),
        ]
    }
    with TestClient(app) as client:
        login(client)
        created = post_order(client, [order_item(6, 2, plan)], "B1-TEL")
        pending = client.get("/api/requisition/pending")
    assert created.status_code == 201, created.text
    row = next(
        item for item in pending.json()["items"] if item["product_code"] == "B1-TEL"
    )
    by_component = {
        component["component_type"]: component
        for component in row["component_requirements"]
    }
    assert by_component["cover"]["semi_finished_reserved_piece_qty"] == 1
    assert by_component["cover"]["remaining_required_piece_qty"] == 1
    assert by_component["base"]["semi_finished_reserved_piece_qty"] == 2
    assert by_component["base"]["remaining_required_piece_qty"] == 0
    assert row["requisition_qty"] == 1


def test_unconfirmed_and_cross_customer_plans_are_rejected_atomically(b1_app) -> None:
    app, factory = b1_app
    lot_id, version = add_semi_lot(
        factory,
        quantity=5,
        key="cross-customer-plan",
        customer_id=2,
        bind_product=False,
    )
    unconfirmed = semi_plan(lot_id, version, 5)
    unconfirmed["confirmed"] = False
    with TestClient(app) as client:
        login(client)
        response_unconfirmed = post_order(
            client,
            [order_item(1, 5, {"semi": [unconfirmed]})],
            "B1-UNCONFIRMED",
        )
        response_cross = post_order(
            client,
            [order_item(1, 5, {"semi": [semi_plan(lot_id, version, 5)]})],
            "B1-CROSS",
        )
    assert response_unconfirmed.status_code == 409
    assert response_cross.status_code == 409
    with factory() as db:
        assert db.scalar(select(Order.id)) is None
        lot = db.get(InventoryLot, lot_id)
        assert (lot.quantity_available, lot.quantity_reserved) == (5, 0)


def test_signature_fields_cannot_change_while_unconsumed_reservation_exists(
    b1_app,
) -> None:
    app, factory = b1_app
    lot_id, version = add_semi_lot(
        factory, quantity=5, key="signature-lock-lot"
    )
    with TestClient(app) as client:
        login(client)
        created = post_order(
            client,
            [
                order_item(
                    1,
                    5,
                    {"semi": [semi_plan(lot_id, version, 5)]},
                )
            ],
            "B1-SIGNATURE-LOCK",
        )
        item = created.json()["items"][0]
        changed = client.put(
            f"/api/orders/items/{item['id']}",
            json={
                "quantity": 6,
                "unit_price": "1",
                "product_code": item["snapshot_product_code"],
                "product_name": item["snapshot_product_name"],
                "material": item["snapshot_material"],
                "specification": item["snapshot_spec"],
            },
        )
    assert changed.status_code == 409
    assert "未消耗库存预占" in changed.json()["detail"]


def test_requisition_save_and_cancel_releases_stock(
    b1_app,
) -> None:
    app, factory = b1_app
    lot_id, version = add_semi_lot(
        factory, quantity=5, key="requisition-cancel-lot"
    )
    with TestClient(app) as client:
        login(client)
        created = post_order(
            client,
            [
                order_item(
                    1,
                    5,
                    {"semi": [semi_plan(lot_id, version, 2)]},
                )
            ],
            "B1-REQ-CANCEL",
        )
        item_id = created.json()["items"][0]["id"]
        batch = client.post(
            "/api/requisition/batches",
            json={
                "supplier_name": "测试供应商",
                "items": [
                    {
                        "order_item_id": item_id,
                        "inventory_deducted_qty": 0,
                        "requisition_qty": 3,
                        "cardboard_len": 800,
                        "cardboard_width": 600,
                        "special_process": "一开一",
                    }
                ],
            },
        )
        assert batch.status_code == 201, batch.text
        assert batch.json()["items"][0]["requisition_qty"] == 3
        login(client, "admin")
        cancelled = client.put(
            f"/api/requisition/items/{item_id}/cancel",
            json={"reason": "撤回到未报料"},
        )
    assert cancelled.status_code == 200, cancelled.text
    with factory() as db:
        lot = db.get(InventoryLot, lot_id)
        assert (lot.quantity_available, lot.quantity_reserved) == (5, 0)


@pytest.mark.parametrize("action", ["delete", "rollback", "cancel"])
def test_delete_rollback_and_cancel_release_only_unconsumed_stock(
    b1_app,
    action: str,
) -> None:
    app, factory = b1_app
    lot_id, version = add_semi_lot(
        factory, quantity=5, key=f"release-{action}-lot"
    )
    with TestClient(app) as client:
        login(client, "admin")
        created = post_order(
            client,
            [
                order_item(
                    1,
                    5,
                    {"semi": [semi_plan(lot_id, version, 5)]},
                )
            ],
            f"B1-{action.upper()}",
        )
        assert created.status_code == 201, created.text
        order_id = created.json()["id"]
        item_id = created.json()["items"][0]["id"]
        if action == "cancel":
            with factory() as db:
                reservation = db.scalar(
                    select(InventoryReservation).where(
                        InventoryReservation.order_item_id == item_id
                    )
                )
                lot = db.get(InventoryLot, lot_id)
                consume_semi_finished_reservation(
                    db,
                    reservation_id=reservation.id,
                    stock_quantity=2,
                    expected_version=lot.version,
                    operator_id=1,
                    idempotency_key=f"consume-before-{action}",
                )
                db.commit()
            result = client.put(
                f"/api/orders/{order_id}/status",
                json={"status": "cancelled", "remark": "客户取消"},
            )
        elif action == "delete":
            result = client.delete(f"/api/orders/{order_id}?confirm=true")
        else:
            result = client.put(
                f"/api/orders/{order_id}/rollback-workflow",
                json={"reason": "测试撤回"},
            )
    assert result.status_code in {200, 204}, result.text
    with factory() as db:
        lot = db.get(InventoryLot, lot_id)
        if action == "cancel":
            assert (lot.quantity_available, lot.quantity_reserved, lot.quantity_consumed) == (
                3,
                0,
                2,
            )
        else:
            assert (lot.quantity_available, lot.quantity_reserved) == (5, 0)


def test_external_packaging_new_order_can_reserve_finished_stock_before_purchase(b1_app):
    from app.services.warehouse_inventory import finished_inventory_candidates, WarehouseInventoryError
    app, factory = b1_app
    with factory() as db:
        product = db.get(Product, 1)
        product.supply_mode = "external_purchase"
        product.external_packaging_category_code = "honeycomb_board"
        product.external_packaging_specification_json = '{"length_mm":800,"width_mm":180,"height_mm":120}'
        product.external_packaging_specification_summary = "蜂窝板800×180×120"
        product.external_packaging_purchase_unit = "片"
        product.external_packaging_candidate_snapshot_json = '[{"is_default":true,"external_product_id":1,"supplier_id":1,"external_product_version":1,"supplier_name":"测试供应商","supplier_product_code":"HC-TEST","product_name":"蜂窝板","purchase_unit":"片"}]'
        product.external_packaging_default_order_quantity_basis = 1
        product.external_packaging_default_purchase_quantity_basis = 1
        db.commit()
    lot_id, version = add_finished_lot(factory, product_id=1, quantity=30, key="external-ready-stock")
    with TestClient(app) as client:
        login(client, "admin")
        response = post_order(client, [order_item(1, 20, {"finished":[finished_plan(lot_id, version, 20)]})], "external-stock-order")
        assert response.status_code == 201, response.text
    item_id = response.json()["items"][0]["id"]
    with factory() as db:
        item = db.get(OrderItem, item_id)
        assert item.requisition_status == "外购包材待确认"
        assert db.get(InventoryLot, lot_id).quantity_reserved == 20
        assert db.get(InventoryLot, lot_id).quantity_available == 10
        assert finished_inventory_candidates(db, item_id)
        item.requisition_status = "外购包材已采购"
        db.flush()
        with pytest.raises(WarehouseInventoryError, match="订单已进入报料"):
            finished_inventory_candidates(db, item_id)
