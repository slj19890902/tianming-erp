from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.test_phase5_orders import _login, order_api_app


def _inventory_counts(session) -> tuple[int, int, int]:
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryMovement,
        InventoryReservation,
    )

    return (
        session.scalar(select(func.count()).select_from(InventoryLot)) or 0,
        session.scalar(select(func.count()).select_from(InventoryReservation)) or 0,
        session.scalar(select(func.count()).select_from(InventoryMovement)) or 0,
    )


def _manual_payload(*, client_line_id: str = "manual-size-line-001") -> dict:
    return {
        "customer_id": 1,
        "customer_po": "P1-03-MANUAL-001",
        "order_date": "2026-07-29",
        "delivery_date": "2026-08-05",
        "items": [
            {
                "client_line_id": client_line_id,
                "manual_size_entry": True,
                "box_type": "A1",
                "product_name": "纸箱",
                "customer_model": "手工规格-B07",
                "length_mm": 520,
                "width_mm": 350,
                "height_mm": 300,
                "crease_type": "压线",
                "crease_left_mm": 175,
                "crease_middle_mm": 300,
                "crease_right_mm": 175,
                "material_id": 1,
                "layer_count": 5,
                "flute_type": "AB",
                "quantity": 200,
                "unit_price": "3.68",
            }
        ],
    }


def _make_fixture_material_valid_for_manual_a1(session_factory) -> None:
    """The generic order fixture keeps a legacy BC value on a five-layer row."""
    from app.models.customer_quote_preference import CustomerQuotePreference
    from app.models.material import Material
    from app.models.supplier import Supplier
    from app.services.supplier_master import normalize_supplier_identity

    with session_factory() as session:
        material = session.get(Material, 1)
        assert material is not None
        material.flute_type = "AB"
        material.supplier_name = "测试纸板供应商"
        session.add(
            Supplier(
                standard_name="测试纸板供应商",
                normalized_name=normalize_supplier_identity("测试纸板供应商"),
                display_name="测试纸板供应商",
                sort_order=10,
                is_active=True,
            )
        )
        session.add(
            CustomerQuotePreference(
                customer_id=1,
                box_type="A1",
                crease_type="压线",
                material_id=material.id,
                flute_type="AB",
                tax_included_square_price=Decimal("3.2500"),
                is_active=True,
            )
        )
        session.commit()


def test_manual_size_order_creates_versioned_a1_common_box_and_freezes_order(
    order_api_app,
) -> None:
    from app.models.master_data_object_version import MasterDataObjectVersion
    from app.models.order import OrderItem
    from app.models.product import Product

    app, session_factory = order_api_app
    _make_fixture_material_valid_for_manual_a1(session_factory)
    with session_factory() as session:
        inventory_before = _inventory_counts(session)
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/orders", json=_manual_payload())

    assert response.status_code == 201, response.text
    item = response.json()["items"][0]
    assert item["snapshot_product_name"] == "纸箱"
    assert item["snapshot_spec"].replace("×", "x") == "520x350x300mm"
    assert item["snapshot_customer_model"] == "手工规格-B07"
    assert item["snapshot_report_length_mm"] == 1770
    assert item["snapshot_report_width_mm"] == 650
    assert item["snapshot_crease_type"] == "压线"
    assert [
        item["snapshot_crease_left_mm"],
        item["snapshot_crease_middle_mm"],
        item["snapshot_crease_right_mm"],
    ] == [175, 300, 175]
    assert Decimal(str(item["unit_price"])) == Decimal("3.68")

    with session_factory() as session:
        product = session.get(Product, item["product_id"])
        stored_item = session.get(OrderItem, item["id"])
        assert product is not None
        assert stored_item is not None
        assert product.product_code.startswith(f"SZ-1-{date.today():%Y%m%d}-")
        assert product.product_code == product.customer_material_code
        assert product.product_name == "纸箱"
        assert product.box_style == "A1"
        assert product.material_id == 1
        assert product.layer_count == 5
        assert product.flute_type == "AB"
        assert product.report_length_mm == 1770
        assert product.report_width_mm == 650
        assert product.crease_type == "压线"
        assert (
            product.crease_left_mm,
            product.crease_middle_mm,
            product.crease_right_mm,
        ) == (175, 300, 175)
        assert product.flap_mm == 30
        assert (product.length_mm, product.width_mm, product.height_mm) == (
            Decimal("520"), Decimal("350"), Decimal("300"),
        )
        assert product.sale_unit_price == Decimal("3.6800")
        assert product.remark == "手工尺寸订单创建常用箱|line:manual-size-line-001"
        assert stored_item.product_id == product.id
        assert session.scalar(
            select(func.count()).select_from(MasterDataObjectVersion).where(
                MasterDataObjectVersion.object_type == "product",
                MasterDataObjectVersion.object_id == product.id,
            )
        ) == 1
        assert _inventory_counts(session) == inventory_before


def test_manual_size_requires_explicit_a1_dimensions_material_and_matching_flute(
    order_api_app,
) -> None:
    app, session_factory = order_api_app
    _make_fixture_material_valid_for_manual_a1(session_factory)
    invalid_cases = [
        ({"box_type": "A3"}, "目前仅支持 A1 箱型"),
        ({"length_mm": None}, "必须填写正数的长、宽、高"),
        ({"material_id": None}, "必须明确选择材质"),
        ({"layer_count": 3}, "层数必须与所选材质真实层数一致"),
        ({"flute_type": "BE"}, "已保存并启用"),
        (
            {
                "crease_type": "净料",
                "crease_left_mm": None,
                "crease_middle_mm": None,
                "crease_right_mm": None,
            },
            "已保存并启用",
        ),
        ({"crease_right_mm": 176}, "左右数值必须相等"),
    ]
    with TestClient(app) as client:
        _login(client)
        for index, (override, message) in enumerate(invalid_cases, start=1):
            payload = _manual_payload(client_line_id=f"manual-invalid-{index}")
            payload["customer_po"] = f"P1-03-INVALID-{index}"
            payload["items"][0].update(override)
            response = client.post("/api/orders", json=payload)
            assert response.status_code == 400, response.text
            assert message in response.json()["detail"]

    from app.models.order import Order
    from app.models.product import Product

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 0
        assert session.scalar(select(func.count()).select_from(Product)) == 2


def test_manual_size_rejects_material_not_saved_in_customer_quote_preferences(
    order_api_app,
) -> None:
    from app.models.material import Material

    app, session_factory = order_api_app
    _make_fixture_material_valid_for_manual_a1(session_factory)
    with session_factory() as session:
        unsaved = Material(
            code="UNSAVED-5",
            supplier_name="测试纸板供应商",
            layer_count=5,
            flute_type="AB",
            is_active=True,
        )
        session.add(unsaved)
        session.commit()
        unsaved_id = unsaved.id
    payload = _manual_payload(client_line_id="manual-unsaved-material")
    payload["customer_po"] = "P1-03-UNSAVED-MATERIAL"
    payload["items"][0]["material_id"] = unsaved_id
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/orders", json=payload)
    assert response.status_code == 400, response.text
    assert "已保存并启用" in response.json()["detail"]


def test_manual_size_rejects_disabled_customer_quote_preference(order_api_app) -> None:
    from app.models.customer_quote_preference import CustomerQuotePreference

    app, session_factory = order_api_app
    _make_fixture_material_valid_for_manual_a1(session_factory)
    with session_factory() as session:
        preference = session.scalar(select(CustomerQuotePreference))
        assert preference is not None
        preference.is_active = False
        session.commit()
    payload = _manual_payload(client_line_id="manual-disabled-preference")
    payload["customer_po"] = "P1-03-DISABLED-PREFERENCE"
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/orders", json=payload)
    assert response.status_code == 400, response.text
    assert "已保存并启用" in response.json()["detail"]


def test_manual_size_retry_reuses_common_box_and_duplicate_order_guard(
    order_api_app,
) -> None:
    from app.models.order import Order
    from app.models.product import Product

    app, session_factory = order_api_app
    _make_fixture_material_valid_for_manual_a1(session_factory)
    with TestClient(app) as client:
        _login(client)
        first = client.post("/api/orders", json=_manual_payload())
        assert first.status_code == 201, first.text
        second = client.post("/api/orders", json=_manual_payload())
        assert second.status_code == 409, second.text
        assert "未重复生成" in second.json()["detail"]

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 1
        assert session.scalar(select(func.count()).select_from(Product)) == 3


def test_manual_size_product_rolls_back_with_failed_order_transaction(order_api_app) -> None:
    from app.models.order import Order
    from app.models.product import Product

    app, session_factory = order_api_app
    _make_fixture_material_valid_for_manual_a1(session_factory)
    with session_factory() as session:
        inventory_before = _inventory_counts(session)
    payload = _manual_payload(client_line_id="manual-rollback-line")
    payload["customer_po"] = "P1-03-ROLLBACK"
    payload["items"].append({"product_id": 999999, "quantity": 1, "unit_price": "1.00"})
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/orders", json=payload)
    assert response.status_code == 400, response.text
    assert "第2条明细产品不存在" in response.json()["detail"]

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 0
        assert session.scalar(select(func.count()).select_from(Product)) == 2
        assert _inventory_counts(session) == inventory_before


def test_manual_size_does_not_relax_existing_product_or_pdf_guards(order_api_app) -> None:
    app, _ = order_api_app
    _make_fixture_material_valid_for_manual_a1(_)
    with TestClient(app) as client:
        _login(client)
        incompatible = _manual_payload()
        incompatible["items"][0]["product_id"] = 1
        response = client.post("/api/orders", json=incompatible)
        assert response.status_code == 400, response.text
        assert "不能同时选择已有常用箱" in response.json()["detail"]

        pdf_null = _manual_payload(client_line_id="pdf-manual-size")
        pdf_null["import_draft"] = True
        pdf_null["pdf_import_confirmation"] = {"confirmed": True, "preview_safety_token": "x"}
        response = client.post("/api/orders", json=pdf_null)
        assert response.status_code == 400, response.text
        assert "PDF 明细必须明确选择唯一常用箱产品" in response.json()["detail"]
