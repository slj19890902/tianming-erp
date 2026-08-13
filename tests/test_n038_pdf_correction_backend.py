from __future__ import annotations

import asyncio
import hashlib
import json
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.api import pdf_training as pdf_training_api
from app.models import Base
from app.models.customer import Customer
from app.models.order import Order
from app.models.pdf_training import PdfOrderCorrectionLog, PdfOrderTrainingSample
from app.models.user import User


@pytest.fixture
def correction_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'n038.sqlite3'}")

    @event.listens_for(engine, "connect")
    def _foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    sample_dir = tmp_path / "samples"
    monkeypatch.setattr(pdf_training_api, "_SAMPLE_DIR", sample_dir)
    monkeypatch.setattr(
        pdf_training_api,
        "_parse_pdf_sample_content",
        lambda _db, _content, _name, **_kwargs: {
            "parser_result_json": json.dumps(
                {
                    "customer_po": "OCR-WRONG",
                    "customer_name": "OCR 客户",
                    "items": [{"product_code": "OCR-1", "quantity": 1}],
                },
                ensure_ascii=False,
            ),
            "extracted_text": "test extracted text",
            "ocr_text_raw": None,
            "parse_method": "text",
        },
    )
    with maker() as session:
        admin = User(
            username="n038-admin",
            password_hash="test-only",
            role="admin",
            real_name="N038 管理员",
            must_change_password=False,
        )
        customer = Customer(name="N038 客户甲")
        other_customer = Customer(name="N038 客户乙")
        session.add_all([admin, customer, other_customer])
        session.commit()
        yield session, admin, customer, other_customer, sample_dir
    engine.dispose()


def _truth(customer_name: str = "N038 客户甲") -> str:
    return json.dumps(
        {
            "customer_name": customer_name,
            "order_no": "PO-N038-1",
            "order_date": "2026-07-18",
            "delivery_date": "2026-07-25",
            "items": [
                {
                    "line_no": 1,
                    "product_code": "P-N038-1",
                    "product_name": "人工修正纸箱",
                    "spec": "100*200*300",
                    "quantity": 10,
                    "unit": "个",
                    "unit_price": 2.5,
                    "amount": 25,
                }
            ],
        },
        ensure_ascii=False,
    )


def _submit(
    session: Session,
    admin: User,
    content: bytes,
    truth: str,
    customer_id: int | None,
):
    upload = UploadFile(
        filename="n038-order.pdf",
        file=BytesIO(content),
        headers={"content-type": "application/pdf"},
    )
    return asyncio.run(
        pdf_training_api.submit_correction_sample(
            file=upload,
            ground_truth_json=truth,
            customer_id=customer_id,
            notes="人工核对订单 PDF 识别结果",
            db=session,
            user=admin,
        )
    )


def test_submit_correction_creates_only_labeled_sample_and_is_idempotent(
    correction_session,
) -> None:
    session, admin, customer, _other, sample_dir = correction_session
    content = b"%PDF-1.4\nN038 isolated test\n%%EOF"
    truth = _truth()

    first = _submit(session, admin, content, truth, customer.id)
    first_log_count = session.scalar(
        select(func.count(PdfOrderCorrectionLog.id)).where(
            PdfOrderCorrectionLog.sample_id == first.id
        )
    )
    second = _submit(session, admin, content, truth, customer.id)

    assert second.id == first.id
    assert session.scalar(select(func.count(PdfOrderTrainingSample.id))) == 1
    assert session.scalar(select(func.count(Order.id))) == 0
    assert second.parse_status == "labeled"
    assert second.gold_review_status == "pending"
    assert second.ground_truth_json == truth
    assert session.scalar(
        select(func.count(PdfOrderCorrectionLog.id)).where(
            PdfOrderCorrectionLog.sample_id == first.id
        )
    ) == first_log_count
    stored = Path(second.file_path)
    assert stored.parent == sample_dir
    assert stored.read_bytes() == content
    assert hashlib.sha256(stored.read_bytes()).hexdigest() == second.file_sha256


def test_existing_unbound_sample_persists_customer_and_rejects_conflicts(
    correction_session,
) -> None:
    session, admin, customer, other_customer, _sample_dir = correction_session
    content = b"%PDF-1.4\nN038 customer binding\n%%EOF"
    sample = _submit(session, admin, content, _truth(), None)

    rebound = _submit(session, admin, content, _truth(), customer.id)
    session.expire_all()
    assert session.get(PdfOrderTrainingSample, sample.id).customer_id == customer.id
    assert rebound.customer_id == customer.id

    with pytest.raises(HTTPException) as conflict:
        _submit(session, admin, content, _truth("N038 客户乙"), other_customer.id)
    assert conflict.value.status_code == 409
    assert session.scalar(select(func.count(Order.id))) == 0


def test_submit_correction_rejects_unknown_customer(correction_session) -> None:
    session, admin, _customer, _other, _sample_dir = correction_session
    with pytest.raises(HTTPException) as invalid:
        _submit(session, admin, b"%PDF-1.4\nunknown customer", _truth(), 999999)
    assert invalid.value.status_code == 400
    assert session.scalar(select(func.count(PdfOrderTrainingSample.id))) == 0


def test_reparse_refuses_source_pdf_sha_mismatch(correction_session, tmp_path: Path) -> None:
    session, admin, customer, _other, _sample_dir = correction_session
    expected = b"expected source"
    damaged_path = tmp_path / "damaged.pdf"
    damaged_path.write_bytes(b"different source")
    sample = PdfOrderTrainingSample(
        customer_id=customer.id,
        file_name="damaged.pdf",
        file_sha256=hashlib.sha256(expected).hexdigest(),
        file_path=str(damaged_path),
        parser_result_json="{}",
        parse_method="text",
        parse_status="pending",
    )
    session.add(sample)
    session.commit()

    with pytest.raises(HTTPException) as mismatch:
        pdf_training_api.reparse_sample(str(sample.id), session, admin)
    assert mismatch.value.status_code == 409
    assert session.scalar(select(func.count(Order.id))) == 0
