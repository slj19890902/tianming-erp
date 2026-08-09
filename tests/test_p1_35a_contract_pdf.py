from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker


TRUSTED_TEST_FONT = next(
    (
        path
        for path in (
            Path(r"C:\Windows\Fonts\simhei.ttf"),
            Path(r"C:\Windows\Fonts\Deng.ttf"),
            Path(r"C:\Windows\Fonts\simsunb.ttf"),
        )
        if path.is_file()
    ),
    None,
)


@pytest.fixture()
def contract_pdf_app(tmp_path, monkeypatch):
    import app.models  # noqa: F401 - register every FK target
    from app.api.auth import router as auth_router
    from app.api.contracts import router as contracts_router
    from app.api.deps import get_db
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope
    from app.models.company_config import CompanyConfig
    from app.models.customer import Customer
    from app.models.customer_contract import CustomerContract, CustomerContractItem
    from app.models.user import User

    assert TRUSTED_TEST_FONT is not None, "Windows 合同 PDF 中文字体不存在"
    monkeypatch.setenv("ERP_CONTRACT_PDF_FONT_PATH", str(TRUSTED_TEST_FONT))

    engine = create_sqlite_engine(tmp_path / "p1-35a-contract-pdf.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        own_customer = Customer(
            customer_number=3501,
            customer_code="P135A-OWN",
            name="P1-35A 自有客户",
            credit_limit=Decimal("0"),
        )
        other_customer = Customer(
            customer_number=3502,
            customer_code="P135A-OTHER",
            name="P1-35A 其他客户",
            credit_limit=Decimal("0"),
        )
        admin = User(
            username="p135a-admin",
            password_hash=hash_password("123456"),
            role="admin",
            real_name="PDF 管理员",
            must_change_password=False,
        )
        scoped = User(
            username="p135a-scoped",
            password_hash=hash_password("123456"),
            role="sales",
            real_name="PDF 受限业务员",
            must_change_password=False,
            customer_access_mode="selected",
        )
        denied = User(
            username="p135a-denied",
            password_hash=hash_password("123456"),
            role="delivery_picker",
            real_name="无合同权限用户",
            must_change_password=False,
        )
        db.add_all([own_customer, other_customer, admin, scoped, denied])
        db.flush()
        db.add(UserCustomerScope(user_id=scoped.id, customer_id=own_customer.id))
        db.add(
            CompanyConfig(
                id=1,
                company_name="苏州天明包装有限公司",
                address="苏州市测试路 10 号",
                contact_person="合同经办人",
                phone="0512-12345678",
            )
        )

        def add_contract(
            *,
            customer: Customer,
            contract_no: str,
            line_count: int,
            status: str = "draft",
            version: int = 3,
        ) -> CustomerContract:
            contract = CustomerContract(
                contract_no=contract_no,
                customer_id=customer.id,
                customer_name=customer.name,
                customer_contact="张经理",
                customer_phone="13800000000",
                customer_address="客户测试地址 <A&B>",
                payment_terms="30",
                contract_date=date(2026, 8, 10),
                customer_po="PO-P1-35A",
                delivery_date=date(2026, 8, 18),
                remarks="只显示客户可见备注 <请核对> & 不泄漏内部字段",
                total_amount=Decimal(line_count * 25),
                status=status,
                version=version,
                created_by=admin.id,
            )
            for line_no in range(1, line_count + 1):
                contract.items.append(
                    CustomerContractItem(
                        line_no=line_no,
                        product_code=f"SECRET-PRODUCT-CODE-{line_no}",
                        product_name=f"测试纸箱 <{line_no}> & 客户款",
                        specification="300×200×150mm",
                        length_mm=Decimal("300"),
                        width_mm=Decimal("200"),
                        height_mm=Decimal("150"),
                        material_code="A=B",
                        flute_type="B楞",
                        quantity=10,
                        unit_price=Decimal("2.5000"),
                        subtotal=Decimal("25.00"),
                        remarks=f"第 {line_no} 行备注",
                    )
                )
            db.add(contract)
            db.flush()
            return contract

        own = add_contract(
            customer=own_customer,
            contract_no="CT-20260810-001",
            line_count=2,
        )
        other = add_contract(
            customer=other_customer,
            contract_no="CT-20260810-002",
            line_count=2,
            status="confirmed",
        )
        long_contract = add_contract(
            customer=own_customer,
            contract_no="CT-20260810-003",
            line_count=80,
            status="confirmed",
            version=5,
        )
        converted_contract = add_contract(
            customer=own_customer,
            contract_no="CT-20260810-004",
            line_count=1,
            status="converted",
            version=7,
        )
        db.commit()
        ids = {
            "own": own.id,
            "other": other.id,
            "long": long_contract.id,
            "converted": converted_contract.id,
            "admin": admin.id,
        }

    app = FastAPI()

    @app.middleware("http")
    async def fixed_request_id(request, call_next):
        request.state.request_id = "p1-35a-request-id"
        return await call_next(request)

    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(contracts_router, prefix="/api/contracts")

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        yield client, factory, ids


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "123456"},
    )
    assert response.status_code == 200, response.text


def _pdf_text(content: bytes) -> tuple[PdfReader, list[str]]:
    reader = PdfReader(BytesIO(content))
    return reader, [page.extract_text() or "" for page in reader.pages]


def _has_embedded_font(reader: PdfReader) -> bool:
    for page in reader.pages:
        fonts = page["/Resources"].get("/Font") or {}
        for reference in fonts.values():
            font = reference.get_object()
            descriptor = font.get("/FontDescriptor")
            if descriptor is None:
                continue
            descriptor = descriptor.get_object()
            if any(
                key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3")
            ):
                return True
    return False


def test_contract_pdf_is_deterministic_embedded_unsealed_and_read_only(
    contract_pdf_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.customer_contract import CustomerContract, CustomerContractItem
    from app.models.order import Order

    client, factory, ids = contract_pdf_app
    _login(client, "p135a-admin")

    def snapshot() -> tuple:
        with factory() as db:
            contract = db.get(CustomerContract, ids["own"])
            return (
                contract.status,
                contract.version,
                contract.total_amount,
                db.scalar(select(func.count()).select_from(CustomerContractItem)),
                db.scalar(select(func.count()).select_from(Order)),
                db.scalar(select(func.count()).select_from(OperationLog)),
            )

    before = snapshot()
    first = client.get(f"/api/contracts/{ids['own']}/pdf?expected_version=3")
    second = client.get(f"/api/contracts/{ids['own']}/pdf?expected_version=3")
    after = snapshot()

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.content == second.content
    assert before == after
    assert first.content.startswith(b"%PDF-")
    assert first.headers["content-type"] == "application/pdf"
    disposition = first.headers["content-disposition"]
    assert 'filename="contract-' in disposition
    assert "filename*=UTF-8''" in disposition
    assert "20260810" in unquote(disposition)
    assert first.headers["cache-control"] == "private, no-store, max-age=0"
    assert first.headers["x-content-type-options"] == "nosniff"
    assert first.headers["x-contract-version"] == "3"
    assert first.headers["x-contract-pdf-template-version"] == "p1-35a-v1"
    assert first.headers["etag"] == (
        f'"sha256-{first.headers["x-contract-pdf-sha256"]}"'
    )

    reader, pages = _pdf_text(first.content)
    text = "\n".join(pages)
    assert len(reader.pages) == 1
    assert _has_embedded_font(reader)
    assert "苏州天明包装有限公司" in text
    assert "购货合同" in text
    assert "草稿（未盖章）" in text
    assert "测试纸箱 <1> & 客户款" in text
    assert "客户测试地址 <A&B>" in text
    assert "合同总金额：￥50.00" in text
    assert "SECRET-PRODUCT-CODE" not in text
    assert "estimated_unit_cost" not in text
    assert "gross_profit" not in text
    assert pages[-1].count("授权代表") == 2


def test_contract_pdf_repeats_table_header_and_keeps_terms_and_signatures_at_end(
    contract_pdf_app,
) -> None:
    client, _factory, ids = contract_pdf_app
    _login(client, "p135a-admin")
    response = client.get(
        f"/api/contracts/{ids['long']}/pdf?expected_version=5"
    )
    assert response.status_code == 200, response.text
    reader, pages = _pdf_text(response.content)
    assert len(reader.pages) >= 3
    for page in pages:
        assert "苏州天明包装有限公司" in page
        assert "CT-20260810-003" in page
        assert "P1-35A 自有客户" in page
        assert "已确认（未盖章）" in page
    item_pages = [page for page in pages if "SECRET-PRODUCT-CODE" not in page and "商品名称" in page]
    assert len(item_pages) >= 2
    assert all("商品名称" in page and "数量" in page for page in item_pages)
    assert all("第二条" not in page for page in pages[:-1])
    assert all("授权代表" not in page for page in pages[:-1])
    assert "第二条" in pages[-1] and "交货" in pages[-1]
    assert "第七条" in pages[-1] and "生效、附件与争议解决" in pages[-1]
    assert pages[-1].count("授权代表") == 2
    assert _has_embedded_font(reader)


def test_contract_pdf_enforces_version_permission_and_customer_scope(
    contract_pdf_app,
) -> None:
    client, _factory, ids = contract_pdf_app
    _login(client, "p135a-scoped")
    own = client.get(f"/api/contracts/{ids['own']}/pdf?expected_version=3")
    assert own.status_code == 200
    stale = client.get(f"/api/contracts/{ids['own']}/pdf?expected_version=2")
    assert stale.status_code == 409
    assert stale.json()["detail"] == "合同版本已更新，请刷新后重新导出 PDF"
    other = client.get(f"/api/contracts/{ids['other']}/pdf?expected_version=3")
    assert other.status_code == 403

    _login(client, "p135a-denied")
    denied = client.get(f"/api/contracts/{ids['own']}/pdf?expected_version=3")
    assert denied.status_code == 403


def test_contract_pdf_requires_login_handles_missing_and_labels_all_unsealed_states(
    contract_pdf_app,
) -> None:
    client, _factory, ids = contract_pdf_app
    unauthenticated = client.get(
        f"/api/contracts/{ids['own']}/pdf?expected_version=3"
    )
    assert unauthenticated.status_code == 401

    _login(client, "p135a-admin")
    missing = client.get("/api/contracts/999999/pdf?expected_version=1")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "客户合同不存在"

    confirmed = client.get(
        f"/api/contracts/{ids['other']}/pdf?expected_version=3"
    )
    assert confirmed.status_code == 200
    _reader, confirmed_pages = _pdf_text(confirmed.content)
    assert "已确认（未盖章）" in "\n".join(confirmed_pages)

    converted = client.get(
        f"/api/contracts/{ids['converted']}/pdf?expected_version=7"
    )
    assert converted.status_code == 200
    _reader, converted_pages = _pdf_text(converted.content)
    assert "已转订单（未盖章）" in "\n".join(converted_pages)


def test_contract_pdf_missing_configured_font_fails_closed(
    contract_pdf_app,
    monkeypatch,
) -> None:
    client, _factory, ids = contract_pdf_app
    _login(client, "p135a-admin")
    monkeypatch.setenv(
        "ERP_CONTRACT_PDF_FONT_PATH",
        r"C:\definitely-missing\contract-font.ttf",
    )
    response = client.get(f"/api/contracts/{ids['own']}/pdf?expected_version=3")
    assert response.status_code == 503
    assert response.json()["detail"] == (
        "合同 PDF 中文字体未配置或无法嵌入，请联系管理员"
    )


def test_contract_pdf_render_failure_is_safe_and_returns_request_id(
    contract_pdf_app,
    monkeypatch,
) -> None:
    client, _factory, ids = contract_pdf_app
    _login(client, "p135a-admin")

    def fail_render(*_args, **_kwargs):
        raise RuntimeError("SECRET-CONTRACT-CONTENT-MUST-NOT-LEAK")

    monkeypatch.setattr("app.api.contracts.render_contract_pdf", fail_render)
    response = client.get(f"/api/contracts/{ids['own']}/pdf?expected_version=3")
    assert response.status_code == 500
    assert response.headers["x-request-id"] == "p1-35a-request-id"
    detail = response.json()["detail"]
    assert detail == {
        "message": "合同 PDF 生成失败：服务器内部错误",
        "code": "CONTRACT_PDF_RENDER_FAILED",
        "request_id": "p1-35a-request-id",
    }
    assert response.headers["content-type"].startswith("application/json")
    assert not response.content.startswith(b"%PDF-")
    assert b"SECRET-CONTRACT-CONTENT-MUST-NOT-LEAK" not in response.content
