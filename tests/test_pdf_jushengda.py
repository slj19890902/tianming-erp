"""Source-based regression contract for the Jushengda purchase-order family.

Only sanitized text is versioned. Set ERP_TEST_JUSHENGDA_PDF to the locally held
original PDF to exercise real extraction without copying customer contacts to Git.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.order_pdf_import import parse_purchase_order_text


FIXTURES = Path(__file__).parent / "fixtures" / "pdf_orders"
SOURCE = (FIXTURES / "jushengda_20l_20261022.txt").read_text(encoding="utf-8")
EXPECTED = json.loads((FIXTURES / "jushengda_20l_20261022.json").read_text(encoding="utf-8"))
ROW = "1 1020*620*960MM 90 PCS 20.5 1845 20L包装"


def parse(text: str = SOURCE, **kwargs) -> dict:
    return parse_purchase_order_text(text, **kwargs)


def assert_source_facts(draft: dict) -> None:
    for field in ("customer_name", "customer_po", "order_date", "delivery_date", "currency", "item_count"):
        assert draft[field] == EXPECTED[field]
    assert draft["supplier_name_raw"] == EXPECTED["supplier_name"]
    assert Decimal(str(draft["tax_rate"])) == Decimal(EXPECTED["tax_rate"])
    assert draft["customer_name_raw"] == EXPECTED["customer_name"]
    assert draft["customer_type"] == "jushengda"
    assert draft["template"] == "jushengda_purchase_v1"
    assert draft["source_format"] == "jushengda_purchase"
    actual = draft["items"][0]
    expected = EXPECTED["items"][0]
    assert actual["line_no"] == expected["line_no"]
    for field in ("raw_product_name", "product_name", "raw_spec_model", "specification"):
        assert actual[field] == expected["description"]
    for field in ("quantity", "unit_price", "amount"):
        assert Decimal(str(actual[field])) == Decimal(expected[field])
    assert actual["unit"] == expected["unit"]
    assert actual["product_code"] == actual["raw_product_code"] == expected["product_code"]
    assert actual["matched_product_id"] is None
    assert actual["delivery_date"] == EXPECTED["delivery_date"]
    assert expected["source_tail"] in actual["production_notes"]
    assert actual["preserve_pdf_price"] is True
    integrity = draft["integrity_check"]
    assert integrity["source_total_amount"] is EXPECTED["source_total_amount"]
    assert integrity["source_total_quantity"] is EXPECTED["source_total_quantity"]
    assert Decimal(integrity["parsed_total_amount"]) == Decimal("1845")
    assert integrity["integrity_status"] == EXPECTED["integrity_status"]
    assert integrity["integrity_errors"] == []
    assert draft["recognition_status"] == draft["parse_status"] == EXPECTED["recognition_status"]
    warnings = "\n".join(draft["warnings"])
    assert "料号" in warnings and "总计" in warnings and "核对" in warnings


def test_source_facts_and_delivery_instruction_are_not_guessed() -> None:
    draft = parse()
    assert_source_facts(draft)
    note = EXPECTED["synthetic_delivery_note"]
    assert note in draft["notes"]
    assert note in draft["items"][0]["production_notes"]
    assert "开票资料" not in draft["notes"]
    assert not draft["requires_manual_quantity_review"]


@pytest.mark.parametrize("filename", ["2026年采购单.10-22（天美）20L.pdf", "unrelated-19000101.pdf", "高泰采购单.pdf"])
def test_filename_never_overrides_document_facts(filename: str) -> None:
    assert_source_facts(parse(source_name=filename))


def test_changed_order_number_and_dates_are_read_from_source() -> None:
    text = SOURCE.replace("JSD20261022001", "JSD20261103007").replace("2026年10月22日", "2026年11月3日").replace("2026年10月27日", "2026年11月9日")
    draft = parse(text, source_name="old-JSD20261022001.pdf")
    assert (draft["customer_po"], draft["order_date"], draft["delivery_date"]) == ("JSD20261103007", "2026-11-03", "2026-11-09")
    assert draft["integrity_check"]["integrity_status"] == "unknown"


@pytest.mark.parametrize("original,replacement", [("20.5 1845", "20.5 1846"), ("90 PCS", "91 PCS"), ("20.5 1845", "21.5 1845")])
def test_inconsistent_line_amount_is_failed_and_printed_values_retained(original: str, replacement: str) -> None:
    draft = parse(SOURCE.replace(original, replacement))
    assert draft["integrity_check"]["integrity_status"] == "failed"
    assert draft["integrity_check"]["integrity_errors"]
    assert draft["recognition_status"] == "needs_confirmation"
    expected_amount = "1846" if replacement.endswith("1846") else "1845"
    assert Decimal(draft["items"][0]["amount"]) == Decimal(expected_amount)


@pytest.mark.parametrize("original,replacement,field", [
    ("2026年10月27日", "2026年2月30日", "delivery_date"),
    ("2026年10月22日", "2026年13月22日", "order_date"),
    ("下单日期: 2026年10月22日", "下单日期:", "order_date"),
    ("交货日期：2026年10月27日", "交货日期：", "delivery_date"),
])
def test_invalid_or_absent_dates_fail_without_filename_fallback(original: str, replacement: str, field: str) -> None:
    draft = parse(SOURCE.replace(original, replacement), source_name="JSD20261022001-2026-10-27.pdf")
    assert draft[field] is None
    assert draft["integrity_check"]["integrity_status"] == "failed"


def test_delivery_before_order_date_fails() -> None:
    draft = parse(SOURCE.replace("2026年10月27日", "2026年10月21日"))
    assert draft["delivery_date"] == "2026-10-21"
    assert draft["integrity_check"]["integrity_status"] == "failed"


def test_decimal_quantity_preserves_source_precision_and_requires_review() -> None:
    draft = parse(SOURCE.replace("90 PCS 20.5 1845", "90.5 PCS 20.5 1855.25"))
    item = draft["items"][0]
    assert Decimal(str(item["quantity"])) == Decimal("90.5")
    assert Decimal(item["amount"]) == Decimal("1855.25")
    assert item["quantity_review_required"] is True
    assert draft["requires_manual_quantity_review"] is True
    assert draft["recognition_status"] == "needs_confirmation"


@pytest.mark.parametrize("replacement", [
    ROW + "\n" + ROW,
    ROW + "\n" + ROW.replace("1 ", "3 ", 1),
    ROW.replace("1 ", "2 ", 1),
    ROW + "\n2 800*600*400MM unreadable PCS 10 100 备注",
    ROW + "\n800*600*400MM 10 PCS 10 100 备注",
    ROW.replace("90 PCS", "90"),
    ROW.replace("20.5 1845", "20.5"),
    "",
])
def test_duplicate_missing_sequence_or_unparsed_detail_fails_closed(replacement: str) -> None:
    draft = parse(SOURCE.replace(ROW, replacement))
    assert draft["integrity_check"]["integrity_status"] == "failed"
    assert draft["recognition_status"] == "needs_confirmation"


def test_total_when_printed_is_checked_but_does_not_auto_approve() -> None:
    draft = parse(SOURCE.replace("总   计(金额):", "总   计(金额):1845"))
    assert Decimal(draft["integrity_check"]["source_total_amount"]) == Decimal("1845")
    assert draft["integrity_check"]["integrity_status"] == "unknown"
    assert draft["recognition_status"] == "needs_confirmation"
    failed = parse(SOURCE.replace("总   计(金额):", "总   计(金额):1846"))
    assert failed["integrity_check"]["integrity_status"] == "failed"


@pytest.mark.parametrize("text", [
    SOURCE.replace("90 PCS", "9" * 25 + " PCS"),
    SOURCE.replace("20.5 1845", "9" * 25 + " 1845"),
    SOURCE.replace("20.5 1845", "20.5 " + "9" * 25),
    SOURCE.replace("总   计(金额):", "总   计(金额):" + "9" * 25),
    SOURCE.replace("90 PCS 20.5 1845", "9" * 24 + " PCS " + "9" * 24 + " " + "9" * 24),
])
def test_oversized_or_overflowing_numbers_fail_without_crashing(text: str) -> None:
    draft = parse(text)
    assert draft["integrity_check"]["integrity_status"] == "failed"
    assert draft["integrity_check"]["integrity_errors"]


@pytest.mark.parametrize("text", [
    SOURCE + "\n" + ROW.replace("1 ", "2 ", 1),
    SOURCE.replace(ROW, ROW + " " + ROW.replace("1 ", "2 ", 1)),
])
def test_detail_after_total_or_two_joined_details_cannot_pass(text: str) -> None:
    draft = parse(text)
    assert draft["integrity_check"]["integrity_status"] == "failed"
    assert draft["recognition_status"] == "needs_confirmation"


@pytest.mark.parametrize("source", [
    SOURCE.replace(EXPECTED["customer_name"], "苏州测试客户有限公司"),
    SOURCE.replace(EXPECTED["customer_name"], EXPECTED["supplier_name"], 1),
])
def test_other_buyer_does_not_trigger_jushengda_adapter(source: str) -> None:
    from app.services.pdf_jushengda import parse_jushengda_order

    assert parse_jushengda_order(source, "聚晟达.pdf", {"status": "unmatched"}) is None


def test_supplier_template_conflict_cannot_bind_as_customer() -> None:
    supplier_rule = {
        "template_id": 17, "customer_id": 73, "template_customer_valid": True,
        "customer_name": EXPECTED["supplier_name"], "customer_type": "generic",
    }
    draft = parse(template_rules=[supplier_rule])
    assert draft["customer_name_raw"] == EXPECTED["customer_name"]
    assert draft["customer_route"]["status"] == "needs_confirmation"
    assert draft["customer_route"]["template_customer_id"] is None
    assert draft["integrity_check"]["integrity_status"] == "failed"


def test_preview_and_training_shared_pipeline_preserves_draft(monkeypatch) -> None:
    from app.services import pdf_parse_pipeline as pipeline

    monkeypatch.setattr(pipeline, "extract_text_from_pdf_bytes", lambda _: SOURCE)

    def unexpected_ocr(*args, **kwargs):
        pytest.fail("Readable native PDF text must not be replaced by OCR")

    monkeypatch.setattr(pipeline, "ocr_pdf_bytes", unexpected_ocr)
    result = pipeline.parse_pdf_bytes(b"local-sanitized-test", "天美.pdf", [])
    assert_source_facts(result.draft)
    assert result.parse_method == "text"
    assert not result.ocr_attempted


@pytest.fixture
def customer_products(tmp_path):
    from sqlalchemy.orm import Session
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.customer import Customer
    from app.models.product import Product

    engine = create_sqlite_engine(tmp_path / "jushengda-only-test.sqlite3")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            buyer = Customer(name=EXPECTED["customer_name"], customer_code="JSD")
            other = Customer(name="测试其他客户", customer_code="OTHER")
            db.add_all([buyer, other])
            db.flush()
            same_spec = Product(customer_id=buyer.id, product_code="JSD-001", customer_material_code="001", product_name="1020*620*960MM", sale_unit_price=Decimal("20.5"), length_mm=1020, width_mm=620, height_mm=960)
            cross_customer = Product(customer_id=other.id, product_code="JSD-001", customer_material_code="001", product_name="1020*620*960MM", sale_unit_price=Decimal("20.5"))
            duplicate = Product(customer_id=buyer.id, product_code="JSD-002", customer_material_code="DUP", product_name="重复编码甲")
            duplicate_other = Product(customer_id=buyer.id, product_code="JSD-003", customer_material_code="DUP", product_name="重复编码乙")
            db.add_all([same_spec, cross_customer, duplicate, duplicate_other])
            db.flush()
            yield db, buyer, same_spec, cross_customer
    finally:
        engine.dispose()


def test_missing_code_never_auto_matches_same_price_and_spec(customer_products) -> None:
    from app.services.order_pdf_import import match_import_draft

    db, buyer, product, wrong = customer_products
    result = match_import_draft(db, parse())
    assert result["matched_customer_id"] == buyer.id
    item = result["items"][0]
    assert item["matched_product_id"] is None
    candidate_ids = {candidate["id"] for candidate in item["product_candidates"]}
    assert product.id in candidate_ids
    assert wrong.id not in candidate_ids
    assert item["match_evidence"]["decision"] == "needs_confirmation"


@pytest.mark.parametrize("code,matched", [("001", True), ("JSD-001", True), ("DUP", False), ("jsd-001", False), ("JSD-O01", False), ("missing", False)])
def test_only_unique_exact_code_in_buyer_scope_can_match(customer_products, code: str, matched: bool) -> None:
    from app.services.order_pdf_import import match_import_draft

    db, buyer, product, wrong = customer_products
    draft = parse()
    draft["items"][0].update(raw_product_code=code, product_code=code)
    result = match_import_draft(db, draft)
    item = result["items"][0]
    assert item["matched_product_id"] == (product.id if matched else None)
    assert wrong.id not in {candidate["id"] for candidate in item["product_candidates"]}
    assert result["recognition_status"] == "needs_confirmation"
    assert Decimal(item["unit_price"]) == Decimal("20.5")


def test_repeated_details_keep_order_and_individual_source_notes(customer_products) -> None:
    from app.services.order_pdf_import import match_import_draft

    db, _, _, _ = customer_products
    draft = parse(SOURCE.replace(ROW, ROW + "\n" + ROW.replace("1 ", "2 ", 1).replace("20L包装", "第二批包装")))
    for item in draft["items"]:
        item.update(raw_product_code="001", product_code="001")
    result = match_import_draft(db, draft)
    assert len(result["items"]) == 2
    assert [item["line_no"] for item in result["items"]] == [1, 2]
    assert [Decimal(str(item["quantity"])) for item in result["items"]] == [Decimal("90"), Decimal("90")]
    assert "第二批包装" in result["items"][1]["production_notes"]


def test_customer_route_conflict_exposes_no_product_candidates(customer_products) -> None:
    from app.services.order_pdf_import import match_import_draft

    db, _, _, wrong = customer_products
    draft = parse(customer_route={"status": "locked", "template_customer_id": wrong.customer_id, "customer_name": "测试其他客户"})
    result = match_import_draft(db, draft)
    assert result["matched_customer_id"] is None
    assert result["customer_match_status"] == "needs_confirmation"
    assert result["items"][0]["matched_product_id"] is None
    assert result["items"][0]["product_candidates"] == []


@pytest.fixture
def manual_review_app(tmp_path):
    from sqlalchemy import select
    from app.api.deps import get_db
    from app.api.orders import _encode_pdf_preview_safety_token
    from app.models.customer import Customer
    from app.models.product import Product
    from app.models.user import User
    from test_phase16_pdf_order_import import _order_import_app

    app = _order_import_app(tmp_path)
    dependency = app.dependency_overrides[get_db]()
    db = next(dependency)
    try:
        db.get(Customer, 1).name = EXPECTED["customer_name"]
        product = db.get(Product, 1)
        product.product_name = EXPECTED["items"][0]["description"]
        product.sale_unit_price = Decimal("20.5")
        product.length_mm, product.width_mm, product.height_mm = 1020, 620, 960
        db.commit()
        draft = parse(source_name="jushengda-local.pdf")
        draft.update(file_hash="b" * 64, matched_customer_id=1, customer_match_status="matched")
        user = db.scalar(select(User).where(User.username == "sales"))
        token = _encode_pdf_preview_safety_token(draft, user)
    finally:
        dependency.close()
    return app, draft, token


def _manual_review_request(client, draft: dict, token: str):
    return client.post("/api/orders/draft-manual-rematch", json={
        "draft": draft, "customer_id": 1, "preview_safety_token": token,
        "manual_complete": True,
    })


def _assert_no_order_written(app) -> None:
    from sqlalchemy import func, select
    from app.models.order import Order
    from test_phase16_pdf_order_import import _database_scalar

    assert _database_scalar(app, select(func.count(Order.id))) == 0


def test_manual_review_preserves_notes_amount_and_signed_template(manual_review_app) -> None:
    from fastapi.testclient import TestClient
    from test_phase16_pdf_order_import import _decode_pdf_preview_token_for_test

    app, draft, token = manual_review_app
    draft["items"][0]["matched_product_id"] = 1
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "sales", "password": "RolePass123!"})
        response = _manual_review_request(client, draft, token)
    assert response.status_code == 200, response.text
    reviewed = response.json()
    assert reviewed["manual_entry"] is True
    assert reviewed["items"][0]["matched_product_id"] == 1
    assert reviewed["notes"] == draft["notes"]
    assert reviewed["items"][0]["production_notes"] == draft["items"][0]["production_notes"]
    assert reviewed["items"][0]["raw_code_and_remark"] == "20L包装"
    assert Decimal(reviewed["items"][0]["amount"]) == Decimal("1845")
    assert Decimal(reviewed["items"][0]["unit_price"]) == Decimal("20.5")
    claims = _decode_pdf_preview_token_for_test(reviewed["preview_safety_token"])
    assert claims["source_template"] == "jushengda_purchase_v1"
    assert claims["source_format"] == "jushengda_purchase"
    assert claims["integrity_status"] == "manual_confirmed"
    _assert_no_order_written(app)


@pytest.mark.parametrize("selected_product_id", [None, 3, 999999])
def test_manual_review_needs_valid_explicit_selection_without_source_code(manual_review_app, selected_product_id) -> None:
    from fastapi.testclient import TestClient

    app, draft, token = manual_review_app
    draft["items"][0]["matched_product_id"] = selected_product_id
    # Client-supplied template changes must not remove the signed template policy.
    draft["template"] = "generic"
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "sales", "password": "RolePass123!"})
        response = _manual_review_request(client, draft, token)
    assert response.status_code == 200, response.text
    reviewed = response.json()
    assert reviewed["template"] == "jushengda_purchase_v1"
    assert reviewed["items"][0]["matched_product_id"] is None
    assert reviewed["items"][0]["match_status"] == "unmatched"
    assert all(candidate["id"] != 3 for candidate in reviewed["items"][0]["product_candidates"])
    _assert_no_order_written(app)


@pytest.mark.parametrize("bad_amount", ["1846", None, "NaN"])
def test_manual_review_rejects_wrong_or_absent_printed_amount(manual_review_app, bad_amount) -> None:
    from fastapi.testclient import TestClient

    app, draft, token = manual_review_app
    draft["items"][0].update(matched_product_id=1, amount=bad_amount)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "sales", "password": "RolePass123!"})
        response = _manual_review_request(client, draft, token)
    assert response.status_code == 400, response.text
    assert "金额" in str(response.json()["detail"])
    _assert_no_order_written(app)


@pytest.mark.parametrize("field,value", [
    ("order_date", None), ("order_date", "2026-02-30"),
    ("delivery_date", None), ("delivery_date", "2026-13-27"),
    ("delivery_date", "2026-10-21"), ("delivery_date", "2026/10/27"),
])
def test_manual_review_requires_real_ordered_iso_dates(manual_review_app, field, value) -> None:
    from fastapi.testclient import TestClient

    app, draft, token = manual_review_app
    draft[field] = value
    draft["items"][0]["matched_product_id"] = 1
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "sales", "password": "RolePass123!"})
        response = _manual_review_request(client, draft, token)
    assert response.status_code == 400, response.text
    assert "日期" in str(response.json()["detail"])
    _assert_no_order_written(app)


def _reviewed_order_payload(client, draft: dict, token: str) -> tuple[dict, dict]:
    from test_phase16_pdf_order_import import _pdf_order_payload

    draft["items"][0]["matched_product_id"] = 1
    response = _manual_review_request(client, draft, token)
    assert response.status_code == 200, response.text
    reviewed = response.json()
    payload = _pdf_order_payload(confirmed=True, token=reviewed["preview_safety_token"])
    payload.update(customer_po=draft["customer_po"], order_date=draft["order_date"], delivery_date=draft["delivery_date"], import_integrity_status="manual_confirmed", remark=draft["notes"])
    payload["items"][0].update(quantity=90, unit_price="20.5", production_notes=draft["items"][0]["production_notes"])
    return reviewed, payload


@pytest.mark.parametrize("field,value", [
    ("customer_po", "JSD20261022002"), ("order_date", "2026-10-23"),
    ("delivery_date", "2026-10-28"), ("quantity", 91),
    ("unit_price", "21.5"), ("production_notes", "已更改送货备注"),
    ("product_id", 2),
])
def test_changed_reviewed_fact_cannot_be_saved_with_old_confirmation(manual_review_app, field, value) -> None:
    from fastapi.testclient import TestClient

    app, draft, token = manual_review_app
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "sales", "password": "RolePass123!"})
        _, payload = _reviewed_order_payload(client, draft, token)
        target = payload if field in {"customer_po", "order_date", "delivery_date"} else payload["items"][0]
        target[field] = value
        response = client.post("/api/orders", json=payload)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "PDF_MANUAL_FACTS_CHANGED"
    _assert_no_order_written(app)


@pytest.mark.parametrize("field,value", [("quantity", 91), ("production_notes", "重匹配后更改送货备注"), ("delivery_date", "2026-10-28")])
def test_normal_rematch_cannot_reconfirm_changed_manual_facts(manual_review_app, field, value) -> None:
    from fastapi.testclient import TestClient
    from test_phase16_pdf_order_import import _decode_pdf_preview_token_for_test

    app, draft, token = manual_review_app
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "sales", "password": "RolePass123!"})
        reviewed, payload = _reviewed_order_payload(client, draft, token)
        original_claims = _decode_pdf_preview_token_for_test(reviewed["preview_safety_token"])
        draft_target = reviewed if field == "delivery_date" else reviewed["items"][0]
        payload_target = payload if field == "delivery_date" else payload["items"][0]
        draft_target[field] = payload_target[field] = value
        if field == "quantity":
            draft_target["amount"] = "1865.5"
        rematched = client.post("/api/orders/draft-rematch", json={
            "draft": reviewed, "customer_id": 1,
            "preview_safety_token": reviewed["preview_safety_token"],
        })
        assert rematched.status_code == 200, rematched.text
        renewed_token = rematched.json()["preview_safety_token"]
        renewed_claims = _decode_pdf_preview_token_for_test(renewed_token)
        assert renewed_claims["manual_facts_hash"] == original_claims["manual_facts_hash"]
        payload["pdf_import_confirmation"]["preview_safety_token"] = renewed_token
        response = client.post("/api/orders", json=payload)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "PDF_MANUAL_FACTS_CHANGED"
    _assert_no_order_written(app)


def test_manual_review_rejects_inactive_selected_product(manual_review_app) -> None:
    from fastapi.testclient import TestClient
    from app.api.deps import get_db
    from app.models.product import Product

    app, draft, token = manual_review_app
    dependency = app.dependency_overrides[get_db]()
    db = next(dependency)
    try:
        db.get(Product, 1).is_active = False
        db.commit()
    finally:
        dependency.close()
    draft["items"][0]["matched_product_id"] = 1
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "sales", "password": "RolePass123!"})
        response = _manual_review_request(client, draft, token)
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["matched_product_id"] is None
    assert all(candidate["id"] != 1 for candidate in response.json()["items"][0]["product_candidates"])
    _assert_no_order_written(app)


def test_only_reviewed_draft_saves_once_and_freezes_source_notes(manual_review_app) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select
    from app.models.order import Order, OrderItem
    from test_phase16_pdf_order_import import _database_scalar, _pdf_order_payload

    app, draft, token = manual_review_app
    payload = _pdf_order_payload(confirmed=True, token=token)
    payload.update(customer_po=EXPECTED["customer_po"], order_date=EXPECTED["order_date"], delivery_date=EXPECTED["delivery_date"], import_integrity_status="unknown", remark=draft["notes"])
    payload["items"][0].update(quantity=90, unit_price="20.5", production_notes=draft["items"][0]["production_notes"])
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "sales", "password": "RolePass123!"})
        blocked = client.post("/api/orders", json=payload)
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["detail"]["code"] == "PDF_PREVIEW_INTEGRITY_FAILED"
        _assert_no_order_written(app)
        draft["items"][0]["matched_product_id"] = 1
        reviewed = _manual_review_request(client, draft, token)
        assert reviewed.status_code == 200, reviewed.text
        payload["pdf_import_confirmation"]["preview_safety_token"] = reviewed.json()["preview_safety_token"]
        payload["import_integrity_status"] = "manual_confirmed"
        saved = client.post("/api/orders", json=payload)
        assert saved.status_code == 201, saved.text
        replay = client.post("/api/orders", json=payload)
        assert replay.status_code in (200, 201), replay.text
        assert replay.json()["id"] == saved.json()["id"]
    assert _database_scalar(app, select(func.count(Order.id))) == 1
    assert _database_scalar(app, select(OrderItem.quantity)) == 90
    assert _database_scalar(app, select(OrderItem.unit_price)) == Decimal("20.5")
    assert _database_scalar(app, select(OrderItem.snapshot_production_notes)) == draft["items"][0]["production_notes"]
    assert _database_scalar(app, select(Order.remark)) == draft["notes"]


@pytest.mark.skipif(not os.getenv("ERP_TEST_JUSHENGDA_PDF"), reason="Original customer PDF is local-only; set ERP_TEST_JUSHENGDA_PDF")
def test_local_original_pdf_through_shared_pipeline() -> None:
    from app.services.pdf_parse_pipeline import parse_pdf_bytes

    path = Path(os.environ["ERP_TEST_JUSHENGDA_PDF"])
    result = parse_pdf_bytes(path.read_bytes(), path.name, [])
    assert_source_facts(result.draft)
    # Compare the live extracted delivery note without storing personal details.
    note = next(line.strip() for line in result.extracted_text.splitlines() if line.strip().startswith("备注:20L"))
    assert note in result.draft["notes"]
    assert note in result.draft["items"][0]["production_notes"]
    assert result.parse_method == "text"
