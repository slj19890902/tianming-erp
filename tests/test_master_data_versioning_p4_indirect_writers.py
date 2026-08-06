from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def db(tmp_path: Path):
    from app.core.database import create_sqlite_engine
    from app.models import Base

    engine = create_sqlite_engine(tmp_path / "master-data-versioning-p4.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session
    engine.dispose()


def _user(db: Session, username: str, role: str = "admin"):
    from app.models.user import User

    user = User(
        username=username,
        password_hash="test-only",
        role=role,
        real_name=username,
        must_change_password=False,
        customer_access_mode="all",
    )
    db.add(user)
    db.flush()
    return user


def _customer(db: Session, suffix: str = "P4"):
    from app.models.customer import Customer

    customer = Customer(
        customer_code=f"C-{suffix}",
        name=f"P4间接写入口客户-{suffix}",
        is_active=True,
    )
    db.add(customer)
    db.flush()
    return customer


def _material(db: Session, code: str = "A6A", *, layer_count: int | None = 3):
    from app.models.material import Material

    material = Material(
        code=code,
        supplier_name="P4测试供应商",
        layer_count=layer_count,
        flute_type=None,
        is_active=True,
    )
    db.add(material)
    db.flush()
    return material


def _product(db: Session, customer_id: int, *, code: str = "P4-BOX", **values):
    from app.models.product import Product

    product = Product(
        customer_id=customer_id,
        product_code=code,
        customer_material_code=code,
        product_name=values.pop("product_name", "P4常用箱"),
        is_active=True,
        **values,
    )
    db.add(product)
    db.flush()
    return product


def _order_item_graph(db: Session):
    from app.models.order import Order, OrderItem

    admin = _user(db, "p4-admin")
    limited = _user(db, "p4-finance", role="finance")
    customer = _customer(db)
    material = _material(db)
    product = _product(
        db,
        customer.id,
        material_id=material.id,
        layer_count=3,
        flute_type="B",
        splice_mode="single",
        pieces_per_box=1,
    )
    order = Order(
        order_number="P4-ORDER-001",
        customer_id=customer.id,
        order_date=date(2026, 7, 17),
        status="pending_production",
        payment_status="unpaid",
        total_amount=Decimal("10.00"),
        created_by=admin.id,
    )
    db.add(order)
    db.flush()
    item = OrderItem(
        order_id=order.id,
        product_id=product.id,
        quantity=10,
        unit_price=Decimal("1.00"),
        subtotal=Decimal("10.00"),
        material_status="pending",
        requisition_status="未报料",
        snapshot_product_code=product.product_code,
        snapshot_product_name=product.product_name,
        snapshot_material=material.code,
        material_id=material.id,
        layer_count=3,
        flute_type="B",
    )
    db.add(item)
    db.commit()
    return admin, limited, material, product, item


def test_order_create_auto_product_records_v1(db: Session) -> None:
    from app.api.orders import OrderCreate, OrderItemCreate, create_order
    from app.models.master_data_object_version import MasterDataObjectVersion
    from app.models.product import Product

    user = _user(db, "p4-order-import-admin")
    customer = _customer(db, "ORDER")
    db.commit()

    response = create_order(
        OrderCreate(
            customer_id=customer.id,
            order_date=date(2026, 7, 17),
            items=[
                OrderItemCreate(
                    is_new_product=True,
                    product_code="AUTO-P4-001",
                    product_name="订单导入自动箱",
                    specification="300×200×100",
                    quantity=12,
                    unit_price=Decimal("1.25"),
                )
            ],
        ),
        db=db,
        user=user,
    )

    product = db.scalar(select(Product).where(Product.product_code == "AUTO-P4-001"))
    assert product is not None
    assert product.version == 1
    revision = db.scalar(
        select(MasterDataObjectVersion).where(
            MasterDataObjectVersion.object_type == "product",
            MasterDataObjectVersion.object_id == product.id,
            MasterDataObjectVersion.version == 1,
        )
    )
    assert revision is not None
    assert revision.action == "create"
    assert revision.reason == "订单导入自动创建常用箱"
    assert revision.source == "orders.create.product-import"
    assert response["items"][0]["product_id"] == product.id


def test_order_and_requisition_sync_default_false() -> None:
    from app.api.orders import OrderItemUpdate
    from app.api.requisition import PendingMaterialUpdate

    order_payload = OrderItemUpdate(
        quantity=1,
        unit_price=Decimal("1.00"),
        product_code="P4-BOX",
        product_name="P4常用箱",
    )
    requisition_payload = PendingMaterialUpdate(material_id=1)

    assert order_payload.sync_product is False
    assert requisition_payload.sync_product is False


def test_order_sync_requires_permission_and_version_but_not_reason(db: Session) -> None:
    from app.api.orders import OrderItemUpdate, update_order_item

    admin, limited, _material_row, product, item = _order_item_graph(db)

    def payload(**overrides):
        values = {
            "quantity": 10,
            "unit_price": Decimal("1.00"),
            "product_code": product.product_code,
            "product_name": product.product_name,
            "sync_product": True,
        }
        values.update(overrides)
        return OrderItemUpdate(**values)

    with pytest.raises(HTTPException) as denied:
        update_order_item(
            item.id,
            payload(product_expected_version=1, product_change_reason="订单同步"),
            db=db,
            user=limited,
        )
    assert denied.value.status_code == 403

    with pytest.raises(HTTPException) as missing_version:
        update_order_item(
            item.id,
            payload(product_change_reason="订单同步"),
            db=db,
            user=admin,
        )
    assert missing_version.value.status_code == 400
    assert "product_expected_version" in str(missing_version.value.detail)

    updated = update_order_item(
        item.id,
        payload(product_expected_version=1, product_change_reason="  "),
        db=db,
        user=admin,
    )
    assert updated["id"] == item.id


def test_requisition_sync_requires_permission_and_version_but_not_reason(
    db: Session,
) -> None:
    from app.api.requisition import PendingMaterialUpdate, update_pending_material

    admin, limited, material, _product_row, item = _order_item_graph(db)

    def payload(**overrides):
        values = {
            "material_id": material.id,
            "sync_product": True,
        }
        values.update(overrides)
        return PendingMaterialUpdate(**values)

    with pytest.raises(HTTPException) as denied:
        update_pending_material(
            item.id,
            payload(product_expected_version=1, product_change_reason="报料同步"),
            db=db,
            user=limited,
        )
    assert denied.value.status_code == 403

    with pytest.raises(HTTPException) as missing_version:
        update_pending_material(
            item.id,
            payload(product_change_reason="报料同步"),
            db=db,
            user=admin,
        )
    assert missing_version.value.status_code == 400
    assert "product_expected_version" in str(missing_version.value.detail)

    updated = update_pending_material(
        item.id,
        payload(product_expected_version=1, product_change_reason=""),
        db=db,
        user=admin,
    )
    assert updated["selection_history_id"] is not None
    assert updated["material_id"] == material.id


def test_quotation_convert_to_product_records_v1(db: Session) -> None:
    from app.api.quotations import ConvertPayload, convert_to_product
    from app.models.master_data_object_version import MasterDataObjectVersion
    from app.models.product import Product
    from app.models.quotation import QuotationItem, QuotationOrder

    user = _user(db, "p4-quotation-admin")
    customer = _customer(db, "QUOTE")
    material = _material(db, "B8B")
    quotation = QuotationOrder(
        quotation_no="P4-Q-001",
        customer_id=customer.id,
        customer_name=customer.name,
        quotation_date=date(2026, 7, 17),
        status="accepted",
        total_amount=Decimal("25.00"),
        created_by=user.id,
    )
    db.add(quotation)
    db.flush()
    item = QuotationItem(
        quotation_id=quotation.id,
        product_name="报价转常用箱",
        box_type="A1/0201 普通开槽箱",
        length_mm=Decimal("300"),
        width_mm=Decimal("200"),
        height_mm=Decimal("100"),
        material_id=material.id,
        material_supplier=material.supplier_name,
        material_code=material.code,
        flute_type="B",
        quantity=20,
        margin_rate=Decimal("20"),
        final_unit_price=Decimal("1.25"),
    )
    db.add(item)
    db.commit()

    result = convert_to_product(
        item.id,
        ConvertPayload(product_code="QUOTE-P4-001", flute_type="B"),
        db=db,
        user=user,
        _product_creator=user,
    )

    product = db.get(Product, result["product_id"])
    assert product is not None
    assert product.version == 1
    revision = db.scalar(
        select(MasterDataObjectVersion).where(
            MasterDataObjectVersion.object_type == "product",
            MasterDataObjectVersion.object_id == product.id,
            MasterDataObjectVersion.version == 1,
        )
    )
    assert revision is not None
    assert revision.reason == "报价转常用箱创建主档"
    assert revision.source == "quotations.convert-to-product"


def test_system_batches_do_not_mutate_master_data_outside_version_service(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.models.material_mapping import MaterialCodeMappingCandidate
    from app.services import master_data_versioning
    from app.services.flute_mapping import (
        apply_flute_consistency_fix,
        apply_flute_mapping,
    )
    from app.services.material_mapping import (
        apply_customer_code_updates,
        apply_high_confidence_material_mapping,
    )

    user = _user(db, "p4-system-admin")
    customer = _customer(db, "SYSTEM")
    code_product = _product(
        db,
        customer.id,
        code="CODE-P4",
        product_name="A1234B/外箱",
    )
    new_material_product = _product(
        db,
        customer.id,
        code="MAT-NEW-P4",
        product_name="普通新材质箱",
        legacy_material_text="OLDNEW",
    )
    existing_material_product = _product(
        db,
        customer.id,
        code="MAT-OLD-P4",
        product_name="普通复用材质箱",
        legacy_material_text="OLDEXIST",
    )
    flute_product = _product(
        db,
        customer.id,
        code="FLUTE-P4",
        product_name="普通楞型箱",
        legacy_material_text="100/100/100/B",
        layer_count=None,
        flute_type=None,
    )
    consistency_product = _product(
        db,
        customer.id,
        code="FLUTE-FIX-P4",
        product_name="楞型一致性修复箱",
        layer_count=5,
        flute_type="B",
    )
    existing_material = _material(db, "B7B", layer_count=None)
    existing_material.flute_type = "B"
    db.add_all(
        [
            MaterialCodeMappingCandidate(
                old_code="OLDNEW",
                new_code="A6A",
                new_supplier="嘉林亿",
                old_supplier="旧供应商",
                layer_count="3层",
                weight_structure="100/100/100",
                confidence_label="可直接替换",
                confidence_level="高可信",
                review_status="pending",
            ),
            MaterialCodeMappingCandidate(
                old_code="OLDEXIST",
                new_code="B7B",
                new_supplier="嘉林亿",
                old_supplier="旧供应商",
                layer_count="3层",
                weight_structure="100/100/100",
                confidence_label="可直接替换",
                confidence_level="高可信",
                review_status="pending",
            ),
        ]
    )
    db.commit()

    update_calls: list[dict] = []
    create_calls: list[dict] = []

    def fake_apply_versioned_update(_db, **kwargs):
        update_calls.append(kwargs)
        return None

    def fake_record_versioned_create(_db, **kwargs):
        create_calls.append(kwargs)
        return None

    monkeypatch.setattr(
        master_data_versioning,
        "apply_versioned_update",
        fake_apply_versioned_update,
    )
    monkeypatch.setattr(
        master_data_versioning,
        "record_versioned_create",
        fake_record_versioned_create,
    )

    apply_customer_code_updates(db, user=user)
    assert code_product.customer_material_code == "CODE-P4"
    assert any(
        call["entity"] is code_product
        and call["updates"]
        == {
            "customer_material_code": "A1234B",
            "legacy_customer_material_code": "CODE-P4",
        }
        for call in update_calls
    )
    assert all(call["action"] == "update" for call in update_calls)

    update_calls.clear()
    apply_flute_consistency_fix(db, user=user)
    assert any(
        call["entity"] is consistency_product
        and call["updates"] == {"layer_count": 3}
        for call in update_calls
    )
    assert all(call["action"] == "update" for call in update_calls)

    update_calls.clear()
    apply_high_confidence_material_mapping(
        db,
        tmp_path / "unused.csv",
        user=user,
    )
    assert new_material_product.material_id is None
    assert existing_material_product.material_id is None
    assert (existing_material.layer_count, existing_material.flute_type) == (None, "B")
    assert any(
        call["object_type"] == "material" and call["entity"].code == "A6A"
        for call in create_calls
    )
    # 同码材质只能在同一供应商内复用；不能把 P4 测试供应商的 B7B
    # 静默改成嘉林亿材质或绑定给嘉林亿映射。
    assert not any(
        call["object_type"] == "material" and call["entity"] is existing_material
        for call in update_calls
    )
    assert any(
        call["object_type"] == "material"
        and call["entity"].code == "B7B"
        and call["entity"].supplier_name == "嘉林亿"
        for call in create_calls
    )
    assert {
        call["entity"].id
        for call in update_calls
        if call["object_type"] == "product" and "material_id" in call["updates"]
    } == {new_material_product.id, existing_material_product.id}
    assert all(call["action"] == "update" for call in update_calls)

    update_calls.clear()
    apply_flute_mapping(db, user=user)
    assert (flute_product.flute_type, flute_product.layer_count) == (None, None)
    assert any(
        call["entity"] is flute_product
        and call["updates"]["flute_type"] == "B"
        and call["source"] == "system.flute-mapping.apply"
        for call in update_calls
    )
    assert all(call["action"] == "update" for call in update_calls)


def test_system_customer_code_batch_requires_preview_bound_object_tokens(
    db: Session,
) -> None:
    from app.api.system import (
        BatchVersionConfirmation,
        apply_customer_codes,
        preview_customer_codes,
    )

    user = _user(db, "p4-preview-admin")
    customer = _customer(db, "PREVIEW")
    product = _product(
        db,
        customer.id,
        code="LEGACY-CODE",
        product_name="A1234B/外箱",
    )
    db.commit()

    with pytest.raises(ValidationError):
        BatchVersionConfirmation()

    preview = preview_customer_codes(db=db, user=user)
    missing_object_confirmation = BatchVersionConfirmation(
        preview_token=preview["preview_token"],
        confirmation_tokens={},
    )
    with pytest.raises(HTTPException) as missing:
        apply_customer_codes(
            request=SimpleNamespace(),
            body=missing_object_confirmation,
            db=db,
            user=user,
        )
    assert missing.value.status_code == 409
    assert missing.value.detail["code"] == (
        "SYSTEM_BATCH_OBJECT_CONFIRMATION_REQUIRED"
    )

    product.version = 2
    db.flush()
    with pytest.raises(HTTPException) as stale:
        apply_customer_codes(
            request=SimpleNamespace(),
            body=BatchVersionConfirmation(
                preview_token=preview["preview_token"],
                confirmation_tokens=preview["confirmation_tokens"],
            ),
            db=db,
            user=user,
        )
    assert stale.value.status_code == 409
    assert stale.value.detail["code"] == "SYSTEM_BATCH_PREVIEW_STALE"
    product.version = 1
    db.flush()

    result = apply_customer_codes(
        request=SimpleNamespace(),
        body=BatchVersionConfirmation(
            preview_token=preview["preview_token"],
            confirmation_tokens=preview["confirmation_tokens"],
        ),
        db=db,
        user=user,
    )
    db.refresh(product)
    assert result["updated"] == 1
    assert product.customer_material_code == "A1234B"
    assert product.version == 2


def test_small_dimension_fix_needs_no_second_confirmation_but_five_x_does(
    db: Session,
) -> None:
    from app.services.master_data_versioning import apply_versioned_update

    user = _user(db, "p4-dimension-admin")
    customer = _customer(db, "DIM")
    product = _product(
        db,
        customer.id,
        code="DIM-P4",
        length_mm=Decimal("100"),
    )
    db.commit()

    apply_versioned_update(
        db,
        object_type="product",
        entity=product,
        updates={"length_mm": Decimal("101")},
        expected_version=1,
        user=user,
        reason="普通尺寸修正",
        source="tests.p4.dimension",
    )
    db.commit()
    assert product.version == 2
    assert product.length_mm == Decimal("101")

    with pytest.raises(HTTPException) as abnormal:
        apply_versioned_update(
            db,
            object_type="product",
            entity=product,
            updates={"length_mm": Decimal("505")},
            expected_version=2,
            user=user,
            reason="异常尺寸修正",
            source="tests.p4.dimension",
        )
    assert abnormal.value.status_code == 409
    assert abnormal.value.detail["code"] == "MASTER_CHANGE_CONFIRMATION_REQUIRED"
    assert any(
        warning["code"] == "NUMERIC_VALUE_MAGNITUDE_CHANGE"
        for warning in abnormal.value.detail["warnings"]
    )


def test_material_and_flute_batches_apply_only_with_their_preview_tokens(
    db: Session,
) -> None:
    from app.api.system import (
        BatchVersionConfirmation,
        apply_flute_mapping as apply_flute_mapping_api,
        apply_high_confidence_mapping,
        preview_flute_mapping as preview_flute_mapping_api,
        preview_high_confidence_mapping,
    )
    from app.models.material_mapping import MaterialCodeMappingCandidate

    user = _user(db, "p4-batch-preview-admin")
    customer = _customer(db, "BATCH-PREVIEW")
    material_product = _product(
        db,
        customer.id,
        code="BATCH-MATERIAL",
        legacy_material_text="OLD-A6A",
    )
    flute_product = _product(
        db,
        customer.id,
        code="BATCH-FLUTE",
        legacy_material_text="100/100/100/B",
        layer_count=None,
        flute_type=None,
    )
    db.add(
        MaterialCodeMappingCandidate(
            old_code="OLD-A6A",
            new_code="A6A",
            new_supplier="嘉林亿",
            old_supplier="旧供应商",
            layer_count="3层",
            weight_structure="100/100/100",
            confidence_label="可直接替换",
            confidence_level="高可信",
            review_status="pending",
        )
    )
    db.commit()

    material_preview = preview_high_confidence_mapping(db=db, user=user)
    material_result = apply_high_confidence_mapping(
        request=SimpleNamespace(),
        body=BatchVersionConfirmation(
            preview_token=material_preview["preview_token"],
            confirmation_tokens=material_preview["confirmation_tokens"],
        ),
        db=db,
        user=user,
    )
    db.refresh(material_product)
    assert material_result["products_updated"] == 1
    assert material_product.material_id is not None
    assert material_product.version == 2

    flute_preview = preview_flute_mapping_api(db=db, user=user)
    flute_result = apply_flute_mapping_api(
        request=SimpleNamespace(),
        body=BatchVersionConfirmation(
            preview_token=flute_preview["preview_token"],
            confirmation_tokens=flute_preview["confirmation_tokens"],
        ),
        db=db,
        user=user,
    )
    db.refresh(flute_product)
    assert flute_result["updated"] == 1
    assert (flute_product.flute_type, flute_product.layer_count) == ("B", 3)
    assert flute_product.version == 2
