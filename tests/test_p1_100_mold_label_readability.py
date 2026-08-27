from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from tests.test_mold_tool_workflow import _login, mold_app
from tests.test_p1_62_mold_40x80_label import _complete_mold


ROOT = Path(__file__).resolve().parents[1]
LABEL_PAGE = (ROOT / "static" / "mold-label.html").read_text(encoding="utf-8")
LAYOUT_JS = (ROOT / "static" / "assets" / "mold-label-layout.js").read_text(
    encoding="utf-8"
)
LAYOUT_CSS = (ROOT / "static" / "assets" / "mold-label-layout.css").read_text(
    encoding="utf-8"
)


def _registered_wide_response(
    client: TestClient,
    *,
    mold_id: int,
    idempotency_key: str,
):
    created = client.post(
        "/api/warehouse/molds/label-prints",
        json={
            "mold_ids": [mold_id],
            "source": "single",
            "template_version": "mold_80x40_v1",
            "idempotency_key": idempotency_key,
        },
    )
    assert created.status_code == 200, created.text
    return client.get(
        f"/api/warehouse/molds/{mold_id}/label",
        params={
            "template_version": "mold_80x40_v1",
            "print_job_id": created.json()["print_job_id"],
        },
    )


def test_shared_80x40_label_keeps_full_detail_but_prints_single_label_summary(
    mold_app,
) -> None:
    from app.models.customer import Customer
    from app.models.product import Product

    app, factory = mold_app
    mold_id = _complete_mold(factory, suffix="100")
    with factory() as db:
        customer = db.get(Customer, 1)
        assert customer is not None
        customer.chinese_short_name = "思迈尔"
        db.add(
            Product(
                customer_id=1,
                product_code="SME-SECOND-100",
                customer_material_code="SME-SECOND-100",
                product_name="第二款完整产品名称",
                length_mm=400,
                width_mm=300,
                height_mm=200,
                report_length_mm=900,
                report_width_mm=650,
                flute_type="B",
                default_cutting_mode="一开一",
                mold_tool_id=mold_id,
            )
        )
        db.commit()

    with TestClient(app) as client:
        _login(client, "workshop")
        response = _registered_wide_response(
            client,
            mold_id=mold_id,
            idempotency_key="p1-100-shared-facts-0001",
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["label_projection_mode"] == "shared_mold"
    assert body["label_customer_names"] == ["思迈尔"]
    assert body["label_product_specifications"] == [
        "520 × 350 × 300",
        "400 × 300 × 200",
    ]
    assert body["label_report_specifications"] == ["1100 × 760", "900 × 650"]
    assert body["label_flute_types"] == ["BC", "B"]
    assert body["label_cutting_modes"] == ["一开二", "一开一"]
    assert body["label_products"] == [
        {
            "product_code": "SME-LONG-CODE-100",
            "product_name": "五层加强纸箱横向标签样例100",
        },
        {
            "product_code": "SME-SECOND-100",
            "product_name": "第二款完整产品名称",
        },
    ]
    assert body["label_inventory_code"] == "SME-LONG-CODE-100 等2款"
    assert body["label_inventory_codes"] == [
        "SME-LONG-CODE-100",
        "SME-SECOND-100",
    ]
    assert body["label_product_specification"] == "多款见扫码"
    assert body["label_report_specification"] == "多款见扫码"
    assert body["label_flute_type"] == "多款见扫码"
    assert body["label_cutting_mode"] == "一开二/一开一"
    rendered = json.dumps(body, ensure_ascii=False)
    assert "按任务显示" not in rendered


def test_shared_80x40_detail_lists_customers_but_print_uses_single_label_summary(
    mold_app,
) -> None:
    from app.models.customer import Customer
    from app.models.product import Product

    app, factory = mold_app
    mold_id = _complete_mold(factory, suffix="100-customer")
    with factory() as db:
        first_customer = db.get(Customer, 1)
        assert first_customer is not None
        first_customer.chinese_short_name = "思迈尔"
        first_product = (
            db.query(Product).filter(Product.mold_tool_id == mold_id).one()
        )
        first_product.product_code = "SME-SHARED-100"
        first_product.customer_material_code = "SME-SHARED-100"
        customer = Customer(
            customer_number=9100,
            customer_code="RB",
            name="瑞邦纸品有限公司",
            chinese_short_name="瑞邦",
            payment_term_days=30,
            credit_limit=0,
        )
        db.add(customer)
        db.flush()
        db.add(
            Product(
                customer_id=customer.id,
                product_code="RB-SHARED-100",
                customer_material_code="RB-SHARED-100",
                product_name="瑞邦共用模具纸箱",
                length_mm=520,
                width_mm=350,
                height_mm=300,
                report_length_mm=1100,
                report_width_mm=760,
                flute_type="BC",
                default_cutting_mode="一开二",
                mold_tool_id=mold_id,
            )
        )
        db.commit()

    with TestClient(app) as client:
        _login(client, "workshop")
        response = _registered_wide_response(
            client,
            mold_id=mold_id,
            idempotency_key="p1-100-shared-customer-0001",
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["label_customer_names"] == ["思迈尔", "瑞邦"]
    assert body["label_customer_name"] == "待完善"


def test_shared_80x40_label_accepts_current_archive_maximum_of_11_products(
    mold_app,
) -> None:
    from app.models.customer import Customer
    from app.models.product import Product

    app, factory = mold_app
    mold_id = _complete_mold(factory, suffix="100-max")
    with factory() as db:
        customer = db.get(Customer, 1)
        assert customer is not None
        customer.chinese_short_name = "思迈尔"
        first_product = (
            db.query(Product).filter(Product.mold_tool_id == mold_id).one()
        )
        first_product.product_code = "SME-MAX-01"
        first_product.customer_material_code = "SME-MAX-01"
        for index in range(2, 12):
            db.add(
                Product(
                    customer_id=1,
                    product_code=f"SME-MAX-{index:02d}",
                    customer_material_code=f"SME-MAX-{index:02d}",
                    product_name=f"共用模具纸箱{index:02d}",
                    length_mm=520,
                    width_mm=350,
                    height_mm=300,
                    report_length_mm=1100,
                    report_width_mm=760,
                    flute_type="BC",
                    default_cutting_mode="一开二",
                    mold_tool_id=mold_id,
                )
            )
        db.commit()

    with TestClient(app) as client:
        _login(client, "workshop")
        response = _registered_wide_response(
            client,
            mold_id=mold_id,
            idempotency_key="p1-100-shared-max-0001",
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["product_count"] == 11
    assert len(body["label_products"]) == 11
    assert body["label_products"][0]["product_code"] == "SME-MAX-01"
    assert body["label_products"][-1]["product_code"] == "SME-MAX-11"


def test_80x40_page_keeps_complete_api_facts_but_prints_the_side_identity_layout() -> None:
    for marker in (
        "board_specification",
        "inventory_code",
        "flute_type",
        "cutting_mode",
        "customer_name",
        "mold_label_name",
        "mold_chinese_short_name",
        "product_specification",
        "mold_qr",
    ):
        assert marker in LAYOUT_JS
    assert "TmMoldLabelLayout.labelHtml" in LABEL_PAGE
    assert "label_product_name" not in LAYOUT_JS
    assert "product.product_name" not in LAYOUT_JS
    assert "多款见扫码" not in LAYOUT_JS
    assert ".mold-layout-qr" in LAYOUT_CSS
    assert ".mold-layout-text" in LAYOUT_CSS
    assert "fitAndValidate" in LAYOUT_JS
