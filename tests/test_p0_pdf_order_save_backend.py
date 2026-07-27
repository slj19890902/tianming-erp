from __future__ import annotations

from collections.abc import Generator
from datetime import date
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def pdf_order_drawing_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.orders import router as orders_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.product import Product
    from app.models.user import User
    from app.models.warehouse_inventory import WarehouseLocation
    from app.services.warehouse_inventory import manual_finished_in

    private_root = tmp_path / "private_uploads"
    token_root = tmp_path / "upload_tokens"
    monkeypatch.setenv("ERP_FILE_STORAGE_DIR", str(private_root))
    monkeypatch.setenv("ERP_UPLOAD_TEMP_DIR", str(token_root))

    engine = create_sqlite_engine(tmp_path / "pdf-order-drawing.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        user = User(
            username="pdf-drawing-admin",
            password_hash=hash_password("TestPass123!"),
            role="admin",
            real_name="PDF Drawing Admin",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=9701,
            customer_code="P0-PDF-DRAWING",
            name="P0 PDF Drawing Customer",
        )
        product = Product(
            customer=customer,
            product_code="P0-PDF-DRAWING-BOX",
            customer_material_code="P0-PDF-DRAWING-BOX",
            product_name="P0 PDF drawing box",
            box_category="normal",
            box_style="普通箱",
            legacy_material_text="A416D",
            default_material_code="A416D",
            flute_type="B",
            layer_count=3,
            report_length_mm=800,
            report_width_mm=600,
            sale_unit_price=Decimal("9.00"),
        )
        location = WarehouseLocation(
            location_code="P0-PDF-FG",
            location_name="P0 PDF finished location",
            warehouse_type="finished",
        )
        db.add_all([user, customer, product, location])
        db.flush()
        lot = manual_finished_in(
            db,
            customer_id=customer.id,
            product_id=product.id,
            location_id=location.id,
            quantity=20,
            stock_date=date(2026, 7, 27),
            source_type="manual",
            remarks=None,
            operator_id=user.id,
            idempotency_key="p0-pdf-order-drawing-lot",
        )
        db.commit()
        ids = {
            "user": user.id,
            "customer": customer.id,
            "product": product.id,
            "lot": lot.id,
            "lot_version": lot.version,
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(orders_router, prefix="/api/orders")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, factory, ids, private_root
    finally:
        engine.dispose()


def _png_bytes() -> bytes:
    image = Image.new("RGB", (24, 24), "white")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "pdf-drawing-admin", "password": "TestPass123!"},
    )
    assert response.status_code == 200, response.text


def _upload_draft(client: TestClient) -> str:
    response = client.post(
        "/api/orders/draft-drawing",
        files={"file": ("drawing.png", _png_bytes(), "image/png")},
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


def _order_payload(
    ids: dict[str, int],
    token: str,
    *,
    customer_po: str,
    save_option: str,
    reserve_finished: bool,
) -> dict:
    item = {
        "client_line_id": "PDF-DRAWING-LINE-1",
        "product_id": ids["product"],
        "quantity": 5,
        "unit_price": "9.00",
        "temp_drawing_token": token,
        "drawing_save_option": save_option,
    }
    if reserve_finished:
        item["reservation_plan"] = {
            "finished": [
                {
                    "lot_id": ids["lot"],
                    "expected_version": ids["lot_version"],
                    "requested_qty": 5,
                    "recommendation_source": "dedicated",
                    "confirmed": True,
                }
            ]
        }
    return {
        "customer_id": ids["customer"],
        "customer_po": customer_po,
        "order_date": "2026-07-27",
        "items": [item],
    }


def _business_counts(db: Session) -> dict[str, int]:
    from app.models.audit import OperationLog
    from app.models.order import Order, OrderItem
    from app.models.product_drawing import ProductDrawing
    from app.models.production import ProductionTask
    from app.models.warehouse_inventory import (
        InventoryMovement,
        InventoryReservation,
    )

    models = (
        Order,
        OrderItem,
        OperationLog,
        ProductionTask,
        InventoryReservation,
        InventoryMovement,
        ProductDrawing,
    )
    return {
        model.__tablename__: int(
            db.scalar(select(func.count()).select_from(model)) or 0
        )
        for model in models
    }


def _stored_drawing_files(private_root: Path) -> list[Path]:
    drawing_root = private_root / "drawings"
    if not drawing_root.exists():
        return []
    return sorted(path for path in drawing_root.rglob("*") if path.is_file())


def test_order_failure_after_drawing_stage_restores_every_side_effect(
    pdf_order_drawing_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.api.orders as orders_api
    from app.models.warehouse_inventory import InventoryLot
    from app.services.secure_uploads import temporary_token_file
    from app.services.warehouse_inventory import WarehouseInventoryError

    app, factory, ids, private_root = pdf_order_drawing_app
    with TestClient(app) as client:
        _login(client)
        token = _upload_draft(client)
        before_token = temporary_token_file(token, owner_id=ids["user"])
        before_token_hash = sha256(before_token.path.read_bytes()).hexdigest()
        with factory() as db:
            before_counts = _business_counts(db)
            lot = db.get(InventoryLot, ids["lot"])
            before_lot = (
                lot.quantity_available,
                lot.quantity_reserved,
                lot.version,
            )

        def fail_after_reservation(*_args, **_kwargs):
            raise WarehouseInventoryError(
                "forced failure after drawing stage",
                status_code=409,
            )

        monkeypatch.setattr(
            orders_api,
            "refresh_order_production_status",
            fail_after_reservation,
        )
        response = client.post(
            "/api/orders",
            json=_order_payload(
                ids,
                token,
                customer_po="P0-PDF-DRAWING-ROLLBACK",
                save_option="save_to_product",
                reserve_finished=True,
            ),
        )

    assert response.status_code == 409, response.text
    with factory() as db:
        assert _business_counts(db) == before_counts
        lot = db.get(InventoryLot, ids["lot"])
        assert (
            lot.quantity_available,
            lot.quantity_reserved,
            lot.version,
        ) == before_lot
    restored_token = temporary_token_file(token, owner_id=ids["user"])
    assert sha256(restored_token.path.read_bytes()).hexdigest() == before_token_hash
    assert restored_token.sha256 == before_token.sha256
    assert _stored_drawing_files(private_root) == []


def test_successful_order_consumes_token_once_and_keeps_permanent_drawing(
    pdf_order_drawing_app,
) -> None:
    from app.models.order import OrderItem
    from app.services.secure_uploads import (
        UploadTokenError,
        resolve_stored_reference,
        stored_file_metadata,
        temporary_token_file,
    )

    app, factory, ids, _private_root = pdf_order_drawing_app
    drawing_bytes = _png_bytes()
    expected_hash = sha256(drawing_bytes).hexdigest()
    with TestClient(app) as client:
        _login(client)
        token = _upload_draft(client)
        response = client.post(
            "/api/orders",
            json=_order_payload(
                ids,
                token,
                customer_po="P0-PDF-DRAWING-SUCCESS",
                save_option="order_only",
                reserve_finished=False,
            ),
        )

    assert response.status_code == 201, response.text
    with pytest.raises(UploadTokenError, match="已使用或已过期"):
        temporary_token_file(token, owner_id=ids["user"])
    with factory() as db:
        item = db.scalar(
            select(OrderItem).where(
                OrderItem.order_id == response.json()["id"]
            )
        )
        assert item is not None
        assert item.drawing_file is not None
        stored_path = resolve_stored_reference(item.drawing_file)
        assert stored_path.is_file()
        assert sha256(stored_path.read_bytes()).hexdigest() == expected_hash
        metadata = stored_file_metadata(stored_path)
        assert metadata["sha256"] == expected_hash
        assert metadata["original_filename"] == "drawing.png"


def test_store_private_upload_metadata_failure_leaves_no_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.secure_uploads as secure_uploads
    from app.services.secure_uploads import ValidatedUpload, store_private_upload

    private_root = tmp_path / "private_uploads"
    monkeypatch.setenv("ERP_FILE_STORAGE_DIR", str(private_root))
    content = _png_bytes()
    upload = ValidatedUpload(
        content=content,
        original_filename="metadata-failure.png",
        extension=".png",
        content_type="image/png",
        size=len(content),
        sha256=sha256(content).hexdigest(),
    )

    def fail_metadata(*_args, **_kwargs):
        raise OSError("forced metadata write failure")

    monkeypatch.setattr(secure_uploads, "_write_metadata", fail_metadata)
    with pytest.raises(OSError, match="forced metadata write failure"):
        store_private_upload(upload, category="drawings")

    assert [
        path for path in private_root.rglob("*") if path.is_file()
    ] == []
