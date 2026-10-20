from datetime import date
from decimal import Decimal
from io import BytesIO
import json

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import select, func

from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.order_import_source import OrderImportSource, OrderImportSourceLine
from app.models.product import Product
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot, WarehouseLocation, InventoryReservation
from app.services.excel_document_import import ExcelImportError, PICK_LIST_TEMPLATE, parse_excel_document
from tests.test_semi_finished_order_reservation import (
    b1_app, fictional_document_evidence, login, order_item, add_finished_lot, finished_plan,
)
from tests.test_t02_order_import_source_identity import _ready_product


def workbook(rows=None, customer="研光"):
    book = Workbook()
    sheet = book.active
    for row in rows or [
        ["纸箱", "001", "A6-2", "80010335", 450, None, None, "80010335", "A", 120, -330, 3.808, 1713.6],
        ["纸箱", "001", "板", "80010335", 10, None, None, "80010335", "A", 5, -5, 3.808, 38.08],
    ]:
        sheet.append(row)
    sheet.append([None] * 12 + ["合计"])
    sheet.append([None, None, None, customer, "出货10/26号或27号"])
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_location_template_preserves_full_demand_prices_rows_and_reference_only():
    doc = parse_excel_document(workbook(), "10.20.xlsx", document_type="order")
    assert doc.template == PICK_LIST_TEMPLATE and doc.customer_code == "YG"
    assert doc.customer_po is None and doc.delivery_date is None
    assert [row.order_quantity for row in doc.rows] == [450, 10]
    assert [row.source_row for row in doc.rows] == [1, 2]
    assert doc.rows[0].unit_price == Decimal("3.808")
    assert doc.rows[0].amount == Decimal("1713.6")
    assert doc.rows[0].source_stock_reference == {"on_hand": "120", "remaining": "-330"}
    assert doc.rows[1].source_location == "板"
    assert "26号" in " ".join(doc.warnings)


@pytest.mark.parametrize("column,value,message", [
    (5, 1.5, "正整数"), (5, 0, "正整数"), (12, -1, "单价"),
    (13, 2, "金额"), (8, "80099999", "编码不一致"), (10, "错误", "原表库存"),
])
def test_location_template_rejects_bad_details(column, value, message):
    rows = [["箱", "001", "", "80010335", 2, None, None, "80010335", "A", 5, 3, 1.125, 2.25],
            ["箱", "001", "", "80010335", 2, None, None, "80010335", "A", 5, 3, 1.125, 2.25]]
    rows[1][column - 1] = value
    with pytest.raises(ExcelImportError, match=message):
        parse_excel_document(workbook(rows), "bad.xlsx", document_type="order")


def test_location_customer_choice_and_uncached_formulas():
    rows = [["箱", "001", "D5-2.3", "80010335", 2, None, None, "80010335", "A", 5, "=J1-E1", 1.125, "=E1*L1"]]
    with pytest.raises(ExcelImportError, match="先选择"):
        parse_excel_document(workbook(rows, ""), "光洋.xlsx", document_type="order")
    doc = parse_excel_document(workbook(rows, ""), "10.20.xlsx", document_type="order", expected_customer_code="GY")
    assert doc.rows[0].amount == Decimal("2.250")
    assert doc.rows[0].source_stock_reference["remaining"] == "3"
    with pytest.raises(ExcelImportError, match="不一致"):
        parse_excel_document(workbook(), "10.20.xlsx", document_type="order", expected_customer_code="GY")


def test_optional_duplicate_code_column_and_blank_location_are_not_required():
    rows = [["箱", "001", "", "80010335", 2, None, None, None, "A", 5, 3, 1.125, 2.25]]
    doc = parse_excel_document(workbook(rows), "list.xlsx", document_type="order")
    assert len(doc.rows) == 1 and doc.rows[0].source_location is None


def preview_setup(factory):
    _ready_product(factory)
    with factory() as db:
        db.get(Customer, 1).customer_code = "YG"
        db.get(Customer, 1).name = "研光测试"
        db.get(Customer, 2).customer_code = "GY"
        db.get(Product, 1).product_code = "80010335"
        db.commit()


def test_real_preview_keeps_repeated_code_rows_and_selected_date(b1_app):
    app, factory = b1_app
    preview_setup(factory)
    content = workbook()
    with TestClient(app) as client:
        login(client)
        response = client.post("/api/orders/import-preview-batch",
            files={"files": ("10.20.xlsx", content)},
            data={"customer_id": 1, "delivery_date": "2026-10-20"})
        assert response.status_code == 200, response.text
        draft = response.json()["drafts"][0]
        assert draft["recognition_status"] != "failed", draft
        assert len(draft["items"]) == 2
        assert [r["quantity"] for r in draft["items"]] == [450, 10]
        assert [r["matched_product_id"] for r in draft["items"]] == [1, 1]
        assert draft["delivery_date"] == "2026-10-20"
        assert draft["customer_po"] is None
        # A client cannot substitute source positions on rematch.
        draft["items"][0]["source_location"] = "伪造位置"
        rematched = client.post("/api/orders/draft-rematch", json={
            "draft": draft, "customer_id": 1, "preview_safety_token": draft["preview_safety_token"]})
        assert rematched.status_code == 200, rematched.text
        from app.api.orders import _decode_pdf_preview_safety_token
        with factory() as db:
            user = db.scalar(select(User).where(User.username == "sales"))
            claims = _decode_pdf_preview_safety_token(rematched.json()["preview_safety_token"], user)
        assert claims["source_lines"][0]["pick_list"]["location"] == "A6-2"
        # Save the real preview and upload the same file again; missing customer
        # PO must not turn an already imported workbook into a second order.
        payload = dict(customer_id=1, customer_po=None, delivery_date=draft["delivery_date"],
            idempotency_key="real-location-workbook-save", import_draft=True,
            import_integrity_status="passed",
            pdf_import_confirmation={"preview_safety_token": draft["preview_safety_token"], "confirmed": True},
            items=[{**order_item(1, r["quantity"], line=f"S{i}"), "unit_price": r["unit_price"]}
                   for i, r in enumerate(draft["items"])])
        saved = client.post("/api/orders", json=payload)
        assert saved.status_code == 201, saved.text
        repeated = client.post("/api/orders/import-preview-batch",
            files={"files": ("renamed.xlsx", content)},
            data={"customer_id": 1, "delivery_date": "2026-10-20"})
        # Workbook bytes must be identical even when its filename changes.
        repeated_draft = repeated.json()["drafts"][0]
        assert repeated_draft["duplicate_status"] == "duplicate_skipped", repeated_draft
        assert repeated_draft["duplicate_order_id"] == saved.json()["id"]


def saved_payload(factory, *, positions=("B1-FG",), quantities=None, plans=None):
    from app.api.orders import _encode_pdf_preview_safety_token
    quantities = quantities or [10] * len(positions)
    with factory() as db:
        user = db.scalar(select(User).where(User.username == "sales"))
        draft = dict(source_name="pick.xlsx", file_hash="f" * 64, source_format="xlsx",
                     template=PICK_LIST_TEMPLATE, recognition_status="recognized",
                     customer_route={"status": "locked"}, customer_match_status="matched",
                     integrity_check={"integrity_status": "passed"}, matched_customer_id=1,
                     items=[dict(line_no=str(i + 1), source_row=i + 1, source_sheet="Sheet1",
                                 raw_product_code="B1-P1", raw_product_name="纸箱", quantity=quantity,
                                 unit_price="1.00", source_location=position,
                                 source_stock_reference={"on_hand": "99999", "remaining": "99989"})
                            for i, (position, quantity) in enumerate(zip(positions, quantities))])
        token = _encode_pdf_preview_safety_token(draft, user)
    return dict(customer_id=1, customer_po=None, delivery_date="2026-10-20",
                idempotency_key="early-pick-import-test", import_draft=True,
                import_integrity_status="passed",
                pdf_import_confirmation={"preview_safety_token": token, "confirmed": True},
                items=[order_item(1, quantity, plan=(plans or [None] * len(positions))[i], line=f"L{i}")
                       for i, quantity in enumerate(quantities)])


def test_saved_source_replay_missing_date_and_read_only_print_without_delivery(b1_app):
    app, factory = b1_app
    _ready_product(factory)
    payload = saved_payload(factory, positions=("B1-FG", "板", ""), quantities=[10, 20, 30])
    with TestClient(app) as client:
        login(client)
        invalid = client.post("/api/orders", json={**payload, "delivery_date": None})
        assert invalid.status_code == 422, invalid.text
        response = client.post("/api/orders", json=payload)
        assert response.status_code == 201, response.text
        order_id = response.json()["id"]
        assert response.json()["early_pick_available"] is True
        repeated = client.post("/api/orders", json={**payload, "idempotency_key": "early-pick-same-source"})
        assert repeated.status_code == 201, repeated.text
        assert repeated.json()["id"] == order_id
        with factory() as db:
            before = db.connection().connection.total_changes
            assert db.scalar(select(func.count(Order.id))) == 1
            assert db.scalar(select(func.count(InventoryLot.id))) == 0
            summary = json.loads(db.scalar(select(OrderImportSourceLine)).confirmation_summary_json)
            assert summary["pick_list"]["location"] == "B1-FG"
        first = client.get(f"/api/orders/{order_id}/early-pick")
        second = client.get(f"/api/orders/{order_id}/early-pick")
        assert first.status_code == second.status_code == 200, first.text
        rows = first.json()["rows"]
        assert [r["order_quantity"] for r in rows] == [10, 20, 30]
        assert rows[0]["available_quantity"] == 0 and rows[0]["shortage_quantity"] == 10
        assert rows[1]["available_quantity"] is None and rows[2]["available_quantity"] is None
        assert "位置待核对" in rows[1]["notes"]
        assert "unit_price" not in first.text
        with factory() as db:
            assert db.scalar(select(func.count(InventoryLot.id))) == 0
            assert db.scalar(select(func.count(InventoryReservation.id))) == 0
            assert db.connection().connection.total_changes == before
            db.get(Order, order_id).status = "cancelled"
            db.commit()
        assert client.get(f"/api/orders/{order_id}/early-pick").status_code == 409


def test_free_stock_is_not_counted_twice_or_taken_from_other_position(b1_app):
    app, factory = b1_app
    _ready_product(factory)
    lot_id, _ = add_finished_lot(factory, product_id=1, quantity=12, key="early-free-stock")
    with factory() as db:
        db.add(WarehouseLocation(location_code="OTHER", location_name="另一个成品位", warehouse_type="finished"))
        db.commit()
    payload = saved_payload(factory, positions=("B1-FG", "B1-FG", "OTHER"))
    with TestClient(app) as client:
        login(client)
        saved = client.post("/api/orders", json=payload)
        assert saved.status_code == 201, saved.text
        response = client.get(f"/api/orders/{saved.json()['id']}/early-pick")
        assert response.status_code == 200, response.text
        assert [r["available_quantity"] for r in response.json()["rows"]] == [10, 2, 0]
        assert "其他位置" in " ".join(response.json()["rows"][2]["notes"])
    with factory() as db:
        lot = db.get(InventoryLot, lot_id)
        assert lot.quantity_available == 12 and lot.quantity_reserved == 0


def test_reserved_stock_counts_only_for_this_order_at_required_location(b1_app):
    app, factory = b1_app
    _ready_product(factory)
    lot_id, version = add_finished_lot(factory, product_id=1, quantity=12, key="early-reserved-stock")
    payload = saved_payload(factory, plans=[dict(finished=[finished_plan(lot_id, version, 6)], semi_finished=[])])
    with TestClient(app) as client:
        login(client)
        saved = client.post("/api/orders", json=payload)
        assert saved.status_code == 201, saved.text
        response = client.get(f"/api/orders/{saved.json()['id']}/early-pick")
        assert response.status_code == 200, response.text
        assert response.json()["rows"][0]["available_quantity"] == 10
        assert response.json()["rows"][0]["locations"][0]["kind"] == "本单已预留"
    with factory() as db:
        lot = db.get(InventoryLot, lot_id)
        assert lot.quantity_available == 6 and lot.quantity_reserved == 6


def test_print_requires_permission_and_customer_scope(b1_app):
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    app, factory = b1_app
    _ready_product(factory)
    with TestClient(app) as client:
        login(client)
        saved = client.post("/api/orders", json=saved_payload(factory))
        assert saved.status_code == 201, saved.text
        order_id = saved.json()["id"]
        with factory() as db:
            sales = db.scalar(select(User).where(User.username == "sales"))
            sales.customer_access_mode = "selected"
            db.add(UserCustomerScope(user_id=sales.id, customer_id=2))
            db.commit()
        assert client.get(f"/api/orders/{order_id}/early-pick").status_code == 403
        with factory() as db:
            sales = db.scalar(select(User).where(User.username == "sales"))
            db.add(UserPermissionOverride(user_id=sales.id, permission_code="orders.view", is_allowed=False))
            db.commit()
        assert client.get(f"/api/orders/{order_id}/early-pick").status_code == 403
    with TestClient(app) as anonymous:
        assert anonymous.get(f"/api/orders/{order_id}/early-pick").status_code == 401


def test_ambiguous_short_location_never_picks_an_arbitrary_slot(b1_app):
    from app.services.order_early_pick import location_index, resolve_location
    _, factory = b1_app
    with factory() as db:
        for code in ("A", "B"):
            db.add(WarehouseLocation(location_code=code, location_name=code, warehouse_type="finished",
                                     rack_display_name="货A6", level_no=2))
        db.commit()
        index, locations = location_index(db)
        assert resolve_location("A6-2", index, locations)[0] is None
        assert resolve_location("D5-2.3", index, locations)[0] is None
