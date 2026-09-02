from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.orders import (
    OrderItemCreate,
    OrderItemReservationPlan,
    SemiReservationPlanEntry,
    _preflight_reservation_plans,
)
from app.api.requisition import (
    PendingSemiInventoryReservationPayload,
    reserve_semi_inventory_from_pending,
)
from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.user import User
from app.models.warehouse_inventory import (
    InventoryLot,
    SemiFinishedLotAllowedProduct,
    WarehouseLocation,
)
from app.services.inventory_insights import build_inventory_insights
from app.services.semi_finished_inventory import (
    CUSTOMER_GENERIC_SEMI_FINISHED_STOCK,
    GENERAL_SEMI_FINISHED_STOCK,
    REVERSE_CREASE_ADMIN_OVERRIDE,
    SemiFinishedSignature,
    SemiFinishedLotVersion,
    confirm_semi_finished_match,
    consume_semi_finished_reservation,
    is_direct_semi_finished_match,
    reserve_semi_finished_inventory,
    reverse_semi_finished_consumption,
    save_order_item_semi_requirement,
    semi_finished_inventory_candidates,
)


def test_direct_deduction_requires_every_physical_fact_to_match(
    eligibility_db,
) -> None:
    db, data = eligibility_db
    lot = _add_lot(
        db,
        data,
        key="direct-exact",
        customer_id=data["customer"].id,
    )
    detail = lot.semi_finished_detail
    assert detail is not None
    detail.customer_generic_eligible = True
    detail.sheet_type = "net_sheet"
    detail.crease_type = "净料"
    expected = SemiFinishedSignature(
        customer_id=data["customer"].id,
        board_length_mm=800,
        board_width_mm=600,
        normalized_material_code="A416D",
        flute_type="B",
        component_type="whole",
        pieces_per_box=1,
        stock_yield_per_sheet=1,
    )

    def matches(**changes) -> bool:
        facts = {
            "layer_count": 3,
            "crease_type": "净料",
            "crease_left_mm": None,
            "crease_middle_mm": None,
            "crease_right_mm": None,
        }
        facts.update(changes)
        return is_direct_semi_finished_match(detail, expected=expected, **facts)

    assert matches() is True
    assert matches(layer_count=5) is False
    assert matches(crease_type="毛片") is False
    assert matches(crease_type="压线", crease_left_mm=100, crease_middle_mm=400, crease_right_mm=100) is False
    detail.normalized_material_code = "B555B"
    assert matches() is False
    detail.normalized_material_code = "A416D"
    detail.owner_customer_id = None
    assert matches() is False


def test_customer_generic_lot_matches_same_customer_without_product_or_material_binding(
    eligibility_db,
) -> None:
    db, data = eligibility_db
    lot = _add_lot(
        db,
        data,
        key="customer-generic-material-diff",
        customer_id=data["customer"].id,
    )
    assert lot.semi_finished_detail is not None
    lot.semi_finished_detail.material_code_snapshot = "B555B"
    lot.semi_finished_detail.normalized_material_code = "B555B"
    lot.semi_finished_detail.customer_generic_eligible = True
    lot.semi_finished_detail.internal_name = "800x600 customer generic board"
    _order_a, _item_a, requirement_a = _add_requirement(
        db, data, product=data["products"][0], key="GENERIC-A"
    )
    _order_b, _item_b, requirement_b = _add_requirement(
        db, data, product=data["products"][1], key="GENERIC-B"
    )
    _other_order, _other_item, other_requirement = _add_requirement(
        db, data, product=data["other_product"], key="GENERIC-OTHER"
    )
    db.flush()

    for requirement in (requirement_a, requirement_b):
        candidate = next(
            row
            for row in semi_finished_inventory_candidates(db, requirement.id)
            if row.lot.id == lot.id
        )
        assert candidate.source == "customer_generic"
        assert "material_code" in candidate.signature_differences
        assert CUSTOMER_GENERIC_SEMI_FINISHED_STOCK in candidate.warning_codes
    assert lot.id not in {
        row.lot.id
        for row in semi_finished_inventory_candidates(db, other_requirement.id)
    }

    batch = reserve_semi_finished_inventory(
        db,
        requirement_id=requirement_b.id,
        requested_requirement_quantity=3,
        lots=[SemiFinishedLotVersion(lot.id, lot.version)],
        operator_id=data["admin"].id,
        idempotency_key="customer-generic-reserve",
        confirmed=True,
        warning_acknowledged_codes=[CUSTOMER_GENERIC_SEMI_FINISHED_STOCK],
    )
    assert batch.allocated_requirement_quantity == 3


def test_customer_generic_crease_is_directional_and_reverse_requires_admin_override(
    eligibility_db,
) -> None:
    db, data = eligibility_db
    product = data["products"][0]

    raw_lot = _add_lot(
        db,
        data,
        key="customer-generic-uncreased",
        customer_id=data["customer"].id,
    )
    assert raw_lot.semi_finished_detail is not None
    raw_lot.semi_finished_detail.customer_generic_eligible = True
    raw_lot.semi_finished_detail.sheet_type = "raw_board"
    raw_lot.semi_finished_detail.crease_type = "毛片"
    _pressed_order, pressed_item, pressed_requirement = _add_requirement(
        db, data, product=product, key="GENERIC-PRESSED"
    )
    pressed_item.snapshot_crease_type = "压线"
    pressed_item.snapshot_crease_left_mm = 200
    pressed_item.snapshot_crease_middle_mm = 200
    pressed_item.snapshot_crease_right_mm = 200
    db.flush()
    forward = reserve_semi_finished_inventory(
        db,
        requirement_id=pressed_requirement.id,
        requested_requirement_quantity=1,
        lots=[SemiFinishedLotVersion(raw_lot.id, raw_lot.version)],
        operator_id=data["admin"].id,
        idempotency_key="customer-generic-forward-crease",
        confirmed=True,
        warning_acknowledged_codes=[CUSTOMER_GENERIC_SEMI_FINISHED_STOCK],
    )
    assert forward.allocated_requirement_quantity == 1

    creased_lot = _add_lot(
        db,
        data,
        key="customer-generic-creased",
        customer_id=data["customer"].id,
    )
    assert creased_lot.semi_finished_detail is not None
    creased_lot.semi_finished_detail.customer_generic_eligible = True
    creased_lot.semi_finished_detail.sheet_type = "creased_sheet"
    creased_lot.semi_finished_detail.crease_type = "压线"
    creased_lot.semi_finished_detail.crease_left_mm = 200
    creased_lot.semi_finished_detail.crease_middle_mm = 200
    creased_lot.semi_finished_detail.crease_right_mm = 200
    _raw_order, raw_item, raw_requirement = _add_requirement(
        db, data, product=product, key="GENERIC-RAW"
    )
    raw_item.snapshot_crease_type = "毛片"
    db.flush()

    reverse_candidate = next(
        row
        for row in semi_finished_inventory_candidates(db, raw_requirement.id)
        if row.lot.id == creased_lot.id
    )
    assert REVERSE_CREASE_ADMIN_OVERRIDE in reverse_candidate.warning_codes

    with pytest.raises(WarehouseInventoryError, match="管理员") as error:
        reserve_semi_finished_inventory(
            db,
            requirement_id=raw_requirement.id,
            requested_requirement_quantity=1,
            lots=[SemiFinishedLotVersion(creased_lot.id, creased_lot.version)],
            operator_id=data["admin"].id,
            idempotency_key="customer-generic-reverse-blocked",
            confirmed=True,
            warning_acknowledged_codes=[CUSTOMER_GENERIC_SEMI_FINISHED_STOCK],
        )
    assert error.value.status_code == 403

    allowed = reserve_semi_finished_inventory(
        db,
        requirement_id=raw_requirement.id,
        requested_requirement_quantity=1,
        lots=[SemiFinishedLotVersion(creased_lot.id, creased_lot.version)],
        operator_id=data["admin"].id,
        idempotency_key="customer-generic-reverse-approved",
        confirmed=True,
        warning_acknowledged_codes=[
            CUSTOMER_GENERIC_SEMI_FINISHED_STOCK,
            REVERSE_CREASE_ADMIN_OVERRIDE,
        ],
        admin_reverse_crease_override=True,
        reverse_crease_override_reason="现场确认特殊返工",
    )
    assert allowed.allocated_requirement_quantity == 1
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    manual_semi_finished_in,
    replace_semi_finished_lot_allowed_products,
)


@pytest.fixture()
def eligibility_db(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "semi-lot-eligibility.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="eligibility-admin",
            password_hash="test",
            role="admin",
            real_name="资格测试管理员",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=9301,
            customer_code="ELIG-A",
            name="资格客户A",
            payment_term_days=0,
            credit_limit=0,
        )
        other_customer = Customer(
            customer_number=9302,
            customer_code="ELIG-B",
            name="资格客户B",
            payment_term_days=0,
            credit_limit=0,
        )
        db.add_all([admin, customer, other_customer])
        db.flush()

        def product(owner: Customer, code: str) -> Product:
            return Product(
                customer_id=owner.id,
                product_code=code,
                customer_material_code=f"{code}-M",
                product_name=f"资格款号{code}",
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
                is_active=True,
            )

        products = [product(customer, f"ELIG-A-{index}") for index in range(1, 4)]
        other_product = product(other_customer, "ELIG-B-1")
        location = WarehouseLocation(
            location_code="ELIG-SF-01",
            location_name="资格半成品库位",
            warehouse_type="semi_finished",
        )
        db.add_all([*products, other_product, location])
        db.commit()
        yield db, {
            "admin": admin,
            "customer": customer,
            "other_customer": other_customer,
            "products": products,
            "other_product": other_product,
            "location": location,
        }


def _add_requirement(
    db: Session,
    data: dict,
    *,
    product: Product,
    key: str,
    required: int = 20,
):
    order = Order(
        order_number=f"ELIG-{key}",
        customer_id=product.customer_id,
        order_date=date.today(),
        status="pending_production",
        payment_status="unpaid",
        total_amount=Decimal(required),
    )
    db.add(order)
    db.flush()
    item = OrderItem(
        order_id=order.id,
        product_id=product.id,
        quantity=required,
        unit_price=Decimal("1"),
        subtotal=Decimal(required),
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
    db.flush()
    requirement = save_order_item_semi_requirement(
        db,
        order_item_id=item.id,
        component_type="whole",
        board_length_mm=800,
        board_width_mm=600,
        material_code="A416D",
        flute_type="B",
        pieces_per_box=1,
        stock_yield_per_sheet=1,
        required_piece_quantity=required,
        operator_id=data["admin"].id,
    )
    return order, item, requirement


def _add_lot(
    db: Session,
    data: dict,
    *,
    key: str,
    customer_id: int | None,
    quantity: int = 20,
    length: int = 800,
):
    return manual_semi_finished_in(
        db,
        location_id=data["location"].id,
        quantity=quantity,
        stock_date=date.today(),
        source_type="manual",
        material_code="A416D",
        layer_count=3,
        flute_type="B",
        board_length_mm=length,
        board_width_mm=600,
        sheet_type="net_sheet",
        component_type="whole",
        pieces_per_box=1,
        stock_yield_per_sheet=1,
        supplier_name=None,
        customer_id=customer_id,
        crease_type=None,
        crease_left_mm=None,
        crease_middle_mm=None,
        crease_right_mm=None,
        cutting_note=None,
        remarks=None,
        operator_id=data["admin"].id,
        idempotency_key=key,
    )


def _bind(
    db: Session,
    data: dict,
    lot,
    products: list[Product],
):
    return replace_semi_finished_lot_allowed_products(
        db,
        inventory_lot_id=lot.id,
        product_ids=[product.id for product in products],
        expected_version=lot.version,
        operator_id=data["admin"].id,
    )


def test_general_lot_matches_cross_customer_and_requires_warning_ack(
    eligibility_db,
) -> None:
    db, data = eligibility_db
    lot = _add_lot(db, data, key="general-cross-customer", customer_id=None)
    mismatched = _add_lot(
        db,
        data,
        key="general-physical-mismatch",
        customer_id=None,
        length=900,
    )
    _order_a, _item_a, requirement_a = _add_requirement(
        db, data, product=data["products"][0], key="GENERAL-A"
    )
    _order_b, _item_b, requirement_b = _add_requirement(
        db, data, product=data["other_product"], key="GENERAL-B"
    )

    for requirement in (requirement_a, requirement_b):
        candidates = semi_finished_inventory_candidates(db, requirement.id)
        assert mismatched.id not in {candidate.lot.id for candidate in candidates}
        candidate = candidates[0]
        assert candidate.lot.id == lot.id
        assert candidate.source == "general_signature"
        assert candidate.signature_differences == ()
        assert GENERAL_SEMI_FINISHED_STOCK in candidate.warning_codes

    with pytest.raises(WarehouseInventoryError, match="GENERAL_SEMI_FINISHED_STOCK") as error:
        reserve_semi_finished_inventory(
            db,
            requirement_id=requirement_a.id,
            requested_requirement_quantity=4,
            lots=[SemiFinishedLotVersion(lot.id, lot.version)],
            operator_id=data["admin"].id,
            idempotency_key="general-missing-warning",
            confirmed=True,
        )
    assert error.value.status_code == 409

    batch = reserve_semi_finished_inventory(
        db,
        requirement_id=requirement_a.id,
        requested_requirement_quantity=4,
        lots=[SemiFinishedLotVersion(lot.id, lot.version)],
        operator_id=data["admin"].id,
        idempotency_key="general-confirmed",
        confirmed=True,
        warning_acknowledged_codes=[GENERAL_SEMI_FINISHED_STOCK],
    )
    assert batch.allocated_requirement_quantity == 4
    assert GENERAL_SEMI_FINISHED_STOCK in batch.reservations[0].warning_codes


def test_dedicated_multi_product_binding_is_hard_eligibility_boundary(
    eligibility_db,
) -> None:
    db, data = eligibility_db
    lot = _add_lot(
        db,
        data,
        key="dedicated-multiple",
        customer_id=data["customer"].id,
    )
    lot = _bind(db, data, lot, data["products"][:2])
    requirements = [
        _add_requirement(db, data, product=product, key=f"MULTI-{index}")[2]
        for index, product in enumerate(
            [*data["products"], data["other_product"]], start=1
        )
    ]

    assert lot.id in {
        row.lot.id for row in semi_finished_inventory_candidates(db, requirements[0].id)
    }
    assert lot.id in {
        row.lot.id for row in semi_finished_inventory_candidates(db, requirements[1].id)
    }
    assert lot.id not in {
        row.lot.id for row in semi_finished_inventory_candidates(db, requirements[2].id)
    }
    assert lot.id not in {
        row.lot.id for row in semi_finished_inventory_candidates(db, requirements[3].id)
    }

    for requirement, message in (
        (requirements[2], "未绑定当前成品款号"),
        (requirements[3], "其他客户专用"),
    ):
        with pytest.raises(WarehouseInventoryError, match=message) as error:
            reserve_semi_finished_inventory(
                db,
                requirement_id=requirement.id,
                requested_requirement_quantity=1,
                lots=[SemiFinishedLotVersion(lot.id, lot.version)],
                operator_id=data["admin"].id,
                idempotency_key=f"rejected-{requirement.id}",
                confirmed=True,
            )
        assert error.value.status_code == 409


def test_shared_match_memory_cannot_authorize_another_same_signature_lot(
    eligibility_db,
) -> None:
    db, data = eligibility_db
    first = _add_lot(
        db, data, key="memory-first", customer_id=data["customer"].id
    )
    second = _add_lot(
        db, data, key="memory-second", customer_id=data["customer"].id
    )
    first = _bind(db, data, first, [data["products"][0]])
    _order, _item, requirement = _add_requirement(
        db, data, product=data["products"][0], key="MEMORY"
    )
    confirm_semi_finished_match(
        db,
        requirement_id=requirement.id,
        inventory_lot_id=first.id,
        operator_id=data["admin"].id,
        override=False,
        warning_acknowledged_codes=[],
    )

    candidate_ids = {
        row.lot.id for row in semi_finished_inventory_candidates(db, requirement.id)
    }
    assert first.id in candidate_ids
    assert second.id not in candidate_ids


def test_forged_direct_reserve_is_rejected_and_abnormal_consume_returns_409(
    eligibility_db,
) -> None:
    db, data = eligibility_db
    lot = _add_lot(
        db, data, key="forged-reserve", customer_id=data["customer"].id
    )
    lot = _bind(db, data, lot, [data["products"][1]])
    _order, _item, requirement = _add_requirement(
        db, data, product=data["products"][0], key="FORGED"
    )
    with pytest.raises(WarehouseInventoryError, match="未绑定当前成品款号") as error:
        reserve_semi_finished_inventory(
            db,
            requirement_id=requirement.id,
            requested_requirement_quantity=2,
            lots=[SemiFinishedLotVersion(lot.id, lot.version)],
            operator_id=data["admin"].id,
            idempotency_key="forged-direct-reserve",
            confirmed=True,
            override=True,
        )
    assert error.value.status_code == 409

    lot = _bind(db, data, lot, [data["products"][0], data["products"][1]])
    batch = reserve_semi_finished_inventory(
        db,
        requirement_id=requirement.id,
        requested_requirement_quantity=2,
        lots=[SemiFinishedLotVersion(lot.id, lot.version)],
        operator_id=data["admin"].id,
        idempotency_key="valid-before-corruption",
        confirmed=True,
    )
    binding = db.scalar(
        select(SemiFinishedLotAllowedProduct).where(
            SemiFinishedLotAllowedProduct.inventory_lot_id == lot.id,
            SemiFinishedLotAllowedProduct.product_id == data["products"][0].id,
        )
    )
    assert binding is not None
    db.delete(binding)
    db.flush()
    current_lot = db.get(InventoryLot, lot.id)
    assert current_lot is not None
    with pytest.raises(WarehouseInventoryError, match="未绑定当前成品款号") as error:
        consume_semi_finished_reservation(
            db,
            reservation_id=batch.reservations[0].id,
            stock_quantity=1,
            expected_version=current_lot.version,
            operator_id=data["admin"].id,
            idempotency_key="corrupt-consume",
        )
    assert error.value.status_code == 409


def test_active_binding_cannot_drift_and_normal_consume_reverse_succeeds(
    eligibility_db,
) -> None:
    db, data = eligibility_db
    lot = _add_lot(
        db, data, key="consume-reverse", customer_id=data["customer"].id
    )
    lot = _bind(db, data, lot, [data["products"][0]])
    _order, _item, requirement = _add_requirement(
        db, data, product=data["products"][0], key="CONSUME"
    )
    batch = reserve_semi_finished_inventory(
        db,
        requirement_id=requirement.id,
        requested_requirement_quantity=5,
        lots=[SemiFinishedLotVersion(lot.id, lot.version)],
        operator_id=data["admin"].id,
        idempotency_key="consume-reserve",
        confirmed=True,
    )
    reservation = batch.reservations[0]
    current_lot = db.get(InventoryLot, lot.id)
    assert current_lot is not None
    with pytest.raises(WarehouseInventoryError, match="活跃预占或净消耗"):
        replace_semi_finished_lot_allowed_products(
            db,
            inventory_lot_id=current_lot.id,
            product_ids=[],
            expected_version=current_lot.version,
            operator_id=data["admin"].id,
        )

    consumed = consume_semi_finished_reservation(
        db,
        reservation_id=reservation.id,
        stock_quantity=2,
        expected_version=current_lot.version,
        operator_id=data["admin"].id,
        idempotency_key="consume-two",
    )
    assert consumed.reservation.consumed_stock_quantity == 2
    current_lot = db.get(InventoryLot, lot.id)
    assert current_lot is not None
    reversed_result = reverse_semi_finished_consumption(
        db,
        reservation_id=reservation.id,
        stock_quantity=1,
        expected_version=current_lot.version,
        operator_id=data["admin"].id,
        idempotency_key="reverse-one",
    )
    assert reversed_result.reservation.consumed_stock_quantity == 1


def test_order_preflight_rechecks_lot_hard_binding(eligibility_db) -> None:
    db, data = eligibility_db
    product = data["products"][0]
    lot = _add_lot(
        db, data, key="order-preflight", customer_id=data["customer"].id
    )

    def payload(version: int) -> OrderItemCreate:
        return OrderItemCreate(
            product_id=product.id,
            quantity=5,
            unit_price=Decimal("1"),
            material="A416D",
            flute_type="B",
            reservation_plan=OrderItemReservationPlan(
                semi=[
                    SemiReservationPlanEntry(
                        lot_id=lot.id,
                        expected_version=version,
                        requested_qty=2,
                        component_type="whole",
                        recommendation_source="signature",
                        confirmed=True,
                    )
                ]
            ),
        )

    with pytest.raises(WarehouseInventoryError, match="未绑定当前成品款号") as error:
        _preflight_reservation_plans(
            db,
            customer_id=data["customer"].id,
            payload_items=[payload(lot.version)],
            resolved_products={1: product},
        )
    assert error.value.status_code == 409

    lot = _bind(db, data, lot, [product])
    states = _preflight_reservation_plans(
        db,
        customer_id=data["customer"].id,
        payload_items=[payload(lot.version)],
        resolved_products={1: product},
    )
    assert states[lot.id]["external_version"] == lot.version


def test_pending_requisition_rechecks_lot_hard_binding(eligibility_db) -> None:
    db, data = eligibility_db
    product = data["products"][0]
    _order, item, _requirement = _add_requirement(
        db, data, product=product, key="PENDING-REQUISITION"
    )
    lot = _add_lot(
        db,
        data,
        key="pending-requisition-lot",
        customer_id=data["customer"].id,
    )
    db.commit()

    def payload(version: int, key: str) -> PendingSemiInventoryReservationPayload:
        return PendingSemiInventoryReservationPayload(
            order_item_id=item.id,
            component_type="whole",
            requested_requirement_quantity=2,
            lots=[{"lot_id": lot.id, "expected_version": version}],
            idempotency_key=key,
        )

    with pytest.raises(HTTPException, match="未绑定当前成品款号") as error:
        reserve_semi_inventory_from_pending(
            payload(lot.version, "pending-unbound"), db=db, user=data["admin"]
        )
    assert error.value.status_code == 409

    lot = db.get(InventoryLot, lot.id)
    assert lot is not None
    lot = _bind(db, data, lot, [product])
    result = reserve_semi_inventory_from_pending(
        payload(lot.version, "pending-bound"), db=db, user=data["admin"]
    )
    assert result["allocated_requirement_quantity"] == 2


def test_inventory_insights_distinguish_general_and_unbound_dedicated_lots(
    eligibility_db,
) -> None:
    db, data = eligibility_db
    general = _add_lot(db, data, key="insight-general", customer_id=None)
    dedicated = _add_lot(
        db, data, key="insight-dedicated", customer_id=data["customer"].id
    )
    result = build_inventory_insights(db, as_of=date.today())
    actions = {row["lot_id"]: row for row in result["action_items"]}

    general_action = actions[general.id]
    general_codes = {row["code"] for row in general_action["reasons"]}
    assert "semi_product_assignment_missing" not in general_codes
    assert general_action["detail"]["binding_scope"] == "general"
    assert (
        general_action["detail"]["deduction_eligibility"]
        == "physical_signature_required"
    )

    dedicated_action = actions[dedicated.id]
    dedicated_codes = {row["code"] for row in dedicated_action["reasons"]}
    assert "semi_product_assignment_missing" in dedicated_codes
    assert (
        dedicated_action["detail"]["deduction_eligibility"]
        == "ineligible_no_product_binding"
    )
