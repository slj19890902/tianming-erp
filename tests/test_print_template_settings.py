from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def print_template_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.system import router as system_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.user import User

    database_path = tmp_path / "print-template.sqlite3"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("ERP_ENVIRONMENT", "test")
    monkeypatch.setenv("ERP_SECRET_KEY", "print-template-test-secret-" * 3)
    engine = create_sqlite_engine(database_path)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        session.add_all(
            [
                User(
                    username="admin",
                    password_hash=hash_password("RolePass123!"),
                    role="admin",
                    real_name="管理员",
                ),
                User(
                    username="finance",
                    password_hash=hash_password("RolePass123!"),
                    role="finance",
                    real_name="财务",
                ),
            ]
        )
        session.commit()

    app = FastAPI()

    def override_get_db():
        with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(system_router, prefix="/api/system")
    try:
        yield app, session_factory, database_path
    finally:
        engine.dispose()


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "RolePass123!"},
    )
    assert response.status_code == 200


def _settings_path() -> Path:
    from app.services.print_template_settings import print_template_settings_path

    return print_template_settings_path()


def test_registry_covers_all_eight_print_surfaces_with_expected_field_names() -> None:
    from app.services.print_template_settings import (
        DEFAULT_TEMPLATE_FIELDS,
        TEMPLATE_KEYS,
    )

    assert set(TEMPLATE_KEYS) == {
        "customer_contract",
        "customer_quotation",
        "legacy_requisition",
        "delivery_note",
        "mold_label",
        "customer_list",
        "supplier_purchase_order",
        "stock_replenishment_order",
    }
    for template_key in TEMPLATE_KEYS:
        fields = DEFAULT_TEMPLATE_FIELDS[template_key]
        assert {"document_title", "address_label", "phone_label", "date_label"} <= set(fields)
        assert all(isinstance(value, str) for value in fields.values())

    contract = DEFAULT_TEMPLATE_FIELDS["customer_contract"]
    assert contract["continuation_label"] == "续"
    assert contract["clause_3_payment_prefix"] == "双方按"
    assert contract["clause_3_body"].startswith("结算。")
    assert "{payment_terms}" not in contract["clause_3_body"]
    assert "column_remarks" in DEFAULT_TEMPLATE_FIELDS["legacy_requisition"]
    assert "column_remarks" in DEFAULT_TEMPLATE_FIELDS["supplier_purchase_order"]
    customer_list = DEFAULT_TEMPLATE_FIELDS["customer_list"]
    assert {
        "column_address",
        "column_credit_terms",
        "column_history_order_count",
    } <= set(customer_list)


def test_get_requires_login_returns_role_capability_and_never_creates_file(
    print_template_app,
) -> None:
    from app.services.print_template_settings import DEFAULT_TEMPLATE_FIELDS

    app, _, database_path = print_template_app
    settings_path = _settings_path()
    with TestClient(app) as client:
        anonymous = client.get("/api/system/print-template-settings/customer_contract")
        assert anonymous.status_code == 401
        _login(client, "finance")
        response = client.get("/api/system/print-template-settings/customer_contract")

    assert response.status_code == 200
    assert response.json() == {
        "template_key": "customer_contract",
        "revision": 0,
        "fields": DEFAULT_TEMPLATE_FIELDS["customer_contract"],
        "can_manage": False,
    }
    assert not settings_path.exists()


def test_admin_put_saves_only_overrides_and_audits_hash_not_full_text(
    print_template_app,
) -> None:
    from app.models.audit import OperationLog

    app, session_factory, database_path = print_template_app
    secret_text = "客户合同自定义标题-不要复制到审计"
    settings_path = _settings_path()
    with TestClient(app) as client:
        _login(client, "admin")
        initial = client.get("/api/system/print-template-settings/customer_contract")
        response = client.put(
            "/api/system/print-template-settings/customer_contract",
            json={
                "expected_revision": 0,
                "fields": {
                    "document_title": secret_text,
                    "clause_2_body": "第一行\n第二行",
                },
            },
        )
        reread = client.get("/api/system/print-template-settings/customer_contract")

    assert initial.json()["can_manage"] is True
    assert response.status_code == 200
    assert response.json()["revision"] == 1
    assert response.json()["can_manage"] is True
    assert response.json()["fields"]["document_title"] == secret_text
    assert reread.json() == response.json()

    stored = json.loads(settings_path.read_text(encoding="utf-8"))
    record = stored["templates"]["customer_contract"]
    assert record["revision"] == 1
    assert record["overrides"] == {
        "clause_2_body": "第一行\n第二行",
        "document_title": secret_text,
    }
    assert "fields" not in record

    with session_factory() as session:
        audit = session.query(OperationLog).filter_by(action="PRINT_TEMPLATE_UPDATE").one()
    details = json.loads(audit.details)
    assert details["template_key"] == "customer_contract"
    assert details["previous_revision"] == 0
    assert details["revision"] == 1
    assert details["changed_fields"] == ["clause_2_body", "document_title"]
    assert len(details["fields_sha256"]) == 64
    assert len(details["operation_id"]) == 32
    assert secret_text not in audit.details
    assert secret_text not in (audit.description or "")
    assert "第一行" not in audit.details


def test_same_content_is_noop_without_revision_or_second_audit(print_template_app) -> None:
    from app.models.audit import OperationLog

    app, session_factory, _ = print_template_app
    with TestClient(app) as client:
        _login(client, "admin")
        first = client.put(
            "/api/system/print-template-settings/customer_quotation",
            json={"expected_revision": 0, "fields": {"document_title": "新报价单"}},
        )
        second = client.put(
            "/api/system/print-template-settings/customer_quotation",
            json={"expected_revision": 1, "fields": {"document_title": "新报价单"}},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["revision"] == 1
    with session_factory() as session:
        count = (
            session.query(OperationLog)
            .filter_by(action="PRINT_TEMPLATE_UPDATE")
            .count()
        )
    assert count == 1


def test_cas_is_per_template_and_stale_write_is_409(print_template_app) -> None:
    app, _, _ = print_template_app
    with TestClient(app) as client:
        _login(client, "admin")
        contract = client.put(
            "/api/system/print-template-settings/customer_contract",
            json={"expected_revision": 0, "fields": {"document_title": "合同 A"}},
        )
        quotation = client.put(
            "/api/system/print-template-settings/customer_quotation",
            json={"expected_revision": 0, "fields": {"document_title": "报价 A"}},
        )
        stale = client.put(
            "/api/system/print-template-settings/customer_contract",
            json={"expected_revision": 0, "fields": {"document_title": "合同 B"}},
        )
        current = client.get("/api/system/print-template-settings/customer_contract")

    assert contract.json()["revision"] == 1
    assert quotation.json()["revision"] == 1
    assert stale.status_code == 409
    assert stale.json()["detail"]["current_revision"] == 1
    assert current.json()["fields"]["document_title"] == "合同 A"


def test_restore_resets_overrides_increments_revision_and_audits(print_template_app) -> None:
    from app.models.audit import OperationLog
    from app.services.print_template_settings import DEFAULT_TEMPLATE_FIELDS

    app, session_factory, database_path = print_template_app
    with TestClient(app) as client:
        _login(client, "admin")
        saved = client.put(
            "/api/system/print-template-settings/delivery_note",
            json={"expected_revision": 0, "fields": {"copies_text": "自定义三联文字"}},
        )
        restored = client.post(
            "/api/system/print-template-settings/delivery_note/restore",
            json={"expected_revision": saved.json()["revision"]},
        )
        stale = client.post(
            "/api/system/print-template-settings/delivery_note/restore",
            json={"expected_revision": 1},
        )

    assert restored.status_code == 200
    assert restored.json()["revision"] == 2
    assert restored.json()["fields"] == DEFAULT_TEMPLATE_FIELDS["delivery_note"]
    assert stale.status_code == 409
    stored = json.loads(
        _settings_path().read_text(encoding="utf-8")
    )
    assert stored["templates"]["delivery_note"] == {
        "overrides": {},
        "revision": 2,
    }
    with session_factory() as session:
        audit = session.query(OperationLog).filter_by(action="PRINT_TEMPLATE_RESTORE").one()
    assert "自定义三联文字" not in (audit.details or "")
    assert json.loads(audit.details)["revision"] == 2


def test_non_admin_can_read_but_cannot_put_or_restore(print_template_app) -> None:
    app, _, _ = print_template_app
    with TestClient(app) as client:
        _login(client, "finance")
        readable = client.get("/api/system/print-template-settings/mold_label")
        write = client.put(
            "/api/system/print-template-settings/mold_label",
            json={"expected_revision": 0, "fields": {"qr_prompt": "扫码"}},
        )
        restore = client.post(
            "/api/system/print-template-settings/mold_label/restore",
            json={"expected_revision": 0},
        )

    assert readable.status_code == 200
    assert readable.json()["can_manage"] is False
    assert write.status_code == 403
    assert restore.status_code == 403


@pytest.mark.parametrize(
    ("template_key", "fields"),
    [
        ("customer_contract", {}),
        ("customer_contract", {"unknown_field": "x"}),
        ("customer_contract", {"document_title": 123}),
        ("customer_contract", {"document_title": "坏字符\x00"}),
        ("customer_contract", {"document_title": "伪装\u202e文字"}),
        ("customer_contract", {"clause_2_body": "长" * 1001}),
    ],
)
def test_invalid_template_fields_are_422(print_template_app, template_key, fields) -> None:
    app, _, _ = print_template_app
    with TestClient(app) as client:
        _login(client, "admin")
        response = client.put(
            f"/api/system/print-template-settings/{template_key}",
            json={"expected_revision": 0, "fields": fields},
        )
    assert response.status_code == 422


def test_unknown_template_and_invalid_request_shape_are_422(print_template_app) -> None:
    app, _, _ = print_template_app
    with TestClient(app) as client:
        _login(client, "admin")
        unknown = client.get("/api/system/print-template-settings/not-a-template")
        negative = client.put(
            "/api/system/print-template-settings/customer_contract",
            json={"expected_revision": -1, "fields": {"document_title": "x"}},
        )
        extra = client.post(
            "/api/system/print-template-settings/customer_contract/restore",
            json={"expected_revision": 0, "unexpected": True},
        )
    assert unknown.status_code == 422
    assert negative.status_code == 422
    assert extra.status_code == 422


def test_corrupt_sidecar_and_atomic_write_failure_are_503(
    print_template_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.services.print_template_settings as service

    app, _, database_path = print_template_app
    settings_path = _settings_path()
    settings_path.write_text("{broken", encoding="utf-8")
    with TestClient(app) as client:
        _login(client, "admin")
        corrupt = client.get("/api/system/print-template-settings/customer_contract")
    assert corrupt.status_code == 503

    settings_path.unlink()

    def fail_write(_path, _store):
        raise service.PrintTemplateStorageError("simulated failure")

    monkeypatch.setattr(service, "_atomic_write_unlocked", fail_write)
    with TestClient(app) as client:
        _login(client, "admin")
        failed = client.put(
            "/api/system/print-template-settings/customer_contract",
            json={"expected_revision": 0, "fields": {"document_title": "新标题"}},
        )
    assert failed.status_code == 503
    assert not settings_path.exists()


def test_known_good_backup_repairs_corrupt_primary(print_template_app) -> None:
    app, _, _ = print_template_app
    settings_path = _settings_path()
    with TestClient(app) as client:
        _login(client, "admin")
        saved = client.put(
            "/api/system/print-template-settings/customer_contract",
            json={"expected_revision": 0, "fields": {"document_title": "可恢复标题"}},
        )
        assert saved.status_code == 200
        settings_path.write_text("{broken", encoding="utf-8")
        recovered = client.get(
            "/api/system/print-template-settings/customer_contract"
        )

    assert recovered.status_code == 200
    assert recovered.json()["revision"] == 1
    assert recovered.json()["fields"]["document_title"] == "可恢复标题"
    assert json.loads(settings_path.read_text(encoding="utf-8"))[
        "templates"
    ]["customer_contract"]["revision"] == 1


def test_audit_commit_failure_rolls_back_sidecar_and_returns_503(
    print_template_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.models.audit import OperationLog

    app, session_factory, _ = print_template_app
    settings_path = _settings_path()
    journal_path = settings_path.with_name(f"{settings_path.name}.txn.json")
    with TestClient(app) as client:
        _login(client, "admin")
        original_commit = session_factory.class_.commit

        def fail_print_template_audit_commit(session) -> None:
            if any(isinstance(item, OperationLog) for item in session.new):
                session.flush()
                raise RuntimeError("simulated audit commit failure")
            original_commit(session)

        monkeypatch.setattr(
            session_factory.class_, "commit", fail_print_template_audit_commit
        )
        response = client.put(
            "/api/system/print-template-settings/customer_contract",
            json={"expected_revision": 0, "fields": {"document_title": "不得残留"}},
        )

    assert response.status_code == 503
    assert not settings_path.exists()
    assert not settings_path.with_name(f"{settings_path.name}.bak").exists()
    assert not journal_path.exists()
    with session_factory() as session:
        assert session.query(OperationLog).filter_by(
            action="PRINT_TEMPLATE_UPDATE"
        ).count() == 0


def test_exception_after_durable_commit_keeps_sidecar_and_returns_success(
    print_template_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.models.audit import OperationLog

    app, session_factory, _ = print_template_app
    settings_path = _settings_path()
    journal_path = settings_path.with_name(f"{settings_path.name}.txn.json")
    with TestClient(app) as client:
        _login(client, "admin")
        original_commit = session_factory.class_.commit

        def commit_then_raise(session) -> None:
            target = any(
                isinstance(item, OperationLog)
                and item.action == "PRINT_TEMPLATE_UPDATE"
                for item in session.new
            )
            original_commit(session)
            if target:
                raise RuntimeError("simulated exception after durable commit")

        monkeypatch.setattr(session_factory.class_, "commit", commit_then_raise)
        response = client.put(
            "/api/system/print-template-settings/customer_contract",
            json={"expected_revision": 0, "fields": {"document_title": "已持久化标题"}},
        )

    assert response.status_code == 200
    assert response.json()["revision"] == 1
    assert response.json()["fields"]["document_title"] == "已持久化标题"
    assert not journal_path.exists()
    stored = json.loads(settings_path.read_text(encoding="utf-8"))
    assert stored["templates"]["customer_contract"]["revision"] == 1
    with session_factory() as session:
        audit = session.query(OperationLog).filter_by(
            action="PRINT_TEMPLATE_UPDATE"
        ).one()
    assert json.loads(audit.details)["operation_id"]


def test_commit_outcome_verification_failure_retains_journal_fail_closed(
    print_template_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.api.system as system_api
    from app.models.audit import OperationLog
    from app.services.print_template_settings import (
        PrintTemplateStorageError,
        get_print_template_settings,
    )

    app, session_factory, _ = print_template_app
    settings_path = _settings_path()
    journal_path = settings_path.with_name(f"{settings_path.name}.txn.json")
    with TestClient(app) as client:
        _login(client, "admin")
        original_commit = session_factory.class_.commit

        def fail_before_commit(session) -> None:
            if any(isinstance(item, OperationLog) for item in session.new):
                session.flush()
                raise RuntimeError("simulated commit uncertainty")
            original_commit(session)

        def fail_independent_verification(_db, _operation_id) -> bool:
            raise RuntimeError("simulated independent connection failure")

        monkeypatch.setattr(session_factory.class_, "commit", fail_before_commit)
        monkeypatch.setattr(
            system_api,
            "_print_template_audit_committed_independently",
            fail_independent_verification,
        )
        response = client.put(
            "/api/system/print-template-settings/customer_contract",
            json={"expected_revision": 0, "fields": {"document_title": "待判定标题"}},
        )

    assert response.status_code == 503
    assert settings_path.exists()
    assert journal_path.exists()
    assert json.loads(settings_path.read_text(encoding="utf-8"))[
        "templates"
    ]["customer_contract"]["revision"] == 1
    with pytest.raises(PrintTemplateStorageError, match="待恢复事务"):
        get_print_template_settings("customer_contract")
    with session_factory() as session:
        assert session.query(OperationLog).filter_by(
            action="PRINT_TEMPLATE_UPDATE"
        ).count() == 0


def test_restore_audit_commit_failure_preserves_last_committed_wording(
    print_template_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.models.audit import OperationLog

    app, session_factory, _ = print_template_app
    with TestClient(app) as client:
        _login(client, "admin")
        saved = client.put(
            "/api/system/print-template-settings/customer_contract",
            json={"expected_revision": 0, "fields": {"document_title": "已提交标题"}},
        )
        assert saved.status_code == 200
        original_commit = session_factory.class_.commit

        def fail_restore_audit_commit(session) -> None:
            if any(
                isinstance(item, OperationLog)
                and item.action == "PRINT_TEMPLATE_RESTORE"
                for item in session.new
            ):
                session.flush()
                raise RuntimeError("simulated restore audit commit failure")
            original_commit(session)

        monkeypatch.setattr(session_factory.class_, "commit", fail_restore_audit_commit)
        failed = client.post(
            "/api/system/print-template-settings/customer_contract/restore",
            json={"expected_revision": 1},
        )
        current = client.get(
            "/api/system/print-template-settings/customer_contract"
        )

    assert failed.status_code == 503
    assert current.status_code == 200
    assert current.json()["revision"] == 1
    assert current.json()["fields"]["document_title"] == "已提交标题"
    with session_factory() as session:
        assert session.query(OperationLog).filter_by(
            action="PRINT_TEMPLATE_UPDATE"
        ).count() == 1
        assert session.query(OperationLog).filter_by(
            action="PRINT_TEMPLATE_RESTORE"
        ).count() == 0


def test_path_tracks_database_directory_and_isolates_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.print_template_settings import (
        get_print_template_settings,
        print_template_settings_path,
        save_print_template_settings,
    )

    first_database = tmp_path / "copies" / "erp-a.sqlite3"
    second_database = tmp_path / "copies" / "erp-b.sqlite3"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(first_database))
    monkeypatch.setenv("ERP_ENVIRONMENT", "test")
    monkeypatch.setenv("ERP_SECRET_KEY", "path-test-secret-" * 4)
    first_path = print_template_settings_path()
    save_print_template_settings(
        "mold_label",
        expected_revision=0,
        fields={"qr_prompt": "第一副本"},
    )
    monkeypatch.setenv("ERP_DATABASE_PATH", str(second_database))
    second_path = print_template_settings_path()
    second = get_print_template_settings("mold_label")

    assert first_path.parent == first_database.parent
    assert second_path.parent == second_database.parent
    assert first_path.name.startswith("print_template_settings.")
    assert second_path.name.startswith("print_template_settings.")
    assert first_path != second_path
    assert second["revision"] == 0
    assert second["fields"]["qr_prompt"] == "扫码查询模具"
    assert not second_path.exists()


def test_nonempty_legacy_sidecar_requires_unambiguous_owner_in_shared_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.print_template_settings import (
        PrintTemplateStorageError,
        get_print_template_settings,
        print_template_settings_path,
    )

    directory = tmp_path / "shared"
    directory.mkdir()
    first_database = directory / "first.sqlite3"
    second_database = directory / "second.sqlite3"
    first_database.write_bytes(b"")
    second_database.write_bytes(b"")
    legacy_path = directory / "print_template_settings.json"
    legacy_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "templates": {
                    "customer_contract": {
                        "revision": 3,
                        "overrides": {"document_title": "旧侧车合同"},
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ERP_DATABASE_PATH", str(first_database))
    monkeypatch.setenv("ERP_ENVIRONMENT", "test")
    monkeypatch.setenv("ERP_SECRET_KEY", "legacy-owner-test-secret-" * 3)

    with pytest.raises(PrintTemplateStorageError, match="归属不明确"):
        get_print_template_settings("customer_contract")

    monkeypatch.setenv(
        "ERP_PRINT_TEMPLATE_LEGACY_DATABASE_PATH", str(first_database)
    )
    first = get_print_template_settings("customer_contract")
    first_path = print_template_settings_path()
    monkeypatch.setenv("ERP_DATABASE_PATH", str(second_database))
    second = get_print_template_settings("customer_contract")
    second_path = print_template_settings_path()

    assert first["revision"] == 3
    assert first["fields"]["document_title"] == "旧侧车合同"
    assert first_path.exists()
    assert json.loads(first_path.read_text(encoding="utf-8"))["database_identity"]
    assert second["revision"] == 0
    assert second["fields"]["document_title"] == "购货合同"
    assert not second_path.exists()
    assert legacy_path.exists()


def test_process_exit_recovery_uses_operation_log_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.print_template_settings import (
        PrintTemplateStorageError,
        get_print_template_settings,
        print_template_settings_path,
        recover_interrupted_print_template_transaction,
    )

    database_path = tmp_path / "crash" / "erp.sqlite3"
    operation_path = tmp_path / "operation-id.txt"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_ENVIRONMENT", "test")
    monkeypatch.setenv("ERP_SECRET_KEY", "crash-recovery-test-secret-" * 3)
    repository_root = Path(__file__).resolve().parents[1]
    child_code = """
import os
from pathlib import Path
from app.services.print_template_settings import staged_save_print_template_settings

with staged_save_print_template_settings(
    "customer_contract",
    expected_revision=0,
    fields={"document_title": "进程中断后的标题"},
) as mutation:
    Path(os.environ["OPERATION_ID_PATH"]).write_text(
        mutation.operation_id, encoding="utf-8"
    )
    os._exit(0)
"""
    child_environment = dict(os.environ)
    child_environment["ERP_DATABASE_PATH"] = str(database_path)
    child_environment["ERP_ENVIRONMENT"] = "test"
    child_environment["ERP_SECRET_KEY"] = "crash-recovery-test-secret-" * 3
    child_environment["OPERATION_ID_PATH"] = str(operation_path)
    child_environment["PYTHONPATH"] = os.pathsep.join(
        filter(
            None,
            [str(repository_root), child_environment.get("PYTHONPATH", "")],
        )
    )

    subprocess.run(
        [sys.executable, "-c", child_code],
        cwd=repository_root,
        env=child_environment,
        check=True,
        timeout=30,
    )
    operation_id = operation_path.read_text(encoding="utf-8")
    settings_path = print_template_settings_path()
    journal_path = settings_path.with_name(f"{settings_path.name}.txn.json")
    assert settings_path.exists()
    assert journal_path.exists()
    with pytest.raises(PrintTemplateStorageError, match="待恢复事务"):
        get_print_template_settings("customer_contract")

    rolled_back = recover_interrupted_print_template_transaction(lambda _: False)
    assert rolled_back == {
        "operation_id": operation_id,
        "outcome": "rolled_back",
    }
    assert not settings_path.exists()
    assert not journal_path.exists()
    assert get_print_template_settings("customer_contract")["revision"] == 0

    subprocess.run(
        [sys.executable, "-c", child_code],
        cwd=repository_root,
        env=child_environment,
        check=True,
        timeout=30,
    )
    committed_operation_id = operation_path.read_text(encoding="utf-8")
    committed = recover_interrupted_print_template_transaction(
        lambda candidate: candidate == committed_operation_id
    )
    current = get_print_template_settings("customer_contract")
    assert committed == {
        "operation_id": committed_operation_id,
        "outcome": "committed",
    }
    assert current["revision"] == 1
    assert current["fields"]["document_title"] == "进程中断后的标题"
    assert not journal_path.exists()


def test_thread_lock_allows_only_one_same_revision_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.print_template_settings import (
        PrintTemplateRevisionConflictError,
        get_print_template_settings,
        print_template_settings_path,
        save_print_template_settings,
    )

    database_path = tmp_path / "threaded" / "erp.sqlite3"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_ENVIRONMENT", "test")
    monkeypatch.setenv("ERP_SECRET_KEY", "thread-test-secret-" * 4)

    def attempt(index: int) -> str:
        try:
            save_print_template_settings(
                "customer_contract",
                expected_revision=0,
                fields={"document_title": f"合同-{index}"},
            )
            return "saved"
        except PrintTemplateRevisionConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(attempt, range(10)))

    assert results.count("saved") == 1
    assert results.count("conflict") == 9
    current = get_print_template_settings("customer_contract")
    assert current["revision"] == 1
    path = print_template_settings_path()
    parsed = json.loads(path.read_text(encoding="utf-8"))
    assert parsed["templates"]["customer_contract"]["revision"] == 1
    assert not list(path.parent.glob(".print_template_settings.*.tmp"))


def test_process_lock_allows_only_one_same_revision_writer(tmp_path: Path) -> None:
    database_path = tmp_path / "processes" / "erp.sqlite3"
    repository_root = Path(__file__).resolve().parents[1]
    child_code = """
import sys
from app.services.print_template_settings import (
    PrintTemplateRevisionConflictError,
    save_print_template_settings,
)

try:
    save_print_template_settings(
        "customer_contract",
        expected_revision=0,
        fields={"document_title": sys.argv[1]},
    )
except PrintTemplateRevisionConflictError:
    raise SystemExit(3)
"""
    child_environment = dict(os.environ)
    child_environment["ERP_DATABASE_PATH"] = str(database_path)
    child_environment["ERP_ENVIRONMENT"] = "test"
    child_environment["ERP_SECRET_KEY"] = "process-lock-test-secret-" * 3
    child_environment["PYTHONPATH"] = os.pathsep.join(
        filter(
            None,
            [str(repository_root), child_environment.get("PYTHONPATH", "")],
        )
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", child_code, f"合同-{index}"],
            cwd=repository_root,
            env=child_environment,
        )
        for index in range(8)
    ]
    return_codes = [process.wait(timeout=30) for process in processes]

    assert return_codes.count(0) == 1
    assert return_codes.count(3) == 7
