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

GAOTAI_OCR_TEXT = """
苏州高泰电子技术股份有限公司 采购合同 苏州工业园区天明纸品包装厂
合同号/0 : 0100-CG260624-02 日期/0ate: 2026/6/24
序号 产品编号 名称 规格 数量 单位 单价(含税) 金额 税率 交货期 备注
3090078 纸箱 SSII 50 510.00 13.00 2026-5-26
3090095 纸箱 2SII 500 Pcs 2950.00 13.00 2026-6-26
3030268 纸箱 190*160 2000 Pcs 13.00 2026-6-26
合计
"""

TIANHUA_PREFIX = "采购订单\n苏州天华超净科技有限公司\n{po}\n2026-07-01\n苏州天明包装有限公司\n行号 料品编码 物料名称 规格型号 单位 数量 含税单价 价税合计 交货日期\n"
TIANHUA_THREE_LINES = "10 23203105 白底黑字内箱 28.5*19.5*5.5cm 个 1000 0.530000 530.00 2026.07.13\n20 21302006 衬板 98*20cm T5P/A 个 30 1.790000 53.70 2026.07.13\n30 21302060 外箱 30*20*10cm W535A/AB 个 150 1.230000 184.50 2026.07.13\n"


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


def test_tianhua_split_unit_integrity_and_decimal_quantity_review() -> None:
    from app.services.order_pdf_import import parse_purchase_order_text

    split_text = (
        TIANHUA_PREFIX.format(po="PO2026070130")
        + '70 21311404 E189白底蓝字内箱（22.5"*30"）\n'
        + "93.5*60*20cm W535A/AB\n"
        + "个\n（无\n小数\n）\n50\n5.760000\n288.00\n2026.07.13\n"
        + "合计 50 288.00\n"
    )
    draft = parse_purchase_order_text(split_text, source_name="PO2026070130.pdf")
    item = draft["items"][0]
    assert (item["line_no"], item["product_code"], item["quantity"], item["unit_price"]) == (
        70,
        "21311404",
        50,
        "5.7600",
    )
    assert item["raw_product_name"] == 'E189白底蓝字内箱（22.5"*30"）'
    assert item["amount"] == "288.00"
    assert item["delivery_date"] == "2026-07-13"
    assert draft["integrity_check"]["integrity_status"] == "passed"

    ok = parse_purchase_order_text(
        TIANHUA_PREFIX.format(po="PO2026070157")
        + TIANHUA_THREE_LINES
        + "合计 1180 768.20\n"
    )
    assert [item["product_code"] for item in ok["items"]] == ["23203105", "21302006", "21302060"]
    assert ok["integrity_check"]["integrity_status"] == "passed"
    assert ok["integrity_check"]["parsed_total_amount"] == "768.20"

    missing = parse_purchase_order_text(
        TIANHUA_PREFIX.format(po="PO2026070157")
        + "10 23203105 白底黑字内箱 28.5*19.5*5.5cm 个 漏识别\n"
        + TIANHUA_THREE_LINES.split("\n", 1)[1]
        + "合计 1180 768.20\n"
    )
    assert [item["line_no"] for item in missing["items"]] == [20, 30]
    assert missing["integrity_check"]["missing_line_numbers"] == ["10"]
    assert missing["integrity_check"]["quantity_total_diff"] == "1000"
    assert missing["integrity_check"]["integrity_status"] == "failed"

    fractional = parse_purchase_order_text(
        TIANHUA_PREFIX.format(po="PO2026070180")
        + "50 21301022 中性外箱 117*68.5*16.5cm 个 0.60000 12.720000 7.63 2026.07.14\n"
        + "合计 0.6 7.63\n"
    )
    assert fractional["items"][0]["quantity"] == 0.6
    assert fractional["items"][0]["raw_quantity"] == "0.60000"
    assert fractional["items"][0]["quantity_review_required"] is True
    assert fractional["requires_manual_quantity_review"] is True
    assert any("数量 0.6 不是整数" in warning for warning in fractional["warnings"])
    assert fractional["integrity_check"]["integrity_status"] == "passed"


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
        assert matched["customer_match_status"] == "matched"
        assert matched["items"][0]["matched_product_id"] is not None
        assert matched["items"][1]["matched_product_id"] is not None
        assert matched["items"][2]["matched_product_id"] is None
    finally:
        engine.dispose()
        database_path.unlink(missing_ok=True)


def test_pdf_material_candidates_keep_supplier_and_weight_for_same_code(
    tmp_path: Path,
) -> None:
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.customer import Customer
    from app.models.material import Material
    from app.services.order_pdf_import import (
        match_import_draft,
        parse_purchase_order_text,
    )

    engine = create_sqlite_engine(tmp_path / "pdf-same-code-materials.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with session_factory() as session:
            customer = Customer(
                customer_number=9001,
                customer_code="PDF-SAME-CODE",
                name="苏州天华超净科技有限公司",
                payment_term_days=30,
                credit_limit=Decimal("100000"),
            )
            session.add_all(
                [
                    customer,
                    Material(
                        code="G9G",
                        supplier_name="昆山鸣朋",
                        layer_count=3,
                        basis_weight_description="250g/170g/250g",
                    ),
                    Material(
                        code="G9G",
                        supplier_name="胜源",
                        layer_count=3,
                        basis_weight_description="230g/140g/230g",
                    ),
                ]
            )
            session.commit()

            draft = parse_purchase_order_text(
                SAMPLE_PO_TEXT,
                source_name="same-code-materials.pdf",
            )
            matched = match_import_draft(session, draft, customer.id)
            candidates = matched["items"][0]["material_candidates"]

            assert {
                (
                    row["code"],
                    row["supplier_name"],
                    row["basis_weight_description"],
                )
                for row in candidates
            } == {
                ("G9G", "昆山鸣朋", "250g/170g/250g"),
                ("G9G", "胜源", "230g/140g/230g"),
            }
    finally:
        engine.dispose()


def _match_simair_duplicate_cpn(tmp_path: Path, item: dict) -> tuple[dict, int, int]:
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.customer import Customer
    from app.models.product import Product
    from app.services.order_pdf_import import match_import_draft

    engine = create_sqlite_engine(tmp_path / "simair-duplicate-cpn.sqlite3")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        customer = Customer(
            customer_number=37,
            customer_code="SMA",
            name="苏州思迈尔电子设备有限公司",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        session.add(customer)
        session.flush()
        first = Product(
            customer_id=customer.id,
            product_code="CPN126831",
            customer_material_code="CPN084557 CPN126831",
            product_name="纸板内衬",
            length_mm=Decimal("73"),
            width_mm=Decimal("35"),
            height_mm=Decimal("24.5"),
            sale_unit_price=Decimal("18.11"),
            box_category="normal",
        )
        second = Product(
            customer_id=customer.id,
            product_code="CPN126830",
            customer_material_code="CPN084557 CPN126830",
            product_name="瓦楞外箱",
            length_mm=Decimal("61"),
            width_mm=Decimal("42"),
            height_mm=Decimal("31"),
            sale_unit_price=Decimal("16.80"),
            box_category="normal",
        )
        session.add_all([first, second])
        session.commit()
        matched = match_import_draft(
            session,
            {
                "customer_type": "simair",
                "recognition_status": "recognized",
                "warnings": [],
                "items": [{"product_code": "CPN084557", "quantity": 100, **item}],
            },
            customer_id=customer.id,
        )
        return matched, first.id, second.id


def test_simair_duplicate_cpn_uses_variant_name_spec_and_price_evidence(
    tmp_path: Path,
) -> None:
    matched, first_id, _second_id = _match_simair_duplicate_cpn(
        tmp_path,
        {
            "reference_product_code": "CPN126831",
            "raw_product_name": "纸板内衬",
            "raw_spec_model": "73×35×24.5mm",
            "unit_price": "18.11",
        },
    )

    item = matched["items"][0]
    assert item["matched_product_id"] == first_id
    assert item["match_evidence"]["policy"] == "simair_duplicate_cpn_fail_closed"
    assert item["match_evidence"]["decision"] == "matched"
    assert item["match_evidence"]["margin"] >= item["match_evidence"]["minimum_margin"]
    assert all("cost" not in candidate for candidate in item["product_candidates"])
    assert {candidate["customer_material_code"] for candidate in item["product_candidates"]} == {
        "CPN084557 CPN126831",
        "CPN084557 CPN126830",
    }


def test_simair_duplicate_cpn_missing_name_and_spec_stays_unmatched(
    tmp_path: Path,
) -> None:
    matched, _first_id, _second_id = _match_simair_duplicate_cpn(
        tmp_path,
        {"raw_product_name": "CPN084557", "raw_spec_model": "", "unit_price": "18.11"},
    )

    item = matched["items"][0]
    assert item["matched_product_id"] is None
    assert item["match_status"] == "unmatched"
    assert any("缺少" in reason for reason in item["match_evidence"]["reasons"])


def test_simair_wrong_ocr_name_and_coincidental_price_does_not_select_variant(
    tmp_path: Path,
) -> None:
    matched, _first_id, _second_id = _match_simair_duplicate_cpn(
        tmp_path,
        {
            "raw_product_name": "乱码错误名称",
            "raw_spec_model": "999×999×999mm",
            "unit_price": "18.11",
        },
    )

    item = matched["items"][0]
    assert item["matched_product_id"] is None
    assert item["match_evidence"]["top_score"] < item["match_evidence"]["minimum_score"]


def test_simair_duplicate_cpn_conflicting_fields_and_small_margin_fail_closed(
    tmp_path: Path,
) -> None:
    matched, _first_id, _second_id = _match_simair_duplicate_cpn(
        tmp_path,
        {
            "raw_product_name": "纸板内衬",
            "raw_spec_model": "61×42×31mm",
            "unit_price": "16.80",
        },
    )

    item = matched["items"][0]
    assert item["matched_product_id"] is None
    assert item["match_evidence"]["margin"] < item["match_evidence"]["minimum_margin"]
    assert any("指向不同候选" in reason for reason in item["match_evidence"]["reasons"])


def test_customer_match_returns_multiple_candidates_for_ambiguous_name(
    tmp_path: Path,
) -> None:
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.customer import Customer
    from app.services.order_pdf_import import _customer_match

    engine = create_sqlite_engine(tmp_path / "ambiguous-customer.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        session.add_all(
            [
                Customer(
                    customer_number=1,
                    customer_code="TH1",
                    name="苏州天华超净科技有限公司",
                    payment_term_days=30,
                    credit_limit=0,
                ),
                Customer(
                    customer_number=2,
                    customer_code="TH2",
                    name="天华科技包装有限公司",
                    payment_term_days=30,
                    credit_limit=0,
                ),
            ]
        )
        session.commit()
        status, customer_id, candidates = _customer_match(session, "天华")

    assert status == "multiple_candidates"
    assert customer_id is None
    assert {row["name"] for row in candidates} == {
        "苏州天华超净科技有限公司",
        "天华科技包装有限公司",
    }
    assert all(row["name"] != "天华" for row in candidates)


def test_full_customer_name_wins_over_short_customer_candidate(
    tmp_path: Path,
) -> None:
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.customer import Customer
    from app.services.order_pdf_import import _customer_match

    engine = create_sqlite_engine(tmp_path / "full-customer-priority.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    full_name = "苏州天华超净科技股份有限公司"
    with session_factory() as session:
        session.add_all(
            [
                Customer(
                    customer_number=1,
                    customer_code="TH",
                    name=full_name,
                    payment_term_days=30,
                    credit_limit=0,
                ),
                Customer(
                    customer_number=2,
                    customer_code="SHORT",
                    name="天华",
                    payment_term_days=30,
                    credit_limit=0,
                ),
                Customer(
                    customer_number=3,
                    customer_code="THXN",
                    name="苏州天华新能源科技股份有限公司",
                    payment_term_days=30,
                    credit_limit=0,
                ),
            ]
        )
        session.commit()
        status, customer_id, candidates = _customer_match(session, full_name)

    assert status == "matched"
    assert customer_id == 1
    assert candidates == [{"id": 1, "name": full_name}]


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
        simair_customer = Customer(
            id=37,
            customer_number=37,
            customer_code="SMA",
            name="苏州思迈尔电子设备有限公司",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        session.add(simair_customer)
        session.flush()
        gaotai_customer = Customer(
            id=46,
            customer_number=46,
            customer_code="GT",
            name="苏州高泰电子技术股份有限公司",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        session.add(gaotai_customer)
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
                Product(
                    customer_id=gaotai_customer.id,
                    product_code="3D90078",
                    customer_material_code="3D90078",
                    product_name="纸箱615*460*375",
                    box_category="normal",
                ),
                Product(
                    customer_id=gaotai_customer.id,
                    product_code="3D90095",
                    customer_material_code="3D90095纸箱460*305*225",
                    product_name="纸箱460*305*225",
                    box_category="normal",
                ),
                Product(
                    customer_id=gaotai_customer.id,
                    product_code="3D30268",
                    customer_material_code="3.D30268",
                    product_name="纸箱190*190*160",
                    box_category="normal",
                ),
                Product(
                    customer_id=simair_customer.id,
                    product_code="CPN126831",
                    customer_material_code="CPN084557 CPN126831",
                    product_name="纸板内衬",
                    length_mm=Decimal("73"),
                    width_mm=Decimal("35"),
                    height_mm=Decimal("24.5"),
                    sale_unit_price=Decimal("18.11"),
                    box_category="normal",
                ),
                Product(
                    customer_id=simair_customer.id,
                    product_code="CPN126830",
                    customer_material_code="CPN084557 CPN126830",
                    product_name="瓦楞外箱",
                    length_mm=Decimal("61"),
                    width_mm=Decimal("42"),
                    height_mm=Decimal("31"),
                    sale_unit_price=Decimal("16.80"),
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


def _database_scalar(app: FastAPI, statement):
    from app.api.deps import get_db

    dependency = app.dependency_overrides[get_db]
    db_generator = dependency()
    db = next(db_generator)
    try:
        return db.scalar(statement)
    finally:
        db_generator.close()


def _signed_pdf_preview_token(
    app: FastAPI,
    *,
    recognition_status: str = "needs_confirmation",
    customer_route_status: str = "needs_confirmation",
    customer_match_status: str = "matched",
    integrity_status: str = "passed",
    matched_customer_id: int = 1,
) -> str:
    from sqlalchemy import select

    from app.api.orders import _encode_pdf_preview_safety_token
    from app.models.user import User

    user = _database_scalar(app, select(User).where(User.username == "sales"))
    return _encode_pdf_preview_safety_token(
        {
            "source_name": "needs-confirmation.pdf",
            "file_hash": "a" * 64,
            "recognition_status": recognition_status,
            "customer_route": {"status": customer_route_status},
            "customer_match_status": customer_match_status,
            "integrity_check": {"integrity_status": integrity_status},
            "matched_customer_id": matched_customer_id,
        },
        user,
    )


def _decode_pdf_preview_token_for_test(token: str) -> dict:
    import jwt

    from app.core.config import load_settings

    return jwt.decode(
        token,
        load_settings().secret_key,
        algorithms=["HS256"],
    )


def test_pdf_preview_endpoint_returns_draft_without_writing_order(
    monkeypatch,
    tmp_path: Path,
) -> None:
    app = _order_import_app(tmp_path)
    import app.services.pdf_parse_pipeline as pdf_pipeline

    monkeypatch.setattr(
        pdf_pipeline,
        "extract_text_from_pdf_bytes",
        lambda _content: SAMPLE_PO_TEXT,
    )
    from sqlalchemy import func, select

    from app.models.order import Order

    before_count = _database_scalar(app, select(func.count(Order.id)))

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
    assert body["preview_safety_token"]
    claims = _decode_pdf_preview_token_for_test(body["preview_safety_token"])
    assert claims["source_name"] == "PO2026060469.pdf"
    assert claims["source_hash"] == body["file_hash"]
    assert claims["recognition_status"] == body["recognition_status"]
    assert claims["customer_route_status"] == body["customer_route"]["status"]
    assert claims["customer_match_status"] == body["customer_match_status"]
    assert claims["integrity_status"] == body["integrity_check"]["integrity_status"]
    assert body["customer_po"] == "PO2026060469"
    assert body["matched_customer_id"] == 1
    assert body["item_count"] == 3
    assert body["items"][0]["matched_product_id"] is not None
    assert body["items"][2]["matched_product_id"] is None
    assert _database_scalar(app, select(func.count(Order.id))) == before_count


def test_pdf_integrity_failed_payload_cannot_create_order(tmp_path: Path) -> None:
    app = _order_import_app(tmp_path)

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "sales", "password": "RolePass123!"})
        response = client.post(
            "/api/orders",
            json={
                "customer_id": 1,
                "customer_po": "PO2026070157",
                "order_date": "2026-07-01",
                "delivery_date": "2026-07-13",
                "import_integrity_status": "failed",
                "import_integrity_errors": ["PDF疑似有3行明细，系统只识别出2行，缺失行号：10"],
                "items": [
                    {
                        "product_id": 1,
                        "quantity": 30,
                        "unit_price": "1.79",
                        "product_code": "21312009",
                        "product_name": "中性内箱",
                    }
                ],
            },
        )

    assert response.status_code == 400
    assert "当前 PDF 识别存在漏行或合计不一致" in response.json()["detail"]


def test_pdf_import_non_integer_quantity_requires_manual_fix(tmp_path: Path) -> None:
    app = _order_import_app(tmp_path)

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "sales", "password": "RolePass123!"})
        response = client.post(
            "/api/orders",
            json={
                "customer_id": 1,
                "customer_po": "PO2026070180",
                "order_date": "2026-07-01",
                "delivery_date": "2026-07-14",
                "items": [
                    {
                        "product_id": 1,
                        "quantity": 0.6,
                        "unit_price": "12.72",
                        "product_code": "21312009",
                        "product_name": "中性内箱",
                    }
                ],
            },
        )

    assert response.status_code == 400
    assert "当前 PDF 识别存在非整数数量" in response.json()["detail"]


def _pdf_order_payload(*, confirmed: bool, token: str) -> dict:
    return {
        "customer_id": 1,
        "customer_po": "PO-PDF-SAFETY-001",
        "order_date": "2026-07-17",
        "delivery_date": "2026-07-24",
        "import_draft": True,
        "import_integrity_status": "passed",
        "import_integrity_errors": [],
        "pdf_import_confirmation": {
            "preview_safety_token": token,
            "confirmed": confirmed,
        },
        "items": [
            {
                "product_id": 1,
                "quantity": 30,
                "unit_price": "1.79",
                "product_code": "21312009",
                "product_name": "中性内箱",
            }
        ],
    }


def test_needs_confirmation_pdf_cannot_save_without_explicit_confirmation(
    tmp_path: Path,
) -> None:
    from sqlalchemy import func, select

    from app.models.order import Order

    app = _order_import_app(tmp_path)
    token = _signed_pdf_preview_token(app)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "sales", "password": "RolePass123!"})
        response = client.post(
            "/api/orders",
            json=_pdf_order_payload(confirmed=False, token=token),
        )

    assert response.status_code == 409
    assert "尚未执行明确确认" in response.json()["detail"]
    assert _database_scalar(app, select(func.count(Order.id))) == 0


def test_pdf_import_marker_without_confirmation_context_cannot_bypass_backend(
    tmp_path: Path,
) -> None:
    from sqlalchemy import func, select

    from app.models.order import Order

    app = _order_import_app(tmp_path)
    payload = _pdf_order_payload(
        confirmed=True,
        token=_signed_pdf_preview_token(app),
    )
    payload.pop("pdf_import_confirmation")
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "sales", "password": "RolePass123!"})
        response = client.post("/api/orders", json=payload)

    assert response.status_code == 400
    assert "缺少服务端保存确认上下文" in response.json()["detail"]
    assert _database_scalar(app, select(func.count(Order.id))) == 0


def test_tampered_pdf_preview_token_is_rejected_without_writing_order(
    tmp_path: Path,
) -> None:
    import jwt

    from sqlalchemy import func, select

    from app.models.order import Order

    app = _order_import_app(tmp_path)
    token = _signed_pdf_preview_token(app)
    tampered_claims = jwt.decode(
        token,
        options={"verify_signature": False},
    )
    tampered_claims["recognition_status"] = "recognized"
    tampered_claims["customer_route_status"] = "locked"
    tampered_token = jwt.encode(
        tampered_claims,
        "attacker-controlled-secret",
        algorithm="HS256",
    )
    payload = _pdf_order_payload(confirmed=True, token=tampered_token)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "sales", "password": "RolePass123!"})
        response = client.post("/api/orders", json=payload)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PDF_PREVIEW_TOKEN_STALE"
    assert _database_scalar(app, select(func.count(Order.id))) == 0


def test_expired_invalid_and_missing_pdf_preview_tokens_are_rejected(
    tmp_path: Path,
) -> None:
    from datetime import datetime, timedelta, timezone

    import jwt

    from app.core.config import load_settings
    from sqlalchemy import func, select

    from app.models.order import Order

    app = _order_import_app(tmp_path)
    valid = _signed_pdf_preview_token(app)
    claims = jwt.decode(
        valid,
        load_settings().secret_key,
        algorithms=["HS256"],
        options={"verify_exp": False},
    )
    claims["exp"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    expired = jwt.encode(claims, load_settings().secret_key, algorithm="HS256")

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "sales", "password": "RolePass123!"})
        expired_response = client.post(
            "/api/orders",
            json=_pdf_order_payload(confirmed=True, token=expired),
        )
        invalid_response = client.post(
            "/api/orders",
            json=_pdf_order_payload(confirmed=True, token="not-a-jwt"),
        )
        missing_payload = _pdf_order_payload(confirmed=True, token=valid)
        missing_payload["pdf_import_confirmation"].pop("preview_safety_token")
        missing_response = client.post("/api/orders", json=missing_payload)

    assert expired_response.status_code == 409
    assert invalid_response.status_code == 409
    assert missing_response.status_code == 422
    assert expired_response.json()["detail"]["code"] == "PDF_PREVIEW_TOKEN_STALE"
    assert invalid_response.json()["detail"]["code"] == "PDF_PREVIEW_TOKEN_STALE"
    assert _database_scalar(app, select(func.count(Order.id))) == 0


def test_signed_integrity_failure_or_missing_cannot_be_changed_to_passed_by_client(
    tmp_path: Path,
) -> None:
    from sqlalchemy import func, select

    from app.models.order import Order

    app = _order_import_app(tmp_path)
    responses = []
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "sales", "password": "RolePass123!"})
        for signed_status in ("failed", "missing"):
            token = _signed_pdf_preview_token(
                app,
                integrity_status=signed_status,
            )
            payload = _pdf_order_payload(confirmed=True, token=token)
            payload["import_integrity_status"] = "passed"
            payload["pdf_import_confirmation"]["integrity_status"] = "passed"
            responses.append(client.post("/api/orders", json=payload))

    assert all(response.status_code == 409 for response in responses)
    assert all(
        response.json()["detail"]["code"] == "PDF_PREVIEW_INTEGRITY_FAILED"
        for response in responses
    )
    assert _database_scalar(app, select(func.count(Order.id))) == 0


def test_draft_rematch_rejects_source_name_or_hash_mismatch(tmp_path: Path) -> None:
    app = _order_import_app(tmp_path)
    token = _signed_pdf_preview_token(app)
    base_draft = {
        "source_name": "needs-confirmation.pdf",
        "file_hash": "a" * 64,
        "recognition_status": "needs_confirmation",
        "customer_route": {"status": "needs_confirmation"},
        "integrity_check": {"integrity_status": "passed"},
        "items": [{"product_code": "21312009", "quantity": 30, "unit_price": "1.79"}],
    }
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "sales", "password": "RolePass123!"})
        wrong_name = client.post(
            "/api/orders/draft-rematch",
            json={
                "draft": {**base_draft, "source_name": "different.pdf"},
                "customer_id": 1,
                "preview_safety_token": token,
            },
        )
        wrong_hash = client.post(
            "/api/orders/draft-rematch",
            json={
                "draft": {**base_draft, "file_hash": "b" * 64},
                "customer_id": 1,
                "preview_safety_token": token,
            },
        )

    assert wrong_name.status_code == 409
    assert wrong_hash.status_code == 409
    assert wrong_name.json()["detail"]["code"] == "PDF_PREVIEW_TOKEN_STALE"
    assert wrong_hash.json()["detail"]["code"] == "PDF_PREVIEW_TOKEN_STALE"


def test_normal_non_pdf_order_create_is_unaffected(tmp_path: Path) -> None:
    app = _order_import_app(tmp_path)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "sales", "password": "RolePass123!"})
        response = client.post(
            "/api/orders",
            json={
                "customer_id": 1,
                "customer_po": "NORMAL-NON-PDF-001",
                "order_date": "2026-07-17",
                "items": [{"product_id": 1, "quantity": 10, "unit_price": "1.79"}],
            },
        )

    assert response.status_code == 201, response.text


def test_manual_pdf_customer_product_and_quantity_confirmation_allows_save_and_logs(
    tmp_path: Path,
) -> None:
    from sqlalchemy import select

    from app.api.deps import get_db
    from app.models.audit import OperationLog
    from app.models.material import Material
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.supplier import Supplier
    from app.services.supplier_master import normalize_supplier_identity

    app = _order_import_app(tmp_path)
    dependency = app.dependency_overrides[get_db]
    db_generator = dependency()
    db = next(db_generator)
    current_material = Material(
        code="P1-CURRENT-3B",
        paper_composition="A=B",
        supplier_name="P1 Supplier",
        layer_count=3,
        flute_type="B",
    )
    pdf_candidate = Material(
        code="P1-PDF-3B",
        paper_composition="B=C",
        supplier_name="PDF Supplier",
        layer_count=3,
        flute_type="B",
    )
    db.add_all(
        [
            Supplier(
                standard_name="P1 Supplier",
                normalized_name=normalize_supplier_identity("P1 Supplier"),
                display_name="P1 Supplier",
                is_active=True,
                version=1,
            ),
            Supplier(
                standard_name="PDF Supplier",
                normalized_name=normalize_supplier_identity("PDF Supplier"),
                display_name="PDF Supplier",
                is_active=True,
                version=1,
            ),
            current_material,
            pdf_candidate,
        ]
    )
    db.flush()
    product = db.get(Product, 1)
    assert product is not None
    product.material_id = current_material.id
    product.layer_count = 3
    product.flute_type = "B"
    db.commit()
    current_material_id = current_material.id
    pdf_candidate_id = pdf_candidate.id
    db_generator.close()
    token = _signed_pdf_preview_token(app)
    payload = _pdf_order_payload(confirmed=True, token=token)
    payload["items"][0].update(
        {
            "material_id": pdf_candidate_id,
            "material": "PDF-RAW-9CCC9",
            "original_material_code": "PDF-RAW-9CCC9",
        }
    )
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "sales", "password": "RolePass123!"})
        response = client.post(
            "/api/orders",
            json=payload,
        )

    assert response.status_code == 201, response.text
    saved_item = _database_scalar(
        app,
        select(OrderItem).order_by(OrderItem.id.desc()),
    )
    assert saved_item is not None
    assert saved_item.material_id == current_material_id
    assert saved_item.snapshot_material == "P1-CURRENT-3B"
    assert saved_item.snapshot_original_material_code == "PDF-RAW-9CCC9"
    log = _database_scalar(
        app,
        select(OperationLog).where(OperationLog.action == "PDF_SAFETY_OVERRIDE"),
    )
    assert log is not None
    assert "recognition_status=needs_confirmation" in (log.details or "")
    assert "customer_route=needs_confirmation" in (log.details or "")


def test_manual_simair_selection_uses_fresh_rematch_token_and_logs_override(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from sqlalchemy import select

    import app.api.orders as orders_api
    from app.models.audit import OperationLog

    app = _order_import_app(tmp_path)
    monkeypatch.setattr(
        orders_api,
        "_parse_order_pdf_preview",
        lambda _content, filename, _rules: {
            "source_name": filename,
            "customer_type": "simair",
            "customer_po": "P-MANUAL-SIMAIR-001",
            "recognition_status": "needs_confirmation",
            "parse_status": "needs_confirmation",
            "customer_route": {"status": "needs_confirmation", "candidates": []},
            "integrity_check": {"integrity_status": "passed"},
            "warnings": [],
            "items": [
                {
                    "line_no": 10,
                    "product_code": "CPN084557",
                    "raw_product_name": "乱码错误名称",
                    "raw_spec_model": "999×999×999mm",
                    "quantity": 100,
                    "unit_price": "18.11",
                }
            ],
        },
    )

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "sales", "password": "RolePass123!"})
        preview = client.post(
            "/api/orders/pdf-preview",
            files={"file": ("simair-manual.pdf", b"%PDF-stub", "application/pdf")},
        )
        assert preview.status_code == 200, preview.text
        preview_draft = preview.json()
        rematch = client.post(
            "/api/orders/draft-rematch",
            json={
                "draft": preview_draft,
                "customer_id": 37,
                "preview_safety_token": preview_draft["preview_safety_token"],
            },
        )
        assert rematch.status_code == 200, rematch.text
        rematched = rematch.json()
        assert rematched["preview_safety_token"] != preview_draft["preview_safety_token"]
        rematch_claims = _decode_pdf_preview_token_for_test(
            rematched["preview_safety_token"]
        )
        assert rematch_claims["source_name"] == "simair-manual.pdf"
        assert rematch_claims["source_hash"] == preview_draft["file_hash"]
        assert rematch_claims["recognition_status"] == "needs_confirmation"
        assert rematch_claims["customer_route_status"] == "needs_confirmation"
        assert rematch_claims["customer_match_status"] == "matched"
        assert rematch_claims["integrity_status"] == "passed"
        assert rematch_claims["matched_customer_id"] == 37
        candidate = next(
            row
            for row in rematched["items"][0]["product_candidates"]
            if row["customer_material_code"].endswith("CPN126831")
        )
        save = client.post(
            "/api/orders",
            json={
                "customer_id": 37,
                "customer_po": rematched["customer_po"],
                "order_date": "2026-07-17",
                "import_draft": True,
                "import_integrity_status": "passed",
                "pdf_import_confirmation": {
                    "preview_safety_token": rematched["preview_safety_token"],
                    "confirmed": True,
                },
                "items": [
                    {
                        "product_id": candidate["id"],
                        "product_code": "CPN084557",
                        "product_name": candidate["product_name"],
                        "specification": candidate["specification"],
                        "quantity": 100,
                        "unit_price": "18.11",
                    }
                ],
            },
        )

    assert save.status_code == 201, save.text
    log = _database_scalar(
        app,
        select(OperationLog).where(OperationLog.action == "PDF_SAFETY_OVERRIDE"),
    )
    assert log is not None
    assert "simair-manual.pdf" in (log.details or "")
    assert "customer_route=needs_confirmation" in (log.details or "")


def test_pdf_preview_uses_ocr_fallback_for_gaotai_image_pdf(
    monkeypatch,
    tmp_path: Path,
) -> None:
    app = _order_import_app(tmp_path)
    import app.services.pdf_parse_pipeline as pdf_pipeline

    monkeypatch.setattr(pdf_pipeline, "extract_text_from_pdf_bytes", lambda _content: "")
    monkeypatch.setattr(
        pdf_pipeline,
        "ocr_pdf_bytes",
        lambda _content: (GAOTAI_OCR_TEXT, "ocr_easyocr"),
    )
    with TestClient(app) as client:
        client.post(
            "/api/auth/login",
            json={"username": "sales", "password": "RolePass123!"},
        )
        response = client.post(
            "/api/orders/pdf-preview",
            files={"file": ("gaotai.pdf", b"%PDF-image-stub", "application/pdf")},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["customer_name"] == "苏州高泰电子技术股份有限公司"
    assert body["customer_po"] == "0100-CG260624-02"
    assert body["matched_customer_id"] == 46
    assert [item["product_code"] for item in body["items"][:3]] == [
        "3D90078",
        "3D90095",
        "3D30268",
    ]
    assert [item["raw_product_code"] for item in body["items"][:3]] == [
        "3090078",
        "3090095",
        "3030268",
    ]
    assert [item["normalized_product_code"] for item in body["items"][:3]] == [
        "3D90078",
        "3D90095",
        "3D30268",
    ]
    assert all(item["matched_product_id"] for item in body["items"][:3])
    assert all(item["match_status"] == "matched" for item in body["items"][:3])
    assert any("3090078" in warning and "3D90078" in warning for warning in body["warnings"])


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
    import app.services.pdf_parse_pipeline as pdf_pipeline

    monkeypatch.setattr(
        pdf_pipeline,
        "extract_text_from_pdf_bytes",
        lambda content: SAMPLE_PO_TEXT if content.endswith(b"good") else "",
    )
    monkeypatch.setattr(
        pdf_pipeline,
        "ocr_pdf_bytes",
        lambda _content: (None, "ocr_failed"),
    )
    with TestClient(app) as client:
        client.post(
            "/api/auth/login",
            json={"username": "sales", "password": "RolePass123!"},
        )
        response = client.post(
            "/api/orders/pdf-preview-batch",
            files=[
                ("files", ("first.pdf", b"%PDF-1.4\ngood", "application/pdf")),
                ("files", ("duplicate.pdf", b"%PDF-1.4\ngood", "application/pdf")),
                ("files", ("bad.pdf", b"%PDF-1.4\nbad", "application/pdf")),
            ],
        )

    assert response.status_code == 200
    drafts = response.json()["drafts"]
    assert all(draft["preview_safety_token"] for draft in drafts)
    assert drafts[0]["recognition_status"] in {"recognized", "needs_confirmation"}
    assert drafts[1]["duplicate_status"] == "duplicate_skipped"
    assert drafts[2]["recognition_status"] == "failed"


def test_batch_pdf_preview_uses_ocr_fallback_without_saving_order(
    monkeypatch,
    tmp_path: Path,
) -> None:
    app = _order_import_app(tmp_path)
    import app.api.orders as orders_api
    import app.services.pdf_parse_pipeline as pdf_pipeline

    monkeypatch.setattr(pdf_pipeline, "extract_text_from_pdf_bytes", lambda _content: "")
    monkeypatch.setattr(
        pdf_pipeline,
        "ocr_pdf_bytes",
        lambda _content: (GAOTAI_OCR_TEXT, "ocr_easyocr"),
    )
    monkeypatch.setattr(orders_api, "match_import_draft", lambda _db, draft: draft)

    with TestClient(app) as client:
        client.post(
            "/api/auth/login",
            json={"username": "sales", "password": "RolePass123!"},
        )
        response = client.post(
            "/api/orders/pdf-preview-batch",
            files=[("files", ("gaotai.pdf", b"%PDF-image-stub", "application/pdf"))],
        )

    assert response.status_code == 200, response.text
    drafts = response.json()["drafts"]
    assert len(drafts) == 1
    assert drafts[0]["customer_po"] == "0100-CG260624-02"
    assert len(drafts[0]["items"]) == 3


def test_simair_merge_keeps_text_numbers_and_dates_while_ocr_supplies_labels() -> None:
    from app.services.order_pdf_import import merge_simair_text_and_ocr_drafts

    text_draft = {
        "customer_type": "simair",
        "customer_po": "P-0028338-2",
        "order_date": "2026-07-13",
        "delivery_date": "2026-07-20",
        "items": [{
            "line_no": 10,
            "product_code": "CPN084557",
            "quantity": 100,
            "unit_price": "18.11",
            "amount": "1811.00",
            "delivery_date": "2026-07-20",
            "raw_product_name": "CPN084557",
        }],
    }
    ocr_draft = {
        "customer_type": "simair",
        "customer_po": "OCR-WRONG",
        "items": [{
            "line_no": 10,
            "product_code": "CPN084557",
            "quantity": 100,
            "unit_price": "99.99",
            "amount": "9999.00",
            "raw_product_name": "纸板内衬",
            "raw_spec_model": "73×35×24.5",
        }],
    }

    merged = merge_simair_text_and_ocr_drafts(text_draft, ocr_draft)

    assert merged["customer_po"] == "P-0028338-2"
    assert merged["order_date"] == "2026-07-13"
    assert merged["items"][0]["quantity"] == 100
    assert merged["items"][0]["unit_price"] == "18.11"
    assert merged["items"][0]["amount"] == "1811.00"
    assert merged["items"][0]["delivery_date"] == "2026-07-20"
    assert merged["items"][0]["raw_product_name"] == "纸板内衬"
    assert merged["items"][0]["raw_spec_model"] == "73×35×24.5"


def test_pdf_draft_edits_and_reservation_changes_invalidate_confirmation() -> None:
    source = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
        encoding="utf-8"
    )

    assert "invalidateImportDraftConfirmation(draft); scheduleOrderLineInventoryRefresh" in source
    assert (
        'v-model="item.unit_price" '
        '@input="invalidateImportDraftConfirmation(draft); refreshPdfPriceConflict(item)"'
        in source
    )
    assert 'v-model.trim="draft.customer_po" @input="invalidateImportDraftConfirmation(draft)"' in source
    assert 'v-model="draft.order_date" @input="invalidateImportDraftConfirmation(draft)"' in source
    assert 'v-model="draft.delivery_date" @input="invalidateImportDraftConfirmation(draft)"' in source
    assert '@change="selectImportProduct(draft,item)"' in source
    assert '@change="toggleNewProduct(draft,item)"' in source
    assert '@click="removeImportDraftItem(draft,item)"' in source
    assert 'v-model.trim="item.production_notes" placeholder="生产说明（来自常用箱，可修改）" @input="invalidateImportDraftConfirmation(draft)"' in source
    assert 'draft?.integrity_check?.integrity_status !== "passed"' in source

    helper = source.split("invalidatePdfDraftForItem(item) {", 1)[1].split(
        "removeImportDraftItem", 1
    )[0]
    assert "this.orderImportDrafts.find" in helper
    assert ".includes(item)" in helper
    assert "invalidateImportDraftConfirmation(draft)" in helper
    assert "orderForm" not in helper

    safe_confirm = source.split("confirmSafeOrderLineInventoryRecommendations(line) {", 1)[1].split(
        "inventorySourcesText", 1
    )[0]
    load_inventory = source.split("async loadOrderLineInventory(line, customerId) {", 1)[1].split(
        "async loadOrderLineManualInventory", 1
    )[0]
    confirm = source.split("confirmOrderLineInventory(line, component", 1)[1].split(
        "skipOrderLineInventory", 1
    )[0]
    skip = source.split("skipOrderLineInventory(line, component)", 1)[1].split(
        "rechooseOrderLineInventory", 1
    )[0]
    rechoose = source.split("rechooseOrderLineInventory(line, component", 1)[1].split(
        "reallocateDraftInventorySequentially", 1
    )[0]
    for block in (safe_confirm, load_inventory, confirm, skip, rechoose):
        assert "invalidatePdfDraftForItem?.(line)" in block
