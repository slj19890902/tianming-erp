from __future__ import annotations

from collections.abc import Generator
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def pricing_api_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.pricing import router as pricing_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "pricing.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        session.add(
            User(
                username="admin",
                password_hash=hash_password("RolePass123!"),
                role="admin",
                real_name="业务员",
                display_name="业务员",
                must_change_password=False,
            )
        )
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(pricing_router, prefix="/api/pricing")

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return app


def test_normal_box_price_uses_factory_formula_and_half_up_rounding() -> None:
    from app.services.pricing import calculate_price

    result = calculate_price(
        box_category="normal",
        length_mm=Decimal("600"),
        width_mm=Decimal("400"),
        height_mm=Decimal("300"),
        board_square_price=Decimal("1.95"),
        extra_fee=Decimal("0.15"),
    )

    assert result.area_m2 == Decimal("1.419264")
    assert result.unit_price == Decimal("2.92")


def test_die_cut_price_uses_unfolded_dimensions() -> None:
    from app.services.pricing import calculate_price

    result = calculate_price(
        box_category="die_cut",
        unfolded_length_mm=Decimal("800"),
        unfolded_width_mm=Decimal("500"),
        board_square_price=Decimal("1.95"),
        extra_fee=Decimal("0.10"),
    )

    assert result.area_m2 == Decimal("0.4")
    assert result.unit_price == Decimal("0.88")


def test_pricing_rejects_missing_or_non_positive_dimensions() -> None:
    from app.services.pricing import PricingError, calculate_price

    with pytest.raises(PricingError):
        calculate_price(
            box_category="normal",
            length_mm=Decimal("600"),
            width_mm=Decimal("0"),
            height_mm=Decimal("300"),
            board_square_price=Decimal("1.95"),
        )

    with pytest.raises(PricingError):
        calculate_price(
            box_category="die_cut",
            unfolded_length_mm=Decimal("800"),
            board_square_price=Decimal("1.95"),
        )


def test_pricing_api_accepts_extra_fee(pricing_api_app) -> None:
    from fastapi.testclient import TestClient

    with TestClient(pricing_api_app) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "RolePass123!"},
        )
        assert login.status_code == 200
        response = client.post(
            "/api/pricing/calculate",
            json={
                "box_category": "normal",
                "length_mm": "600",
                "width_mm": "400",
                "height_mm": "300",
                "board_square_price": "1.95",
                "extra_fee": "0.15",
            },
        )

    assert response.status_code == 200
    assert Decimal(str(response.json()["unit_price"])) == Decimal("2.92")
