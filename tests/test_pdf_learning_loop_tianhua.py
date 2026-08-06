from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import pdf_training as pdf_training_api
from app.models import Base
from app.models.customer import Customer
from app.models.order import Order
from app.models.pdf_training import PdfOrderCustomerTemplate, PdfOrderTrainingSample
from app.models.user import User
from app.services import pdf_customer_templates
from app.services.order_pdf_import import parse_purchase_order_text
from app.services.pdf_parse_pipeline import PdfParsePipelineResult


TIANHUA_WRAPPED_TEXT = """采购订单
苏州天华超净科技有限公司
PO2026080148
2026-08-06
行号 料品编码 物料名称 规格型号 番号 销售订单号 单位 数量 含税单价 价税合计 交货日期
10 21311384 中性纸箱（26\"*45\"）
116*67*11cm
9CCC9/AB
50本/箱 黑色印刷
个 2.00000 15.650000 31.30 2026.08.13
20 21302101 衬板（30*60cm） 30*60cm
A535T/AB 个 4.00000 0.660000 2.64 2026.08.13
30 21311206 中性内箱（26\"*45\"）
68*116*1.8/2.
1cm
A535T/BE THB8
个 1,500.0000
0 7.970000 11,955.00 2026.08.13
40 21302085 满衬板（26”*45”）
107.5*66cm
+/-0.5cm
A535T/AB
个 1,500.0000
0 2.620000 3,930.00 2026.08.13
合计 3,006.0000 15,918.94
"""


def _truth(*, line_no: int = 10) -> str:
    return json.dumps(
        {
            "order_no": "PO-LEARN-1",
            "customer_name": "持续学习客户",
            "items": [
                {
                    "line_no": line_no,
                    "product_code": "P-1",
                    "product_name": "纸箱",
                    "spec": "100*200*300",
                    "quantity": 10,
                    "unit": "个",
                    "unit_price": 2.5,
                    "amount": 25,
                    "delivery_date": "2026-08-15",
                }
            ],
        },
        ensure_ascii=False,
    )


def _parsed(customer_id: int, *, raw_name: bool = False) -> dict:
    return {
        "customer_po": "PO-LEARN-1",
        "customer_name": "持续学习客户",
        "customer_route": {
            "status": "locked",
            "template_customer_id": customer_id,
        },
        "items": [
            {
                "line_no": 10,
                "product_code": "P-1",
                "product_name": "PDF原始品名" if raw_name else "纸箱",
                "spec": "PDF原始规格" if raw_name else "100*200*300",
                "quantity": 10,
                "unit": "个",
                "unit_price": 2.5,
                "amount": 25,
                "delivery_date": "2026-08-15",
            }
        ],
    }


def _pipeline(draft: dict) -> PdfParsePipelineResult:
    return PdfParsePipelineResult(
        draft=draft,
        extracted_text="text",
        ocr_text_raw=None,
        parse_method="text",
        text_quality="readable_text",
    )


@pytest.fixture
def learning_session(tmp_path: Path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'learning.sqlite3'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker() as db:
        admin = User(
            username="learning-admin",
            password_hash="test-only",
            role="admin",
            real_name="学习管理员",
            must_change_password=False,
        )
        customer = Customer(name="持续学习客户")
        db.add_all([admin, customer])
        db.commit()
        yield db, admin, customer, tmp_path
    engine.dispose()


def _add_sample(db, customer: Customer, directory: Path, index: int, *, approved: bool = True, line_no: int = 10):
    content = f"gold-{index}".encode()
    path = directory / f"gold-{index}.pdf"
    path.write_bytes(content)
    sample = PdfOrderTrainingSample(
        customer_id=customer.id,
        file_name=path.name,
        file_sha256=hashlib.sha256(content).hexdigest(),
        file_path=str(path),
        parser_result_json=json.dumps(_parsed(customer.id), ensure_ascii=False),
        ground_truth_json=_truth(line_no=line_no),
        parse_status="reviewed" if approved else "labeled",
        parse_method="text",
        gold_review_status="approved" if approved else "pending",
    )
    db.add(sample)
    db.commit()
    return sample


def test_tianhua_wrapped_quantity_is_repaired_and_integrity_passes() -> None:
    result = parse_purchase_order_text(TIANHUA_WRAPPED_TEXT, "PO2026080148.pdf")

    assert [item["line_no"] for item in result["items"]] == [10, 20, 30, 40]
    assert [item["quantity"] for item in result["items"]] == [2, 4, 1500, 1500]
    assert result["items"][2]["raw_quantity"] == "1,500.00000"
    assert result["items"][3]["raw_quantity"] == "1,500.00000"
    assert result["items"][2]["layout_repairs"] == ["tianhua_wrapped_quantity"]
    assert "layout_repairs" not in result["items"][1]
    assert result["integrity_check"]["parsed_total_quantity"] == "3006"
    assert result["integrity_check"]["quantity_total_diff"] == "0"
    assert result["integrity_check"]["integrity_status"] == "passed"


def test_replay_uses_matched_order_preview_and_accepts_legacy_ordinal_lines(
    learning_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, _admin, customer, directory = learning_session
    for index in range(1, 4):
        _add_sample(db, customer, directory, index, line_no=1)
    candidate = PdfOrderCustomerTemplate(
        customer_id=customer.id,
        template_name="候选规则",
        status="draft",
        is_active=False,
        version=1,
    )
    db.add(candidate)
    db.commit()

    monkeypatch.setattr(
        pdf_customer_templates,
        "parse_pdf_bytes",
        lambda *_args: _pipeline(_parsed(customer.id, raw_name=True)),
    )
    monkeypatch.setattr(
        pdf_customer_templates,
        "match_import_draft",
        lambda _db, _draft, customer_id: _parsed(customer_id),
    )

    evidence = pdf_customer_templates.activation_dry_run(db, candidate)

    assert evidence["parser_output_layer"] == "order_preview_matched"
    assert evidence["can_activate"] is True
    assert all(row["legacy_ordinal_line_numbers"] for row in evidence["sample_results"])
    assert all(row["strict_checks"]["items[0].line_no"] for row in evidence["sample_results"])


def test_approved_gold_auto_draft_replay_is_idempotent_and_one_click_activates(
    learning_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, admin, customer, directory = learning_session
    active = PdfOrderCustomerTemplate(
        customer_id=customer.id,
        template_name="正式规则-v1",
        column_map_json=json.dumps(
            {"customer_name": customer.name, "customer_type": "generic"},
            ensure_ascii=False,
        ),
        status="active",
        is_active=True,
        version=1,
    )
    db.add(active)
    db.commit()
    _add_sample(db, customer, directory, 1)
    _add_sample(db, customer, directory, 2)
    pending = _add_sample(db, customer, directory, 3, approved=False)
    before_orders = db.query(Order).count()

    monkeypatch.setattr(
        pdf_training_api,
        "parse_pdf_bytes",
        lambda *_args: _pipeline(_parsed(customer.id)),
    )
    monkeypatch.setattr(
        pdf_training_api,
        "match_import_draft",
        lambda _db, draft, customer_id: draft,
    )
    monkeypatch.setattr(
        pdf_customer_templates,
        "parse_pdf_bytes",
        lambda *_args: _pipeline(_parsed(customer.id)),
    )
    monkeypatch.setattr(
        pdf_customer_templates,
        "match_import_draft",
        lambda _db, draft, customer_id: draft,
    )

    detail = pdf_training_api.review_gold_sample(
        str(pending.id),
        pdf_training_api.GoldReviewPayload(status="approved"),
        db,
        admin,
    )
    draft_id = detail.learning_draft["id"]
    assert detail.learning_draft["sample_count"] == 3
    assert detail.learning_replay["can_activate"] is True
    assert detail.learning_replay["input_fingerprint"]

    repeated = pdf_training_api.run_sample_learning_loop(str(pending.id), db, admin)
    assert repeated.learning_draft["id"] == draft_id
    assert db.query(PdfOrderCustomerTemplate).filter_by(status="draft").count() == 1

    enabled = pdf_training_api.activate_template(
        draft_id,
        pdf_training_api.TemplateActivationPayload(),
        db,
        admin,
    )
    db.refresh(active)
    assert enabled.status == "active"
    assert active.status == "retired"
    assert db.query(Order).count() == before_orders == 0


def test_learning_draft_stays_blocked_with_fewer_than_three_gold_samples(
    learning_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, admin, customer, directory = learning_session
    sample = _add_sample(db, customer, directory, 1)
    monkeypatch.setattr(
        pdf_training_api,
        "parse_pdf_bytes",
        lambda *_args: _pipeline(_parsed(customer.id)),
    )
    monkeypatch.setattr(
        pdf_training_api,
        "match_import_draft",
        lambda _db, draft, customer_id: draft,
    )
    monkeypatch.setattr(
        pdf_customer_templates,
        "parse_pdf_bytes",
        lambda *_args: _pipeline(_parsed(customer.id)),
    )
    monkeypatch.setattr(
        pdf_customer_templates,
        "match_import_draft",
        lambda _db, draft, customer_id: draft,
    )

    detail = pdf_training_api.run_sample_learning_loop(str(sample.id), db, admin)

    assert detail.learning_replay["can_activate"] is False
    assert "至少需要 3 份同客户已批准金样本" in detail.learning_replay["reasons"]
    assert detail.learning_draft["status"] == "draft"
    assert db.query(PdfOrderCustomerTemplate).filter_by(status="active").count() == 0
