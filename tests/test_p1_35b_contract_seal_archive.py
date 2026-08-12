from __future__ import annotations

import hashlib
import struct
import zlib
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker
from pypdf import PdfReader
from io import BytesIO


TEST_FONT = next(
    (path for path in (Path(r"C:\Windows\Fonts\simhei.ttf"), Path(r"C:\Windows\Fonts\Deng.ttf")) if path.is_file()),
    None,
)


def _png() -> bytes:
    width = height = 16
    rows = b"".join(b"\x00" + b"\xcc\x22\x22\xff" * width for _ in range(height))
    def chunk(kind: bytes, value: bytes) -> bytes:
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


@pytest.fixture()
def seal_app(tmp_path, monkeypatch):
    import app.models  # noqa: F401
    from app.api.auth import router as auth_router
    from app.api.contract_seals import router as seal_router
    from app.api.deps import get_db
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.company_config import CompanyConfig
    from app.models.customer import Customer
    from app.models.customer_contract import CustomerContract, CustomerContractItem
    from app.models.user import User

    assert TEST_FONT is not None
    monkeypatch.setenv("ERP_CONTRACT_PDF_FONT_PATH", str(TEST_FONT))
    engine = create_sqlite_engine(tmp_path / "p1-35b.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        customer = Customer(customer_number=3511, customer_code="P135B", name="P1-35B 客户", credit_limit=Decimal("0"))
        users = [
            User(username="seal-admin", password_hash=hash_password("123456"), role="admin", real_name="印章管理员", must_change_password=False),
            User(username="seal-boss", password_hash=hash_password("123456"), role="boss", real_name="老板", must_change_password=False),
            User(username="seal-sales", password_hash=hash_password("123456"), role="sales", real_name="业务员", must_change_password=False),
        ]
        db.add_all([customer, *users])
        db.flush()
        db.add(CompanyConfig(id=1, company_name="苏州天明包装有限公司"))
        def add(no: str, status: str, version: int) -> CustomerContract:
            contract = CustomerContract(
                contract_no=no, customer_id=customer.id, customer_name=customer.name,
                payment_terms="30", contract_date=date(2026, 8, 12), total_amount=Decimal("25"),
                status=status, version=version, created_by=users[0].id,
            )
            contract.items.append(CustomerContractItem(
                line_no=1, product_name="测试纸箱", specification="300×200×150mm",
                quantity=10, unit_price=Decimal("2.5"), subtotal=Decimal("25"),
            ))
            db.add(contract)
            db.flush()
            return contract
        draft = add("CT-20260812-001", "draft", 1)
        confirmed = add("CT-20260812-002", "confirmed", 3)
        converted = add("CT-20260812-003", "converted", 5)
        db.commit()
        ids = {"draft": draft.id, "confirmed": confirmed.id, "converted": converted.id}
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(seal_router, prefix="/api/contract-seals")
    def override_db():
        with factory() as db:
            yield db
    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        yield client, factory, ids


def _login(client: TestClient, username: str) -> None:
    assert client.post("/api/auth/login", json={"username": username, "password": "123456"}).status_code == 200


def _upload_and_enable(client: TestClient) -> dict:
    uploaded = client.post("/api/contract-seals/assets", files={"file": ("seal.png", _png(), "image/png")})
    assert uploaded.status_code == 200, uploaded.text
    asset = uploaded.json()["asset"]
    enabled = client.put("/api/contract-seals/selection", json={
        "expected_version": 0, "operation_key": "enable-seal-operation-1",
        "asset_version_id": asset["id"], "is_enabled": True,
    })
    assert enabled.status_code == 200, enabled.text
    return enabled.json()


def test_admin_maintains_seal_and_roles_are_fail_closed(seal_app) -> None:
    client, _factory, _ids = seal_app
    assert client.get("/api/contract-seals/status").status_code == 401
    _login(client, "seal-sales")
    assert client.get("/api/contract-seals/status").status_code == 403
    assert client.post("/api/contract-seals/assets", files={"file": ("x.png", _png(), "image/png")}).status_code == 403
    _login(client, "seal-boss")
    assert client.get("/api/contract-seals/status").status_code == 200
    assert client.post("/api/contract-seals/assets", files={"file": ("x.png", _png(), "image/png")}).status_code == 403
    _login(client, "seal-admin")
    state = _upload_and_enable(client)
    assert state["version"] == 1 and state["is_enabled"] is True
    assert "png_content" not in str(state)
    replay = client.put("/api/contract-seals/selection", json={
        "expected_version": 0, "operation_key": "enable-seal-operation-1",
        "asset_version_id": state["asset"]["id"], "is_enabled": True,
    })
    assert replay.status_code == 200 and replay.json()["version"] == 1
    reused_for_other_state = client.put("/api/contract-seals/selection", json={
        "expected_version": 1, "operation_key": "enable-seal-operation-1",
        "asset_version_id": state["asset"]["id"], "is_enabled": False,
    })
    assert reused_for_other_state.status_code == 409
    assert client.put("/api/contract-seals/selection", json={
        "expected_version": 0, "operation_key": "enable-seal-operation-2",
        "asset_version_id": state["asset"]["id"], "is_enabled": True,
    }).status_code == 409


def test_confirmed_and_converted_export_once_and_archive_immutable(seal_app) -> None:
    from app.models.audit import OperationLog
    from app.models.contract_seal import ContractSealedPdfArchive

    client, factory, ids = seal_app
    _login(client, "seal-admin")
    state = _upload_and_enable(client)
    _login(client, "seal-boss")
    payload = {"expected_contract_version": 3, "expected_seal_selection_version": state["version"], "operation_key": "sealed-export-confirmed-1"}
    first = client.post(f"/api/contract-seals/contracts/{ids['confirmed']}/exports", json=payload)
    second = client.post(f"/api/contract-seals/contracts/{ids['confirmed']}/exports", json=payload)
    assert first.status_code == second.status_code == 200
    assert first.content == second.content and first.content.startswith(b"%PDF-")
    assert hashlib.sha256(first.content).hexdigest() == first.headers["x-content-sha256"]
    assert first.headers["x-template-version"] == "p1-35b-v1"
    reader = PdfReader(BytesIO(first.content))
    assert any(
        (page["/Resources"].get("/XObject") or {})
        for page in reader.pages
    ), "盖章归档 PDF 必须真正嵌入印章图像对象"
    archive_id = int(first.headers["x-contract-sealed-archive-id"])
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(ContractSealedPdfArchive)) == 1
        assert db.scalar(select(func.count()).select_from(OperationLog).where(OperationLog.action == "contract.sealed_pdf.export")) == 1
        archive = db.get(ContractSealedPdfArchive, archive_id)
        with pytest.raises(Exception):
            archive.pdf_sha256 = "0" * 64
            db.commit()
        db.rollback()
    listing = client.get(f"/api/contract-seals/contracts/{ids['confirmed']}/archives")
    assert listing.status_code == 200 and len(listing.json()["items"]) == 1
    stored = client.get(f"/api/contract-seals/archives/{archive_id}/pdf")
    assert stored.status_code == 200 and stored.content == first.content
    converted = client.post(f"/api/contract-seals/contracts/{ids['converted']}/exports", json={
        "expected_contract_version": 5, "expected_seal_selection_version": state["version"], "operation_key": "sealed-export-converted-1",
    })
    assert converted.status_code == 200


def test_draft_stale_versions_and_disabled_seal_are_rejected(seal_app) -> None:
    client, _factory, ids = seal_app
    _login(client, "seal-admin")
    state = _upload_and_enable(client)
    draft = client.post(f"/api/contract-seals/contracts/{ids['draft']}/exports", json={
        "expected_contract_version": 1, "expected_seal_selection_version": state["version"], "operation_key": "sealed-export-draft-1",
    })
    assert draft.status_code == 409
    stale = client.post(f"/api/contract-seals/contracts/{ids['confirmed']}/exports", json={
        "expected_contract_version": 2, "expected_seal_selection_version": state["version"], "operation_key": "sealed-export-stale-contract",
    })
    assert stale.status_code == 409
    disabled = client.put("/api/contract-seals/selection", json={
        "expected_version": state["version"], "operation_key": "disable-seal-operation-1",
        "asset_version_id": state["asset"]["id"], "is_enabled": False,
    })
    assert disabled.status_code == 200
    refused = client.post(f"/api/contract-seals/contracts/{ids['confirmed']}/exports", json={
        "expected_contract_version": 3, "expected_seal_selection_version": disabled.json()["version"], "operation_key": "sealed-export-disabled-1",
    })
    assert refused.status_code == 409
