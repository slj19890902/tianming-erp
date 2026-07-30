from __future__ import annotations

from base64 import b64decode
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from openpyxl.drawing.image import Image as WorksheetImage
from PIL import Image as PILImage
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker


EXCEL_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PNG_MIME = "image/png"
PNG_BYTES = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP8zwACTGCS"
    "AQANHQEDgslx/wAAAABJRU5ErkJggg=="
)


@pytest.fixture()
def product_workbook_app(tmp_path, monkeypatch):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.product_import import router as product_import_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.mold_tool import MoldTool
    from app.models.product import Product
    from app.models.supplier import Supplier
    from app.models.user import User
    from app.services.supplier_master import normalize_supplier_identity

    monkeypatch.setenv("ERP_FILE_STORAGE_DIR", str(tmp_path / "private"))
    monkeypatch.setenv("ERP_UPLOAD_TEMP_DIR", str(tmp_path / "temporary"))
    engine = create_sqlite_engine(tmp_path / "product-workbook.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="product-workbook-admin",
            password_hash=hash_password("WorkbookPass123!"),
            role="admin",
            real_name="常用箱管理员",
            must_change_password=False,
        )
        sales = User(
            username="product-workbook-sales",
            password_hash=hash_password("WorkbookPass123!"),
            role="sales",
            real_name="销售",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=9301,
            customer_code="UAT-PWB",
            name="匿名样品客户",
            payment_term_days=30,
            statement_cycle_start_day=20,
            default_tax_rate=Decimal("0.13"),
            status="active",
        )
        other_customer = Customer(
            customer_number=9302,
            customer_code="UAT-PWB-OTHER",
            name="匿名其他客户",
            payment_term_days=30,
            statement_cycle_start_day=20,
            default_tax_rate=Decimal("0.13"),
            status="active",
        )
        supplier = Supplier(
            standard_name="苏州嘉林亿",
            normalized_name=normalize_supplier_identity("苏州嘉林亿"),
            display_name="嘉林亿",
            business_code="JLY",
            normalized_business_code="JLY",
            sort_order=10,
            is_active=True,
            version=1,
        )
        material = Material(
            code="C4C",
            supplier_name="苏州嘉林亿",
            layer_count=3,
            basis_weight_description="80g/100g/80g",
            paper_composition="面纸:C | 瓦楞纸:4 | 里纸:C",
            quote_price=Decimal("1.2000"),
            quote_date=date(2026, 7, 1),
            price_unit="元/㎡",
            is_active=True,
            version=1,
        )
        mold = MoldTool(
            mold_code="M-EXIST",
            mold_name="既有模具",
            rack_location="模具架 A-01",
            is_active=True,
        )
        db.add_all([admin, sales, customer, other_customer, supplier, material, mold])
        db.flush()
        db.add(
            Product(
                customer_id=customer.id,
                product_code="EXIST-001",
                customer_material_code="ERP-CUST-001",
                product_name="既有开槽箱",
                material_id=material.id,
                length_mm=Decimal("300"),
                width_mm=Decimal("200"),
                height_mm=Decimal("100"),
                box_category="normal",
                box_style="A1",
                print_content="无印刷",
                production_process="开槽、打钉",
                unit="只",
                sale_unit_price=Decimal("1.0000"),
                flute_type="B",
                layer_count=3,
                report_length_mm=720,
                report_width_mm=305,
                crease_type="净料",
                splice_mode="single",
                pieces_per_box=1,
                default_cutting_mode="一开一",
                flap_mm=30,
                is_active=True,
                version=1,
            )
        )
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(product_import_router, prefix="/api/master/products")

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    app.state.session_factory = factory
    app.state.private_root = tmp_path / "private"
    yield app
    engine.dispose()


def _login(client: TestClient, username: str = "product-workbook-admin") -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "WorkbookPass123!"},
    )
    assert response.status_code == 200


def _ids(app: FastAPI) -> tuple[int, int]:
    from app.models.customer import Customer

    with app.state.session_factory() as db:
        customer = db.scalar(
            select(Customer).where(Customer.customer_code == "UAT-PWB")
        )
        other = db.scalar(
            select(Customer).where(Customer.customer_code == "UAT-PWB-OTHER")
        )
        return customer.id, other.id


def _download(client: TestClient, customer_id: int) -> bytes:
    response = client.get(
        "/api/master/products/import-template.xlsx",
        params={"customer_id": customer_id},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(EXCEL_MIME)
    return response.content


def _save_workbook(workbook) -> bytes:
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def _filled_workbook(
    template: bytes,
    *,
    image_anchors: tuple[str, ...] = ("J3", "J3"),
    image_bytes: bytes = PNG_BYTES,
    external_names: str = "",
) -> bytes:
    workbook = load_workbook(BytesIO(template))
    sheet = workbook["样品录入"]
    values = {
        "B": "HAND-CANDIDATE-001",
        "C": "120×80×40",
        "D": "B",
        "E": "模切",
        "F": "是",
        "G": "黑色",
        "H": "粘合",
        "I": "是",
        "J": external_names,
        "K": "M-EXIST",
        "L": "仅整理核对",
        "M": "新增",
        "P": "CUSTOMER-001",
        "Q": "匿名模切内盒",
        "R": "C4C",
        "S": 3,
        "T": Decimal("1.2500"),
        "U": 500,
        "V": 400,
        "W": "是",
    }
    for column, value in values.items():
        sheet[f"{column}3"] = value
    for anchor in image_anchors:
        image = WorksheetImage(BytesIO(image_bytes))
        image.width = 80
        image.height = 80
        sheet.add_image(image, anchor)
    return _save_workbook(workbook)


def _preview(
    client: TestClient,
    customer_id: int,
    workbook: bytes,
    *,
    drawings: list[tuple[str, bytes, str]] | None = None,
):
    files = [("file", ("样品常用箱.xlsx", workbook, EXCEL_MIME))]
    files.extend(
        ("drawing_files", (name, content, content_type))
        for name, content, content_type in (drawings or [])
    )
    return client.post(
        "/api/master/products/import/preview",
        data={"customer_id": str(customer_id)},
        files=files,
    )


def _rewrite_zip(content: bytes, mutate) -> bytes:
    source = BytesIO(content)
    output = BytesIO()
    with zipfile.ZipFile(source) as archive, zipfile.ZipFile(
        output,
        "w",
        zipfile.ZIP_DEFLATED,
    ) as rebuilt:
        for info in archive.infolist():
            name = info.filename
            payload = archive.read(info)
            replacement = mutate(name, payload)
            if replacement is not None:
                rebuilt.writestr(name, replacement)
    return output.getvalue()


def _database_counts(app: FastAPI) -> tuple[int, int, int]:
    from app.models.mold_tool import MoldTool
    from app.models.product import Product
    from app.models.product_drawing import ProductDrawing

    with app.state.session_factory() as db:
        return (
            db.scalar(select(func.count(Product.id))),
            db.scalar(select(func.count(MoldTool.id))),
            db.scalar(select(func.count(ProductDrawing.id))),
        )


def test_template_is_customer_locked_preview_only_and_keeps_v2_layout(
    product_workbook_app: FastAPI,
) -> None:
    customer_id, _ = _ids(product_workbook_app)
    with TestClient(product_workbook_app) as client:
        _login(client, "product-workbook-sales")
        content = _download(client, customer_id)

    workbook = load_workbook(BytesIO(content))
    try:
        assert workbook.sheetnames == ["使用说明", "样品录入", "模具档案", "导入信息"]
        sheet = workbook["样品录入"]
        assert len(sheet[1]) == 23
        assert sheet["B2"].value == "EXIST-001"
        assert sheet["M2"].value == "不变"
        assert sheet["A3"].value == "YP001"
        assert sheet.print_area.startswith("'样品录入'!$A$1:$I$")
        assert workbook["导入信息"].sheet_state == "hidden"
        instructions = "\n".join(
            str(row[1].value or "")
            for row in workbook["使用说明"].iter_rows(min_row=2, max_col=2)
        )
        assert "只用于样品核对" in instructions
        assert "禁止正式导入" in instructions
        assert "恰好两张图片" in instructions
    finally:
        workbook.close()


def test_two_embedded_images_preview_without_token_or_database_writes(
    product_workbook_app: FastAPI,
) -> None:
    customer_id, _ = _ids(product_workbook_app)
    before = _database_counts(product_workbook_app)
    with TestClient(product_workbook_app) as client:
        _login(client)
        workbook = _filled_workbook(_download(client, customer_id))
        response = _preview(client, customer_id, workbook)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["valid"] is True
        assert body["import_allowed"] is False
        assert "preview_token" not in body
        assert "映射" in body["write_blocked_reason"]
        candidate = next(
            item for item in body["items"] if item["action"] == "create"
        )
        assert candidate["candidate_model"] == "HAND-CANDIDATE-001"
        assert candidate["drawing_mode"] == "embedded"
        assert len(candidate["drawing_files"]) == 2
        assert body["summary"]["drawing_files"] == 2

        blocked = client.post(
            "/api/master/products/import/apply",
            json={"preview_token": "x" * 32},
        )
        assert blocked.status_code == 409
        assert "禁止正式导入" in blocked.json()["detail"]["message"]

    assert _database_counts(product_workbook_app) == before
    assert not any(Path(product_workbook_app.state.private_root).rglob("*"))


@pytest.mark.parametrize("count", [1, 3])
def test_new_row_requires_exactly_two_embedded_images(
    product_workbook_app: FastAPI,
    count: int,
) -> None:
    customer_id, _ = _ids(product_workbook_app)
    anchors = tuple("J3" for _ in range(count))
    with TestClient(product_workbook_app) as client:
        _login(client)
        response = _preview(
            client,
            customer_id,
            _filled_workbook(_download(client, customer_id), image_anchors=anchors),
        )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert any("恰好对应两张" in error["message"] for error in body["errors"])
    assert "preview_token" not in body


@pytest.mark.parametrize("anchor", ["I3", "K3", "J1"])
def test_embedded_image_wrong_column_or_header_row_is_rejected(
    product_workbook_app: FastAPI,
    anchor: str,
) -> None:
    customer_id, _ = _ids(product_workbook_app)
    with TestClient(product_workbook_app) as client:
        _login(client)
        response = _preview(
            client,
            customer_id,
            _filled_workbook(
                _download(client, customer_id),
                image_anchors=(anchor, "J3"),
            ),
        )
    assert response.status_code == 400
    assert "J 列图片区域" in response.json()["detail"]["message"]


def test_images_on_other_sheet_and_mixed_upload_mode_are_rejected(
    product_workbook_app: FastAPI,
) -> None:
    customer_id, _ = _ids(product_workbook_app)
    with TestClient(product_workbook_app) as client:
        _login(client)
        template = _download(client, customer_id)
        workbook = load_workbook(BytesIO(template))
        workbook["模具档案"].add_image(WorksheetImage(BytesIO(PNG_BYTES)), "A2")
        misplaced = _save_workbook(workbook)
        response = _preview(client, customer_id, misplaced)
        assert response.status_code == 400
        assert "只能放在" in response.json()["detail"]["message"]

        embedded = _filled_workbook(template)
        mixed = _preview(
            client,
            customer_id,
            embedded,
            drawings=[("legacy.png", PNG_BYTES, PNG_MIME)],
        )
        assert mixed.status_code == 200
        assert mixed.json()["valid"] is False
        assert any(
            "不能同时上传外部图纸" in error["message"]
            for error in mixed.json()["errors"]
        )


def test_legacy_external_two_file_preview_remains_compatible(
    product_workbook_app: FastAPI,
) -> None:
    customer_id, _ = _ids(product_workbook_app)
    before = _database_counts(product_workbook_app)
    with TestClient(product_workbook_app) as client:
        _login(client)
        workbook = _filled_workbook(
            _download(client, customer_id),
            image_anchors=(),
            external_names="a.png;b.png",
        )
        response = _preview(
            client,
            customer_id,
            workbook,
            drawings=[
                ("a.png", PNG_BYTES, PNG_MIME),
                ("b.png", PNG_BYTES, PNG_MIME),
            ],
        )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    candidate = next(item for item in body["items"] if item["action"] == "create")
    assert candidate["drawing_mode"] == "external"
    assert body["import_allowed"] is False
    assert _database_counts(product_workbook_app) == before


def test_formula_external_relationship_chart_or_orphan_media_are_rejected(
    product_workbook_app: FastAPI,
) -> None:
    customer_id, _ = _ids(product_workbook_app)
    with TestClient(product_workbook_app) as client:
        _login(client)
        template = _download(client, customer_id)
        good = _filled_workbook(template)

        workbook = load_workbook(BytesIO(template))
        workbook["样品录入"]["B3"] = "=1+1"
        formula = _preview(client, customer_id, _save_workbook(workbook))
        assert formula.status_code == 400
        assert "公式" in formula.json()["detail"]["message"]

        external = _rewrite_zip(
            good,
            lambda name, payload: (
                payload.replace(
                    b'Target="/xl/media/',
                    b'TargetMode="External" Target="/xl/media/',
                    1,
                )
                if name.startswith("xl/drawings/_rels/")
                else payload
            ),
        )
        external_response = _preview(client, customer_id, external)
        assert external_response.status_code == 400
        assert "外部链接" in external_response.json()["detail"]["message"]

        chart_output = BytesIO()
        with zipfile.ZipFile(BytesIO(good)) as archive, zipfile.ZipFile(
            chart_output,
            "w",
            zipfile.ZIP_DEFLATED,
        ) as rebuilt:
            for info in archive.infolist():
                rebuilt.writestr(info.filename, archive.read(info))
            rebuilt.writestr("xl/charts/chart1.xml", b"<chart/>")
        chart = _preview(client, customer_id, chart_output.getvalue())
        assert chart.status_code == 400

        orphan_output = BytesIO()
        with zipfile.ZipFile(BytesIO(good)) as archive, zipfile.ZipFile(
            orphan_output,
            "w",
            zipfile.ZIP_DEFLATED,
        ) as rebuilt:
            for info in archive.infolist():
                rebuilt.writestr(info.filename, archive.read(info))
            rebuilt.writestr("xl/media/orphan.gif", b"GIF89a")
        orphan = _preview(client, customer_id, orphan_output.getvalue())
        assert orphan.status_code == 400
        assert "JPG" in orphan.json()["detail"]["message"]

        shape = _rewrite_zip(
            good,
            lambda name, payload: (
                payload.replace(b"<pic>", b"<sp>", 1).replace(
                    b"</pic>",
                    b"</sp>",
                    1,
                )
                if name.startswith("xl/drawings/drawing")
                and name.endswith(".xml")
                else payload
            ),
        )
        shape_response = _preview(client, customer_id, shape)
        assert shape_response.status_code == 400
        assert "形状" in shape_response.json()["detail"]["message"]


def test_damaged_or_excessive_pixel_embedded_image_is_rejected(
    product_workbook_app: FastAPI,
) -> None:
    customer_id, _ = _ids(product_workbook_app)
    with TestClient(product_workbook_app) as client:
        _login(client)
        template = _download(client, customer_id)
        good = _filled_workbook(template)
        damaged = _rewrite_zip(
            good,
            lambda name, payload: (
                b"\x89PNG\r\n\x1a\nbroken"
                if name.startswith("xl/media/image")
                else payload
            ),
        )
        damaged_response = _preview(client, customer_id, damaged)
        assert damaged_response.status_code == 400

        oversized_buffer = BytesIO()
        PILImage.new("1", (20_001, 1), 1).save(oversized_buffer, "PNG")
        oversized = _filled_workbook(
            template,
            image_bytes=oversized_buffer.getvalue(),
        )
        oversized_response = _preview(client, customer_id, oversized)
        assert oversized_response.status_code == 400
        assert "像素尺寸过大" in oversized_response.json()["detail"]["message"]


def test_preview_is_admin_only_and_customer_locked(
    product_workbook_app: FastAPI,
) -> None:
    customer_id, other_customer_id = _ids(product_workbook_app)
    with TestClient(product_workbook_app) as client:
        _login(client, "product-workbook-sales")
        template = _download(client, customer_id)
        forbidden = _preview(client, customer_id, template)
        assert forbidden.status_code == 403

        _login(client)
        mismatch = _preview(client, other_customer_id, template)
        assert mismatch.status_code == 409
        assert "跨客户" in mismatch.json()["detail"]["message"]


def test_generic_inventory_preflight_still_rejects_workbooks_with_images(
    product_workbook_app: FastAPI,
) -> None:
    from app.services.inventory_onboarding_uploads import _preflight_xlsx_container
    from app.services.secure_uploads import UploadValidationError

    customer_id, _ = _ids(product_workbook_app)
    with TestClient(product_workbook_app) as client:
        _login(client)
        content = _filled_workbook(_download(client, customer_id))
    with pytest.raises(UploadValidationError, match="图片"):
        _preflight_xlsx_container(content)
    _preflight_xlsx_container(content, allow_worksheet_images=True)
