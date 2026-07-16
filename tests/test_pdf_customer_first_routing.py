from decimal import Decimal
from sqlalchemy.orm import sessionmaker

GAOTAI_TEXT = """苏州高泰电子技术股份有限公司 采购合同
合同号/PO: 0100-CG260624-02 日期: 2026/6/24
序号 产品编号 名称 规格 数量 单位 单价(含税) 金额 税率 交货期 备注
3090078 纸箱 SSII 50 510.00 13.00 2026-5-26
合计
"""

def test_locked_route_survives_conflicting_ocr_keywords() -> None:
    from app.services.order_pdf_import import parse_purchase_order_text, resolve_pdf_customer_route
    from app.services.pdf_customer_templates import GAOTAI_TEMPLATE_RULE

    gaotai = {**GAOTAI_TEMPLATE_RULE, "template_id": 1, "customer_id": 10,
              "template_customer_valid": True}
    simair = {"template_id": 2, "customer_id": 20, "template_customer_valid": True,
              "customer_name": "苏州思迈尔电子设备有限公司", "customer_type": "simair"}
    route = resolve_pdf_customer_route(GAOTAI_TEXT, [gaotai, simair])
    parsed = parse_purchase_order_text(
        GAOTAI_TEXT + "\n苏州思迈尔电子设备有限公司", template_rules=[gaotai, simair],
        customer_route=route,
    )

    assert route["status"] == "locked" and route["parser_key"] == "gaotai"
    assert parsed["customer_type"] == "gaotai" and parsed["customer_route"] == route
    assert parsed["items"][0]["product_code"] == "3D90078"


def test_strong_order_number_routes_to_unique_customer_template() -> None:
    from app.services.order_pdf_import import resolve_pdf_customer_route

    simair = {
        "template_id": 8,
        "customer_id": 37,
        "template_customer_valid": True,
        "customer_name": "苏州思迈尔电子设备有限公司",
        "customer_type": "simair",
    }
    route = resolve_pdf_customer_route("P-0029425", [simair])

    assert route["status"] == "locked"
    assert route["parser_key"] == "simair"
    assert route["template_customer_id"] == 37

def test_invalid_and_cross_customer_routes_require_confirmation() -> None:
    from app.services.order_pdf_import import resolve_pdf_customer_route

    invalid = {"template_id": 1, "customer_id": 1, "template_customer_valid": False,
               "customer_name": "甲客户", "customer_type": "generic"}
    assert resolve_pdf_customer_route("甲客户", [invalid])["status"] == "needs_confirmation"
    rules = [
        {**invalid, "template_customer_valid": True},
        {"template_id": 2, "customer_id": 2, "template_customer_valid": True,
         "customer_name": "乙客户", "customer_type": "generic"},
    ]
    route = resolve_pdf_customer_route("甲客户 乙客户", rules)
    assert route["status"] == "needs_confirmation"
    assert route["template_customer_id"] is None and len(route["candidates"]) == 2

    conflicting_parsers = [
        {**invalid, "template_customer_valid": True, "customer_type": "simair"},
        {**invalid, "template_id": 3, "template_customer_valid": True,
         "customer_type": "gaotai"},
    ]
    parser_conflict = resolve_pdf_customer_route("甲客户", conflicting_parsers)
    assert parser_conflict["status"] == "needs_confirmation"

def test_bound_template_skips_name_guess_and_limits_products(tmp_path) -> None:
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.customer import Customer
    from app.models.product import Product
    from app.services.order_pdf_import import match_import_draft

    engine = create_sqlite_engine(tmp_path / "route.sqlite3")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        guessed = Customer(name="甲客户", customer_code="A", credit_limit=Decimal("0"))
        bound = Customer(name="乙客户", customer_code="B", credit_limit=Decimal("0"))
        db.add_all([guessed, bound]); db.flush()
        db.add_all([
            Product(customer_id=guessed.id, product_code="P1", customer_material_code="P1", product_name="甲产品", box_category="normal"),
            Product(customer_id=bound.id, product_code="P1", customer_material_code="P1", product_name="乙产品", box_category="normal"),
        ]); db.commit()
        draft = {
            "customer_name_raw": "甲客户", "customer_type": "unknown",
            "recognition_status": "recognized", "warnings": [],
            "customer_route": {"status": "locked", "template_customer_id": bound.id,
                               "candidates": [], "parser_key": "generic"},
            "items": [{"product_code": "P1", "quantity": 1, "unit_price": "1"}],
        }
        matched = match_import_draft(db, draft)
        bound.is_active = False; bound.status = "inactive"; db.commit()
        rejected = match_import_draft(db, draft)

    assert matched["matched_customer_id"] == bound.id
    assert matched["items"][0]["matched_product_id"] == 2
    assert rejected["customer_match_status"] == "needs_confirmation"
    assert rejected["matched_customer_id"] is None and not rejected["items"][0]["product_candidates"]
