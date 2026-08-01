from __future__ import annotations

from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def disabled_workbook_app(tmp_path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.materials import router as materials_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.material import Material
    from app.models.material_price_history import MaterialPriceHistory
    from app.models.supplier_paper_code import SupplierPaperCode
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "p0-10-disabled-workbook.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add_all(
            [
                User(
                    username="p0-10-admin",
                    password_hash=hash_password("P0-10-Test-Only!"),
                    role="admin",
                    real_name="测试管理员",
                    must_change_password=False,
                ),
                User(
                    username="p0-10-sales",
                    password_hash=hash_password("P0-10-Test-Only!"),
                    role="sales",
                    real_name="测试销售",
                    must_change_password=False,
                ),
                SupplierPaperCode(
                    supplier_name="匿名供应商",
                    code_char="A",
                    paper_name="匿名纸种",
                    gram_weight=100,
                    is_active=True,
                ),
                Material(
                    code="AAA",
                    supplier_name="匿名供应商",
                    layer_count=3,
                    quote_price=Decimal("1.0000"),
                    price_unit="元/㎡",
                    is_active=True,
                    version=1,
                ),
            ]
        )
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(materials_router, prefix="/api/master/materials")

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    app.state.session_factory = factory
    app.state.models = (SupplierPaperCode, Material, MaterialPriceHistory)
    yield app
    engine.dispose()


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "P0-10-Test-Only!"},
    )
    assert response.status_code == 200


def _counts(app: FastAPI) -> tuple[int, int, int]:
    paper_model, material_model, price_model = app.state.models
    with app.state.session_factory() as db:
        return (
            db.scalar(select(func.count()).select_from(paper_model)) or 0,
            db.scalar(select(func.count()).select_from(material_model)) or 0,
            db.scalar(select(func.count()).select_from(price_model)) or 0,
        )


def test_admin_legacy_workbook_endpoints_fail_closed_without_writes(
    disabled_workbook_app: FastAPI,
) -> None:
    expected_detail = {
        "code": "SUPPLIER_MATERIAL_WORKBOOK_DISABLED",
        "message": "供应商材质 Excel 导入导出功能已暂停，请使用页面维护",
    }
    before = _counts(disabled_workbook_app)

    with TestClient(disabled_workbook_app) as client:
        _login(client, "p0-10-admin")
        responses = (
            client.get("/api/master/materials/import-template.xlsx"),
            client.post(
                "/api/master/materials/import/preview",
                files={"file": ("旧文件.xlsx", b"not-used", "application/octet-stream")},
            ),
            client.post(
                "/api/master/materials/import/apply",
                json={"preview_token": "old-client-token"},
            ),
        )

    for response in responses:
        assert response.status_code == 410
        assert response.json()["detail"] == expected_detail
    assert _counts(disabled_workbook_app) == before


def test_non_admin_workbook_requests_remain_forbidden(
    disabled_workbook_app: FastAPI,
) -> None:
    with TestClient(disabled_workbook_app) as client:
        _login(client, "p0-10-sales")
        assert (
            client.get("/api/master/materials/import-template.xlsx").status_code
            == 403
        )
        assert client.post("/api/master/materials/import/preview").status_code == 403
        assert client.post("/api/master/materials/import/apply").status_code == 403
