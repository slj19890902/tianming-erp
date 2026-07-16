from __future__ import annotations

from collections.abc import Generator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryItem
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.user import User
from app.models.warehouse_inventory import (
    DeliveryInventoryAllocation,
    InventoryMovement,
    InventoryReservation,
    SemiFinishedMatchRule,
    SemiFinishedMatchRuleProduct,
    WarehouseLocation,
)
from app.services.semi_finished_inventory import (
    SIGNATURE_OVERRIDE_WARNING,
    SemiFinishedLotVersion,
    active_semi_requirement_credited_quantity,
    browse_semi_finished_inventory,
    confirm_semi_finished_match,
    consume_semi_finished_reservation,
    release_semi_finished_reservation,
    replace_semi_finished_lot_product_assignments,
    reserve_semi_finished_inventory,
    reverse_semi_finished_consumption,
    save_order_item_semi_requirement,
    semi_finished_lot_assigned_product_ids,
    semi_finished_candidates_for_product,
    semi_finished_inventory_candidates,
)
from app.services.warehouse_inventory import (
    SEMI_FINISHED_ASSIGN_CUSTOMER_REASON,
    SEMI_FINISHED_UNASSIGN_CUSTOMER_REASON,
    SEMI_FINISHED_VOID_REASON,
    WarehouseInventoryError,
    edit_semi_finished_lot_customer,
    finished_inventory_candidates,
    manual_finished_in,
    manual_semi_finished_in,
    reserve_finished_inventory,
    void_semi_finished_lot,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REVISION = "ac30v7w8x9y20"
PARENT_REVISION = "ab29u7v8w9x18"


@pytest.fixture()
def semi_db(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "semi-shared.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="semi-admin",
            password_hash="test",
            role="admin",
            real_name="半成品管理员",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=8101,
            customer_code="SEMI-A",
            name="共享库存客户A",
            payment_term_days=0,
            credit_limit=0,
        )
        other_customer = Customer(
            customer_number=8102,
            customer_code="SEMI-B",
            name="共享库存客户B",
            payment_term_days=0,
            credit_limit=0,
        )
        db.add_all([admin, customer, other_customer])
        db.flush()
        products = []
        for index in range(1, 5):
            products.append(
                Product(
                    customer_id=customer.id,
                    product_code=f"SEMI-P{index}",
                    customer_material_code=f"SEMI-M{index}",
                    product_name=f"同规格不同印刷-{index}",
                    box_category="normal",
                    box_style="普通箱",
                    default_material_code=" a 416 d ",
                    flute_type="B",
                    report_length_mm=800,
                    report_width_mm=600,
                    pieces_per_box=1,
                )
            )
        db.add_all(products)
        semi_location = WarehouseLocation(
            location_code="SEMI-01",
            location_name="半成品共享库位",
            warehouse_type="semi_finished",
        )
        finished_location = WarehouseLocation(
            location_code="FG-01",
            location_name="成品库位",
            warehouse_type="finished",
        )
        db.add_all([semi_location, finished_location])
        db.flush()
        order = Order(
            order_number="TM-SEMI-001",
            customer_id=customer.id,
            order_date=date.today(),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("400"),
        )
        db.add(order)
        db.flush()
        items = []
        for product in products:
            item = OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=200,
                unit_price=Decimal("1"),
                subtotal=Decimal("200"),
                material_status="pending",
                requisition_status="未报料",
                snapshot_product_code=product.product_code,
                snapshot_product_name=product.product_name,
                snapshot_spec="800×600mm",
                snapshot_material="A416D",
                flute_type="B",
                snapshot_report_length_mm=800,
                snapshot_report_width_mm=600,
                snapshot_splice_mode="single",
                snapshot_pieces_per_box=1,
            )
            db.add(item)
            items.append(item)
        db.flush()
        db.commit()
        yield db, {
            "admin": admin,
            "customer": customer,
            "other_customer": other_customer,
            "products": products,
            "order": order,
            "items": items,
            "semi_location": semi_location,
            "finished_location": finished_location,
        }


def add_requirement(
    db: Session,
    data: dict,
    *,
    item_index: int = 0,
    component_type: str = "whole",
    length: int = 800,
    width: int = 600,
    material: str = "A416D",
    flute: str = "B",
    pieces_per_box: int = 1,
    stock_yield_per_sheet: int = 1,
    required: int = 200,
):
    return save_order_item_semi_requirement(
        db,
        order_item_id=data["items"][item_index].id,
        component_type=component_type,
        board_length_mm=length,
        board_width_mm=width,
        material_code=material,
        flute_type=flute,
        pieces_per_box=pieces_per_box,
        stock_yield_per_sheet=stock_yield_per_sheet,
        required_piece_quantity=required,
        operator_id=data["admin"].id,
    )


def add_semi_lot(
    db: Session,
    data: dict,
    *,
    key: str,
    quantity: int = 100,
    customer_id: int | None = None,
    length: int = 800,
    width: int = 600,
    material: str = " A416D ",
    flute: str = "B",
    component_type: str = "whole",
    pieces_per_box: int = 1,
    stock_yield_per_sheet: int = 1,
    unowned: bool = False,
):
    return manual_semi_finished_in(
        db,
        location_id=data["semi_location"].id,
        quantity=quantity,
        stock_date=date.today(),
        source_type="production_surplus",
        material_code=material,
        layer_count=5 if flute in {"AB", "BE"} else 3,
        flute_type=flute,
        board_length_mm=length,
        board_width_mm=width,
        sheet_type="net_sheet",
        component_type=component_type,
        pieces_per_box=pieces_per_box,
        stock_yield_per_sheet=stock_yield_per_sheet,
        supplier_name=None,
        customer_id=(
            None
            if unowned
            else (data["customer"].id if customer_id is None else customer_id)
        ),
        crease_type=None,
        crease_left_mm=None,
        crease_middle_mm=None,
        crease_right_mm=None,
        cutting_note=None,
        remarks=None,
        operator_id=data["admin"].id,
        idempotency_key=key,
    )


def confirm(
    db: Session,
    data: dict,
    requirement_id: int,
    lot_id: int,
    *,
    override: bool = False,
):
    return confirm_semi_finished_match(
        db,
        requirement_id=requirement_id,
        inventory_lot_id=lot_id,
        operator_id=data["admin"].id,
        override=override,
        warning_acknowledged_codes=(
            [SIGNATURE_OVERRIDE_WARNING] if override else []
        ),
    )


@pytest.fixture()
def semi_api(semi_db):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.warehouse import router as warehouse_router
    from app.core.security import hash_password

    db, data = semi_db
    password = "SemiApiTest123!"
    data["admin"].password_hash = hash_password(password)
    db.add(User(
        username="semi-operator", password_hash=hash_password(password),
        role="workshop", real_name="半成品操作员", must_change_password=False,
    ))
    db.commit()
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(warehouse_router, prefix="/api/warehouse")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as api_db:
            yield api_db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, data, password, db
    finally:
        app.dependency_overrides.clear()


def _login_semi_api(client: TestClient, username: str, password: str) -> None:
    response = client.post("/api/auth/login", json={
        "username": username, "password": password,
    })
    assert response.status_code == 200, response.text


def test_edit_semi_lot_assign_cancel_and_reassign_preserves_shared_mapping(
    semi_db,
) -> None:
    db, data = semi_db
    lot = add_semi_lot(db, data, key="edit-lifecycle", unowned=True)
    original_version = lot.version
    balances = (
        lot.quantity_available, lot.quantity_reserved, lot.quantity_consumed,
        lot.quantity_damaged, lot.quantity_scrapped,
    )
    assigned = edit_semi_finished_lot_customer(
        db, lot_id=lot.id, customer_id=data["customer"].id,
        expected_version=original_version, operator_id=data["admin"].id,
    )
    assigned_version = assigned.version
    rule, _products = replace_semi_finished_lot_product_assignments(
        db,
        inventory_lot_id=lot.id,
        product_ids=[data["products"][0].id, data["products"][1].id],
        operator_id=data["admin"].id,
    )
    assert rule is not None
    with pytest.raises(WarehouseInventoryError, match="先取消当前归属"):
        edit_semi_finished_lot_customer(
            db,
            lot_id=lot.id,
            customer_id=data["other_customer"].id,
            expected_version=assigned_version,
            operator_id=data["admin"].id,
        )
    with pytest.raises(WarehouseInventoryError, match="刷新后重试"):
        edit_semi_finished_lot_customer(
            db,
            lot_id=lot.id,
            customer_id=None,
            expected_version=assigned_version + 1,
            operator_id=data["admin"].id,
        )
    unassigned = edit_semi_finished_lot_customer(
        db,
        lot_id=lot.id,
        customer_id=None,
        expected_version=assigned_version,
        operator_id=data["admin"].id,
    )
    unassigned_version = unassigned.version
    assert unassigned.semi_finished_detail.owner_customer_id is None
    assert db.scalars(
        select(SemiFinishedMatchRuleProduct).where(
            SemiFinishedMatchRuleProduct.rule_id == rule.id
        )
    ).all()
    reassigned = edit_semi_finished_lot_customer(
        db,
        lot_id=lot.id,
        customer_id=data["other_customer"].id,
        expected_version=unassigned_version,
        operator_id=data["admin"].id,
    )
    assert reassigned.semi_finished_detail.owner_customer_id == data["other_customer"].id
    assert (
        reassigned.quantity_available,
        reassigned.quantity_reserved,
        reassigned.quantity_consumed,
        reassigned.quantity_damaged,
        reassigned.quantity_scrapped,
    ) == balances
    movements = db.scalars(select(InventoryMovement).where(
        InventoryMovement.inventory_lot_id == lot.id
    ).order_by(InventoryMovement.id)).all()
    assert [row.reason for row in movements[-3:]] == [
        SEMI_FINISHED_ASSIGN_CUSTOMER_REASON,
        SEMI_FINISHED_UNASSIGN_CUSTOMER_REASON,
        SEMI_FINISHED_ASSIGN_CUSTOMER_REASON,
    ]
    assert all(row.quantity == 0 for row in movements[-3:])


def test_edit_and_void_api_are_admin_only_and_versioned(semi_api) -> None:
    app, data, password, db = semi_api
    lot = add_semi_lot(db, data, key="edit-api", unowned=True)
    version = lot.version
    db.commit()
    with TestClient(app) as client:
        _login_semi_api(client, "semi-operator", password)
        assert client.post(
            f"/api/warehouse/lots/{lot.id}/edit-semi-finished",
            json={"customer_id": data["customer"].id, "expected_version": version},
        ).status_code == 403
        assert client.post(
            f"/api/warehouse/lots/{lot.id}/void-semi-finished",
            json={"expected_version": version, "reason": "误录"},
        ).status_code == 403
        _login_semi_api(client, data["admin"].username, password)
        stale = client.post(
            f"/api/warehouse/lots/{lot.id}/edit-semi-finished",
            json={
                "customer_id": data["customer"].id,
                "expected_version": version + 1,
            },
        )
        assert stale.status_code == 409
        saved = client.post(
            f"/api/warehouse/lots/{lot.id}/edit-semi-finished",
            json={"customer_id": data["customer"].id, "expected_version": version},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["version"] == version + 1


def test_void_semi_lot_closes_balance_and_keeps_audit(semi_db) -> None:
    db, data = semi_db
    lot = add_semi_lot(db, data, key="void-safe", quantity=37, unowned=True)
    version = lot.version
    voided = void_semi_finished_lot(
        db,
        lot_id=lot.id,
        expected_version=version,
        reason="重复录入",
        operator_id=data["admin"].id,
    )
    assert (voided.status, voided.version, voided.quantity_available) == (
        "closed",
        version + 1,
        0,
    )
    movements = db.scalars(
        select(InventoryMovement)
        .where(InventoryMovement.inventory_lot_id == lot.id)
        .order_by(InventoryMovement.id)
    ).all()
    assert len(movements) == 2
    assert movements[-1].movement_type == "adjust"
    assert (movements[-1].quantity, movements[-1].before_available) == (37, 37)
    assert movements[-1].after_available == 0
    assert movements[-1].reason == f"{SEMI_FINISHED_VOID_REASON}：重复录入"


@pytest.mark.parametrize(
    "field",
    [
        "quantity_reserved",
        "quantity_consumed",
        "quantity_damaged",
        "quantity_scrapped",
    ],
)
def test_void_semi_lot_rejects_protected_balances(semi_db, field: str) -> None:
    db, data = semi_db
    lot = add_semi_lot(db, data, key=f"void-{field}", unowned=True)
    setattr(lot, field, 1)
    db.flush()
    with pytest.raises(WarehouseInventoryError, match="不能删除"):
        void_semi_finished_lot(
            db,
            lot_id=lot.id,
            expected_version=lot.version,
            reason="误录",
            operator_id=data["admin"].id,
        )


def test_void_semi_lot_rejects_source_ref_and_any_reservation(semi_db) -> None:
    db, data = semi_db
    sourced = add_semi_lot(db, data, key="void-source", unowned=True)
    sourced.source_ref_type = "incoming_receipt_item"
    sourced.source_ref_id = 1
    db.flush()
    with pytest.raises(WarehouseInventoryError, match="关联来料或补库"):
        void_semi_finished_lot(
            db,
            lot_id=sourced.id,
            expected_version=sourced.version,
            reason="误录",
            operator_id=data["admin"].id,
        )
    reserved = add_semi_lot(db, data, key="void-reservation", unowned=True)
    db.add(
        InventoryReservation(
            reservation_number="RS-VOID-HISTORY",
            inventory_lot_id=reserved.id,
            reservation_type="semi_order",
            reserved_stock_quantity=1,
            credited_requirement_quantity=1,
            yield_factor=1,
            released_stock_quantity=1,
            released_requirement_quantity=1,
            status="released",
            idempotency_key="void-reservation-history",
        )
    )
    db.flush()
    with pytest.raises(WarehouseInventoryError, match="预占记录"):
        void_semi_finished_lot(
            db,
            lot_id=reserved.id,
            expected_version=reserved.version,
            reason="误录",
            operator_id=data["admin"].id,
        )


def test_signature_fallback_then_one_rule_maps_four_products_and_new_lots_learn(
    semi_db,
) -> None:
    db, data = semi_db
    requirements = [add_requirement(db, data, item_index=index) for index in range(4)]
    first_lot = add_semi_lot(db, data, key="four-products-first")

    initial = semi_finished_inventory_candidates(db, requirements[0].id)
    assert [(row.lot.id, row.source) for row in initial] == [
        (first_lot.id, "signature")
    ]
    draft_initial = semi_finished_candidates_for_product(
        db,
        product_id=data["products"][0].id,
        customer_id=data["customer"].id,
        board_length_mm=800,
        board_width_mm=600,
        material_code=" a 416 d ",
        flute_type="b",
        component_type="whole",
        pieces_per_box=1,
        stock_yield_per_sheet=1,
    )
    assert [(row.lot.id, row.source) for row in draft_initial] == [
        (first_lot.id, "signature")
    ]

    confirmations = [
        confirm(db, data, requirement.id, first_lot.id)
        for requirement in requirements
    ]
    assert len({row.rule.id for row in confirmations}) == 1
    rule_id = confirmations[0].rule.id
    mappings = db.scalars(
        select(SemiFinishedMatchRuleProduct).where(
            SemiFinishedMatchRuleProduct.rule_id == rule_id
        )
    ).all()
    assert {row.product_id for row in mappings} == {
        product.id for product in data["products"]
    }

    second_lot = add_semi_lot(db, data, key="four-products-new-lot")
    learned = semi_finished_inventory_candidates(db, requirements[3].id)
    assert {row.lot.id for row in learned} == {first_lot.id, second_lot.id}
    assert {row.source for row in learned} == {"learned"}
    assert {row.match_rule_id for row in learned} == {rule_id}
    draft_learned = semi_finished_candidates_for_product(
        db,
        product_id=data["products"][0].id,
        customer_id=data["customer"].id,
        board_length_mm=800,
        board_width_mm=600,
        material_code="A416D",
        flute_type="B",
        component_type="whole",
        pieces_per_box=1,
        stock_yield_per_sheet=1,
    )
    assert {row.source for row in draft_learned} == {"learned"}


def test_warehouse_can_assign_one_semi_signature_to_multiple_finished_products(
    semi_db,
) -> None:
    db, data = semi_db
    lot = add_semi_lot(db, data, key="warehouse-multi-product-assignment")

    rule, products = replace_semi_finished_lot_product_assignments(
        db,
        inventory_lot_id=lot.id,
        product_ids=[
            data["products"][0].id,
            data["products"][1].id,
            data["products"][2].id,
        ],
        operator_id=data["admin"].id,
    )
    assert rule is not None
    assert {row.id for row in products} == {
        data["products"][0].id,
        data["products"][1].id,
        data["products"][2].id,
    }
    assert set(semi_finished_lot_assigned_product_ids(db, lot.id)) == {
        data["products"][0].id,
        data["products"][1].id,
        data["products"][2].id,
    }

    replace_semi_finished_lot_product_assignments(
        db,
        inventory_lot_id=lot.id,
        product_ids=[data["products"][1].id, data["products"][3].id],
        operator_id=data["admin"].id,
    )
    assert set(semi_finished_lot_assigned_product_ids(db, lot.id)) == {
        data["products"][1].id,
        data["products"][3].id,
    }

    other_product = Product(
        customer_id=data["other_customer"].id,
        product_code="OTHER-CUSTOMER-P1",
        customer_material_code="OTHER-CUSTOMER-M1",
        product_name="其他客户产品",
        box_category="normal",
        box_style="普通箱",
        is_active=True,
    )
    db.add(other_product)
    db.flush()
    with pytest.raises(WarehouseInventoryError, match="同一客户"):
        replace_semi_finished_lot_product_assignments(
            db,
            inventory_lot_id=lot.id,
            product_ids=[other_product.id],
            operator_id=data["admin"].id,
        )


@pytest.mark.parametrize(
    ("changed_field", "lot_values"),
    [
        ("customer", {"customer_id": "other"}),
        ("length", {"length": 801}),
        ("width", {"width": 601}),
        ("material", {"material": "C6C"}),
        ("flute", {"flute": "A"}),
        ("component", {"component_type": "cover"}),
        ("pieces_per_box", {"pieces_per_box": 2}),
        ("stock_yield_per_sheet", {"stock_yield_per_sheet": 2}),
    ],
)
def test_signature_fallback_rejects_any_key_difference(
    semi_db,
    changed_field: str,
    lot_values: dict,
) -> None:
    db, data = semi_db
    requirement = add_requirement(db, data)
    if lot_values.get("customer_id") == "other":
        lot_values["customer_id"] = data["other_customer"].id
    lot = add_semi_lot(db, data, key=f"mismatch-{changed_field}", **lot_values)

    assert semi_finished_inventory_candidates(db, requirement.id) == []
    if changed_field == "component":
        assert all(
            row.lot.id != lot.id
            for row in browse_semi_finished_inventory(db, requirement.id)
        )


def test_manual_override_learns_only_selected_inventory_signature(semi_db) -> None:
    db, data = semi_db
    requirement = add_requirement(db, data)
    mismatched = add_semi_lot(
        db,
        data,
        key="override-first",
        length=820,
        material="C6C",
        stock_yield_per_sheet=2,
    )
    assert semi_finished_inventory_candidates(db, requirement.id) == []
    assert mismatched.id in {
        row.lot.id for row in browse_semi_finished_inventory(db, requirement.id)
    }
    with pytest.raises(WarehouseInventoryError, match="必须明确 override"):
        confirm_semi_finished_match(
            db,
            requirement_id=requirement.id,
            inventory_lot_id=mismatched.id,
            operator_id=data["admin"].id,
            override=False,
            warning_acknowledged_codes=[],
        )
    with pytest.raises(WarehouseInventoryError, match="必须确认"):
        confirm_semi_finished_match(
            db,
            requirement_id=requirement.id,
            inventory_lot_id=mismatched.id,
            operator_id=data["admin"].id,
            override=True,
            warning_acknowledged_codes=[],
        )

    learned = confirm(db, data, requirement.id, mismatched.id, override=True)
    same_signature = add_semi_lot(
        db,
        data,
        key="override-new-same",
        length=820,
        material="C6C",
        stock_yield_per_sheet=2,
    )
    other_signature = add_semi_lot(
        db,
        data,
        key="override-new-other",
        length=821,
        material="C6C",
        stock_yield_per_sheet=2,
    )
    candidates = semi_finished_inventory_candidates(db, requirement.id)
    assert {row.lot.id for row in candidates} == {mismatched.id, same_signature.id}
    assert other_signature.id not in {row.lot.id for row in candidates}
    assert {row.source for row in candidates} == {"learned"}
    assert {row.match_rule_id for row in candidates} == {learned.rule.id}
    assert all(SIGNATURE_OVERRIDE_WARNING in row.warning_codes for row in candidates)


def test_customer_isolation_blocks_browse_learning_forged_mapping_and_reserve(
    semi_db,
) -> None:
    db, data = semi_db
    requirement = add_requirement(db, data, required=10)
    other_customer_lot = add_semi_lot(
        db,
        data,
        key="customer-isolation-other",
        quantity=10,
        customer_id=data["other_customer"].id,
    )
    unowned_lot = add_semi_lot(
        db,
        data,
        key="customer-isolation-unowned",
        quantity=10,
        unowned=True,
    )
    forged_rule = SemiFinishedMatchRule(
        customer_id=data["other_customer"].id,
        board_length_mm=800,
        board_width_mm=600,
        normalized_material_code="A416D",
        flute_type="B",
        component_type="whole",
        pieces_per_box=1,
        stock_yield_per_sheet=1,
        active=True,
        created_by=data["admin"].id,
    )
    db.add(forged_rule)
    db.flush()
    db.add(
        SemiFinishedMatchRuleProduct(
            rule_id=forged_rule.id,
            product_id=data["products"][0].id,
            confirmed_by=data["admin"].id,
            confirmed_at=other_customer_lot.last_movement_at,
        )
    )
    db.flush()

    assert semi_finished_inventory_candidates(db, requirement.id) == []
    assert browse_semi_finished_inventory(db, requirement.id) == []
    assert semi_finished_candidates_for_product(
        db,
        product_id=data["products"][0].id,
        customer_id=data["customer"].id,
        board_length_mm=800,
        board_width_mm=600,
        material_code="A416D",
        flute_type="B",
        component_type="whole",
        pieces_per_box=1,
        stock_yield_per_sheet=1,
    ) == []

    with pytest.raises(WarehouseInventoryError, match="其他客户专用"):
        confirm(db, data, requirement.id, other_customer_lot.id, override=True)
    with pytest.raises(WarehouseInventoryError, match="未归属客户"):
        confirm(db, data, requirement.id, unowned_lot.id, override=True)

    for lot, key, message in (
        (other_customer_lot, "direct-reserve-other", "其他客户专用"),
        (unowned_lot, "direct-reserve-unowned", "未归属客户"),
    ):
        with pytest.raises(WarehouseInventoryError, match=message):
            reserve_semi_finished_inventory(
                db,
                requirement_id=requirement.id,
                requested_requirement_quantity=1,
                lots=[SemiFinishedLotVersion(lot.id, lot.version)],
                operator_id=data["admin"].id,
                idempotency_key=key,
                confirmed=True,
                override=True,
                warning_acknowledged_codes=[SIGNATURE_OVERRIDE_WARNING],
            )
        db.refresh(lot)
        assert (lot.quantity_available, lot.quantity_reserved) == (10, 0)
    assert db.scalar(
        select(InventoryReservation.id).where(
            InventoryReservation.reservation_group_key.in_(
                ["direct-reserve-other", "direct-reserve-unowned"]
            )
        )
    ) is None


def test_pool_allocates_50_30_20_without_over_reservation_and_is_idempotent(
    semi_db,
) -> None:
    db, data = semi_db
    requirement = add_requirement(db, data, required=110)
    lot = add_semi_lot(db, data, key="pool-100", quantity=100)

    first = reserve_semi_finished_inventory(
        db,
        requirement_id=requirement.id,
        requested_requirement_quantity=50,
        lots=[SemiFinishedLotVersion(lot.id, lot.version)],
        operator_id=data["admin"].id,
        idempotency_key="pool-request-50",
        confirmed=True,
    )
    db.refresh(lot)
    assert first.allocated_requirement_quantity == 50
    assert (lot.quantity_available, lot.quantity_reserved) == (50, 50)

    repeated = reserve_semi_finished_inventory(
        db,
        requirement_id=requirement.id,
        requested_requirement_quantity=50,
        lots=[SemiFinishedLotVersion(lot.id, 1)],
        operator_id=data["admin"].id,
        idempotency_key="pool-request-50",
        confirmed=True,
    )
    assert [row.id for row in repeated.reservations] == [
        row.id for row in first.reservations
    ]
    db.refresh(lot)
    assert lot.quantity_reserved == 50

    second = reserve_semi_finished_inventory(
        db,
        requirement_id=requirement.id,
        requested_requirement_quantity=30,
        lots=[SemiFinishedLotVersion(lot.id, lot.version)],
        operator_id=data["admin"].id,
        idempotency_key="pool-request-30-a",
        confirmed=True,
    )
    db.refresh(lot)
    third = reserve_semi_finished_inventory(
        db,
        requirement_id=requirement.id,
        requested_requirement_quantity=30,
        lots=[SemiFinishedLotVersion(lot.id, lot.version)],
        operator_id=data["admin"].id,
        idempotency_key="pool-request-30-b",
        confirmed=True,
    )
    db.refresh(lot)
    assert second.allocated_requirement_quantity == 30
    assert third.allocated_requirement_quantity == 20
    assert third.unallocated_requirement_quantity == 10
    assert (lot.quantity_available, lot.quantity_reserved) == (0, 100)
    assert sum(
        row.reserved_stock_quantity
        for row in db.scalars(
            select(InventoryReservation).where(
                InventoryReservation.semi_requirement_id == requirement.id
            )
        ).all()
    ) == 100


def test_old_version_conflicts_atomically_without_duplicate_use(semi_db) -> None:
    db, data = semi_db
    requirement = add_requirement(db, data, required=100)
    first_lot = add_semi_lot(db, data, key="cas-first", quantity=20)
    second_lot = add_semi_lot(db, data, key="cas-second", quantity=20)
    stale_version = second_lot.version
    second_lot.version += 1
    db.flush()

    with pytest.raises(WarehouseInventoryError, match="刷新候选"):
        reserve_semi_finished_inventory(
            db,
            requirement_id=requirement.id,
            requested_requirement_quantity=30,
            lots=[
                SemiFinishedLotVersion(first_lot.id, first_lot.version),
                SemiFinishedLotVersion(second_lot.id, stale_version),
            ],
            operator_id=data["admin"].id,
            idempotency_key="cas-atomic-conflict",
            confirmed=True,
        )
    db.refresh(first_lot)
    db.refresh(second_lot)
    assert (first_lot.quantity_available, first_lot.quantity_reserved) == (20, 0)
    assert (second_lot.quantity_available, second_lot.quantity_reserved) == (20, 0)
    assert db.scalar(
        select(InventoryReservation.id).where(
            InventoryReservation.reservation_group_key == "cas-atomic-conflict"
        )
    ) is None


def test_multi_lot_reservation_uses_stable_stock_date_and_id_order(semi_db) -> None:
    db, data = semi_db
    requirement = add_requirement(db, data, required=30)
    first_lot = add_semi_lot(db, data, key="fixed-order-first", quantity=20)
    second_lot = add_semi_lot(db, data, key="fixed-order-second", quantity=20)

    batch = reserve_semi_finished_inventory(
        db,
        requirement_id=requirement.id,
        requested_requirement_quantity=30,
        lots=[
            SemiFinishedLotVersion(second_lot.id, second_lot.version),
            SemiFinishedLotVersion(first_lot.id, first_lot.version),
        ],
        operator_id=data["admin"].id,
        idempotency_key="fixed-lot-order-request",
        confirmed=True,
    )
    assert [row.inventory_lot_id for row in batch.reservations] == [
        first_lot.id,
        second_lot.id,
    ]
    assert [row.credited_requirement_quantity for row in batch.reservations] == [
        20,
        10,
    ]


def test_partial_consume_release_remaining_and_reverse_consumption(semi_db) -> None:
    db, data = semi_db
    requirement = add_requirement(db, data, required=20)
    lot = add_semi_lot(db, data, key="lifecycle-lot", quantity=20)
    batch = reserve_semi_finished_inventory(
        db,
        requirement_id=requirement.id,
        requested_requirement_quantity=10,
        lots=[SemiFinishedLotVersion(lot.id, lot.version)],
        operator_id=data["admin"].id,
        idempotency_key="lifecycle-reserve",
        confirmed=True,
    )
    reservation = batch.reservations[0]
    db.refresh(lot)

    consumed = consume_semi_finished_reservation(
        db,
        reservation_id=reservation.id,
        stock_quantity=4,
        expected_version=lot.version,
        operator_id=data["admin"].id,
        idempotency_key="lifecycle-consume-4",
    )
    repeated = consume_semi_finished_reservation(
        db,
        reservation_id=reservation.id,
        stock_quantity=4,
        expected_version=1,
        operator_id=data["admin"].id,
        idempotency_key="lifecycle-consume-4",
    )
    assert repeated.movement.id == consumed.movement.id
    db.refresh(lot)
    assert (lot.quantity_available, lot.quantity_reserved, lot.quantity_consumed) == (
        10,
        6,
        4,
    )
    assert reservation.status == "partial"

    released = release_semi_finished_reservation(
        db,
        reservation_id=reservation.id,
        expected_version=lot.version,
        operator_id=data["admin"].id,
        release_reason="订单撤回，释放未消耗部分",
        idempotency_key="lifecycle-release-rest",
    )
    db.refresh(lot)
    assert released.reservation.released_stock_quantity == 6
    assert (lot.quantity_available, lot.quantity_reserved, lot.quantity_consumed) == (
        16,
        0,
        4,
    )

    reversed_result = reverse_semi_finished_consumption(
        db,
        reservation_id=reservation.id,
        stock_quantity=2,
        expected_version=lot.version,
        operator_id=data["admin"].id,
        idempotency_key="lifecycle-reverse-2",
    )
    db.refresh(lot)
    assert reversed_result.reservation.consumed_stock_quantity == 2
    assert (lot.quantity_available, lot.quantity_reserved, lot.quantity_consumed) == (
        16,
        2,
        2,
    )
    movement_types = set(
        db.scalars(
            select(InventoryMovement.movement_type).where(
                InventoryMovement.reservation_id == reservation.id
            )
        ).all()
    )
    assert {"reserve", "consume", "release_reserve", "reverse_consume"} <= movement_types


def test_ceil_credit_is_exact_for_consume_release_reverse_and_release_first(
    semi_db,
) -> None:
    db, data = semi_db
    requirement = add_requirement(
        db,
        data,
        required=5,
        stock_yield_per_sheet=3,
    )
    lot = add_semi_lot(
        db,
        data,
        key="ceil-consume-first-lot",
        quantity=2,
        stock_yield_per_sheet=3,
    )
    reservation = reserve_semi_finished_inventory(
        db,
        requirement_id=requirement.id,
        requested_requirement_quantity=5,
        lots=[SemiFinishedLotVersion(lot.id, lot.version)],
        operator_id=data["admin"].id,
        idempotency_key="ceil-consume-first-reserve",
        confirmed=True,
    ).reservations[0]
    assert (reservation.credited_requirement_quantity, reservation.reserved_stock_quantity) == (
        5,
        2,
    )
    db.refresh(lot)
    consume_semi_finished_reservation(
        db,
        reservation_id=reservation.id,
        stock_quantity=1,
        expected_version=lot.version,
        operator_id=data["admin"].id,
        idempotency_key="ceil-consume-first-consume",
    )
    assert reservation.consumed_requirement_quantity == 3
    assert active_semi_requirement_credited_quantity(db, requirement.id) == 5
    db.refresh(lot)
    release_semi_finished_reservation(
        db,
        reservation_id=reservation.id,
        stock_quantity=1,
        expected_version=lot.version,
        operator_id=data["admin"].id,
        release_reason="释放最后一张未消耗库存",
        idempotency_key="ceil-consume-first-release",
    )
    assert reservation.released_requirement_quantity == 2
    assert active_semi_requirement_credited_quantity(db, requirement.id) == 3
    db.refresh(lot)
    reverse_semi_finished_consumption(
        db,
        reservation_id=reservation.id,
        stock_quantity=1,
        expected_version=lot.version,
        operator_id=data["admin"].id,
        idempotency_key="ceil-consume-first-reverse",
    )
    assert reservation.consumed_requirement_quantity == 0
    assert reservation.released_requirement_quantity == 2
    assert active_semi_requirement_credited_quantity(db, requirement.id) == 3

    release_first_requirement = add_requirement(
        db,
        data,
        item_index=1,
        required=5,
        stock_yield_per_sheet=3,
    )
    release_first_lot = add_semi_lot(
        db,
        data,
        key="ceil-release-first-lot",
        quantity=2,
        stock_yield_per_sheet=3,
    )
    release_first = reserve_semi_finished_inventory(
        db,
        requirement_id=release_first_requirement.id,
        requested_requirement_quantity=5,
        lots=[SemiFinishedLotVersion(release_first_lot.id, release_first_lot.version)],
        operator_id=data["admin"].id,
        idempotency_key="ceil-release-first-reserve",
        confirmed=True,
    ).reservations[0]
    db.refresh(release_first_lot)
    release_semi_finished_reservation(
        db,
        reservation_id=release_first.id,
        stock_quantity=1,
        expected_version=release_first_lot.version,
        operator_id=data["admin"].id,
        release_reason="先释放一张",
        idempotency_key="ceil-release-first-release",
    )
    assert release_first.released_requirement_quantity == 2
    db.refresh(release_first_lot)
    consume_semi_finished_reservation(
        db,
        reservation_id=release_first.id,
        stock_quantity=1,
        expected_version=release_first_lot.version,
        operator_id=data["admin"].id,
        idempotency_key="ceil-release-first-consume",
    )
    assert release_first.consumed_requirement_quantity == 3
    assert active_semi_requirement_credited_quantity(
        db, release_first_requirement.id
    ) == 3


def test_delivery_allocation_tracks_consumption_and_reversal(semi_db) -> None:
    db, data = semi_db
    requirement = add_requirement(
        db, data, required=5, stock_yield_per_sheet=3
    )
    lot = add_semi_lot(
        db,
        data,
        key="delivery-allocation-lot",
        quantity=2,
        stock_yield_per_sheet=3,
    )
    batch = reserve_semi_finished_inventory(
        db,
        requirement_id=requirement.id,
        requested_requirement_quantity=5,
        lots=[SemiFinishedLotVersion(lot.id, lot.version)],
        operator_id=data["admin"].id,
        idempotency_key="delivery-allocation-reserve",
        confirmed=True,
    )
    reservation = batch.reservations[0]
    delivery = Delivery(
        delivery_number="TM-DLV-SEMI-001",
        customer_id=data["customer"].id,
        delivery_date=date.today(),
        status="dispatched",
        total_quantity=5,
        created_by=data["admin"].id,
    )
    db.add(delivery)
    db.flush()
    delivery_item = DeliveryItem(
        delivery_id=delivery.id,
        order_item_id=data["items"][0].id,
        delivered_quantity=5,
    )
    db.add(delivery_item)
    db.flush()
    db.refresh(lot)

    consumed = consume_semi_finished_reservation(
        db,
        reservation_id=reservation.id,
        stock_quantity=2,
        expected_version=lot.version,
        operator_id=data["admin"].id,
        idempotency_key="delivery-allocation-consume",
        delivery_item_id=delivery_item.id,
    )
    allocation = consumed.allocation
    assert isinstance(allocation, DeliveryInventoryAllocation)
    assert allocation.credited_requirement_quantity == 5
    assert reservation.consumed_requirement_quantity == 5
    db.refresh(lot)
    reversed_result = reverse_semi_finished_consumption(
        db,
        reservation_id=reservation.id,
        stock_quantity=1,
        expected_version=lot.version,
        operator_id=data["admin"].id,
        idempotency_key="delivery-allocation-reverse",
        allocation_id=allocation.id,
    )
    assert reversed_result.allocation.id == allocation.id
    assert allocation.reversed_stock_quantity == 1
    assert allocation.reversed_requirement_quantity == 2
    assert reservation.consumed_requirement_quantity == 3
    assert allocation.status == "partial"
    assert reversed_result.movement.reversal_of_movement_id == consumed.movement.id
    repeated = reverse_semi_finished_consumption(
        db,
        reservation_id=reservation.id,
        stock_quantity=1,
        expected_version=1,
        operator_id=data["admin"].id,
        idempotency_key="delivery-allocation-reverse",
        allocation_id=allocation.id,
    )
    assert repeated.allocation.id == allocation.id


def test_cover_base_are_isolated_and_double_splice_conversion_is_preserved(
    semi_db,
) -> None:
    db, data = semi_db
    cover = add_requirement(
        db,
        data,
        component_type="cover",
        length=400,
        width=300,
        required=100,
    )
    base = add_requirement(
        db,
        data,
        component_type="base",
        length=375,
        width=275,
        required=100,
    )
    cover_lot = add_semi_lot(
        db,
        data,
        key="cover-lot",
        length=400,
        width=300,
        component_type="cover",
    )
    base_lot = add_semi_lot(
        db,
        data,
        key="base-lot",
        length=375,
        width=275,
        component_type="base",
    )
    assert {row.lot.id for row in semi_finished_inventory_candidates(db, cover.id)} == {
        cover_lot.id
    }
    assert {row.lot.id for row in semi_finished_inventory_candidates(db, base.id)} == {
        base_lot.id
    }
    with pytest.raises(WarehouseInventoryError, match="严格隔离"):
        confirm(db, data, cover.id, base_lot.id, override=True)

    double_requirement = add_requirement(
        db,
        data,
        item_index=1,
        pieces_per_box=2,
        stock_yield_per_sheet=3,
        required=400,
    )
    double_lot = add_semi_lot(
        db,
        data,
        key="double-splice-lot",
        quantity=10,
        pieces_per_box=2,
        stock_yield_per_sheet=3,
    )
    candidate = semi_finished_inventory_candidates(db, double_requirement.id)[0]
    assert double_requirement.pieces_per_box == 2
    assert double_requirement.stock_yield_per_sheet == 3
    assert double_lot.semi_finished_detail.pieces_per_box == 2
    assert double_lot.semi_finished_detail.stock_yield_per_sheet == 3
    assert candidate.deductible_requirement_quantity == 30


def test_finished_goods_candidate_and_reservation_regression(semi_db) -> None:
    db, data = semi_db
    item = data["items"][0]
    product = data["products"][0]
    lot = manual_finished_in(
        db,
        customer_id=data["customer"].id,
        product_id=product.id,
        location_id=data["finished_location"].id,
        quantity=25,
        stock_date=date.today(),
        source_type="manual",
        remarks=None,
        operator_id=data["admin"].id,
        idempotency_key="finished-regression-lot",
    )
    assert [row.id for row in finished_inventory_candidates(db, item.id)] == [lot.id]
    reservation = reserve_finished_inventory(
        db,
        order_item_id=item.id,
        inventory_lot_id=lot.id,
        quantity=10,
        expected_version=lot.version,
        operator_id=data["admin"].id,
        idempotency_key="finished-regression-reserve",
        warning_acknowledged_codes=[],
    )
    db.refresh(lot)
    assert reservation.reservation_type == "finished_order"
    assert reservation.consumed_stock_quantity == 0
    assert reservation.released_stock_quantity == 0
    assert (lot.quantity_available, lot.quantity_reserved) == (15, 10)


def test_migration_upgrade_and_downgrade_on_disposable_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "semi-migration.sqlite3"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "semi-migration-test")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))

    command.upgrade(config, PARENT_REVISION)
    command.upgrade(config, REVISION)
    engine = create_sqlite_engine(database_path)
    inspector = inspect(engine)
    assert {
        "order_item_semi_requirements",
        "semi_finished_match_rules",
        "semi_finished_match_rule_products",
        "delivery_inventory_allocations",
    } <= set(inspector.get_table_names())
    reservation_columns = {
        row["name"] for row in inspector.get_columns("inventory_reservations")
    }
    assert {
        "semi_requirement_id",
        "match_rule_id",
        "consumed_stock_quantity",
        "released_stock_quantity",
        "consumed_requirement_quantity",
        "released_requirement_quantity",
        "reservation_group_key",
    } <= reservation_columns
    engine.dispose()

    command.downgrade(config, PARENT_REVISION)
    downgraded_engine = create_sqlite_engine(database_path)
    downgraded_inspector = inspect(downgraded_engine)
    assert "order_item_semi_requirements" not in set(
        downgraded_inspector.get_table_names()
    )
    downgraded_columns = {
        row["name"]
        for row in downgraded_inspector.get_columns("inventory_reservations")
    }
    assert "semi_requirement_id" not in downgraded_columns
    assert "consumed_stock_quantity" not in downgraded_columns
    assert "consumed_requirement_quantity" not in downgraded_columns
    downgraded_engine.dispose()
