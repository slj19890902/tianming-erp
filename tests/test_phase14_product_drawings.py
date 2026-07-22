from __future__ import annotations

from collections.abc import Generator
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfWriter
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def drawing_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.products import router as products_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.product import Product
    from app.models.user import User

    upload_dir = tmp_path / "drawings"
    monkeypatch.setenv("ERP_DRAWING_DIR", str(upload_dir))
    engine = create_sqlite_engine(tmp_path / "phase14_drawings.sqlite3")
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
                for role in ("admin", "sales", "finance", "workshop")
            ]
        )
        customer = Customer(
            customer_number=1,
            customer_code="TH",
            name="Tianhua",
            payment_term_days=30,
            credit_limit=0,
        )
        material = Material(code="WCX1", layer_count=5, flute_type="AB")
        session.add_all([customer, material])
        session.flush()
        session.add(
            Product(
                customer_id=customer.id,
                product_code="001A",
                customer_material_code="001A",
                product_name="001A outer carton",
                material_id=material.id,
                box_category="normal",
                layer_count=5,
                flute_type="AB",
            )
        )
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(products_router, prefix="/api/master/products")

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.state.upload_dir = upload_dir
    app.state.session_factory = session_factory
    return app


def _login(client: TestClient, role: str = "admin") -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": role, "password": "RolePass123!"},
    )
    assert response.status_code == 200


def _image_bytes(color: str) -> bytes:
    image = Image.new("RGB", (1200, 800), color)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _static_pdf_bytes() -> bytes:
    buffer = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(buffer)
    return buffer.getvalue()


def _upload(client: TestClient, filename: str, color: str):
    return client.post(
        "/api/master/products/1/drawings",
        files={"file": (filename, _image_bytes(color), "image/png")},
    )


def test_each_upload_creates_a_new_drawing_version_latest_first(
    drawing_app: FastAPI,
) -> None:
    with TestClient(drawing_app) as client:
        _login(client, "admin")
        first = _upload(client, "v1.png", "white")
        second = _upload(client, "v2.png", "blue")
        detail = client.get("/api/master/products/1")

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert detail.status_code == 200
    drawings = detail.json()["drawings"]
    assert len(drawings) == 2
    assert drawings[0]["id"] == second.json()["id"]
    assert drawings[1]["id"] == first.json()["id"]
    assert drawings[0]["image_path"].startswith("/static/uploads/drawings/")
    assert drawings[0]["thumbnail_path"].startswith("/static/uploads/drawings/")
    assert "base64" not in str(drawings).lower()


def test_delete_drawing_removes_only_selected_version_and_files(
    drawing_app: FastAPI,
) -> None:
    with TestClient(drawing_app) as client:
        _login(client, "admin")
        first_response = _upload(client, "v1.png", "white")
        second_response = _upload(client, "v2.png", "blue")
        assert first_response.status_code == 201, first_response.text
        assert second_response.status_code == 201, second_response.text
        first = first_response.json()
        second = second_response.json()
        response = client.delete(
            f"/api/master/products/drawings/{first['id']}"
        )
        detail = client.get("/api/master/products/1")

    assert response.status_code == 204, response.text
    assert [item["id"] for item in detail.json()["drawings"]] == [second["id"]]
    upload_dir = drawing_app.state.upload_dir
    assert not (upload_dir / Path(first["image_path"]).name).exists()
    assert not (upload_dir / Path(first["thumbnail_path"]).name).exists()
    assert (upload_dir / Path(second["image_path"]).name).exists()
    assert (upload_dir / Path(second["thumbnail_path"]).name).exists()


def test_delete_product_version_preserves_file_still_used_by_order_item(
    drawing_app: FastAPI,
) -> None:
    from app.models.order import Order, OrderItem
    from app.models.product_drawing import ProductDrawing

    with TestClient(drawing_app) as client:
        _login(client, "admin")
        uploaded = _upload(client, "shared.png", "white")
        assert uploaded.status_code == 201, uploaded.text
        drawing = uploaded.json()
        with drawing_app.state.session_factory() as db:
            order = Order(
                order_number="PHASE14-SHARED-DRAWING",
                customer_id=1,
                order_date=date(2026, 7, 22),
                total_amount=Decimal("9.00"),
            )
            db.add(order)
            db.flush()
            db.add(
                OrderItem(
                    order_id=order.id,
                    product_id=1,
                    quantity=1,
                    unit_price=Decimal("9.00"),
                    subtotal=Decimal("9.00"),
                    snapshot_product_name="001A outer carton",
                    drawing_file=drawing["image_path"].lstrip("/"),
                )
            )
            db.commit()

        deleted = client.delete(
            f"/api/master/products/drawings/{drawing['id']}"
        )

    assert deleted.status_code == 204, deleted.text
    upload_dir = drawing_app.state.upload_dir
    assert (upload_dir / Path(drawing["image_path"]).name).is_file()
    assert not (upload_dir / Path(drawing["thumbnail_path"]).name).exists()
    with drawing_app.state.session_factory() as db:
        assert db.get(ProductDrawing, drawing["id"]) is None
        item = db.scalar(
            select(OrderItem).where(
                OrderItem.drawing_file == drawing["image_path"].lstrip("/")
            )
        )
        assert item is not None


def test_drawing_mutation_permissions_are_restricted(
    drawing_app: FastAPI,
) -> None:
    with TestClient(drawing_app) as client:
        _login(client, "workshop")
        workshop_upload = _upload(client, "blocked.png", "white")
        _login(client, "finance")
        finance_upload = _upload(client, "blocked.png", "white")

    assert workshop_upload.status_code == 403
    assert finance_upload.status_code == 403


def test_delete_current_drawing_promotes_previous_version(
    drawing_app: FastAPI,
) -> None:
    with TestClient(drawing_app) as client:
        _login(client, "admin")
        first = _upload(client, "v1.png", "white").json()
        second = _upload(client, "v2.png", "blue").json()
        deleted = client.delete(
            f"/api/master/products/drawings/{second['id']}"
        )
        detail = client.get("/api/master/products/1")

    assert deleted.status_code == 204
    assert [row["id"] for row in detail.json()["drawings"]] == [first["id"]]


def test_pdf_drawing_upload_is_saved_as_viewable_version(
    drawing_app: FastAPI,
) -> None:
    pdf_content = _static_pdf_bytes()
    with TestClient(drawing_app) as client:
        _login(client, "admin")
        uploaded = client.post(
            "/api/master/products/1/drawings",
            files={"file": ("customer-drawing.pdf", pdf_content, "application/pdf")},
        )
        detail = client.get("/api/master/products/1")

    assert uploaded.status_code == 201, uploaded.text
    drawing = uploaded.json()
    assert drawing["image_path"].endswith(".pdf")
    assert drawing["thumbnail_path"] == drawing["image_path"]
    assert detail.json()["drawings"][0]["image_path"].endswith(".pdf")
    assert (
        drawing_app.state.upload_dir / Path(drawing["image_path"]).name
    ).read_bytes() == pdf_content


def test_invalid_pdf_drawing_is_rejected(drawing_app: FastAPI) -> None:
    with TestClient(drawing_app) as client:
        _login(client, "admin")
        response = client.post(
            "/api/master/products/1/drawings",
            files={"file": ("broken.pdf", b"not-a-pdf", "application/pdf")},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "PDF 图纸文件无法识别"


def test_oversized_product_drawing_is_bounded_and_has_no_side_effects(
    drawing_app: FastAPI,
) -> None:
    from app.models.product_drawing import ProductDrawing
    from app.services.product_drawings import MAX_DRAWING_BYTES

    with TestClient(drawing_app) as client:
        _login(client, "admin")
        response = client.post(
            "/api/master/products/1/drawings",
            files={
                "file": (
                    "oversized.png",
                    b"0" * (MAX_DRAWING_BYTES + 1),
                    "image/png",
                )
            },
        )

    assert response.status_code == 400
    with drawing_app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(ProductDrawing)) == 0
    assert list(drawing_app.state.upload_dir.glob("*")) == []


@pytest.mark.parametrize("fault_kind", ["audit", "commit"])
def test_product_drawing_precommit_failure_removes_files_and_rolls_back(
    drawing_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    fault_kind: str,
) -> None:
    import app.api.products as products_api
    from app.models.product_drawing import ProductDrawing

    def fail_audit(*_args, **_kwargs) -> None:
        raise RuntimeError("injected product drawing audit failure")

    def fail_commit(_session) -> None:
        raise RuntimeError("injected product drawing commit failure")

    factory = drawing_app.state.session_factory
    listener_installed = False
    with TestClient(drawing_app) as client:
        _login(client, "admin")
        if fault_kind == "audit":
            monkeypatch.setattr(products_api, "audit_master_change", fail_audit)
            expected_message = "injected product drawing audit failure"
        else:
            event.listen(factory.class_, "before_commit", fail_commit)
            listener_installed = True
            expected_message = "injected product drawing commit failure"
        try:
            with pytest.raises(RuntimeError, match=expected_message):
                client.post(
                    "/api/master/products/1/drawings",
                    files={"file": ("fault.png", _image_bytes("white"), "image/png")},
                )
        finally:
            if listener_installed:
                event.remove(factory.class_, "before_commit", fail_commit)

    with factory() as db:
        assert db.scalar(select(func.count()).select_from(ProductDrawing)) == 0
    assert list(drawing_app.state.upload_dir.glob("*")) == []


def test_common_box_process_and_print_type_round_trip_without_drawing(
    drawing_app: FastAPI,
) -> None:
    payload = {
        "customer_id": 1,
        "product_code": "001A",
        "customer_material_code": "001A",
        "product_name": "001A outer carton",
        "material_id": 1,
        "layer_count": 5,
        "flute_type": "AB",
        "length_mm": 680,
        "width_mm": 240,
        "height_mm": 165,
        "box_category": "normal",
        "print_content": "双色印刷",
        "production_process": "粘贴,打钉",
        "sale_unit_price": "4.1600",
        "remark": "常用箱编辑回显测试",
        "expected_version": 1,
        "change_reason": "验证常用箱工艺和印刷字段回显",
    }
    with TestClient(drawing_app) as client:
        _login(client, "admin")
        updated = client.put("/api/master/products/1", json=payload)
        detail = client.get("/api/master/products/1")

    assert updated.status_code == 200, updated.text
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["production_process"] == "粘贴,打钉"
    assert body["print_content"] == "双色印刷"
    assert body["drawings"] == []
