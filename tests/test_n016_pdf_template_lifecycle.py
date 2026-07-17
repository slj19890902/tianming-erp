from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.api import pdf_training as pdf_training_api
from app.models import Base
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.pdf_training import PdfOrderCustomerTemplate, PdfOrderTrainingSample
from app.models.user import User
from app.services import pdf_customer_templates
from app.services.pdf_parse_pipeline import PdfParsePipelineError, PdfParsePipelineResult
from app.services.order_pdf_import import PdfParseError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "bb55v8x9z45"
TARGET_REVISION = "bc56v8x9z46"


def _alembic_config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(path.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "n016-migration-test-only")
    monkeypatch.setenv("ERP_ENVIRONMENT", "test")
    return Config(str(PROJECT_ROOT / "alembic.ini"))


def _upgrade(monkeypatch: pytest.MonkeyPatch, path: Path, revision: str) -> None:
    command.upgrade(_alembic_config(monkeypatch, path), revision)


def _downgrade(monkeypatch: pytest.MonkeyPatch, path: Path, revision: str) -> None:
    command.downgrade(_alembic_config(monkeypatch, path), revision)


def _assert_integrity(connection: sqlite3.Connection, revision: str) -> None:
    assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (revision,)
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_n016_migration_maps_legacy_rows_roundtrips_and_enforces_one_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "n016-legacy.sqlite3"
    _upgrade(monkeypatch, path, PARENT_REVISION)
    with sqlite3.connect(path) as connection:
        customer_id = connection.execute("INSERT INTO customers (name) VALUES ('N016 客户')").lastrowid
        connection.executemany(
            """
            INSERT INTO pdf_order_customer_templates
                (customer_id, template_name, is_active, created_at, order_no_pattern,
                 date_pattern, item_row_pattern, customer_name_pattern, column_map_json, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    customer_id, "old-active", 1, "2026-01-01 00:00:00", "A",
                    "date-a", "item-a", "customer-a", '{"aliases":["A"]}', "note-a",
                ),
                (
                    customer_id, "old-inactive", 0, "2026-01-02 00:00:00", "B",
                    "date-b", "item-b", "customer-b", '{"aliases":["B"]}', "note-b",
                ),
            ],
        )
        connection.execute(
            """
            INSERT INTO pdf_order_training_samples
                (file_name, file_sha256, parse_status, parse_method)
            VALUES ('legacy.pdf', ?, 'reviewed', 'text')
            """,
            ("a" * 64,),
        )
        connection.commit()

    _upgrade(monkeypatch, path, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT template_name, status, version, is_active, order_no_pattern, date_pattern, "
            "item_row_pattern, customer_name_pattern, column_map_json, notes "
            "FROM pdf_order_customer_templates ORDER BY id"
        ).fetchall() == [
            ("old-active", "active", 1, 1, "A", "date-a", "item-a", "customer-a", '{"aliases":["A"]}', "note-a"),
            ("old-inactive", "retired", 2, 0, "B", "date-b", "item-b", "customer-b", '{"aliases":["B"]}', "note-b"),
        ]
        assert connection.execute(
            "SELECT gold_review_status FROM pdf_order_training_samples"
        ).fetchall() == [("pending",)]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO pdf_order_customer_templates
                    (customer_id, template_name, is_active, status, version)
                VALUES (?, 'second-active', 1, 'active', 3)
                """,
                (customer_id,),
            )
        _assert_integrity(connection, TARGET_REVISION)

    _downgrade(monkeypatch, path, PARENT_REVISION)
    with sqlite3.connect(path) as connection:
        assert "status" not in {row[1] for row in connection.execute("PRAGMA table_info(pdf_order_customer_templates)")}
        assert connection.execute(
            "SELECT template_name, is_active, order_no_pattern, date_pattern, item_row_pattern, "
            "customer_name_pattern, column_map_json, notes FROM pdf_order_customer_templates ORDER BY id"
        ).fetchall() == [
            ("old-active", 1, "A", "date-a", "item-a", "customer-a", '{"aliases":["A"]}', "note-a"),
            ("old-inactive", 0, "B", "date-b", "item-b", "customer-b", '{"aliases":["B"]}', "note-b"),
        ]
        _assert_integrity(connection, PARENT_REVISION)

    _upgrade(monkeypatch, path, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT template_name, status, version FROM pdf_order_customer_templates ORDER BY id"
        ).fetchall() == [("old-active", "active", 1), ("old-inactive", "retired", 2)]
        _assert_integrity(connection, TARGET_REVISION)


def test_n016_migration_refuses_legacy_multi_active(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "n016-multi-active.sqlite3"
    _upgrade(monkeypatch, path, PARENT_REVISION)
    with sqlite3.connect(path) as connection:
        customer_id = connection.execute("INSERT INTO customers (name) VALUES ('冲突客户')").lastrowid
        connection.executemany(
            "INSERT INTO pdf_order_customer_templates (customer_id, template_name, is_active) VALUES (?, ?, 1)",
            [(customer_id, "first"), (customer_id, "second")],
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="多个旧 is_active=true"):
        _upgrade(monkeypatch, path, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        _assert_integrity(connection, PARENT_REVISION)


def test_n016_downgrade_refuses_lifecycle_or_gold_facts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "n016-no-lossy-downgrade.sqlite3"
    _upgrade(monkeypatch, path, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO pdf_order_training_samples
                (file_name, file_sha256, parse_status, parse_method, gold_review_status)
            VALUES ('reviewed.pdf', ?, 'reviewed', 'text', 'approved')
            """,
            ("b" * 64,),
        )
        # The empty baseline contains no templates; a new version is independently
        # non-lossless even before it can be activated.
        connection.execute(
            "INSERT INTO pdf_order_customer_templates (template_name, is_active, status, version) "
            "VALUES ('new-version', 0, 'draft', 1)"
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="禁止破坏性降级"):
        _downgrade(monkeypatch, path, PARENT_REVISION)


def _truth() -> str:
    return json.dumps(
        {
            "order_no": "PO-N016-1",
            "customer_name": "N016 服务客户",
            "items": [
                {
                    "line_no": 1,
                    "product_code": "P-1",
                    "product_name": "纸箱",
                    "spec": "100*200*300",
                    "quantity": "10",
                    "unit": "个",
                    "unit_price": "2.50",
                    "amount": "25.00",
                    "delivery_date": "2026-07-20",
                }
            ],
        },
        ensure_ascii=False,
    )


def _parsed(
    customer_id: int,
    *,
    product_code: str = "P-1",
    product_name: str = "纸箱",
    spec: str = "100*200*300",
    route: str = "locked",
) -> dict:
    return {
        "customer_po": "PO-N016-1",
        "customer_name": "N016 服务客户",
        "customer_route": {
            "status": route,
            "template_customer_id": customer_id if route == "locked" else None,
        },
        "items": [
            {
                "line_no": 1,
                "product_code": product_code,
                "product_name": product_name,
                "spec": spec,
                "quantity": "10",
                "unit": "个",
                "unit_price": "2.50",
                "amount": "25.00",
                "delivery_date": "2026-07-20",
            }
        ],
    }


@pytest.fixture
def lifecycle_session(tmp_path: Path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'lifecycle.sqlite3'}")

    @event.listens_for(engine, "connect")
    def _foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker() as session:
        admin = User(
            username="n016-admin",
            password_hash="test-only",
            role="admin",
            real_name="N016 管理员",
            must_change_password=False,
        )
        customer = Customer(name="N016 服务客户")
        session.add_all([admin, customer])
        session.commit()
        yield session, admin, customer, tmp_path
    engine.dispose()


def _add_sample(
    session: Session,
    customer: Customer,
    directory: Path,
    index: int,
    *,
    gold_status: str = "approved",
) -> PdfOrderTrainingSample:
    content = f"PDF-{index}".encode()
    path = directory / f"sample-{index}.pdf"
    path.write_bytes(content)
    sample = PdfOrderTrainingSample(
        customer_id=customer.id,
        file_name=path.name,
        file_sha256=hashlib.sha256(content).hexdigest(),
        file_path=str(path),
        ground_truth_json=_truth(),
        parse_status="reviewed" if gold_status == "approved" else "labeled",
        parse_method="text",
        gold_review_status=gold_status,
    )
    session.add(sample)
    session.commit()
    return sample


def test_gold_review_and_readonly_dry_run_failure_gates(
    lifecycle_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, admin, customer, directory = lifecycle_session
    pending = _add_sample(session, customer, directory, 1, gold_status="pending")
    _add_sample(session, customer, directory, 2)
    _add_sample(session, customer, directory, 3)
    rejected = _add_sample(session, customer, directory, 4, gold_status="pending")
    pdf_training_api.review_gold_sample(
        str(pending.id),
        pdf_training_api.GoldReviewPayload(status="approved"),
        session,
        admin,
    )
    pdf_training_api.review_gold_sample(
        str(rejected.id),
        pdf_training_api.GoldReviewPayload(status="rejected", note="标注待补齐"),
        session,
        admin,
    )
    assert pending.gold_review_status == "approved"
    assert pending.parse_status == "reviewed"
    assert rejected.gold_review_status == "rejected"
    log_count = session.query(OperationLog).count()
    pdf_training_api.review_gold_sample(
        str(rejected.id),
        pdf_training_api.GoldReviewPayload(status="rejected", note="标注待补齐"),
        session,
        admin,
    )
    assert session.query(OperationLog).count() == log_count

    template = pdf_training_api.create_template(
        pdf_training_api.TemplateCreate(customer_id=customer.id, template_name="candidate"),
        session,
        admin,
    )

    def successful_pipeline(_content: bytes, _name: str, _rules: list[dict]):
        return PdfParsePipelineResult(
            draft=_parsed(customer.id),
            extracted_text="text",
            ocr_text_raw=None,
            parse_method="text",
            text_quality="readable_text",
        )

    monkeypatch.setattr(pdf_customer_templates, "parse_pdf_bytes", successful_pipeline)
    session.commit()
    database_path = directory / "lifecycle.sqlite3"
    before = (database_path.stat().st_mtime_ns, database_path.stat().st_size, hashlib.sha256(database_path.read_bytes()).hexdigest())
    evidence = pdf_customer_templates.activation_dry_run(session, template)
    after = (database_path.stat().st_mtime_ns, database_path.stat().st_size, hashlib.sha256(database_path.read_bytes()).hexdigest())
    assert evidence["can_activate"] is True
    assert evidence["evidence_hash"]
    assert before == after

    pending.gold_review_status = "rejected"
    session.commit()
    assert pdf_customer_templates.activation_dry_run(session, template)["can_activate"] is False
    pending.gold_review_status = "approved"
    pending.ground_truth_json = "{bad json"
    session.commit()
    assert pdf_customer_templates.activation_dry_run(session, template)["can_activate"] is False
    pending.ground_truth_json = _truth()
    session.commit()

    pending.file_path = str(directory / "missing.pdf")
    session.commit()
    assert pdf_customer_templates.activation_dry_run(session, template)["can_activate"] is False
    pending.file_path = str(directory / "sample-1.pdf")
    pending.file_sha256 = "0" * 64
    session.commit()
    assert pdf_customer_templates.activation_dry_run(session, template)["can_activate"] is False
    pending.file_sha256 = hashlib.sha256((directory / "sample-1.pdf").read_bytes()).hexdigest()
    session.commit()

    monkeypatch.setattr(
        pdf_customer_templates,
        "parse_pdf_bytes",
        lambda *_args: PdfParsePipelineResult(
            draft=_parsed(customer.id, route="needs_confirmation"),
            extracted_text="text", ocr_text_raw=None, parse_method="text", text_quality="readable_text"
        ),
    )
    assert pdf_customer_templates.activation_dry_run(session, template)["can_activate"] is False

    def _one_sample_with(**overrides: str):
        def parse_one(content: bytes, *_args):
            item_overrides = overrides if content == b"PDF-1" else {}
            return PdfParsePipelineResult(
                draft=_parsed(customer.id, **item_overrides),
                extracted_text="text",
                ocr_text_raw=None,
                parse_method="text",
                text_quality="readable_text",
            )
        return parse_one

    # A single bad name/spec still leaves its weighted score >= .90 and the
    # three-sample mean >= .95.  Only the explicit critical gate blocks it.
    for field, wrong in (("product_name", "错误品名"), ("spec", "999*999*999")):
        monkeypatch.setattr(
            pdf_customer_templates, "parse_pdf_bytes", _one_sample_with(**{field: wrong})
        )
        evidence = pdf_customer_templates.activation_dry_run(session, template)
        assert evidence["can_activate"] is False
        assert evidence["average_score"] >= 0.95
        assert evidence["sample_results"][0]["score"] >= 0.90
        assert evidence["sample_results"][0]["strict_checks"][
            f"items[0].{field}"
        ] is False

    monkeypatch.setattr(
        pdf_customer_templates,
        "parse_pdf_bytes",
        lambda *_args: PdfParsePipelineResult(
            draft=_parsed(customer.id, product_code="WRONG"),
            extracted_text="text", ocr_text_raw=None, parse_method="text", text_quality="readable_text"
        ),
    )
    assert pdf_customer_templates.activation_dry_run(session, template)["can_activate"] is False
    monkeypatch.setattr(
        pdf_customer_templates,
        "parse_pdf_bytes",
        lambda *_args: PdfParsePipelineResult(
            draft=_parsed(customer.id), extracted_text="", ocr_text_raw=None,
            parse_method="ocr_unavailable", text_quality="image_only"
        ),
    )
    assert pdf_customer_templates.activation_dry_run(session, template)["can_activate"] is False
    monkeypatch.setattr(
        pdf_customer_templates,
        "parse_pdf_bytes",
        lambda *_args: (_ for _ in ()).throw(PdfParsePipelineError(
            PdfParseError("forced parse failure", "failed")
        )),
    )
    assert pdf_customer_templates.activation_dry_run(session, template)["can_activate"] is False


def test_template_clone_edit_delete_activate_retire_and_shared_pipeline(
    lifecycle_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, admin, customer, directory = lifecycle_session
    for index in range(1, 4):
        _add_sample(session, customer, directory, index)
    legacy_inactive = PdfOrderCustomerTemplate(
        customer_id=customer.id,
        template_name="legacy inactive",
        is_active=False,
        status="retired",
        version=1,
    )
    session.add(legacy_inactive)
    session.commit()
    with pytest.raises(HTTPException, match="只有 draft"):
        pdf_training_api.update_template(
            legacy_inactive.id,
            pdf_training_api.TemplateCreate(
                customer_id=customer.id, template_name="must not update"
            ),
            session,
            admin,
        )
    with pytest.raises(HTTPException, match="只有 draft"):
        pdf_training_api.delete_template(legacy_inactive.id, session, admin)

    template = pdf_training_api.clone_template_draft(
        legacy_inactive.id,
        pdf_training_api.TemplateClonePayload(template_name="v2 from legacy inactive"),
        session,
        admin,
    )
    assert (template.status, template.version, template.supersedes_template_id) == (
        "draft", 2, legacy_inactive.id
    )
    with pytest.raises(HTTPException, match="不可更换客户"):
        pdf_training_api.update_template(
            template.id,
            pdf_training_api.TemplateCreate(customer_id=None, template_name="v1"),
            session,
            admin,
        )
    updated = pdf_training_api.update_template(
        template.id,
        pdf_training_api.TemplateCreate(customer_id=customer.id, template_name="v1 edited", notes="edited"),
        session,
        admin,
    )
    clone = pdf_training_api.clone_template_draft(
        updated.id,
        pdf_training_api.TemplateClonePayload(template_name="v2"),
        session,
        admin,
    )
    assert (clone.status, clone.version, clone.supersedes_template_id) == ("draft", 3, updated.id)
    with pytest.raises(HTTPException, match="后继版本"):
        pdf_training_api.delete_template(updated.id, session, admin)

    monkeypatch.setattr(
        pdf_customer_templates,
        "parse_pdf_bytes",
        lambda *_args: PdfParsePipelineResult(
            draft=_parsed(customer.id), extracted_text="text", ocr_text_raw=None,
            parse_method="text", text_quality="readable_text"
        ),
    )
    active = pdf_training_api.activate_template(
        updated.id,
        pdf_training_api.TemplateActivationPayload(reason="first evidence"),
        session,
        admin,
    )
    assert (active.status, active.is_active, active.evidence_json is not None) == ("active", True, True)
    with pytest.raises(HTTPException, match="只有 draft"):
        pdf_training_api.update_template(
            active.id,
            pdf_training_api.TemplateCreate(customer_id=customer.id, template_name="blocked"),
            session,
            admin,
        )
    second_active = pdf_training_api.activate_template(
        clone.id,
        pdf_training_api.TemplateActivationPayload(reason="replace evidence"),
        session,
        admin,
    )
    session.refresh(active)
    assert (active.status, active.is_active, second_active.status, second_active.is_active) == (
        "retired", False, "active", True
    )
    retired = pdf_training_api.retire_template(
        second_active.id,
        pdf_training_api.TemplateRetirePayload(reason="manual retirement"),
        session,
        admin,
    )
    assert (retired.status, retired.is_active) == ("retired", False)
    with pytest.raises(HTTPException, match="只有 draft"):
        pdf_training_api.delete_template(retired.id, session, admin)

    from app.api import orders as orders_api
    from app.services import pdf_parse_pipeline

    assert orders_api.parse_pdf_bytes is pdf_parse_pipeline.parse_pdf_bytes
    assert pdf_training_api.parse_pdf_bytes is pdf_parse_pipeline.parse_pdf_bytes


def test_unique_active_conflict_rolls_back_losing_session_and_audit(
    lifecycle_session,
) -> None:
    """Equivalent SQLite transaction-conflict test for the partial active index.

    SQLite serializes writers, so this deterministically exercises the losing
    commit after the winner is visible rather than relying on timing-sensitive
    simultaneous writes.  The production partial index is the same guard.
    """
    session, admin, customer, _directory = lifecycle_session
    session.execute(
        text(
            "CREATE UNIQUE INDEX uq_test_pdf_templates_one_active_customer "
            "ON pdf_order_customer_templates (customer_id) "
            "WHERE status = 'active' AND customer_id IS NOT NULL"
        )
    )
    first = PdfOrderCustomerTemplate(
        customer_id=customer.id,
        template_name="race first",
        status="draft",
        is_active=False,
        version=1,
    )
    second = PdfOrderCustomerTemplate(
        customer_id=customer.id,
        template_name="race second",
        status="draft",
        is_active=False,
        version=2,
    )
    session.add_all([first, second])
    session.commit()

    maker = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    winner = maker()
    loser = maker()
    try:
        winning_template = winner.get(PdfOrderCustomerTemplate, first.id)
        assert winning_template is not None
        winning_template.status = "active"
        winning_template.is_active = True
        pdf_training_api._log(winner, admin, "pdf_training.template.activate", "winner")
        winner.commit()

        losing_template = loser.get(PdfOrderCustomerTemplate, second.id)
        assert losing_template is not None
        losing_template.status = "active"
        losing_template.is_active = True
        pdf_training_api._log(loser, admin, "pdf_training.template.activate", "loser")
        with pytest.raises(HTTPException, match="模板生命周期约束"):
            pdf_training_api._commit_template_change(loser, "同客户 active 冲突")
        assert not loser.new and not loser.dirty
    finally:
        winner.close()
        loser.close()

    session.expire_all()
    templates = session.execute(
        select(PdfOrderCustomerTemplate)
        .where(PdfOrderCustomerTemplate.customer_id == customer.id)
        .order_by(PdfOrderCustomerTemplate.version)
    ).scalars().all()
    assert [(template.status, template.is_active) for template in templates] == [
        ("active", True),
        ("draft", False),
    ]
    assert session.query(OperationLog).filter(OperationLog.description.in_(["winner", "loser"])).count() == 1
