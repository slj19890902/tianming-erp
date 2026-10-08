from __future__ import annotations

import logging
import re
from collections.abc import Generator
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.middleware.performance import PerformanceObservabilityMiddleware


def test_performance_middleware_generates_server_request_id_and_ignores_client_id() -> None:
    application = FastAPI()
    application.add_middleware(
        PerformanceObservabilityMiddleware,
        slow_request_ms=60_000,
    )

    @application.get("/api/request-id")
    def request_id_probe(request: Request) -> dict[str, str]:
        return {"request_id": request.state.request_id}

    forged = "client-controlled-request-id"
    with TestClient(application) as client:
        response = client.get(
            "/api/request-id",
            headers={"X-Request-ID": forged},
        )

    assert response.status_code == 200
    response_id = response.headers["X-Request-ID"]
    assert response_id == response.json()["request_id"]
    assert response_id != forged
    assert re.fullmatch(r"[0-9a-f]{32}", response_id)


@pytest.fixture()
def observable_order_app(tmp_path: Path, seed_supplier_master):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.orders import router as orders_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.product import Product
    from app.models.material import Material
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "order-observability.sqlite3")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    seed_supplier_master(factory, '虚构异常路径供应商', 'OBS-TEST')
    with factory() as db:
        user = User(
            username="observability-admin",
            password_hash=hash_password("TestOnlyPass123!"),
            role="admin",
            real_name="Observability Admin",
            display_name="Observability Admin",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=9701,
            customer_code="OBS",
            name="Observability Customer",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        db.add_all([user, customer])
        db.flush()
        # Reach the injected reservation failure with a valid test-only product;
        # incomplete master data must continue to fail the original readiness gate.
        material = Material(code="OBS/AB", supplier_name="虚构异常路径供应商",
                            layer_count=3, flute_type="B", is_active=True)
        db.add(material); db.flush()
        product = Product(
            material_id=material.id, default_material_code=material.code,
            layer_count=3, flute_type="B", box_style="普通箱",
            report_length_mm=800, report_width_mm=600,
            splice_mode="single", pieces_per_box=1,
            customer_id=customer.id,
            product_code="OBS-PRODUCT-001",
            customer_material_code="OBS-PRODUCT-001",
            product_name="Observability Product",
            box_category="normal",
            sale_unit_price=Decimal("1.00"),
        )
        db.add(product)
        db.commit()
        ids = {
            "user": user.id,
            "customer": customer.id,
            "product": product.id,
        }

    application = FastAPI()
    application.add_middleware(
        PerformanceObservabilityMiddleware,
        slow_request_ms=60_000,
    )
    application.include_router(auth_router, prefix="/api/auth")
    application.include_router(orders_router, prefix="/api/orders")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    application.dependency_overrides[get_db] = override_get_db
    try:
        yield application, factory, ids
    finally:
        engine.dispose()


def test_order_precommit_failure_returns_request_id_and_logs_only_safe_context(
    observable_order_app,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import app.api.orders as orders_api
    from app.models.order import Order
    from app.models.user import User

    application, factory, ids = observable_order_app
    with factory() as db:
        actor = db.get(User, ids["user"])
        assert actor is not None
        preview_token = orders_api._encode_pdf_preview_safety_token(
            {
                "source_name": "../PO-OBSERVABILITY.pdf",
                "file_hash": "a" * 64,
                "recognition_status": "recognized",
                "customer_route": {"status": "locked"},
                "customer_match_status": "matched",
                "integrity_check": {"integrity_status": "passed"},
                "matched_customer_id": ids["customer"],
                "items": [{"product_code": "FULL_ITEMS_SENTINEL",
                           "product_name": "FULL_ITEMS_SENTINEL",
                           "quantity": 1, "unit_price": "1.00"}],
            },
            actor,
        )

    failure_message = "PRECOMMIT_INTERNAL_SECRET_SENTINEL"

    def fail_before_commit(_db: Session, _order_id: int) -> None:
        raise RuntimeError(failure_message)

    monkeypatch.setattr(
        orders_api,
        "refresh_order_production_status",
        fail_before_commit,
    )

    forbidden_values = {
        "PASSWORD_SENTINEL",
        "COOKIE_SENTINEL",
        "AUTHORIZATION_SENTINEL",
        "REMARK_SENTINEL",
        "FULL_ITEMS_SENTINEL",
        failure_message,
        preview_token,
    }
    payload = {
        "customer_id": ids["customer"],
        "customer_po": "PO-OBS-ERROR",
        "idempotency_key": "fictional-observability-failure",
        "order_date": "2026-07-27",
        "import_draft": True,
        "import_integrity_status": "passed",
        "pdf_import_confirmation": {
            "preview_safety_token": preview_token,
            "confirmed": True,
        },
        "remark": (
            "REMARK_SENTINEL password=PASSWORD_SENTINEL "
            "Cookie=COOKIE_SENTINEL Authorization=AUTHORIZATION_SENTINEL"
        ),
        "items": [
            {
                "product_id": ids["product"],
                "product_code": "FULL_ITEMS_SENTINEL",
                "product_name": "FULL_ITEMS_SENTINEL",
                "quantity": 1,
                "unit_price": "1.00",
            }
        ],
    }

    with caplog.at_level(logging.WARNING, logger="erp.order_save"):
        with TestClient(application) as client:
            login = client.post(
                "/api/auth/login",
                json={
                    "username": "observability-admin",
                    "password": "TestOnlyPass123!",
                },
            )
            assert login.status_code == 200
            client.cookies.set("sensitive_probe", "COOKIE_SENTINEL")
            response = client.post(
                "/api/orders",
                json=payload,
                headers={
                    "X-Request-ID": "client-forged-order-id",
                    "Authorization": "Bearer AUTHORIZATION_SENTINEL",
                    "X-Password-Probe": "PASSWORD_SENTINEL",
                },
            )

    error_detail = response.json().get("detail")
    safe_error_code = error_detail.get("code") if isinstance(error_detail, dict) else str(error_detail)[:160]
    assert response.status_code == 500, (response.status_code, safe_error_code)
    detail = response.json()["detail"]
    response_id = response.headers["X-Request-ID"]
    assert detail["code"] == "ORDER_SAVE_INTERNAL_ERROR"
    assert detail["request_id"] == response_id
    assert response_id != "client-forged-order-id"
    assert re.fullmatch(r"[0-9a-f]{32}", response_id)

    failure_records = [
        record
        for record in caplog.records
        if record.name == "erp.order_save"
        and record.getMessage().startswith("order_save_failed ")
    ]
    assert len(failure_records) == 1
    record = failure_records[0]
    message = record.getMessage()
    for expected in (
        f"request_id={response_id}",
        f"actor_id={ids['user']}",
        "actor=observability-admin",
        "pdf_filename=PO-OBSERVABILITY.pdf",
        f"customer_id={ids['customer']}",
        "customer_po=PO-OBS-ERROR",
        "item_count=1",
        "failure_stage=inventory_reservation",
        "status=500",
        "error_type=RuntimeError",
    ):
        assert expected in message
    assert record.exc_info is not None

    rendered_log = caplog.text
    for forbidden in forbidden_values:
        assert forbidden not in rendered_log

    with factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(Order)
                .where(Order.customer_po == "PO-OBS-ERROR")
            )
            == 0
        )


def test_expired_signed_pdf_token_records_only_trusted_basename(
    observable_order_app,
) -> None:
    import jwt
    from fastapi import HTTPException

    import app.api.orders as orders_api
    from app.core.config import load_settings
    from app.models.user import User

    _, factory, ids = observable_order_app
    with factory() as db:
        actor = db.get(User, ids["user"])
        assert actor is not None
        valid_token = orders_api._encode_pdf_preview_safety_token(
            {
                "source_name": "../PO-EXPIRED-TRUSTED.pdf",
                "file_hash": "b" * 64,
                "recognition_status": "recognized",
                "customer_route": {"status": "locked"},
                "customer_match_status": "matched",
                "integrity_check": {"integrity_status": "passed"},
                "matched_customer_id": ids["customer"],
            },
            actor,
        )
        claims = jwt.decode(
            valid_token,
            load_settings().secret_key,
            algorithms=["HS256"],
            options={"verify_exp": False},
        )
        claims["exp"] = 0
        expired_token = jwt.encode(
            claims,
            load_settings().secret_key,
            algorithm="HS256",
        )

        observability: dict[str, object] = {}
        with pytest.raises(HTTPException) as caught:
            orders_api._decode_pdf_preview_safety_token(
                expired_token,
                actor,
                observability=observability,
            )

    assert caught.value.status_code == 409
    assert observability["pdf_filename"] == "PO-EXPIRED-TRUSTED.pdf"
