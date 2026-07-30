from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.version import (
    APP_CHANGES,
    APP_EXTERNAL_ACCEPTANCE_REQUIRED,
    APP_VERIFICATION_STEPS,
    APP_VERSION,
)
from scripts.admin import release_erp


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _database(path: Path, revision: str) -> Path:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE alembic_version (version_num TEXT NOT NULL);
            CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE products (id INTEGER PRIMARY KEY);
            CREATE TABLE sales_orders (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL REFERENCES customers(id)
            );
            CREATE TABLE sales_order_items (id INTEGER PRIMARY KEY);
            CREATE TABLE material_requisitions (id INTEGER PRIMARY KEY);
            CREATE TABLE incoming_receipts (id INTEGER PRIMARY KEY);
            CREATE TABLE sales_deliveries (id INTEGER PRIMARY KEY);
            CREATE TABLE finance_return_receipts (id INTEGER PRIMARY KEY);
            CREATE TABLE finance_statements (id INTEGER PRIMARY KEY);
            CREATE TABLE finance_invoices (id INTEGER PRIMARY KEY);
            CREATE TABLE users (id INTEGER PRIMARY KEY);
            CREATE TABLE operation_logs (id INTEGER PRIMARY KEY);
            """
        )
        connection.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
        connection.execute("INSERT INTO customers VALUES (1, '隔离测试客户')")
        connection.execute("INSERT INTO sales_orders VALUES (1, 1)")
        connection.commit()
    return path


def test_startup_check_is_read_only_and_rejects_revision_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected = "expected_revision"
    database = _database(tmp_path / "uat.sqlite3", expected)
    before = database.read_bytes()
    monkeypatch.setattr(release_erp, "code_revision", lambda _root=PROJECT_ROOT: expected)

    result = release_erp.check_startup(database, PROJECT_ROOT)

    assert result["ok"] is True
    assert result["mode"] == "read_only_startup_check"
    assert result["database"]["revision"] == expected
    assert database.read_bytes() == before

    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE alembic_version SET version_num = 'older_revision'")
        connection.commit()
    mismatched = database.read_bytes()
    with pytest.raises(release_erp.ReleaseGateError, match="普通启动不会自动迁移"):
        release_erp.check_startup(database, PROJECT_ROOT)
    assert database.read_bytes() == mismatched


def test_two_phase_release_requires_bound_token_and_unchanged_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    old_revision = "old_revision"
    expected_revision = "new_revision"
    expected_sha = "a" * 40
    database = _database(tmp_path / "formal.sqlite3", old_revision)
    source_before_prepare = database.read_bytes()

    monkeypatch.setattr(release_erp, "git_sha", lambda _root=PROJECT_ROOT: expected_sha)
    monkeypatch.setattr(
        release_erp,
        "code_revision",
        lambda _root=PROJECT_ROOT: expected_revision,
    )

    def fake_migrate(path: Path, revision: str, _root: Path = PROJECT_ROOT) -> None:
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE alembic_version SET version_num = ?", (revision,))
            connection.commit()

    monkeypatch.setattr(release_erp, "_run_migration", fake_migrate)
    report = tmp_path / "reports" / "release.json"
    plan = release_erp.prepare_release(
        database=database,
        backup_dir=tmp_path / "backups",
        rehearsal_dir=tmp_path / "rehearsals",
        report_path=report,
        expected_code_sha=expected_sha,
        expected_revision=expected_revision,
        expected_app_version=APP_VERSION,
        project_root=PROJECT_ROOT,
    )

    assert database.read_bytes() == source_before_prepare
    assert plan["status"] == "awaiting_human_approval"
    assert plan["source"]["revision"] == old_revision
    assert plan["backup"]["revision"] == old_revision
    assert plan["rehearsal"]["revision"] == expected_revision
    assert plan["source"]["core_counts"] == plan["rehearsal"]["core_counts"]
    assert plan["expected_app_version"] == APP_VERSION
    assert (
        plan["external_acceptance_required"]
        is APP_EXTERNAL_ACCEPTANCE_REQUIRED
        is True
    )
    assert plan["release_metadata"]["external_acceptance_required"] is True
    assert plan["release_metadata"]["changes"] == APP_CHANGES
    assert plan["release_metadata"]["verification_steps"] == APP_VERIFICATION_STEPS
    assert plan["automated_release_metadata_gate"] == "passed"
    assert plan["human_acceptance_status"] == "not_recorded"
    assert report.is_file()

    with pytest.raises(release_erp.ReleaseGateError, match="人工授权口令不匹配"):
        release_erp.apply_release(
            plan_path=report,
            approval_token="APPLY-WRONG",
            project_root=PROJECT_ROOT,
        )
    assert database.read_bytes() == source_before_prepare

    applied = release_erp.apply_release(
        plan_path=report,
        approval_token=plan["approval_token"],
        project_root=PROJECT_ROOT,
    )
    assert applied["status"] == "applied_pending_service_start"
    assert release_erp.inspect_database(database)["revision"] == expected_revision
    completed = release_erp.mark_service_started(report)
    assert completed["status"] == "completed"
    assert completed["human_acceptance_status"] == "not_recorded"


def test_apply_refuses_if_database_changed_after_prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    revision = "same_revision"
    expected_sha = "b" * 40
    database = _database(tmp_path / "formal.sqlite3", revision)
    monkeypatch.setattr(release_erp, "git_sha", lambda _root=PROJECT_ROOT: expected_sha)
    monkeypatch.setattr(release_erp, "code_revision", lambda _root=PROJECT_ROOT: revision)
    monkeypatch.setattr(release_erp, "_run_migration", lambda *_args, **_kwargs: None)
    report = tmp_path / "release.json"
    plan = release_erp.prepare_release(
        database=database,
        backup_dir=tmp_path / "backups",
        rehearsal_dir=tmp_path / "rehearsals",
        report_path=report,
        expected_code_sha=expected_sha,
        expected_revision=revision,
        expected_app_version=APP_VERSION,
        project_root=PROJECT_ROOT,
    )
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO customers VALUES (2, '准备后变化')")
        connection.commit()

    with pytest.raises(release_erp.ReleaseGateError, match="正式数据库 sha256 已变化"):
        release_erp.apply_release(
            plan_path=report,
            approval_token=plan["approval_token"],
            project_root=PROJECT_ROOT,
        )


@pytest.mark.parametrize(
    ("metadata_patch", "message"),
    [
        ({"changes": []}, "changes 在需要外部验收时必须包含 1～5 条"),
        (
            {"verification_steps": []},
            "verification_steps 在需要外部验收时必须包含 1～5 条",
        ),
        ({"changes": [" "]}, "changes 不能包含空白内容"),
        ({"changes": ("一条",)}, "changes 必须是列表"),
        ({"verification_steps": [123]}, "verification_steps 只能包含文字"),
        (
            {"changes": ["1", "2", "3", "4", "5", "6"]},
            "changes 在需要外部验收时必须包含 1～5 条",
        ),
        (
            {"external_acceptance_required": "false"},
            "external_acceptance_required 必须明确填写 true 或 false",
        ),
        ({"build_date": "2026/07/29"}, "build_date 必须使用 YYYY-MM-DD"),
    ],
)
def test_release_metadata_rejects_incomplete_or_invalid_content(
    metadata_patch: dict[str, object],
    message: str,
) -> None:
    from app.version import current_release_metadata, validate_release_metadata

    metadata = current_release_metadata()
    metadata.update(metadata_patch)

    with pytest.raises(ValueError, match=message):
        validate_release_metadata(metadata)


def test_release_metadata_allows_empty_user_acceptance_lists_when_not_required() -> None:
    from app.version import current_release_metadata, validate_release_metadata

    metadata = current_release_metadata()
    metadata.update(
        {
            "external_acceptance_required": False,
            "changes": [],
            "verification_steps": [],
        }
    )

    validated = validate_release_metadata(metadata)

    assert validated["external_acceptance_required"] is False
    assert validated["changes"] == []
    assert validated["verification_steps"] == []


def test_release_metadata_requires_explicit_external_acceptance_declaration() -> None:
    from app.version import current_release_metadata, validate_release_metadata

    metadata = current_release_metadata()
    metadata.pop("external_acceptance_required")

    with pytest.raises(
        ValueError,
        match="external_acceptance_required 必须明确填写 true 或 false",
    ):
        validate_release_metadata(metadata)


def test_release_metadata_still_limits_optional_user_acceptance_lists() -> None:
    from app.version import current_release_metadata, validate_release_metadata

    metadata = current_release_metadata()
    metadata.update(
        {
            "external_acceptance_required": False,
            "changes": ["1", "2", "3", "4", "5", "6"],
            "verification_steps": [],
        }
    )

    with pytest.raises(ValueError, match="changes 最多包含 5 条"):
        validate_release_metadata(metadata)


def test_release_metadata_rejects_expected_version_mismatch() -> None:
    with pytest.raises(release_erp.ReleaseGateError, match="版本号不匹配"):
        release_erp.release_metadata(expected_version="v9.9.9")


def test_apply_rejects_release_metadata_changed_after_prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    revision = "same_revision"
    expected_sha = "c" * 40
    database = _database(tmp_path / "formal.sqlite3", revision)
    monkeypatch.setattr(release_erp, "git_sha", lambda _root=PROJECT_ROOT: expected_sha)
    monkeypatch.setattr(release_erp, "code_revision", lambda _root=PROJECT_ROOT: revision)
    monkeypatch.setattr(release_erp, "_run_migration", lambda *_args, **_kwargs: None)
    report = tmp_path / "release.json"
    plan = release_erp.prepare_release(
        database=database,
        backup_dir=tmp_path / "backups",
        rehearsal_dir=tmp_path / "rehearsals",
        report_path=report,
        expected_code_sha=expected_sha,
        expected_revision=revision,
        expected_app_version=APP_VERSION,
        project_root=PROJECT_ROOT,
    )
    changed = {**plan["release_metadata"], "changes": ["准备后被修改"]}
    monkeypatch.setattr(
        release_erp,
        "release_metadata",
        lambda *, expected_version=None: changed,
    )

    with pytest.raises(release_erp.ReleaseGateError, match="正式版本说明已变化"):
        release_erp.apply_release(
            plan_path=report,
            approval_token=plan["approval_token"],
            project_root=PROJECT_ROOT,
        )


def test_mark_started_failure_does_not_claim_completed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    metadata = release_erp.release_metadata(expected_version=APP_VERSION)
    report = tmp_path / "release.json"
    report.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "status": "applied_pending_service_start",
                "expected_app_version": APP_VERSION,
                "release_metadata": metadata,
                "release_metadata_sha256": release_erp.release_metadata_sha256(metadata),
                "human_acceptance_status": "not_recorded",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        release_erp,
        "release_metadata",
        lambda *, expected_version=None: {
            **metadata,
            "verification_steps": ["服务启动后被修改"],
        },
    )

    with pytest.raises(release_erp.ReleaseGateError, match="与发布计划不一致"):
        release_erp.mark_service_started(report)

    stored = json.loads(report.read_text(encoding="utf-8"))
    assert stored["status"] == "service_started_release_metadata_failed"
    assert stored["human_acceptance_status"] == "not_recorded"


def test_uat_launcher_isolated_from_factory_database_and_port() -> None:
    source = (
        PROJECT_ROOT / "scripts" / "windows" / "start_erp_uat.ps1"
    ).read_text(encoding="utf-8")

    for marker in (
        'ERP_ENVIRONMENT = "test"',
        'ERP_BIND_HOST = "127.0.0.1"',
        "check-startup",
        "18000",
        "19999",
        "UAT 禁止连接工厂正式数据库",
        "UAT 禁止使用正式端口 8000",
        '"D:\\纸箱厂erp软件搭建\\data\\carton_erp.sqlite3"',
        "$PythonPath",
    ):
        assert marker in source
    assert "alembic upgrade" not in source
    assert '"-m", "alembic"' not in source
