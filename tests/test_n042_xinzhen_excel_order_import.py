from __future__ import annotations

from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
from io import BytesIO
from pathlib import Path
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _carton_marking_xlsx(
    *,
    customer_po: str = "PO-N042-001",
    dimensions: str = "38*26*17cm",
    product_code: str = "",
    cartons: int = 25,
    pieces_per_carton: int = 4,
    total_pieces: int = 100,
) -> bytes:
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    headers = [
        "PO No",
        "Vendor Style",
        "Color",
        "Size",
        "Product Code",
        "Carton Dimensions",
        "Pcs/Carton",
        "Carton Qty",
        "Total Pcs",
        "",
        "",
        "",
        "",
    ]
    for column, value in enumerate(headers, start=2):
        sheet.cell(row=10, column=column, value=value)
    values = [
        customer_po,
        "VENDOR-STYLE-38",
        "Navy",
        "M",
        product_code,
        dimensions,
        pieces_per_carton,
        cartons,
        total_pieces,
    ]
    for column, value in enumerate(values, start=2):
        sheet.cell(row=11, column=column, value=value)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _verified_new_zhen_sample_shape_xlsx() -> bytes:
    """Mirror the real B10:N30 carton-marking form without user file data."""

    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "1"
    sheet["B10"] = "From: HENGLU INTERNATIONAL"
    sheet["C11"] = "COMPANY LIMITED"
    sheet["F10"] = "TO: Winners Merchant Int'l LP"
    sheet["F11"] = "8181 Churchill St"
    sheet["F12"] = "Delta, British Columbia"
    sheet["F13"] = "Canada V4K 0C2"
    sheet["B13"] = "PO# 35 726340"
    sheet["B14"] = "Dept#55"
    sheet["B16"] = "Vendor Style#"
    sheet["D16"] = "Color"
    sheet["F16"] = "Size"
    sheet["H16"] = "Units"
    sheet["B17"] = "MB0091C1"
    sheet["D17"] = "C1Blush"
    sheet["F17"] = '42"x42"'
    sheet["H17"] = 4
    sheet["H21"] = 4
    sheet["H23"] = 25
    sheet["C28"] = "款号#"
    sheet["D28"] = "颜色"
    sheet["E28"] = "箱内配比"
    sheet["F28"] = "总数量"
    sheet["C29"] = "MB0091C1"
    sheet["D29"] = "C1Blush"
    sheet["E29"] = 4
    sheet["F29"] = 100
    sheet["J30"] = "#1到#25"
    sheet["L30"] = "38*26*17cm 25个"
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_parse_real_xlsx_carton_marking_uses_carton_count_not_units_or_total() -> None:
    from app.services.xinzhen_excel_order_import import parse_xinzhen_excel_order

    draft = parse_xinzhen_excel_order(_carton_marking_xlsx(), "carton-marking.xlsx")

    assert draft["customer_po"] == "PO-N042-001"
    assert draft["requires_manual_confirmation"] is True
    item = draft["items"][0]
    assert item["raw_vendor_style"] == "VENDOR-STYLE-38"
    assert item["raw_color"] == "Navy"
    assert item["raw_size"] == "M"
    assert item["raw_carton_dimensions"] == "38*26*17cm"
    assert item["carton_dimensions_mm"] == {
        "length_mm": 380,
        "width_mm": 260,
        "height_mm": 170,
        "source_unit": "cm",
    }
    assert item["quantity"] == 25
    assert item["quantity_check"]["units_per_carton"] == 4
    assert item["quantity_check"]["garment_total"] == 100
    assert item["quantity_check"]["status"] == "passed"
    assert item["unit_price"] is None
    assert item["order_date"] is None
    assert item["delivery_date"] is None


def test_parse_verified_new_zhen_layout_builds_one_25_carton_line() -> None:
    from app.services.xinzhen_excel_order_import import parse_xinzhen_excel_order

    draft = parse_xinzhen_excel_order(
        _verified_new_zhen_sample_shape_xlsx(), "CARTON MARKING PO#35.xls.xlsx"
    )

    assert draft["sheet_name"] == "1"
    assert draft["customer_po"] == "35 726340"
    assert draft["department_code"] == "55"
    assert draft["customer_name_raw"] == "Winners Merchant Int'l LP"
    assert len(draft["items"]) == 1
    item = draft["items"][0]
    assert item["raw_vendor_style"] == "MB0091C1"
    assert item["raw_color"] == "C1Blush"
    assert item["raw_size"] == '42"x42"'
    assert item["raw_carton_dimensions"] == "38*26*17cm 25个"
    assert item["carton_dimensions_mm"]["length_mm"] == 380
    assert item["carton_dimensions_mm"]["width_mm"] == 260
    assert item["carton_dimensions_mm"]["height_mm"] == 170
    assert item["quantity"] == 25
    assert item["quantity_check"] == {
        "units_per_carton": 4,
        "garment_total": 100,
        "reported_carton_count": 25,
        "range_carton_count": 25,
        "dimension_carton_count": 25,
        "derived_carton_count": 25,
        "carton_count": 25,
        "style_units_total": 4,
        "conflicts": [],
        "status": "passed",
        "message": "款式行Units、H21汇总、总件数、H23箱数、箱号范围和尺寸箱数已交叉核对。",
    }


@pytest.mark.parametrize(
    ("cell", "value", "expected_fragment"),
    [
        ("H17", 5, "款式行 Units 合计与 H21 汇总不一致"),
        ("H21", 5, "款式行 Units 合计与 H21 汇总不一致"),
        ("H23", 24, "H23箱数、箱号范围、尺寸箱数或推导箱数不一致"),
        ("J30", "#1到#24", "H23箱数、箱号范围、尺寸箱数或推导箱数不一致"),
        ("L30", "38*26*17cm 24个", "H23箱数、箱号范围、尺寸箱数或推导箱数不一致"),
        ("F29", 96, "H23箱数、箱号范围、尺寸箱数或推导箱数不一致"),
    ],
)
def test_fixed_layout_any_quantity_cross_check_conflict_blocks_transfer(
    cell: str,
    value: object,
    expected_fragment: str,
) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    from app.services.xinzhen_excel_order_import import parse_xinzhen_excel_order

    workbook = openpyxl.load_workbook(BytesIO(_verified_new_zhen_sample_shape_xlsx()))
    workbook.active[cell] = value
    output = BytesIO()
    workbook.save(output)
    workbook.close()

    draft = parse_xinzhen_excel_order(output.getvalue(), "quantity-conflict.xlsx")
    check = draft["items"][0]["quantity_check"]
    assert check["status"] == "needs_review"
    assert draft["quantity_check"]["status"] == "needs_review"
    assert expected_fragment in check["conflicts"]


def test_fixed_layout_rejects_order_evidence_after_row_30() -> None:
    openpyxl = pytest.importorskip("openpyxl")
    from app.services.xinzhen_excel_order_import import (
        XinzhenExcelParseError,
        parse_xinzhen_excel_order,
    )

    workbook = openpyxl.load_workbook(BytesIO(_verified_new_zhen_sample_shape_xlsx()))
    workbook.active["B31"] = "PO# SECOND-ORDER"
    output = BytesIO()
    workbook.save(output)
    workbook.close()

    with pytest.raises(XinzhenExcelParseError, match="第31行后.*疑似截断"):
        parse_xinzhen_excel_order(output.getvalue(), "overflow-row.xlsx")


def test_multiple_visible_order_sheets_require_explicit_selection() -> None:
    openpyxl = pytest.importorskip("openpyxl")
    from app.services.xinzhen_excel_order_import import (
        XinzhenWorksheetSelectionRequired,
        parse_xinzhen_excel_order,
    )

    workbook = openpyxl.load_workbook(BytesIO(_carton_marking_xlsx()))
    workbook.active.title = "Order A"
    copied = workbook.copy_worksheet(workbook.active)
    copied.title = "Order B"
    copied["B11"] = "PO-N042-002"
    output = BytesIO()
    workbook.save(output)
    workbook.close()

    with pytest.raises(XinzhenWorksheetSelectionRequired) as error:
        parse_xinzhen_excel_order(output.getvalue(), "multiple-sheets.xlsx")
    assert error.value.sheet_names == ["Order A", "Order B"]
    selected = parse_xinzhen_excel_order(
        output.getvalue(),
        "multiple-sheets.xlsx",
        worksheet_name="Order B",
    )
    assert selected["sheet_name"] == "Order B"
    assert selected["customer_po"] == "PO-N042-002"


def test_hidden_order_sheet_is_not_considered() -> None:
    openpyxl = pytest.importorskip("openpyxl")
    from app.services.xinzhen_excel_order_import import parse_xinzhen_excel_order

    workbook = openpyxl.load_workbook(BytesIO(_carton_marking_xlsx()))
    workbook.active.title = "Visible Order"
    copied = workbook.copy_worksheet(workbook.active)
    copied.title = "Hidden Order"
    copied.sheet_state = "hidden"
    output = BytesIO()
    workbook.save(output)
    workbook.close()

    draft = parse_xinzhen_excel_order(output.getvalue(), "hidden-sheet.xlsx")
    assert draft["sheet_name"] == "Visible Order"


def test_generic_duplicate_header_conflicting_values_are_rejected() -> None:
    openpyxl = pytest.importorskip("openpyxl")
    from app.services.xinzhen_excel_order_import import (
        XinzhenExcelParseError,
        parse_xinzhen_excel_order,
    )

    workbook = openpyxl.load_workbook(BytesIO(_carton_marking_xlsx()))
    workbook.active["K10"] = "Carton Qty"
    workbook.active["K11"] = 26
    output = BytesIO()
    workbook.save(output)
    workbook.close()

    with pytest.raises(XinzhenExcelParseError, match="重复表头.*冲突值"):
        parse_xinzhen_excel_order(output.getvalue(), "duplicate-header.xlsx")


def test_fixed_layout_needs_review_when_only_one_carton_count_signal_exists() -> None:
    openpyxl = pytest.importorskip("openpyxl")
    from app.services.xinzhen_excel_order_import import parse_xinzhen_excel_order

    workbook = openpyxl.load_workbook(BytesIO(_verified_new_zhen_sample_shape_xlsx()))
    sheet = workbook.active
    sheet["H21"] = None
    sheet["F29"] = None
    sheet["J30"] = None
    sheet["L30"] = "38*26*17cm"
    output = BytesIO()
    workbook.save(output)
    workbook.close()

    draft = parse_xinzhen_excel_order(output.getvalue(), "single-signal.xlsx")

    assert draft["items"][0]["quantity"] == 25
    assert draft["items"][0]["quantity_check"]["status"] == "needs_review"
    assert draft["quantity_check"]["status"] == "needs_review"


def test_parse_sanitized_real_layout_xls_without_machine_specific_path() -> None:
    from app.services.xinzhen_excel_order_import import parse_xinzhen_excel_order

    fixture = FIXTURES / "n042_xinzhen_carton_marking_sanitized.xls.fixture"
    draft = parse_xinzhen_excel_order(
        fixture.read_bytes(), "n042_xinzhen_carton_marking_sanitized.xls"
    )

    assert draft["sheet_name"] == "1"
    assert draft["customer_po"] == "XZ-SAMPLE-001"
    assert draft["vendor_name"] == "SANITIZED GARMENT COMPANY LIMITED"
    assert draft["customer_name_raw"] == "Example Customer"
    assert len(draft["items"]) == 1
    item = draft["items"][0]
    assert item["raw_vendor_style"] == "STYLE-001"
    assert item["raw_color"] == "Blue"
    assert item["raw_size"] == "L"
    assert item["raw_carton_dimensions"] == "38*26*17cm 25个"
    assert item["carton_dimensions_mm"] == {
        "length_mm": 380,
        "width_mm": 260,
        "height_mm": 170,
        "source_unit": "cm",
    }
    assert item["quantity"] == 25
    assert item["quantity_check"]["status"] == "passed"


def test_parse_rejects_incomplete_generic_headers_instead_of_guessing_rows() -> None:
    openpyxl = pytest.importorskip("openpyxl")
    from app.services.xinzhen_excel_order_import import (
        XinzhenExcelParseError,
        parse_xinzhen_excel_order,
    )

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet["B10"] = "Vendor Style"
    sheet["C10"] = "Carton Qty"
    sheet["B11"] = "STYLE-WITH-NUMBERS"
    sheet["C11"] = 25
    sheet["D11"] = 4
    sheet["E11"] = 100
    output = BytesIO()
    workbook.save(output)

    with pytest.raises(XinzhenExcelParseError, match="表头不完整"):
        parse_xinzhen_excel_order(output.getvalue(), "ambiguous.xlsx")


def test_parse_rejects_multiple_customer_pos_in_one_generic_draft() -> None:
    openpyxl = pytest.importorskip("openpyxl")
    from app.services.xinzhen_excel_order_import import (
        XinzhenExcelParseError,
        parse_xinzhen_excel_order,
    )

    workbook = openpyxl.load_workbook(BytesIO(_carton_marking_xlsx()))
    sheet = workbook.active
    for column, value in enumerate(
        [
            "PO-N042-002",
            "SECOND-STYLE",
            "Black",
            "XL",
            "",
            "38*26*17cm",
            4,
            25,
            100,
        ],
        start=2,
    ):
        sheet.cell(row=12, column=column, value=value)
    output = BytesIO()
    workbook.save(output)
    workbook.close()

    with pytest.raises(XinzhenExcelParseError, match="存在多个客户单号"):
        parse_xinzhen_excel_order(output.getvalue(), "multiple-pos.xlsx")


def test_parse_rejects_malformed_xlsx() -> None:
    from app.services.xinzhen_excel_order_import import (
        XinzhenExcelParseError,
        parse_xinzhen_excel_order,
    )

    with pytest.raises(XinzhenExcelParseError, match="结构无效"):
        parse_xinzhen_excel_order(b"not-an-xlsx", "broken.xlsx")


def test_parse_rejects_xlsx_with_excessive_expanded_content(monkeypatch) -> None:
    import app.services.xinzhen_excel_order_import as service

    payload = BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", "x" * 128)
    monkeypatch.setattr(service, "MAX_XLSX_UNCOMPRESSED_BYTES", 64)

    with pytest.raises(service.XinzhenExcelParseError, match="解压后内容过大"):
        service.parse_xinzhen_excel_order(payload.getvalue(), "expanded.xlsx")


@pytest.fixture
def xinzhen_app(tmp_path: Path):
    from app.api.deps import get_db
    from app.api.orders import can_create, router as orders_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "n042-xinzhen.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        admin = User(
            username="n042-admin",
            password_hash=hash_password("RolePass123!"),
            role="admin",
            real_name="N042 Admin",
            display_name="N042 Admin",
            must_change_password=False,
        )
        sales = User(
            username="n042-sales",
            password_hash=hash_password("RolePass123!"),
            role="sales",
            customer_access_mode="selected",
            real_name="N042 Sales",
            display_name="N042 Sales",
            must_change_password=False,
        )
        xinzhen = Customer(
            customer_number=42,
            customer_code="XINZHEN",
            name="苏州工业园区新振针纺织品有限公司",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        other = Customer(
            customer_number=43,
            customer_code="OTHER",
            name="范围外客户",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        session.add_all([admin, sales, xinzhen, other])
        session.flush()
        session.add(UserCustomerScope(user_id=sales.id, customer_id=other.id))
        session.add(
            Product(
                customer_id=xinzhen.id,
                product_code="XZ-380-260-170",
                customer_material_code="XZ-380-260-170",
                product_name="新振标准箱",
                length_mm=Decimal("380"),
                width_mm=Decimal("260"),
                height_mm=Decimal("170"),
                sale_unit_price=Decimal("12.3456"),
                box_category="normal",
            )
        )
        session.commit()
        # Permission helpers inspect this relationship.  Load it before the
        # fixture returns the detached test user.
        _ = list(sales.permission_overrides)

    app = FastAPI()
    app.include_router(orders_router, prefix="/api/orders")

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[can_create] = lambda: admin
    return app, session_factory, sales


def _preview(
    client: TestClient,
    content: bytes,
    *,
    filename: str = "N042-carton-marking.xlsx",
    content_type: str = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
) -> dict:
    response = client.post(
        "/api/orders/xinzhen-excel-preview",
        files={"file": (filename, content, content_type)},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _order_payload_from_preview(
    draft: dict,
    *,
    quantity: int | None = None,
    unit_price: str = "12.3456",
    confirm_quantity_change: bool = False,
) -> dict:
    item = draft["items"][0]
    source_quantity = int(item["quantity"])
    return {
        "customer_id": draft["matched_customer_id"],
        "customer_po": draft.get("customer_po") or None,
        "order_date": "2026-07-20",
        "delivery_date": "2026-07-27",
        "items": [
            {
                "product_id": item["matched_product_id"],
                "quantity": quantity if quantity is not None else source_quantity,
                "unit_price": unit_price,
                "xinzhen_source_row": item["source_row"],
                "xinzhen_source_quantity": source_quantity,
                "xinzhen_quantity_change_confirmed": confirm_quantity_change,
            }
        ],
    }


def _confirm_order(
    client: TestClient,
    draft: dict,
    order: dict,
    *,
    idempotency_key: str = "n042-test-request-0001",
) -> dict:
    response = client.post(
        "/api/orders/xinzhen-excel-confirm",
        json={
            "preview_token": draft["preview_confirmation_token"],
            "idempotency_key": idempotency_key,
            "confirmed": True,
            "order": order,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _confirmed_order_payload(order: dict, confirmation: dict) -> dict:
    return {
        **order,
        "xinzhen_excel_confirmation": {
            "confirmation_token": confirmation["confirmation_token"],
            "idempotency_key": confirmation["idempotency_key"],
            "confirmed": True,
        },
    }


def test_preview_is_read_only_and_matches_unique_dimensions(xinzhen_app) -> None:
    from app.models.excel_order_import import ExcelOrderImportBatch, ExcelOrderImportRow
    from app.models.audit import OperationLog
    from app.models.order import Order, OrderItem
    from app.models.product import Product

    app, session_factory, _sales = xinzhen_app
    with TestClient(app) as client:
        draft = _preview(client, _carton_marking_xlsx())

    item = draft["items"][0]
    assert draft["matched_customer_id"]
    assert draft["requires_manual_confirmation"] is True
    assert item["match_status"] == "matched"
    assert item["match_basis"] == "exact_carton_dimensions_cm_to_mm"
    assert item["product_default_price"] == "12.3456"
    assert item["unit_price"] is None
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 0
        assert session.scalar(select(func.count()).select_from(OrderItem)) == 0
        assert session.scalar(select(func.count()).select_from(Product)) == 1
        assert session.scalar(select(func.count()).select_from(OperationLog)) == 0
        assert session.scalar(select(func.count()).select_from(ExcelOrderImportBatch)) == 1
        assert session.scalar(select(func.count()).select_from(ExcelOrderImportRow)) == 1


def test_preview_reuses_one_immutable_source_batch_for_repeated_file(xinzhen_app) -> None:
    from app.models.excel_order_import import ExcelOrderImportBatch, ExcelOrderImportRow

    app, session_factory, _sales = xinzhen_app
    content = _carton_marking_xlsx(customer_po="")
    with TestClient(app) as client:
        first = _preview(client, content, filename="no-po.xlsx")
        second = _preview(client, content, filename="renamed-no-po.xlsx")

    assert first["import_batch_id"] == second["import_batch_id"]
    assert first["file_hash"] == second["file_hash"]
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ExcelOrderImportBatch)) == 1
        assert session.scalar(select(func.count()).select_from(ExcelOrderImportRow)) == 1


def test_preview_multiple_order_sheets_requires_and_honors_explicit_selection(
    xinzhen_app,
) -> None:
    openpyxl = pytest.importorskip("openpyxl")

    app, _session_factory, _sales = xinzhen_app
    workbook = openpyxl.load_workbook(BytesIO(_carton_marking_xlsx()))
    workbook.active.title = "Order A"
    copied = workbook.copy_worksheet(workbook.active)
    copied.title = "Order B"
    copied["B11"] = "PO-N042-002"
    output = BytesIO()
    workbook.save(output)
    workbook.close()

    with TestClient(app) as client:
        rejected = client.post(
            "/api/orders/xinzhen-excel-preview",
            files={"file": ("multi.xlsx", output.getvalue())},
        )
        selected = client.post(
            "/api/orders/xinzhen-excel-preview",
            files={"file": ("multi.xlsx", output.getvalue())},
            data={"worksheet_name": "Order B"},
        )

    assert rejected.status_code == 409
    assert rejected.json()["detail"]["worksheet_options"] == ["Order A", "Order B"]
    assert selected.status_code == 200, selected.text
    assert selected.json()["sheet_name"] == "Order B"
    assert selected.json()["customer_po"] == "PO-N042-002"


def test_preview_reads_sanitized_xls_fixture_without_writing_orders(xinzhen_app) -> None:
    from app.models.order import Order, OrderItem

    app, session_factory, _sales = xinzhen_app
    fixture = FIXTURES / "n042_xinzhen_carton_marking_sanitized.xls.fixture"
    with TestClient(app) as client:
        draft = _preview(
            client,
            fixture.read_bytes(),
            filename="n042_xinzhen_carton_marking_sanitized.xls",
            content_type="application/vnd.ms-excel",
        )

    assert draft["customer_po"] == "XZ-SAMPLE-001"
    assert draft["items"][0]["quantity"] == 25
    assert draft["items"][0]["match_status"] == "matched"
    assert draft["requires_manual_confirmation"] is True
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 0
        assert session.scalar(select(func.count()).select_from(OrderItem)) == 0


def test_preview_prefers_unique_stable_customer_code_over_name_candidates(xinzhen_app) -> None:
    from app.models.customer import Customer
    from app.models.order import Order

    app, session_factory, _sales = xinzhen_app
    with session_factory() as session:
        session.add(
            Customer(
                customer_number=44,
                customer_code="XINZHEN-DUPLICATE",
                name="新振同名候选客户",
                payment_term_days=30,
                credit_limit=Decimal("100000"),
            )
        )
        session.commit()

    with TestClient(app) as client:
        response = client.post(
            "/api/orders/xinzhen-excel-preview",
            files={
                "file": (
                    "N042-carton-marking.xlsx",
                    _carton_marking_xlsx(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )

    assert response.status_code == 200
    assert response.json()["matched_customer_name"] == "苏州工业园区新振针纺织品有限公司"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 0


def test_preview_name_fallback_fails_closed_when_stable_code_is_missing_and_names_ambiguous(
    xinzhen_app,
) -> None:
    from app.models.customer import Customer

    app, session_factory, _sales = xinzhen_app
    with session_factory() as session:
        primary = session.scalar(
            select(Customer).where(Customer.customer_code == "XINZHEN")
        )
        assert primary is not None
        primary.customer_code = "LEGACY-XZ"
        session.add(
            Customer(
                customer_number=44,
                customer_code="OTHER-XZ",
                name="新振同名候选客户",
                payment_term_days=30,
                credit_limit=Decimal("100000"),
            )
        )
        session.commit()

    with TestClient(app) as client:
        response = client.post(
            "/api/orders/xinzhen-excel-preview",
            files={
                "file": (
                    "N042-carton-marking.xlsx",
                    _carton_marking_xlsx(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "存在多个启用的新振客户，无法安全导入。"


def test_preview_fails_closed_for_ambiguous_or_unmatched_products(xinzhen_app) -> None:
    from app.models.product import Product

    app, session_factory, _sales = xinzhen_app
    with session_factory() as session:
        customer_id = session.scalar(select(Product.customer_id).limit(1))
        session.add(
            Product(
                customer_id=customer_id,
                product_code="XZ-SECOND",
                customer_material_code="XZ-SECOND",
                product_name="新振同尺寸第二箱",
                length_mm=Decimal("380"),
                width_mm=Decimal("260"),
                height_mm=Decimal("170"),
                box_category="normal",
            )
        )
        session.commit()
    with TestClient(app) as client:
        ambiguous = _preview(client, _carton_marking_xlsx())
        unmatched = _preview(client, _carton_marking_xlsx(dimensions="39*26*17cm"))

    assert ambiguous["items"][0]["match_status"] == "ambiguous"
    assert ambiguous["items"][0]["matched_product_id"] is None
    assert unmatched["items"][0]["match_status"] == "unmatched"
    assert unmatched["items"][0]["matched_product_id"] is None


def test_preview_fails_closed_when_product_code_conflicts_with_dimensions(xinzhen_app) -> None:
    app, _session_factory, _sales = xinzhen_app
    with TestClient(app) as client:
        draft = _preview(
            client,
            _carton_marking_xlsx(
                dimensions="39*26*17cm",
                product_code="XZ-380-260-170",
            ),
        )

    item = draft["items"][0]
    assert item["match_status"] == "conflict"
    assert item["match_basis"] is None
    assert item["matched_product_id"] is None
    assert any("尺寸不一致" in warning for warning in item["warnings"])


def test_preview_does_not_guess_by_dimensions_when_source_code_is_unknown(xinzhen_app) -> None:
    app, _session_factory, _sales = xinzhen_app
    with TestClient(app) as client:
        draft = _preview(
            client,
            _carton_marking_xlsx(
                dimensions="38*26*17cm",
                product_code="UNKNOWN-SOURCE-CODE",
            ),
        )

    item = draft["items"][0]
    assert item["match_status"] == "conflict"
    assert item["match_basis"] is None
    assert item["matched_product_id"] is None
    assert any("原始产品编码未匹配" in warning for warning in item["warnings"])


def test_preview_can_use_exact_product_code_when_source_has_no_dimensions(xinzhen_app) -> None:
    app, _session_factory, _sales = xinzhen_app
    with TestClient(app) as client:
        draft = _preview(
            client,
            _carton_marking_xlsx(
                dimensions="",
                product_code="XZ-380-260-170",
            ),
        )

    item = draft["items"][0]
    assert item["match_status"] == "matched"
    assert item["match_basis"] == "exact_product_code"
    assert item["matched_product_id"] is not None


def test_preview_enforces_new_zhen_customer_scope(xinzhen_app) -> None:
    from app.api.orders import can_create

    app, _session_factory, sales = xinzhen_app
    app.dependency_overrides[can_create] = lambda: sales
    with TestClient(app) as client:
        response = client.post(
            "/api/orders/xinzhen-excel-preview",
            files={
                "file": (
                    "N042-carton-marking.xlsx",
                    _carton_marking_xlsx(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )

    assert response.status_code == 403


def test_preview_reports_quantity_reconciliation_without_fabricating_values(xinzhen_app) -> None:
    app, _session_factory, _sales = xinzhen_app
    with TestClient(app) as client:
        draft = _preview(client, _carton_marking_xlsx(total_pieces=99))

    item = draft["items"][0]
    assert item["quantity"] == 25
    assert item["quantity_check"]["status"] == "needs_review"
    assert draft["quantity_check"]["status"] == "needs_review"
    assert item["unit_price"] is None
    assert item["order_date"] is None


def test_preview_rejects_upload_larger_than_five_megabytes(xinzhen_app) -> None:
    app, _session_factory, _sales = xinzhen_app
    with TestClient(app) as client:
        response = client.post(
            "/api/orders/xinzhen-excel-preview",
            files={"file": ("too-large.xlsx", b"x" * (5 * 1024 * 1024 + 1))},
        )

    assert response.status_code == 413
    assert response.json()["detail"] == "Excel 文件不能超过 5MB"


def test_preview_requires_product_view_even_when_order_create_is_allowed(
    xinzhen_app,
) -> None:
    from app.api.orders import can_create
    from app.models.access_control import UserPermissionOverride
    from app.models.user import User

    app, session_factory, _sales = xinzhen_app
    with session_factory() as session:
        restricted = User(
            username="n042-no-product-view",
            password_hash="not-used",
            role="sales",
            customer_access_mode="all",
            real_name="N042 Restricted",
            display_name="N042 Restricted",
            must_change_password=False,
        )
        session.add(restricted)
        session.flush()
        session.add(
            UserPermissionOverride(
                user_id=restricted.id,
                permission_code="products.view",
                is_allowed=False,
            )
        )
        session.commit()
        _ = list(restricted.permission_overrides)

    app.dependency_overrides[can_create] = lambda: restricted
    with TestClient(app) as client:
        response = client.post(
            "/api/orders/xinzhen-excel-preview",
            files={"file": ("restricted.xlsx", _carton_marking_xlsx())},
        )

    assert response.status_code == 403


def test_workshop_product_candidates_are_price_redacted(xinzhen_app) -> None:
    from app.api.orders import can_create
    from app.models.user import User

    app, session_factory, _sales = xinzhen_app
    with session_factory() as session:
        workshop = User(
            username="n042-workshop",
            password_hash="not-used",
            role="workshop",
            customer_access_mode="all",
            real_name="N042 Workshop",
            display_name="N042 Workshop",
            must_change_password=False,
        )
        session.add(workshop)
        session.commit()
        _ = list(workshop.permission_overrides)

    app.dependency_overrides[can_create] = lambda: workshop
    with TestClient(app) as client:
        draft = _preview(client, _carton_marking_xlsx())

    item = draft["items"][0]
    assert item["product_default_price"] is None
    assert item["product_candidates"]
    assert all("sale_unit_price" not in row for row in item["product_candidates"])


def test_confirmation_rejects_source_quantity_tamper_and_requires_edit_ack(
    xinzhen_app,
) -> None:
    app, _session_factory, _sales = xinzhen_app
    with TestClient(app) as client:
        draft = _preview(client, _carton_marking_xlsx())
        tampered = _order_payload_from_preview(draft)
        tampered["items"][0]["xinzhen_source_quantity"] = 24
        tampered_response = client.post(
            "/api/orders/xinzhen-excel-confirm",
            json={
                "preview_token": draft["preview_confirmation_token"],
                "idempotency_key": "n042-tampered-source-0001",
                "confirmed": True,
                "order": tampered,
            },
        )

        unconfirmed_edit = _order_payload_from_preview(draft, quantity=26)
        unconfirmed_response = client.post(
            "/api/orders/xinzhen-excel-confirm",
            json={
                "preview_token": draft["preview_confirmation_token"],
                "idempotency_key": "n042-unconfirmed-edit-0001",
                "confirmed": True,
                "order": unconfirmed_edit,
            },
        )

        confirmed_edit = _order_payload_from_preview(
            draft,
            quantity=26,
            confirm_quantity_change=True,
        )
        confirmed = _confirm_order(
            client,
            draft,
            confirmed_edit,
            idempotency_key="n042-confirmed-edit-0001",
        )

    assert tampered_response.status_code == 409
    assert unconfirmed_response.status_code == 409
    assert confirmed["quantity_states"] == [
        {
            "source_row": draft["items"][0]["source_row"],
            "source_quantity": 25,
            "edited_quantity": 26,
            "status": "edited_confirmed",
        }
    ]


def test_confirmation_token_is_operator_and_exact_payload_bound(xinzhen_app) -> None:
    from app.api.orders import can_create
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.user import User

    app, session_factory, sales = xinzhen_app
    with TestClient(app) as client:
        draft = _preview(client, _carton_marking_xlsx())
        order = _order_payload_from_preview(draft)
        confirmation = _confirm_order(client, draft, order)

        changed = _confirmed_order_payload(deepcopy(order), confirmation)
        changed["items"][0]["unit_price"] = "13.0000"
        changed_response = client.post("/api/orders", json=changed)

    with session_factory() as session:
        customer_id = session.scalar(
            select(Customer.id).where(Customer.customer_code == "XINZHEN")
        )
        session.add(UserCustomerScope(user_id=sales.id, customer_id=customer_id))
        session.commit()
        _ = list(sales.permission_overrides)

    app.dependency_overrides[can_create] = lambda: sales
    with TestClient(app) as client:
        operator_response = client.post(
            "/api/orders/xinzhen-excel-confirm",
            json={
                "preview_token": draft["preview_confirmation_token"],
                "idempotency_key": "n042-other-operator-0001",
                "confirmed": True,
                "order": order,
            },
        )

    assert changed_response.status_code == 409
    assert operator_response.status_code == 409


def test_no_po_source_converts_once_and_repeated_save_is_idempotent(
    xinzhen_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.excel_order_import import ExcelOrderImportConversion
    from app.models.order import Order

    app, session_factory, _sales = xinzhen_app
    content = _carton_marking_xlsx(customer_po="")
    with TestClient(app) as client:
        draft = _preview(client, content, filename="no-po.xlsx")
        order = _order_payload_from_preview(draft)
        assert order["customer_po"] is None
        confirmation = _confirm_order(
            client,
            draft,
            order,
            idempotency_key="n042-no-po-idempotent-0001",
        )
        payload = _confirmed_order_payload(order, confirmation)
        first = client.post("/api/orders", json=payload)
        second = client.post("/api/orders", json=payload)

        changed_order = _order_payload_from_preview(
            draft,
            quantity=26,
            confirm_quantity_change=True,
        )
        changed_confirmation = client.post(
            "/api/orders/xinzhen-excel-confirm",
            json={
                "preview_token": draft["preview_confirmation_token"],
                "idempotency_key": "n042-no-po-changed-0002",
                "confirmed": True,
                "order": changed_order,
            },
        )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["id"] == second.json()["id"]
    assert changed_confirmation.status_code == 409
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 1
        assert (
            session.scalar(select(func.count()).select_from(ExcelOrderImportConversion))
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OperationLog)
                .where(OperationLog.action == "XINZHEN_EXCEL_CONVERT")
            )
            == 1
        )


def test_concurrent_double_click_creates_one_order_and_one_conversion(
    xinzhen_app,
) -> None:
    from app.models.excel_order_import import ExcelOrderImportConversion
    from app.models.order import Order

    app, session_factory, _sales = xinzhen_app
    with TestClient(app) as client:
        draft = _preview(client, _carton_marking_xlsx(customer_po=""))
        order = _order_payload_from_preview(draft)
        confirmation = _confirm_order(
            client,
            draft,
            order,
            idempotency_key="n042-concurrent-double-click-0001",
        )
    payload = _confirmed_order_payload(order, confirmation)

    def save_once() -> tuple[int, dict]:
        with TestClient(app) as thread_client:
            response = thread_client.post("/api/orders", json=payload)
            return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: save_once(), range(2)))

    assert [status for status, _body in results] == [201, 201]
    assert len({body["id"] for _status, body in results}) == 1
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 1
        assert (
            session.scalar(select(func.count()).select_from(ExcelOrderImportConversion))
            == 1
        )
