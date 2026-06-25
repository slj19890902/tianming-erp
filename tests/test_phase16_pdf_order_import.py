from __future__ import annotations

from collections.abc import Generator
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker


SAMPLE_PO_TEXT = """PURCHASE ORDER 地址：苏州工业园区双马街99号  电话：0512-62852366  传真：0512-62852195
采购订单 Canmax Ultra Clean Technology Co., Ltd.
订单号：供应商全称:
苏州天华超净科技有限公司
PO2026060469
人民币元
1245
2026-06-17
次月30天后6个月承兑
13%税率
苏州天明包装有限公司
行号 料品编码 物料名称 规格型号 番号 销售订单号 单位 数量 含税单价 价税合计 交货日期
10 21312009 中性内箱 (26"*45")
116*68*1.8/2.
1cm T5P/B 常规
印刷
个 25.00000 5.410000 135.25 2026.06.25
20 21308002 衬板 98*20cm
T5P/A R 个 25.00000 0.490000 12.25 2026.06.25
30 21312013 中性外箱 (26"*45")
118*69.5*13.5
cm 5盒/箱
A535T/AB
个 5.00000 13.070000 65.35 2026.06.25
合计 55.00000 212.85
"""


def test_parse_purchase_order_text_extracts_header_and_lines() -> None:
    from app.services.order_pdf_import import parse_purchase_order_text

    draft = parse_purchase_order_text(SAMPLE_PO_TEXT, source_name="PO2026060469.pdf")

    assert draft["customer_name"] == "苏州天华超净科技有限公司"
    assert draft["customer_po"] == "PO2026060469"
    assert draft["order_date"] == "2026-06-17"
    assert draft["delivery_date"] == "2026-06-25"
    assert draft["item_count"] == 3
    assert draft["items"][0]["product_code"] == "21312009"
    # Hotfix-2: 修复后括号内容保留，品名包含完整括号描述
    assert "中性内箱" in draft["items"][0]["raw_product_name"]
    # v0.19.1 F-3: 斜杠规格不截断，116*68*1.8/2.1cm 应完整保留
    assert draft["items"][0]["raw_spec_model"] == "116*68*1.8/2.1cm"
    assert draft["items"][0]["quantity"] == 25
    assert draft["items"][0]["unit_price"] == "5.4100"
    assert draft["items"][2]["amount"] == "65.35"


def test_parse_purchase_order_text_uses_first_due_date_when_multiple_lines_share_date() -> None:
    from app.services.order_pdf_import import parse_purchase_order_text

    draft = parse_purchase_order_text(SAMPLE_PO_TEXT, source_name="PO2026060469.pdf")

    assert {item["delivery_date"] for item in draft["items"]} == {"2026-06-25"}
    assert draft["delivery_date"] == "2026-06-25"


def test_tianhua_specification_keeps_only_dimension_not_price_quantity_or_date() -> None:
    from app.services.order_pdf_import import parse_purchase_order_text

    text = """采购订单
苏州天华超净科技有限公司
PO2026060469
2026-06-17
苏州天明包装有限公司
行号 料品编码 物料名称 规格型号 单位 数量 含税单价 价税合计 交货日期
10 21312009 中性内箱 28.5*19.5*5.5cm 个 25.00000 5.410000 135.25 2026.06.25
合计 25.00000 135.25
"""
    draft = parse_purchase_order_text(text)

    assert draft["items"][0]["raw_spec_model"] == "28.5*19.5*5.5cm"
    assert "25.00000" not in draft["items"][0]["raw_spec_model"]
    assert "5.410000" not in draft["items"][0]["raw_spec_model"]
    assert "2026" not in draft["items"][0]["raw_spec_model"]


def test_match_import_draft_links_customer_and_products(tmp_path: Path) -> None:
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.customer import Customer
    from app.models.product import Product
    from app.services.order_pdf_import import match_import_draft, parse_purchase_order_text

    database_path = tmp_path / "pdf-match.sqlite3"
    engine = create_sqlite_engine(database_path)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with session_factory() as session:
            customer = Customer(
                customer_number=1,
                customer_code="TH",
                name="苏州天华超净科技有限公司",
                payment_term_days=30,
                credit_limit=Decimal("100000"),
            )
            session.add(customer)
            session.flush()
            session.add_all(
                [
                    Product(
                        customer_id=customer.id,
                        product_code="21312009",
                        customer_material_code="21312009",
                        product_name="中性内箱",
                        box_category="normal",
                    ),
                    Product(
                        customer_id=customer.id,
                        product_code="21308002",
                        customer_material_code="21308002",
                        product_name="衬板",
                        box_category="normal",
                    ),
                ]
            )
            session.commit()

            draft = parse_purchase_order_text(SAMPLE_PO_TEXT, source_name="PO2026060469.pdf")
            matched = match_import_draft(session, draft)

        assert matched["matched_customer_id"] == 1
        assert matched["items"][0]["matched_product_id"] is not None
        assert matched["items"][1]["matched_product_id"] is not None
        assert matched["items"][2]["matched_product_id"] is None
    finally:
        engine.dispose()
        database_path.unlink(missing_ok=True)


def _order_import_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.orders import router as orders_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "order-import.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        session.add_all(
            [
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
        )
        customer = Customer(
            customer_number=1,
            customer_code="TH",
            name="苏州天华超净科技有限公司",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        session.add(customer)
        session.flush()
        session.add_all(
            [
                Product(
                    customer_id=customer.id,
                    product_code="21312009",
                    customer_material_code="21312009",
                    product_name="中性内箱",
                    box_category="normal",
                ),
                Product(
                    customer_id=customer.id,
                    product_code="21308002",
                    customer_material_code="21308002",
                    product_name="衬板",
                    box_category="normal",
                ),
            ]
        )
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(orders_router, prefix="/api/orders")

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return app


def test_pdf_preview_endpoint_returns_draft_without_writing_order(
    monkeypatch,
    tmp_path: Path,
) -> None:
    app = _order_import_app(tmp_path)
    import app.api.orders as orders_api

    monkeypatch.setattr(
        orders_api,
        "extract_text_from_pdf_bytes",
        lambda _content: SAMPLE_PO_TEXT,
    )

    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "sales", "password": "RolePass123!"},
        )
        assert login.status_code == 200
        response = client.post(
            "/api/orders/pdf-preview",
            files={"file": ("PO2026060469.pdf", b"%PDF-stub", "application/pdf")},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["customer_po"] == "PO2026060469"
    assert body["matched_customer_id"] == 1
    assert body["item_count"] == 3
    assert body["items"][0]["matched_product_id"] is not None
    assert body["items"][2]["matched_product_id"] is None


def test_manual_customer_rematch_preserves_recognized_fields(tmp_path: Path) -> None:
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.customer import Customer
    from app.models.product import Product
    from app.services.order_pdf_import import match_import_draft, parse_purchase_order_text

    engine = create_sqlite_engine(tmp_path / "manual-rematch.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        customer = Customer(
            customer_number=1,
            customer_code="TH",
            name="人工选择客户",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        session.add(customer)
        session.flush()
        session.add(
            Product(
                customer_id=customer.id,
                product_code="21312009",
                customer_material_code="21312009",
                product_name="中性内箱",
                box_category="normal",
            )
        )
        session.commit()
        original = parse_purchase_order_text(SAMPLE_PO_TEXT)
        before = original["items"][0].copy()
        matched = match_import_draft(session, original, customer_id=customer.id)

    after = matched["items"][0]
    for field in (
        "raw_product_code",
        "raw_product_name",
        "raw_spec_model",
        "quantity",
        "unit_price",
    ):
        assert after[field] == before[field]
    assert after["matched_product_id"] is not None


def test_batch_preview_isolates_failures_and_skips_duplicate_files(
    monkeypatch,
    tmp_path: Path,
) -> None:
    app = _order_import_app(tmp_path)
    import app.api.orders as orders_api

    monkeypatch.setattr(
        orders_api,
        "extract_text_from_pdf_bytes",
        lambda content: SAMPLE_PO_TEXT if content == b"good" else "",
    )
    with TestClient(app) as client:
        client.post(
            "/api/auth/login",
            json={"username": "sales", "password": "RolePass123!"},
        )
        response = client.post(
            "/api/orders/pdf-preview-batch",
            files=[
                ("files", ("first.pdf", b"good", "application/pdf")),
                ("files", ("duplicate.pdf", b"good", "application/pdf")),
                ("files", ("bad.pdf", b"bad", "application/pdf")),
            ],
        )

    assert response.status_code == 200
    drafts = response.json()["drafts"]
    assert drafts[0]["recognition_status"] in {"recognized", "needs_confirmation"}
    assert drafts[1]["duplicate_status"] == "duplicate_skipped"
    assert drafts[2]["recognition_status"] == "failed"
