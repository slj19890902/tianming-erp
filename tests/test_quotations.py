from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker


def test_quotation_baseline_create_generate_accept_and_print(tmp_path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.quotations import router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserPermissionOverride
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "quotation.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        customer = Customer(
            customer_number=1,
            customer_code="Q-CUSTOMER",
            name="报价测试客户",
            credit_limit=Decimal("0"),
        )
        other_customer = Customer(
            customer_number=2,
            customer_code="WXTH",
            name="无锡市天华超净科技有限公司",
            credit_limit=Decimal("0"),
        )
        sales = User(
            username="quote-sales",
            password_hash=hash_password("QuotePass123!"),
            role="sales",
            real_name="报价业务",
            must_change_password=False,
        )
        workshop = User(
            username="quote-workshop",
            password_hash=hash_password("QuotePass123!"),
            role="workshop",
            real_name="报价无权限",
            must_change_password=False,
        )
        db.add_all([customer, other_customer, sales, workshop])
        material = Material(
            code="A416D",
            layer_count=5,
            flute_type=None,
            quote_price=Decimal("5.0000"),
            supplier_name="报价测试纸板厂",
            is_active=True,
        )
        db.add(material)
        db.flush()
        db.add_all(
            [
                UserPermissionOverride(
                    user_id=sales.id,
                    permission_code="cost.view",
                    is_allowed=True,
                ),
                UserPermissionOverride(
                    user_id=sales.id,
                    permission_code="products.create",
                    is_allowed=True,
                ),
            ]
        )
        historical_products = [
            Product(
                customer_id=customer.id,
                product_code=f"TH-HISTORY-{index}",
                customer_material_code=f"TH-HISTORY-{index}",
                product_name=f"天华历史常用箱{index}",
                box_category="normal",
                is_active=True,
                layer_count=3 if index == 1 else None,
                flute_type="AB" if index == 1 else None,
            )
            for index in range(1, 4)
        ]
        other_product = Product(
            customer_id=other_customer.id,
            product_code="WXTH-001",
            customer_material_code="WXTH-001",
            product_name="无锡天华常用箱",
            box_category="normal",
            is_active=True,
        )
        db.add_all([*historical_products, other_product])
        db.commit()
        customer_id = customer.id
        other_customer_id = other_customer.id
        material_id = material.id
        historical_snapshot = {
            row.product_code: (row.customer_id, row.product_name, row.layer_count, row.flute_type)
            for row in historical_products
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(router, prefix="/api/quotations")

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    payload = {
        "quotation_date": "2026-06-30",
        "remarks": "baseline",
        "items": [
            {
                "product_name": "A1 测试纸箱",
                "temporary_code": "TEMP-001",
                "box_type": "A1/0201 普通开槽箱",
                "length_mm": 300,
                "width_mm": 200,
                "height_mm": 150,
                "material_id": material_id,
                "flute_type": "AB",
                "quantity": 100,
                "margin_rate": 20,
                "final_unit_price": 2.5,
            },
            {
                "product_name": "手工报价产品",
                "box_type": "其他",
                "length_mm": 200,
                "width_mm": 100,
                "height_mm": 50,
                "material_id": material_id,
                "flute_type": None,
                "quantity": 50,
                "margin_rate": 20,
                "final_unit_price": 1.2,
            },
        ],
    }

    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={"username": "quote-sales", "password": "QuotePass123!"},
        ).status_code == 200
        preview = client.post(
            "/api/quotations/preview",
            json={
                "box_type": "A1",
                "length_mm": 300,
                "width_mm": 200,
                "height_mm": 150,
                "material_id": material_id,
                "flute_type": "AB",
            },
        )
        assert preview.status_code == 200
        assert preview.json()["estimated_unit_cost"] == "2.2620"
        assert preview.json()["suggested_unit_price"] == "2.8275"
        changed_margin = client.post(
            "/api/quotations/preview",
            json={
                "box_type": "A1",
                "length_mm": 300,
                "width_mm": 200,
                "height_mm": 150,
                "material_id": material_id,
                "flute_type": "AB",
                "margin_rate": 25,
            },
        )
        assert changed_margin.json()["suggested_unit_price"] == "3.0160"

        created = client.post(
            f"/api/quotations?customer_id={customer_id}",
            json=payload,
        )
        assert created.status_code == 201
        quotation_id = created.json()["id"]
        assert created.json()["quotation_no"] == "QT-20260630-001"
        assert created.json()["total_amount"] == "310.00"
        assert len(created.json()["items"]) == 2
        first_item_id = created.json()["items"][0]["id"]
        second_item_id = created.json()["items"][1]["id"]

        generated = client.post(
            f"/api/quotations/{quotation_id}/generate"
        )
        assert generated.status_code == 200
        assert generated.json()["status"] == "quoted"
        before_accept = client.post(
            f"/api/quotations/items/{first_item_id}/convert-to-product",
            json={"product_code": "Q-001"},
        )
        assert before_accept.status_code == 409
        accepted = client.post(
            f"/api/quotations/{quotation_id}/accept"
        )
        assert accepted.status_code == 200
        assert accepted.json()["status"] == "accepted"
        assert client.put(
            f"/api/quotations/{quotation_id}", json=payload
        ).status_code == 409
        assert client.post(
            f"/api/quotations/{quotation_id}/generate"
        ).status_code == 409
        printed = client.get(
            f"/api/quotations/{quotation_id}/print"
        )
        assert printed.status_code == 200
        print_payload = printed.json()
        first_printed_item = print_payload["items"][0]
        assert first_printed_item["product_name"] == "A1 测试纸箱"
        assert first_printed_item["temporary_code"] == "TEMP-001"
        assert "total_amount" not in print_payload
        expected_customer_fields = {
            "sequence",
            "temporary_code",
            "product_name",
            "specification",
            "material",
            "quantity",
            "unit_price",
            "remarks",
        }
        for row in print_payload["items"]:
            assert set(row) == expected_customer_fields
            assert "estimated_unit_cost" not in row
            assert "margin_rate" not in row
            assert "suggested_unit_price" not in row

        assert client.post(
            f"/api/quotations/items/{first_item_id}/convert-to-product",
            json={"product_code": "   "},
        ).status_code == 422
        duplicate_history = client.post(
            f"/api/quotations/items/{first_item_id}/convert-to-product",
            json={"product_code": "TH-HISTORY-1"},
        )
        assert duplicate_history.status_code == 409
        assert "相同存货编码" in duplicate_history.json()["detail"]
        mismatched_manual = client.post(
            f"/api/quotations/items/{first_item_id}/convert-to-product",
            json={
                "product_code": "Q-001",
                "product_name": "正式 A1 纸箱",
                "report_length_mm": 1030,
                "report_width_mm": 355,
                "crease_type": "压线",
                "crease_left_mm": 100,
                "crease_middle_mm": 150,
                "crease_right_mm": 100,
            },
        )
        assert mismatched_manual.status_code == 400
        assert "三段合计 350mm" in mismatched_manual.json()["detail"]
        converted = client.post(
            f"/api/quotations/items/{first_item_id}/convert-to-product",
            json={"product_code": "Q-001", "product_name": "正式 A1 纸箱"},
        )
        assert converted.status_code == 201
        assert converted.json()["quotation_status"] == "accepted"
        assert client.post(
            f"/api/quotations/{quotation_id}/void"
        ).status_code == 409
        assert client.post(
            f"/api/quotations/items/{first_item_id}/convert-to-product",
            json={"product_code": "Q-001"},
        ).status_code == 409
        assert client.post(
            f"/api/quotations/items/{second_item_id}/convert-to-product",
            json={"product_code": "Q-001"},
        ).status_code == 409
        missing_manual_report_size = client.post(
            f"/api/quotations/items/{second_item_id}/convert-to-product",
            json={"product_code": "Q-002", "flute_type": "BE"},
        )
        assert missing_manual_report_size.status_code == 400
        assert "报料长宽" in missing_manual_report_size.json()["detail"]
        converted_second = client.post(
            f"/api/quotations/items/{second_item_id}/convert-to-product",
            json={
                "product_code": "Q-002",
                "flute_type": "BE",
                "report_length_mm": 600,
                "report_width_mm": 300,
                "crease_type": "净料",
            },
        )
        assert converted_second.status_code == 201
        assert converted_second.json()["quotation_status"] == "converted"

        second_quote = client.post(
            f"/api/quotations?customer_id={customer_id}",
            json=payload,
        )
        assert second_quote.status_code == 201
        assert second_quote.json()["quotation_no"] == "QT-20260630-002"
        second_quote_id = second_quote.json()["id"]
        void_item_id = second_quote.json()["items"][0]["id"]
        assert client.post(
            f"/api/quotations/{second_quote_id}/generate"
        ).status_code == 200
        assert client.post(
            f"/api/quotations/{second_quote_id}/void"
        ).json()["status"] == "voided"
        assert client.post(
            f"/api/quotations/items/{void_item_id}/convert-to-product",
            json={"product_code": "Q-VOID"},
        ).status_code == 409

        history = client.get(
            f"/api/quotations?customer_id={customer_id}"
        )
        assert history.status_code == 200
        assert history.json()["total"] == 2
        assert history.json()["items"][0]["quotation_no"] == "QT-20260630-002"

    with factory() as db:
        from app.api.products import list_products

        product = db.query(Product).filter(Product.product_code == "Q-001").one()
        assert product.customer_id == customer_id
        assert product.product_name == "正式 A1 纸箱"
        assert product.sale_unit_price == Decimal("2.5000")
        assert product.report_length_mm == 1030
        assert product.report_width_mm == 350
        assert product.crease_type == "压线"
        assert (
            product.crease_left_mm,
            product.crease_middle_mm,
            product.crease_right_mm,
        ) == (100, 150, 100)
        assert product.flute_type == "AB"
        assert product.material.flute_type is None
        second_product = db.query(Product).filter(Product.product_code == "Q-002").one()
        assert second_product.report_length_mm == 600
        assert second_product.report_width_mm == 300
        assert second_product.crease_type == "净料"
        assert second_product.flute_type == "BE"
        current_history = {
            row.product_code: (row.customer_id, row.product_name, row.layer_count, row.flute_type)
            for row in db.query(Product)
            .filter(Product.product_code.like("TH-HISTORY-%"))
            .order_by(Product.product_code)
            .all()
        }
        assert current_history == historical_snapshot
        assert db.query(Product).filter(Product.customer_id == customer_id).count() == 5
        sales_user = db.query(User).filter(User.username == "quote-sales").one()
        tianhua_list = list_products(
            customer_id=customer_id,
            page=1,
            page_size=200,
            db=db,
            user=sales_user,
        )
        wuxi_list = list_products(
            customer_id=other_customer_id,
            page=1,
            page_size=200,
            db=db,
            user=sales_user,
        )
        assert tianhua_list["total"] == 5
        assert {row["product_code"] for row in tianhua_list["items"]} >= {
            "TH-HISTORY-1",
            "TH-HISTORY-2",
            "TH-HISTORY-3",
            "Q-001",
            "Q-002",
        }
        assert wuxi_list["total"] == 1
        assert [row["product_code"] for row in wuxi_list["items"]] == ["WXTH-001"]

    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={"username": "quote-workshop", "password": "QuotePass123!"},
        ).status_code == 200
        assert client.get("/api/quotations").status_code == 403


@pytest.mark.parametrize("flute_type", ["AAA", "ABC"])
def test_seven_layer_quotation_converts_to_product_without_material_flute_backfill(
    tmp_path,
    flute_type,
):
    from datetime import date

    from app.api.quotations import (
        ConvertPayload,
        QuotationItemPayload,
        _replace_items,
        convert_to_product,
    )
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.product import Product
    from app.models.quotation import QuotationOrder
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / f"quotation-seven-{flute_type}.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        customer = Customer(name=f"七层报价客户-{flute_type}", is_active=True)
        user = User(
            username=f"quote-seven-{flute_type.lower()}",
            password_hash="test-only",
            role="admin",
            real_name="七层报价测试",
            must_change_password=False,
        )
        material = Material(
            code="A12345B",
            layer_count=7,
            flute_type=None,
            quote_price=Decimal("5.0000"),
            supplier_name="七层报价纸板厂",
            is_active=True,
        )
        db.add_all([customer, user, material])
        db.flush()
        quotation = QuotationOrder(
            quotation_no=f"QT-20260716-{1 if flute_type == 'AAA' else 2:03d}",
            customer_id=customer.id,
            customer_name=customer.name,
            quotation_date=date(2026, 7, 16),
            status="draft",
            total_amount=Decimal("0"),
            created_by=user.id,
        )
        db.add(quotation)
        db.flush()
        _replace_items(
            db,
            quotation,
            [
                QuotationItemPayload(
                    product_name=f"七层纸箱-{flute_type}",
                    box_type="A1/0201 普通开槽箱",
                    length_mm=Decimal("300"),
                    width_mm=Decimal("200"),
                    height_mm=Decimal("150"),
                    material_id=material.id,
                    flute_type=flute_type.lower(),
                    quantity=10,
                    final_unit_price=Decimal("2.50"),
                )
            ],
        )
        quotation.status = "accepted"
        db.commit()
        item = quotation.items[0]
        assert item.flute_type == flute_type

        result = convert_to_product(
            item.id,
            ConvertPayload(product_code=f"Q7-{flute_type}"),
            db=db,
            user=user,
            _product_creator=user,
        )

        product = db.get(Product, result["product_id"])
        assert product is not None
        assert product.layer_count == 7
        assert product.flute_type == flute_type
        assert product.material.code == "A12345B"
        assert product.material.flute_type is None


@pytest.mark.parametrize(
    "flute_type",
    [None, "", "A", "B", "E", "AB", "BE"],
)
def test_seven_layer_quotation_rejects_invalid_flute_on_save_and_conversion(
    tmp_path,
    flute_type,
):
    from datetime import date

    from fastapi import HTTPException

    from app.api.quotations import (
        ConvertPayload,
        QuotationItemPayload,
        _replace_items,
        convert_to_product,
    )
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.product import Product
    from app.models.quotation import QuotationItem, QuotationOrder
    from app.models.user import User

    suffix = "empty" if not flute_type else flute_type
    engine = create_sqlite_engine(tmp_path / f"quotation-seven-invalid-{suffix}.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        customer = Customer(name=f"七层无效楞型客户-{suffix}", is_active=True)
        user = User(
            username=f"quote-seven-invalid-{suffix}",
            password_hash="test-only",
            role="admin",
            real_name="七层报价测试",
            must_change_password=False,
        )
        material = Material(
            code="A12345B",
            layer_count=7,
            flute_type=None,
            quote_price=Decimal("5.0000"),
            supplier_name="七层报价纸板厂",
            is_active=True,
        )
        db.add_all([customer, user, material])
        db.flush()
        quotation = QuotationOrder(
            quotation_no="QT-20260716-099",
            customer_id=customer.id,
            customer_name=customer.name,
            quotation_date=date(2026, 7, 16),
            status="accepted",
            total_amount=Decimal("25"),
            created_by=user.id,
        )
        db.add(quotation)
        db.flush()
        quote_payload = QuotationItemPayload(
            product_name="七层无效楞型纸箱",
            box_type="A1/0201 普通开槽箱",
            length_mm=Decimal("300"),
            width_mm=Decimal("200"),
            height_mm=Decimal("150"),
            material_id=material.id,
            flute_type=flute_type,
            quantity=10,
            final_unit_price=Decimal("2.50"),
        )
        with pytest.raises(HTTPException) as save_error:
            _replace_items(db, quotation, [quote_payload])
        assert save_error.value.status_code == 400
        save_detail = str(save_error.value.detail)
        assert "AAA" in save_detail and "ABC" in save_detail

        item = QuotationItem(
            quotation_id=quotation.id,
            product_name="七层历史无效楞型纸箱",
            box_type="A1/0201 普通开槽箱",
            length_mm=Decimal("300"),
            width_mm=Decimal("200"),
            height_mm=Decimal("150"),
            material_id=material.id,
            material_supplier=material.supplier_name,
            material_code=material.code,
            flute_type=flute_type,
            quantity=10,
            margin_rate=Decimal("20"),
            final_unit_price=Decimal("2.50"),
        )
        db.add(item)
        db.commit()

        with pytest.raises(HTTPException) as conversion_error:
            convert_to_product(
                item.id,
                ConvertPayload(product_code=f"Q7-BAD-{suffix}"),
                db=db,
                user=user,
                _product_creator=user,
            )
        assert conversion_error.value.status_code == 400
        conversion_detail = str(conversion_error.value.detail)
        assert "AAA" in conversion_detail and "ABC" in conversion_detail
        assert db.query(Product).count() == 0


def test_quotation_print_page_calls_api():
    from pathlib import Path

    html = (
        Path(__file__).resolve().parents[1]
        / "static"
        / "quotation-print.html"
    ).read_text(encoding="utf-8")
    assert "客户报价单" in html
    assert "/api/quotations/" in html
    assert "estimated_unit_cost" not in html
    assert "margin_rate" not in html
    assert "suggested_unit_price" not in html
    assert "<th>临时编码</th>" in html
    assert "row.temporary_code" in html
    assert "<th>箱型</th>" not in html
    assert "<th>金额</th>" not in html
    assert "合计金额" not in html
    assert "row.box_type" not in html
    assert "row.amount" not in html
    assert "data.total_amount" not in html

    index_html = (
        Path(__file__).resolve().parents[1]
        / "static"
        / "index.html"
    ).read_text(encoding="utf-8")
    assert 'activePage === \'quotations\'' in index_html
    assert 'openCustomerQuotations(row)' in index_html
    assert "生成报价单" in index_html
    assert "转入常用箱" in index_html
    assert "quotationEditable" in index_html
