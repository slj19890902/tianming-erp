from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def db(tmp_path: Path):
    from app.core.database import create_sqlite_engine
    from app.models import Base

    engine = create_sqlite_engine(tmp_path / "seven-layer-write-guards.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session
    engine.dispose()


def _customer_and_user(db):
    from app.models.customer import Customer
    from app.models.user import User

    customer = Customer(name="七层门禁测试客户", is_active=True)
    user = User(
        username="seven-layer-guard-admin",
        password_hash="test-only",
        role="admin",
        real_name="七层门禁测试",
        must_change_password=False,
    )
    db.add_all([customer, user])
    db.flush()
    return customer, user


def _material(db, *, layer_count: int, code: str):
    from app.models.material import Material

    material = Material(
        code=code,
        layer_count=layer_count,
        flute_type=None,
        supplier_name=f"{layer_count}层供应商",
        basis_weight_description=f"{layer_count}层克重",
        is_active=True,
    )
    db.add(material)
    db.flush()
    return material


def _product(
    db,
    *,
    customer_id: int,
    material,
    layer_count: int | None,
    flute_type: str | None,
    code: str = "BOX-001",
):
    from app.models.product import Product

    product = Product(
        customer_id=customer_id,
        product_code=code,
        customer_material_code=code,
        product_name=f"测试箱-{code}",
        material_id=material.id if material else None,
        layer_count=layer_count,
        flute_type=flute_type,
        box_category="normal",
        is_active=True,
    )
    db.add(product)
    db.flush()
    return product


def _order_item(db, *, customer_id: int, user_id: int, product, material):
    from app.models.order import Order, OrderItem

    order = Order(
        order_number="TM20260717001",
        customer_id=customer_id,
        order_date=dt.date(2026, 7, 17),
        status="pending_production",
        payment_status="unpaid",
        total_amount=Decimal("10.00"),
        created_by=user_id,
    )
    db.add(order)
    db.flush()
    item = OrderItem(
        order_id=order.id,
        product_id=product.id,
        item_sequence=1,
        item_order_number="TM20260717001-001",
        quantity=10,
        unit_price=Decimal("1.00"),
        subtotal=Decimal("10.00"),
        material_status="pending",
        requisition_status="未报料",
        snapshot_product_code=product.product_code,
        snapshot_product_name=product.product_name,
        snapshot_material=material.code,
        material_id=material.id,
        layer_count=material.layer_count,
        flute_type="B",
    )
    db.add(item)
    db.flush()
    return order, item


def _confirmation_token(exc: HTTPException) -> str:
    assert exc.status_code == 409
    assert exc.detail["code"] == "MASTER_CHANGE_CONFIRMATION_REQUIRED"
    return exc.detail["confirmation_token"]


@pytest.mark.parametrize(
    ("layer_count", "flute_type"),
    [(3, "A"), (3, "B"), (3, "E"), (5, "AB"), (5, "BE"), (7, "AAA"), (7, "ABC")],
)
def test_strict_write_validator_accepts_legal_matrix(layer_count, flute_type):
    from app.services.flute_mapping import validate_flute_for_write

    assert validate_flute_for_write(flute_type, layer_count) is None


@pytest.mark.parametrize(
    ("layer_count", "flute_type"),
    [(3, "AB"), (5, "B"), (7, None), (7, "AB"), (None, "B"), (4, "B")],
)
def test_strict_write_validator_rejects_bypasses(layer_count, flute_type):
    from app.services.flute_mapping import validate_flute_for_write

    assert validate_flute_for_write(flute_type, layer_count)


def test_historical_empty_flute_read_remains_available(db):
    from app.api.products import get_product
    from app.services.flute_mapping import (
        normalize_flute_type,
        validate_flute_consistency,
        validate_flute_for_write,
    )

    customer, user = _customer_and_user(db)
    material = _material(db, layer_count=7, code="A12345B")
    product = _product(
        db,
        customer_id=customer.id,
        material=material,
        layer_count=7,
        flute_type=None,
    )
    db.commit()

    assert validate_flute_consistency(None, 7) is None
    assert validate_flute_for_write(None, 7)
    assert normalize_flute_type(None) is None
    response = get_product(product.id, db=db, user=user)
    assert response["layer_count"] == 7
    assert response["flute_type"] is None


@pytest.mark.parametrize("flute_type", [None, ""])
def test_product_post_put_schema_rejects_empty_seven_layer_flute(flute_type):
    from app.api.products import ProductPayload

    with pytest.raises(ValidationError):
        ProductPayload(
            customer_id=1,
            product_code="BOX-EMPTY-7",
            customer_material_code="BOX-EMPTY-7",
            product_name="七层空楞型",
            material_id=1,
            box_category="normal",
            layer_count=7,
            flute_type=flute_type,
        )


def test_product_post_put_cannot_spoof_selected_material_layer(db):
    from app.api.products import (
        ProductPayload,
        ProductUpdatePayload,
        create_product,
        update_product,
    )
    from app.models.product import Product

    customer, user = _customer_and_user(db)
    material = _material(db, layer_count=7, code="A12345B")
    db.commit()

    spoofed_create = ProductPayload(
        customer_id=customer.id,
        product_code="BOX-SPOOF-CREATE",
        customer_material_code="BOX-SPOOF-CREATE",
        product_name="伪造层数创建",
        material_id=material.id,
        box_category="normal",
        layer_count=3,
        flute_type=None,
    )
    with pytest.raises(HTTPException) as exc_info:
        create_product(spoofed_create, db=db, user=user)
    assert exc_info.value.status_code == 400
    assert db.query(Product).count() == 0

    valid_payload = ProductPayload(
        customer_id=customer.id,
        product_code="BOX-VALID-7",
        customer_material_code="BOX-VALID-7",
        product_name="合法七层箱",
        material_id=material.id,
        box_category="normal",
        layer_count=None,
        flute_type="AAA",
    )
    created = create_product(valid_payload, db=db, user=user)
    product = db.get(Product, created["id"])
    assert (product.layer_count, product.flute_type) == (7, "AAA")

    spoofed_update = ProductUpdatePayload(
        customer_id=customer.id,
        product_code=product.product_code,
        customer_material_code=product.customer_material_code,
        product_name=product.product_name,
        material_id=material.id,
        box_category="normal",
        layer_count=3,
        flute_type=None,
        expected_version=product.version,
        change_reason="验证材质层数不可伪造",
    )
    with pytest.raises(HTTPException) as exc_info:
        update_product(product.id, spoofed_update, db=db, user=user)
    assert exc_info.value.status_code == 400
    assert (product.layer_count, product.flute_type) == (7, "AAA")

    valid_update = ProductUpdatePayload(
        customer_id=customer.id,
        product_code=product.product_code,
        customer_material_code=product.customer_material_code,
        product_name=product.product_name,
        material_id=material.id,
        box_category="normal",
        layer_count=None,
        flute_type="ABC",
        expected_version=product.version,
        change_reason="修正七层产品楞型",
    )
    with pytest.raises(HTTPException) as exc_info:
        update_product(product.id, valid_update, db=db, user=user)
    valid_update = valid_update.model_copy(
        update={"confirmation_token": _confirmation_token(exc_info.value)}
    )
    update_product(product.id, valid_update, db=db, user=user)
    assert (product.layer_count, product.flute_type) == (7, "ABC")


def test_product_sync_fields_uses_selected_material_layer(db):
    from app.api.products import SyncFieldsPayload, sync_product_fields

    customer, user = _customer_and_user(db)
    material = _material(db, layer_count=7, code="A12345B")
    product = _product(
        db,
        customer_id=customer.id,
        material=material,
        layer_count=7,
        flute_type=None,
    )
    db.commit()

    with pytest.raises(HTTPException) as exc_info:
        sync_product_fields(
            product.id,
            SyncFieldsPayload(
                fields={"remark": "不能绕过七层空楞型"},
                expected_version=product.version,
                change_reason="验证七层楞型校验",
            ),
            db=db,
            user=user,
        )
    assert exc_info.value.status_code == 400

    with pytest.raises(HTTPException) as exc_info:
        sync_product_fields(
            product.id,
            SyncFieldsPayload(
                fields={
                    "material_id": material.id,
                    "layer_count": 3,
                    "flute_type": "ABC",
                },
                expected_version=product.version,
                change_reason="验证材质层数不可伪造",
            ),
            db=db,
            user=user,
        )
    assert exc_info.value.status_code == 400

    sync_payload = SyncFieldsPayload(
        fields={
            "material_id": material.id,
            "layer_count": 7,
            "flute_type": "ABC",
        },
        expected_version=product.version,
        change_reason="同步订单确认的七层楞型",
    )
    with pytest.raises(HTTPException) as exc_info:
        sync_product_fields(product.id, sync_payload, db=db, user=user)
    sync_product_fields(
        product.id,
        sync_payload.model_copy(
            update={"confirmation_token": _confirmation_token(exc_info.value)}
        ),
        db=db,
        user=user,
    )
    assert (product.layer_count, product.flute_type) == (7, "ABC")
    assert material.flute_type is None


def test_order_create_uses_real_material_layer(db):
    from app.api.orders import OrderCreate, OrderItemCreate, create_order
    from app.models.order import OrderItem

    customer, user = _customer_and_user(db)
    material = _material(db, layer_count=7, code="A12345B")
    product = _product(
        db,
        customer_id=customer.id,
        material=material,
        layer_count=3,
        flute_type="B",
    )
    db.commit()

    with pytest.raises(HTTPException) as exc_info:
        create_order(
            OrderCreate(
                customer_id=customer.id,
                order_date=dt.date(2026, 7, 17),
                items=[
                    OrderItemCreate(
                        product_id=product.id,
                        material_id=material.id,
                        layer_count=3,
                        flute_type="B",
                        quantity=10,
                        unit_price=Decimal("1.00"),
                    )
                ],
            ),
            db=db,
            user=user,
        )
    assert exc_info.value.status_code == 400

    create_order(
        OrderCreate(
            customer_id=customer.id,
            order_date=dt.date(2026, 7, 17),
            items=[
                OrderItemCreate(
                    product_id=product.id,
                    material_id=material.id,
                    layer_count=7,
                    flute_type="AAA",
                    quantity=10,
                    unit_price=Decimal("1.00"),
                )
            ],
        ),
        db=db,
        user=user,
    )
    item = db.query(OrderItem).one()
    assert (item.material_id, item.layer_count, item.flute_type) == (
        material.id,
        7,
        "AAA",
    )


def test_order_edit_uses_real_selected_material_layer(db):
    from app.api.orders import OrderItemUpdate, update_order_item

    customer, user = _customer_and_user(db)
    three_layer = _material(db, layer_count=3, code="A3B")
    seven_layer = _material(db, layer_count=7, code="A12345B")
    product = _product(
        db,
        customer_id=customer.id,
        material=three_layer,
        layer_count=3,
        flute_type="B",
    )
    _, item = _order_item(
        db,
        customer_id=customer.id,
        user_id=user.id,
        product=product,
        material=three_layer,
    )
    db.commit()

    def payload(flute_type: str, layer_count: int = 7) -> OrderItemUpdate:
        return OrderItemUpdate(
            quantity=10,
            unit_price=Decimal("1.00"),
            product_code=product.product_code,
            product_name=product.product_name,
            material=seven_layer.code,
            material_id=seven_layer.id,
            layer_count=layer_count,
            flute_type=flute_type,
            sync_product=False,
        )

    with pytest.raises(HTTPException) as exc_info:
        update_order_item(item.id, payload("B"), db=db, user=user)
    assert exc_info.value.status_code == 400
    assert (item.material_id, item.layer_count, item.flute_type) == (
        three_layer.id,
        3,
        "B",
    )

    with pytest.raises(HTTPException) as exc_info:
        update_order_item(item.id, payload("ABC", 3), db=db, user=user)
    assert exc_info.value.status_code == 400

    update_order_item(item.id, payload("ABC"), db=db, user=user)
    assert (item.material_id, item.layer_count, item.flute_type) == (
        seven_layer.id,
        7,
        "ABC",
    )


@pytest.mark.parametrize("flute_type", [None, "", "B"])
def test_requisition_material_replacement_rejects_empty_or_spoofed_flute(
    db,
    flute_type,
):
    from app.api.requisition import PendingMaterialUpdate, update_pending_material

    customer, user = _customer_and_user(db)
    three_layer = _material(db, layer_count=3, code="A3B")
    seven_layer = _material(db, layer_count=7, code="A12345B")
    product = _product(
        db,
        customer_id=customer.id,
        material=three_layer,
        layer_count=3,
        flute_type="B",
    )
    _, item = _order_item(
        db,
        customer_id=customer.id,
        user_id=user.id,
        product=product,
        material=three_layer,
    )
    db.commit()

    with pytest.raises(HTTPException) as exc_info:
        update_pending_material(
            item.id,
            PendingMaterialUpdate(
                material_id=seven_layer.id,
                layer_count=7,
                flute_type=flute_type,
                sync_product=True,
                product_expected_version=product.version,
                product_change_reason="验证七层楞型校验",
            ),
            db=db,
            user=user,
        )
    assert exc_info.value.status_code == 400
    assert (item.material_id, item.layer_count, item.flute_type) == (
        three_layer.id,
        3,
        "B",
    )


def test_requisition_material_replacement_stores_snapshot_not_dictionary_flute(db):
    from app.api.requisition import PendingMaterialUpdate, update_pending_material

    customer, user = _customer_and_user(db)
    three_layer = _material(db, layer_count=3, code="A3B")
    seven_layer = _material(db, layer_count=7, code="A12345B")
    product = _product(
        db,
        customer_id=customer.id,
        material=three_layer,
        layer_count=3,
        flute_type="B",
    )
    _, item = _order_item(
        db,
        customer_id=customer.id,
        user_id=user.id,
        product=product,
        material=three_layer,
    )
    db.commit()

    with pytest.raises(HTTPException) as exc_info:
        update_pending_material(
            item.id,
            PendingMaterialUpdate(
                material_id=seven_layer.id,
                layer_count=3,
                flute_type="AAA",
                sync_product=True,
                product_expected_version=product.version,
                product_change_reason="验证材质层数不可伪造",
            ),
            db=db,
            user=user,
        )
    assert exc_info.value.status_code == 400

    sync_payload = PendingMaterialUpdate(
        material_id=seven_layer.id,
        layer_count=7,
        flute_type="AAA",
        sync_product=True,
        product_expected_version=product.version,
        product_change_reason="报料材质调整同步常用箱",
    )
    with pytest.raises(HTTPException) as exc_info:
        update_pending_material(item.id, sync_payload, db=db, user=user)
    update_pending_material(
        item.id,
        sync_payload.model_copy(
            update={"product_confirmation_token": _confirmation_token(exc_info.value)}
        ),
        db=db,
        user=user,
    )
    assert (item.material_id, item.layer_count, item.flute_type) == (
        seven_layer.id,
        7,
        "AAA",
    )
    assert (product.material_id, product.layer_count, product.flute_type) == (
        seven_layer.id,
        7,
        "AAA",
    )
    assert seven_layer.flute_type is None


@pytest.mark.parametrize("declared_layer", [None, 3, 5])
def test_material_master_rejects_seven_character_code_with_wrong_layer(
    declared_layer,
):
    from app.api.materials import MaterialPayload

    with pytest.raises(ValidationError, match="7位材质代码必须按七层保存"):
        MaterialPayload(
            code="JA616AJ",
            layer_count=declared_layer,
            supplier_name="七层测试供应商",
        )


def test_product_without_material_id_cannot_disguise_seven_layer_code(db):
    from app.api.products import ProductPayload, create_product

    customer, user = _customer_and_user(db)
    payload = ProductPayload(
        customer_id=customer.id,
        product_code="FREE-7-AS-3",
        customer_material_code="FREE-7-AS-3",
        product_name="七位代码伪装三层",
        legacy_material_text="JA616AJ",
        box_category="normal",
        layer_count=3,
        flute_type="B",
    )

    with pytest.raises(HTTPException, match="7位材质代码必须按七层保存"):
        create_product(payload, db=db, user=user)


def test_order_free_text_cannot_disguise_seven_layer_code(db):
    from app.api.orders import OrderCreate, OrderItemCreate, create_order

    customer, user = _customer_and_user(db)
    product = _product(
        db,
        customer_id=customer.id,
        material=None,
        layer_count=3,
        flute_type="B",
        code="LEGACY-FREE-7",
    )
    product.legacy_material_text = "JA616AJ"
    db.commit()

    with pytest.raises(HTTPException, match="7位材质代码必须按七层保存"):
        create_order(
            OrderCreate(
                customer_id=customer.id,
                order_date=dt.date(2026, 7, 17),
                items=[
                    OrderItemCreate(
                        product_id=product.id,
                        material="JA616AJ",
                        layer_count=3,
                        flute_type="B",
                        quantity=10,
                        unit_price=Decimal("1.00"),
                    )
                ],
            ),
            db=db,
            user=user,
        )


def test_bulk_flute_mapping_never_downgrades_linked_seven_layer_material(db):
    from app.services.flute_mapping import apply_flute_mapping, preview_flute_mapping

    customer, user = _customer_and_user(db)
    material = _material(db, layer_count=7, code="JA616AJ")
    product = _product(
        db,
        customer_id=customer.id,
        material=material,
        layer_count=7,
        flute_type=None,
        code="MAP-7-PRODUCT",
    )
    product.legacy_material_text = "100/100/100/B"
    db.commit()

    preview = preview_flute_mapping(db)
    result = apply_flute_mapping(db, user=user)

    assert all(row.product_id != product.id for row in preview.rows)
    assert result.updated == 0
    assert (product.layer_count, product.flute_type) == (7, None)
