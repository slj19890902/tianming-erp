from __future__ import annotations

from datetime import date
from decimal import Decimal
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def price_app(tmp_path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.external_packaging_prices import router as price_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.supplier import (
        ExternalPackagingProduct,
        Supplier,
        SupplierSupplyCategory,
    )
    from app.models.user import User
    from app.services.supplier_master import normalize_supplier_identity

    engine = create_sqlite_engine(tmp_path / "external-packaging-prices.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    specification = json.dumps(
        {
            "length_mm": 1200.0,
            "shape": "L",
            "side_a_mm": 50.0,
            "side_b_mm": 50.0,
            "thickness_mm": 5.0,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    with factory() as db:
        db.add_all(
            [
                User(
                    username="price-admin",
                    password_hash=hash_password("123456"),
                    role="admin",
                    real_name="价格管理员",
                    must_change_password=False,
                ),
                User(
                    username="price-boss",
                    password_hash=hash_password("123456"),
                    role="boss",
                    real_name="老板",
                    must_change_password=False,
                ),
                User(
                    username="price-sales",
                    password_hash=hash_password("123456"),
                    role="sales",
                    real_name="销售",
                    must_change_password=False,
                ),
            ]
        )
        for index, name in enumerate(("匿名护角甲", "匿名护角乙"), start=1):
            supplier = Supplier(
                standard_name=name,
                normalized_name=normalize_supplier_identity(name),
                display_name=f"护角{index}",
                is_active=True,
                sort_order=index * 10,
                version=1,
                supply_categories=[
                    SupplierSupplyCategory(category_code="other_packaging")
                ],
            )
            supplier.packaging_products.append(
                ExternalPackagingProduct(
                    category_code="other_packaging",
                    supplier_product_code=f"HJ-{index}",
                    normalized_supplier_product_code=f"HJ-{index}",
                    product_name="L型纸护角",
                    purchase_unit="根",
                    specification_summary="L型 50×50×5mm，长1200mm",
                    specification_json=specification,
                    is_active=True,
                    version=1,
                )
            )
            db.add(supplier)
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(price_router, prefix="/api/master/external-packaging")

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    app.state.session_factory = factory
    yield app
    engine.dispose()


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": "123456"}
    )
    assert response.status_code == 200


def _product_ids(app: FastAPI) -> list[int]:
    from app.models.supplier import ExternalPackagingProduct

    with app.state.session_factory() as db:
        return list(db.scalars(select(ExternalPackagingProduct.id).order_by(ExternalPackagingProduct.id)).all())


def _price_payload(price: str = "5.00", *, effective_from: str = "2026-08-01") -> dict:
    return {
        "quote_unit": "根",
        "currency": "CNY",
        "tax_mode": "tax_inclusive",
        "tax_rate": "0.13",
        "tax_amount_per_unit": "0.575221",
        "unit_price": price,
        "effective_from": effective_from,
        "moq_quantity": "100",
        "moq_unit": "根",
        "packaging_multiple": "10",
        "tier_prices": [
            {"min_quantity": "100", "unit_price": price},
            {"min_quantity": "500", "unit_price": "4.80"},
        ],
        "shipping_fee_mode": "per_order",
        "shipping_fee": "60",
        "sample_fee": "50",
        "plate_fee": "0",
        "die_fee": "120",
        "evidence_reference": "UAT-QUOTE-001",
    }


def test_cost_permission_and_admin_write_boundary(price_app: FastAPI) -> None:
    product_id = _product_ids(price_app)[0]
    with TestClient(price_app) as client:
        _login(client, "price-sales")
        assert client.get(
            f"/api/master/external-packaging/products/{product_id}/prices"
        ).status_code == 403

        _login(client, "price-boss")
        assert client.get(
            f"/api/master/external-packaging/products/{product_id}/prices"
        ).status_code == 200
        assert client.post(
            f"/api/master/external-packaging/products/{product_id}/prices",
            json=_price_payload(),
        ).status_code == 403


def test_price_change_creates_version_same_facts_are_idempotent(
    price_app: FastAPI,
) -> None:
    from app.models.external_packaging_price import ExternalPackagingPriceVersion

    product_id = _product_ids(price_app)[0]
    with TestClient(price_app) as client:
        _login(client, "price-admin")
        first = client.post(
            f"/api/master/external-packaging/products/{product_id}/prices",
            json=_price_payload("5.00"),
        )
        assert first.status_code == 201, first.text
        assert first.json()["created"] is True
        assert first.json()["item"]["version_number"] == 1

        retry_payload = _price_payload("5.00")
        retry_payload["evidence_reference"] = "UAT-QUOTE-001-SCAN-COPY"
        retry = client.post(
            f"/api/master/external-packaging/products/{product_id}/prices",
            json=retry_payload,
        )
        assert retry.status_code == 201
        assert retry.json()["created"] is False
        assert retry.json()["item"]["version_number"] == 1

        changed = client.post(
            f"/api/master/external-packaging/products/{product_id}/prices",
            json=_price_payload("5.20"),
        )
        assert changed.status_code == 201, changed.text
        assert changed.json()["created"] is True
        assert changed.json()["item"]["version_number"] == 2

    with price_app.state.session_factory() as db:
        count = db.scalar(
            select(func.count()).select_from(ExternalPackagingPriceVersion)
        )
        assert count == 2


def test_future_price_does_not_replace_current(price_app: FastAPI) -> None:
    product_id = _product_ids(price_app)[0]
    with TestClient(price_app) as client:
        _login(client, "price-admin")
        assert client.post(
            f"/api/master/external-packaging/products/{product_id}/prices",
            json=_price_payload("5.20"),
        ).status_code == 201
        assert client.post(
            f"/api/master/external-packaging/products/{product_id}/prices",
            json=_price_payload("5.50", effective_from="2099-01-01"),
        ).status_code == 201
        listed = client.get(
            f"/api/master/external-packaging/products/{product_id}/prices"
        ).json()
        assert listed["current"]["unit_price"] == "5.2"
        assert [row["version_number"] for row in listed["items"]] == [2, 1]


def test_same_caliber_comparison_and_condition_differences(price_app: FastAPI) -> None:
    first_id, second_id = _product_ids(price_app)
    with TestClient(price_app) as client:
        _login(client, "price-admin")
        assert client.post(
            f"/api/master/external-packaging/products/{first_id}/prices",
            json=_price_payload("5.20"),
        ).status_code == 201
        second = _price_payload("5.10")
        second["shipping_fee"] = "80"
        assert client.post(
            f"/api/master/external-packaging/products/{second_id}/prices",
            json=second,
        ).status_code == 201
        compared = client.get(
            f"/api/master/external-packaging/products/{first_id}/price-comparison"
        )
        assert compared.status_code == 200
        data = compared.json()
        assert data["status"] == "comparable"
        assert len(data["items"]) == 2
        lowest = next(row for row in data["items"] if row["external_product_id"] == second_id)
        assert lowest["difference_to_lowest"] == "0"
        assert "运费" in lowest["condition_differences"]


def test_different_unit_requires_evidence_and_different_tax_is_not_compared(
    price_app: FastAPI,
) -> None:
    first_id, second_id = _product_ids(price_app)
    with TestClient(price_app) as client:
        _login(client, "price-admin")
        wrong_unit = _price_payload()
        wrong_unit["quote_unit"] = "米"
        wrong_unit["moq_unit"] = "米"
        blocked = client.post(
            f"/api/master/external-packaging/products/{first_id}/prices",
            json=wrong_unit,
        )
        assert blocked.status_code == 422
        assert "换算依据" in blocked.json()["detail"]

        assert client.post(
            f"/api/master/external-packaging/products/{first_id}/prices",
            json=_price_payload("5.20"),
        ).status_code == 201
        other_tax = _price_payload("4.80")
        other_tax["tax_mode"] = "tax_exclusive"
        assert client.post(
            f"/api/master/external-packaging/products/{second_id}/prices",
            json=other_tax,
        ).status_code == 201
        data = client.get(
            f"/api/master/external-packaging/products/{first_id}/price-comparison"
        ).json()
        assert len(data["items"]) == 1
        assert data["incompatible_count"] == 1


def test_price_audit_redacts_amounts(price_app: FastAPI) -> None:
    from app.models.audit import OperationLog

    product_id = _product_ids(price_app)[0]
    with TestClient(price_app) as client:
        _login(client, "price-admin")
        response = client.post(
            f"/api/master/external-packaging/products/{product_id}/prices",
            json=_price_payload("5.20"),
        )
        assert response.status_code == 201
    with price_app.state.session_factory() as db:
        log = db.scalar(
            select(OperationLog).where(
                OperationLog.resource == "ExternalPackagingPriceVersion"
            )
        )
        assert log is not None
        assert "price_values_redacted" in (log.details or "")
        assert "5.20" not in (log.details or "")


def test_price_frontend_is_cost_gated_and_separates_fees() -> None:
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "static/index.html").read_text(
        encoding="utf-8"
    )
    for needle in (
        'v-if="canViewCosts" class="btn small primary" @click="openSupplierPackagingPrices(row)"',
        "采购价待完善（不会自动填 0）",
        "维护正式报价",
        "同规格同口径比价",
        "打样费",
        "版费",
        "刀模费",
        "阶梯价",
        "price-comparison",
    ):
        assert needle in source

