from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.admin import (
    release_erp,
    release_state_common,
    rollback_execute,
    rollback_runtime,
)


REVISION = "cr74v8x9z63"
CURRENT_SHA = "2" * 40
PREVIOUS_SHA = "1" * 40


@pytest.fixture(autouse=True)
def _fixed_release_evidence_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        release_state_common,
        "_release_evidence_key",
        lambda _root: b"test-release-evidence-key".ljust(32, b"!"),
    )


def _database(path: Path, revision: str = REVISION) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE alembic_version (version_num TEXT NOT NULL);
            CREATE TABLE customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL
            );
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
        connection.execute("INSERT INTO customers(name) VALUES ('隔离测试客户')")
        connection.execute("INSERT INTO sales_orders VALUES (1, 1)")
        connection.commit()
    return path


def _write_signed(
    path: Path,
    payload: dict,
    *,
    purpose: str,
    project_root: Path,
) -> dict:
    return rollback_execute._write_signed_atomic(
        path,
        payload,
        project_root=project_root,
        purpose=purpose,
        replace=False,
    )


def _future(minutes: int = 30) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(minutes=minutes)
    ).isoformat(timespec="seconds")


def _plan_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str = "code_only",
    approval_token: str = "ROLLBACK-primary-token",
    database_token: str | None = None,
    expires_at: str | None = None,
    plan_id: str = "plan-a",
) -> tuple[Path, Path, dict]:
    project_root = tmp_path / f"formal-{plan_id}"
    project_root.mkdir()
    evidence_root = tmp_path / f"evidence-{plan_id}"
    plan_path = evidence_root / "switch_plan.json"
    ticket_path = evidence_root / "execution_ticket.json"
    activation_claim_path, revocation_path = (
        rollback_execute._activation_state_paths(plan_id, project_root)
    )
    formal_state = {
        "branch": "factory-current-baseline",
        "head": CURRENT_SHA,
        "tracked_status": "",
    }
    plan = {
        "schema_version": 1,
        "status": "awaiting_switch_approval",
        "execution_eligible": True,
        "plan_id": plan_id,
        "plan_path": str(plan_path.resolve()),
        "expires_at": expires_at or _future(),
        "mode": mode,
        "requires_database_approval": mode == "full_rollback",
        "approval_token_digest": rollback_execute._token_digest(
            purpose="switch",
            plan_id=plan_id,
            token=approval_token,
        ),
        "database_approval_token_digest": (
            rollback_execute._token_digest(
                purpose="database",
                plan_id=plan_id,
                token=database_token or "",
            )
            if mode == "full_rollback"
            else None
        ),
        "consumption_path": str(
            (
                project_root
                / "data"
                / "release_state"
                / "rollback_consumptions"
                / f"{plan_id}.json"
            ).resolve()
        ),
        "ticket_path": str(ticket_path.resolve()),
        "activation_claim_path": str(activation_claim_path),
        "revocation_path": str(revocation_path),
        "activation_event_path": str(
            (evidence_root / "activation_event.json").resolve()
        ),
        "validation_event_path": str(
            (evidence_root / "loopback_validation_event.json").resolve()
        ),
        "auto_restore_permit_path": str(
            (evidence_root / "auto_restore_permit.json").resolve()
        ),
        "auto_restore_permit_consumed_path": str(
            (
                evidence_root
                / "auto_restore_permit.consumed.json"
            ).resolve()
        ),
        "production_exposure_intent_path": str(
            (evidence_root / "production_exposure_intent.json").resolve()
        ),
        "production_exposure_path": str(
            (
                project_root
                / "data"
                / "release_state"
                / "rollback_activations"
                / f"{plan_id}.exposed.json"
            ).resolve()
        ),
        "restoration_event_path": str(
            (evidence_root / "restoration_event.json").resolve()
        ),
        "result_path": str((evidence_root / "switch_result.json").resolve()),
        "active_pointer_path": str(
            (
                project_root
                / "data"
                / "release_state"
                / "active_runtime.json"
            ).resolve()
        ),
        "formal_checkout": formal_state,
        "formal_database": {
            "path": str((evidence_root / "formal.sqlite3").resolve())
        },
        "rollback_site_backup": {
            "path": str((evidence_root / "site-backup.sqlite3").resolve())
        },
        "candidate_database": {
            "path": str((evidence_root / "candidate.sqlite3").resolve())
        },
        "current_runtime": {"runtime_id": "current-runtime"},
        "target_runtime": {"runtime_id": "rollback-runtime"},
        "service": {"port": 18000},
        "validation_service": {
            "bind_host": "127.0.0.1",
            "port": 18001,
            "workers": 1,
            "local_health_url": "http://127.0.0.1:18001/api/health",
        },
    }
    _write_signed(
        Path(plan["auto_restore_permit_path"]),
        {
            "schema_version": 1,
            "status": "pre_exposure_auto_restore_permitted",
            "plan_id": plan_id,
            "runtime_id": plan["target_runtime"]["runtime_id"],
            "permit_path": plan["auto_restore_permit_path"],
            "consumed_path": plan["auto_restore_permit_consumed_path"],
        },
        purpose=rollback_execute.AUTO_RESTORE_PERMIT_PURPOSE,
        project_root=project_root,
    )
    _write_signed(
        plan_path,
        plan,
        purpose=rollback_execute.PLAN_PURPOSE,
        project_root=project_root,
    )
    monkeypatch.setattr(
        rollback_execute,
        "_revalidate_plan",
        lambda *_args, **_kwargs: None,
    )
    return project_root, plan_path, plan


def _authorize(
    project_root: Path,
    plan_path: Path,
    *,
    approval_token: str = "ROLLBACK-primary-token",
    database_token: str | None = None,
) -> Path:
    result = rollback_execute.authorize_switch(
        plan_path=plan_path,
        approval_token=approval_token,
        database_approval_token=database_token,
        project_root=project_root,
    )
    return Path(result["ticket_path"])


@pytest.mark.parametrize("mode", ["code_only", "full_rollback"])
def test_prepare_copies_site_backup_and_candidate_without_overwriting_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
) -> None:
    project_root = tmp_path / "formal-checkout"
    project_root.mkdir()
    formal_database = _database(
        project_root / "data" / "carton_erp.sqlite3"
    )
    release_backup = _database(
        tmp_path / "release-evidence" / "before-update.sqlite3"
    )
    with sqlite3.connect(release_backup) as connection:
        connection.execute(
            "INSERT INTO customers(name) VALUES ('更新前备份独有行')"
        )
        connection.commit()
    stage1_runtime = tmp_path / "stage1" / "runtime"
    stage1_runtime.mkdir(parents=True)
    (stage1_runtime / "app.py").write_text(
        "APP_MARKER = 'old-runtime'\n",
        encoding="utf-8",
    )
    runtime_manifest_path = tmp_path / "stage1" / "runtime_manifest.json"
    runtime_manifest_path.write_text(
        json.dumps({"python_environment": {"sha256": "python-env"}}),
        encoding="utf-8",
    )
    release_plan_path = tmp_path / "release-evidence" / "release_plan.json"
    release_plan_path.write_text("{}\n", encoding="utf-8")
    compatibility_path = tmp_path / "stage1" / "compatibility.json"
    compatibility_path.write_text("{}\n", encoding="utf-8")

    formal_before = rollback_execute._database_snapshot(
        formal_database,
        label="测试正式库",
    )
    release_backup_hash = release_erp.sha256_file(release_backup)
    formal_hash = release_erp.sha256_file(formal_database)
    config_fingerprint = {"sha256": "runtime-config"}
    report = {
        "mode": mode,
        "mode_label": mode,
        "release_code_sha": CURRENT_SHA,
        "previous_code_sha": PREVIOUS_SHA,
        "previous_code_revision": REVISION,
        "formal_database": str(formal_database.resolve()),
        "formal_database_logical_fingerprint": formal_before[
            "logical_fingerprint"
        ],
        "runtime_dir": str(stage1_runtime.resolve()),
        "runtime_manifest_path": str(runtime_manifest_path.resolve()),
        "evidence_nonce": "random-stage1-evidence",
        "plan_path": str(release_plan_path.resolve()),
        "runtime_environment": {
            "bind_host": "127.0.0.1",
            "port": 18120 if mode == "code_only" else 18121,
            "workers": 1,
        },
    }
    release_plan = {
        "completed_runtime_config": config_fingerprint,
        "prepared_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "backup": {
            "path": str(release_backup.resolve()),
            "sha256": release_backup_hash,
        },
    }
    monkeypatch.setattr(
        rollback_execute,
        "_load_compatibility_report",
        lambda *_args, **_kwargs: (report, release_plan),
    )
    checkout_state = {
        "branch": "factory-current-baseline",
        "head": CURRENT_SHA,
        "tracked_status": "",
    }
    monkeypatch.setattr(
        rollback_execute,
        "_assert_formal_checkout",
        lambda *_args, **_kwargs: checkout_state,
    )
    monkeypatch.setattr(
        rollback_execute,
        "runtime_config_fingerprint",
        lambda *_args: config_fingerprint,
    )
    monkeypatch.setattr(
        rollback_execute,
        "_controller_manifest",
        lambda *_args: {"sha256": "controller"},
    )
    monkeypatch.setattr(
        rollback_execute,
        "_runtime_service_config",
        lambda *_args: {
            "database_path": str(formal_database.resolve()),
            "bind_host": "0.0.0.0",
            "port": 8000,
            "workers": 1,
            "environment": "production",
            "production_transport": "lan_http",
            "local_health_url": "http://127.0.0.1:8000/api/health",
            "health_url": "http://127.0.0.1:8000/api/health",
            "browser_url": "http://127.0.0.1:8000/",
        },
    )
    monkeypatch.setattr(
        release_erp,
        "code_revision",
        lambda *_args: REVISION,
    )

    def fake_activation(
        _source: Path,
        destination: Path,
        _root: Path,
    ) -> dict:
        destination.mkdir(parents=True)
        (destination / "app.py").write_text(
            "APP_MARKER = 'activation-copy'\n",
            encoding="utf-8",
        )
        return rollback_execute._immutable_runtime_manifest(destination)

    monkeypatch.setattr(
        rollback_execute,
        "_prepare_activation_runtime",
        fake_activation,
    )
    monkeypatch.setattr(
        rollback_runtime,
        "_python_environment",
        lambda *_args, **_kwargs: {"sha256": "python-env"},
    )

    output_root = (
        project_root.parent
        / "tm-rollback-runtimes"
        / f"prepare-{mode}"
    )
    plan_path = output_root / "switch_plan.json"
    result = rollback_execute.prepare_switch(
        compatibility_report_path=compatibility_path,
        active_pointer_path=(
            project_root
            / "data"
            / "release_state"
            / "active_runtime.json"
        ),
        output_root=output_root,
        plan_path=plan_path,
        project_root=project_root,
    )

    site_backup = Path(result["rollback_site_backup"]["path"])
    candidate = Path(result["candidate_database"]["path"])
    assert site_backup.is_file()
    assert candidate.is_file()
    assert len({formal_database.resolve(), site_backup, candidate}) == 3
    assert release_erp.sha256_file(formal_database) == formal_hash
    assert release_erp.sha256_file(release_backup) == release_backup_hash
    assert result["formal_database_unchanged"] is True
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    release_state_common.verify_evidence(
        plan,
        project_root=project_root,
        purpose=rollback_execute.PLAN_PURPOSE,
    )
    assert plan["target_runtime"]["previous_pointer_sha256"] == hashlib.sha256(
        rollback_execute._canonical_bytes(plan["current_runtime"])
    ).hexdigest()
    validation = plan["validation_environment"]
    validation_overrides = validation["environment_overrides"]
    validation_root = Path(validation["root"])
    assert validation_root == (output_root / "validation").resolve()
    assert Path(validation_overrides["ERP_SECRET_KEY_FILE"]).is_file()
    assert Path(validation_overrides["ERP_FILE_STORAGE_DIR"]).is_dir()
    assert Path(validation_overrides["ERP_UPLOAD_TEMP_DIR"]).is_dir()
    assert Path(validation_overrides["ERP_BACKUP_DIR"]).is_dir()
    assert all(
        Path(validation_overrides[name]).resolve().is_relative_to(output_root)
        for name in (
            "ERP_DATABASE_PATH",
            "ERP_SECRET_KEY_FILE",
            "ERP_FILE_STORAGE_DIR",
            "ERP_UPLOAD_TEMP_DIR",
            "ERP_BACKUP_DIR",
        )
    )
    assert validation_overrides["ERP_FILE_STORAGE_DIR"] != str(
        (project_root / "data" / "private_uploads").resolve()
    )
    serialized = plan_path.read_text(encoding="utf-8")
    assert result["approval_token"] not in serialized
    if mode == "full_rollback":
        assert result["database_approval_token"]
        assert result["database_approval_token"] not in serialized
        assert (
            rollback_execute._database_snapshot(
                candidate,
                label="完整回退候选",
            )["logical_fingerprint"]
            == rollback_execute._database_snapshot(
                release_backup,
                label="更新前备份",
            )["logical_fingerprint"]
        )
    else:
        assert result["database_approval_token"] is None
        assert (
            rollback_execute._database_snapshot(
                candidate,
                label="仅代码候选",
            )["logical_fingerprint"]
            == rollback_execute._database_snapshot(
                site_backup,
                label="现场备份",
            )["logical_fingerprint"]
        )
    authorized = rollback_execute.authorize_switch(
        plan_path=plan_path,
        approval_token=result["approval_token"],
        database_approval_token=result["database_approval_token"],
        project_root=project_root,
    )
    assert authorized["status"] == "authorized_pending_service_stop"


def test_authorize_rejects_wrong_token_and_requires_independent_second_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_token = "DATABASE-second-token"
    project_root, plan_path, plan = _plan_fixture(
        tmp_path,
        monkeypatch,
        mode="full_rollback",
        database_token=database_token,
    )
    ticket_path = Path(plan["ticket_path"])

    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="一次授权口令不匹配",
    ):
        rollback_execute.authorize_switch(
            plan_path=plan_path,
            approval_token="wrong",
            database_approval_token=database_token,
            project_root=project_root,
        )
    assert not ticket_path.exists()

    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="第二个数据库授权口令",
    ):
        rollback_execute.authorize_switch(
            plan_path=plan_path,
            approval_token="ROLLBACK-primary-token",
            project_root=project_root,
        )
    assert not ticket_path.exists()

    result = rollback_execute.authorize_switch(
        plan_path=plan_path,
        approval_token="ROLLBACK-primary-token",
        database_approval_token=database_token,
        project_root=project_root,
    )
    assert result["status"] == "authorized_pending_service_stop"
    ticket = json.loads(ticket_path.read_text(encoding="utf-8"))
    release_state_common.verify_evidence(
        ticket,
        project_root=project_root,
        purpose=rollback_execute.TICKET_PURPOSE,
    )
    assert "ROLLBACK-primary-token" not in ticket_path.read_text(encoding="utf-8")
    assert database_token not in ticket_path.read_text(encoding="utf-8")


def test_authorization_token_is_plan_bound_and_consumed_once_even_if_ticket_deleted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root, plan_path, plan = _plan_fixture(
        tmp_path,
        monkeypatch,
        plan_id="first-plan",
    )
    ticket_path = _authorize(project_root, plan_path)
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="已存在|重放|消费",
    ):
        _authorize(project_root, plan_path)

    ticket_path.unlink()
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="重放|消费",
    ):
        _authorize(project_root, plan_path)

    other_root, other_plan_path, _ = _plan_fixture(
        tmp_path,
        monkeypatch,
        approval_token="DIFFERENT-plan-token",
        plan_id="second-plan",
    )
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="一次授权口令不匹配",
    ):
        rollback_execute.authorize_switch(
            plan_path=other_plan_path,
            approval_token="ROLLBACK-primary-token",
            project_root=other_root,
        )


def test_plan_tampering_and_expiration_block_before_ticket_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expired_root, expired_path, expired_plan = _plan_fixture(
        tmp_path,
        monkeypatch,
        expires_at=(
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat(timespec="seconds"),
        plan_id="expired",
    )
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="已过期",
    ):
        rollback_execute.authorize_switch(
            plan_path=expired_path,
            approval_token="ROLLBACK-primary-token",
            project_root=expired_root,
        )
    assert not Path(expired_plan["ticket_path"]).exists()

    tampered_root, tampered_path, tampered_plan = _plan_fixture(
        tmp_path,
        monkeypatch,
        plan_id="tampered",
    )
    payload = json.loads(tampered_path.read_text(encoding="utf-8"))
    payload["mode"] = "full_rollback"
    tampered_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="签名验证失败",
    ):
        rollback_execute.authorize_switch(
            plan_path=tampered_path,
            approval_token="ROLLBACK-primary-token",
            project_root=tampered_root,
        )
    assert not Path(tampered_plan["ticket_path"]).exists()


def test_stage1_report_tampering_and_expiration_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "formal"
    project_root.mkdir()
    release_plan_path = tmp_path / "release_plan.json"
    _write_signed(
        release_plan_path,
        {"schema_version": 3, "status": "completed"},
        purpose=rollback_runtime.PLAN_PURPOSE,
        project_root=project_root,
    )
    monkeypatch.setattr(
        rollback_runtime,
        "verify_compatibility",
        lambda **_kwargs: {"status": "compatibility_verified"},
    )

    def report_payload(expires_at: str) -> dict:
        return {
            "schema_version": 1,
            "status": "compatibility_ready",
            "execution_eligible": False,
            "mode": "code_only",
            "evidence_nonce": "nonce",
            "expires_at": expires_at,
            "plan_path": str(release_plan_path.resolve()),
        }

    expired = tmp_path / "expired_compatibility.json"
    _write_signed(
        expired,
        report_payload(
            (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat(timespec="seconds")
        ),
        purpose=rollback_runtime.COMPATIBILITY_REPORT_PURPOSE,
        project_root=project_root,
    )
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="已过期",
    ):
        rollback_execute._load_compatibility_report(
            expired,
            project_root=project_root,
        )

    tampered = tmp_path / "tampered_compatibility.json"
    _write_signed(
        tampered,
        report_payload(_future()),
        purpose=rollback_runtime.COMPATIBILITY_REPORT_PURPOSE,
        project_root=project_root,
    )
    payload = json.loads(tampered.read_text(encoding="utf-8"))
    payload["evidence_nonce"] = "changed"
    tampered.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="签名验证失败",
    ):
        rollback_execute._load_compatibility_report(
            tampered,
            project_root=project_root,
        )


def _archive_pointer_fixture(
    tmp_path: Path,
    *,
    runtime_dir: Path | None = None,
    database: Path | None = None,
) -> tuple[Path, Path, dict]:
    project_root = tmp_path / "formal"
    project_root.mkdir(parents=True, exist_ok=True)
    for relative in rollback_execute.CONTROLLER_FILES:
        controller_file = project_root / relative
        controller_file.parent.mkdir(parents=True, exist_ok=True)
        controller_file.write_text(
            f"# controller fixture: {relative}\n",
            encoding="utf-8",
        )
    subprocess.run(
        ["git", "init", "-b", "factory-current-baseline"],
        cwd=project_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "add", "."],
        cwd=project_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Codex Test",
            "-c",
            "user.email=codex-test@example.invalid",
            "commit",
            "-m",
            "test: controller fixture",
        ],
        cwd=project_root,
        check=True,
        capture_output=True,
    )
    allowed_root = project_root.parent / "tm-rollback-runtimes" / "run"
    selected_runtime = runtime_dir or (
        allowed_root / "activation" / "runtime"
    )
    selected_runtime.mkdir(parents=True, exist_ok=True)
    (selected_runtime / "app.py").write_text(
        "APP_MARKER = 'signed-runtime'\n",
        encoding="utf-8",
    )
    selected_database = database or _database(
        allowed_root / "databases" / "rollback.sqlite3"
    )
    snapshot = rollback_execute._database_snapshot(
        selected_database,
        label="活动数据库",
    )
    pointer_path = (
        project_root / "data" / "release_state" / "active_runtime.json"
    )
    plan_id = "pointer-fixture"
    claim_path, revocation_path = rollback_execute._activation_state_paths(
        plan_id,
        project_root,
    )
    run_root = selected_runtime.resolve().parent.parent
    validation_event_path = run_root / "loopback_validation_event.json"
    auto_restore_permit_path = run_root / "auto_restore_permit.json"
    auto_restore_permit_consumed_path = (
        run_root / "auto_restore_permit.consumed.json"
    )
    exposure_intent_path = run_root / "production_exposure_intent.json"
    exposure_path = claim_path.with_name(f"{plan_id}.exposed.json")
    pointer = {
        "schema_version": 1,
        "pointer_version": 2,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "runtime_id": "rollback-runtime",
        "runtime_kind": "archive_activation",
        "runtime_dir": str(selected_runtime.resolve()),
        "code_sha": PREVIOUS_SHA,
        "code_revision": REVISION,
        "database_path": str(selected_database.resolve()),
        "database_activation_snapshot": snapshot,
        "python_path": str(Path(sys.executable).resolve()),
        "python_environment": rollback_runtime._python_environment(
            project_root,
            Path(sys.executable),
        ),
        "service": {
            "bind_host": "0.0.0.0",
            "port": 8000,
            "workers": 1,
            "environment": "production",
            "production_transport": "lan_http",
            "local_health_url": "http://127.0.0.1:8000/api/health",
            "health_url": "http://127.0.0.1:8000/api/health",
            "browser_url": "http://127.0.0.1:8000/",
        },
        "environment_overrides": rollback_execute._safe_environment_overrides(
            project_root,
            selected_database,
        ),
        "immutable_manifest": rollback_execute._immutable_runtime_manifest(
            selected_runtime
        ),
        "checkout_state": rollback_execute._checkout_state(project_root),
        "controller_manifest": rollback_execute._controller_manifest(
            project_root
        ),
        "previous_pointer_sha256": "0" * 64,
        "activation_plan_id": plan_id,
        "activation_claim_path": str(claim_path),
        "revocation_path": str(revocation_path),
        "validation_event_path": str(validation_event_path.resolve()),
        "auto_restore_permit_path": str(
            auto_restore_permit_path.resolve()
        ),
        "auto_restore_permit_consumed_path": str(
            auto_restore_permit_consumed_path.resolve()
        ),
        "production_exposure_intent_path": str(
            exposure_intent_path.resolve()
        ),
        "production_exposure_path": str(exposure_path.resolve()),
    }
    pointer_digest = hashlib.sha256(
        rollback_execute._canonical_bytes(pointer)
    ).hexdigest()
    _write_signed(
        claim_path,
        {
            "schema_version": 1,
            "status": "activation_claimed",
            "plan_id": plan_id,
            "ticket_id": "pointer-fixture-ticket",
            "runtime_id": pointer["runtime_id"],
            "target_pointer_sha256": pointer_digest,
        },
        purpose=rollback_execute.ACTIVATION_CLAIM_PURPOSE,
        project_root=project_root,
    )
    _write_signed(
        validation_event_path,
        {
            "schema_version": 1,
            "status": "loopback_validation_completed",
            "plan_id": plan_id,
            "ticket_id": "pointer-fixture-ticket",
            "runtime_id": pointer["runtime_id"],
            "network_exposure": "loopback_only",
            "health": {
                "status_code": 200,
                "body": {"ok": True},
                "url": "http://127.0.0.1:18161/api/health",
            },
        },
        purpose=rollback_execute.VALIDATION_PURPOSE,
        project_root=project_root,
    )
    _write_signed(
        auto_restore_permit_consumed_path,
        {
            "schema_version": 1,
            "status": "pre_exposure_auto_restore_permitted",
            "plan_id": plan_id,
            "runtime_id": pointer["runtime_id"],
            "permit_path": str(auto_restore_permit_path.resolve()),
            "consumed_path": str(
                auto_restore_permit_consumed_path.resolve()
            ),
        },
        purpose=rollback_execute.AUTO_RESTORE_PERMIT_PURPOSE,
        project_root=project_root,
    )
    _write_signed(
        exposure_intent_path,
        {
            "schema_version": 1,
            "status": "production_exposure_intent_recorded",
            "plan_id": plan_id,
            "ticket_id": "pointer-fixture-ticket",
            "runtime_id": pointer["runtime_id"],
            "target_pointer_sha256": pointer_digest,
            "auto_restore_permit_consumed_path": str(
                auto_restore_permit_consumed_path.resolve()
            ),
            "auto_restore_permit_consumed_sha256": (
                release_erp.sha256_file(
                    auto_restore_permit_consumed_path
                )
            ),
            "validation_event_path": str(validation_event_path.resolve()),
            "validation_event_sha256": release_erp.sha256_file(
                validation_event_path
            ),
        },
        purpose=rollback_execute.EXPOSURE_INTENT_PURPOSE,
        project_root=project_root,
    )
    _write_signed(
        exposure_path,
        {
            "schema_version": 1,
            "status": "production_exposure_committed",
            "plan_id": plan_id,
            "ticket_id": "pointer-fixture-ticket",
            "runtime_id": pointer["runtime_id"],
            "target_pointer_sha256": pointer_digest,
            "auto_restore_permit_consumed_path": str(
                auto_restore_permit_consumed_path.resolve()
            ),
            "auto_restore_permit_consumed_sha256": (
                release_erp.sha256_file(
                    auto_restore_permit_consumed_path
                )
            ),
            "validation_event_path": str(validation_event_path.resolve()),
            "validation_event_sha256": release_erp.sha256_file(
                validation_event_path
            ),
            "production_exposure_intent_path": str(
                exposure_intent_path.resolve()
            ),
            "production_exposure_intent_sha256": release_erp.sha256_file(
                exposure_intent_path
            ),
        },
        purpose=rollback_execute.EXPOSURE_PURPOSE,
        project_root=project_root,
    )
    _write_signed(
        pointer_path,
        pointer,
        purpose=rollback_execute.POINTER_PURPOSE,
        project_root=project_root,
    )
    return project_root, pointer_path, pointer


def test_signed_active_pointer_resolves_and_tampering_is_rejected(
    tmp_path: Path,
) -> None:
    project_root, pointer_path, pointer = _archive_pointer_fixture(tmp_path)
    resolved = rollback_execute.resolve_active_runtime(
        pointer_path=pointer_path,
        project_root=project_root,
    )
    assert resolved["pointer_present"] is True
    assert resolved["runtime_id"] == pointer["runtime_id"]
    assert resolved["database_path"] == pointer["database_path"]

    payload = json.loads(pointer_path.read_text(encoding="utf-8"))
    payload["runtime_id"] = "tampered-runtime"
    pointer_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="签名验证失败",
    ):
        rollback_execute.resolve_active_runtime(
            pointer_path=pointer_path,
            project_root=project_root,
        )


def test_active_pointer_rejects_same_content_database_file_replacement(
    tmp_path: Path,
) -> None:
    project_root, pointer_path, pointer = _archive_pointer_fixture(tmp_path)
    database = Path(pointer["database_path"])
    replacement = database.with_name("replacement.sqlite3")
    replacement.write_bytes(database.read_bytes())
    os.replace(replacement, database)
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="file_identity",
    ):
        rollback_execute.resolve_active_runtime(
            pointer_path=pointer_path,
            project_root=project_root,
        )


def test_mutable_runtime_and_controller_paths_reject_nested_reparse(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    mutable = runtime / "static" / "uploads"
    mutable.mkdir(parents=True)
    (mutable / "example.pdf").write_bytes(b"pdf")
    original_check = rollback_execute._is_link_or_reparse
    monkeypatch.setattr(
        rollback_execute,
        "_is_link_or_reparse",
        lambda path: (
            True
            if Path(os.path.abspath(os.fspath(path))) == mutable.resolve()
            else original_check(path)
        ),
    )
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="链接|reparse",
    ):
        rollback_execute._immutable_runtime_manifest(runtime)

    source = tmp_path / "mutable-source"
    nested = source / "nested"
    nested.mkdir(parents=True)
    monkeypatch.setattr(
        rollback_execute,
        "_is_link_or_reparse",
        lambda path: (
            True
            if Path(os.path.abspath(os.fspath(path))) == nested.resolve()
            else original_check(path)
        ),
    )
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="链接|reparse",
    ):
        rollback_execute._copy_mutable_snapshot(
            source,
            tmp_path / "mutable-copy",
        )

    controller_root = tmp_path / "controller"
    controller = controller_root / "controller.py"
    controller.parent.mkdir(parents=True)
    controller.write_text("CONTROLLER = True\n", encoding="utf-8")
    monkeypatch.setattr(
        rollback_execute,
        "CONTROLLER_FILES",
        ("controller.py",),
    )
    monkeypatch.setattr(
        rollback_execute,
        "_is_link_or_reparse",
        lambda path: (
            True
            if Path(os.path.abspath(os.fspath(path))) == controller.resolve()
            else original_check(path)
        ),
    )
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="链接|reparse",
    ):
        rollback_execute._controller_manifest(controller_root)


def test_historic_signed_pointer_is_rejected_after_persistent_revocation(
    tmp_path: Path,
) -> None:
    project_root, pointer_path, pointer = _archive_pointer_fixture(tmp_path)
    _write_signed(
        Path(pointer["revocation_path"]),
        {
            "schema_version": 1,
            "status": "activation_revoked",
            "plan_id": pointer["activation_plan_id"],
            "ticket_id": "historic-ticket",
            "runtime_id": pointer["runtime_id"],
        },
        purpose=rollback_execute.REVOCATION_PURPOSE,
        project_root=project_root,
    )
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="已经撤销",
    ):
        rollback_execute.resolve_active_runtime(
            pointer_path=pointer_path,
            project_root=project_root,
        )


def test_claim_only_archive_pointer_cannot_start_as_production_runtime(
    tmp_path: Path,
) -> None:
    project_root, pointer_path, pointer = _archive_pointer_fixture(tmp_path)
    Path(pointer["production_exposure_intent_path"]).unlink()
    Path(pointer["production_exposure_path"]).unlink()

    with pytest.raises(rollback_execute.RollbackExecutionError):
        rollback_execute.resolve_active_runtime(
            pointer_path=pointer_path,
            project_root=project_root,
        )


def test_implicit_runtime_requires_formal_branch_and_fixed_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "formal"
    project_root.mkdir()
    pointer_path = (
        project_root / "data" / "release_state" / "active_runtime.json"
    )
    state = {
        "branch": "codex/not-formal",
        "head": CURRENT_SHA,
        "tracked_status": "",
    }
    monkeypatch.setattr(
        rollback_execute,
        "_checkout_state",
        lambda *_args: dict(state),
    )
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="factory-current-baseline",
    ):
        rollback_execute.resolve_active_runtime(
            pointer_path=pointer_path,
            project_root=project_root,
        )

    state["branch"] = "factory-current-baseline"
    monkeypatch.setattr(
        rollback_execute,
        "_runtime_service_config",
        lambda *_args: {
            "database_path": str(tmp_path / "outside.sqlite3"),
        },
    )
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="固定正式数据库",
    ):
        rollback_execute.resolve_active_runtime(
            pointer_path=pointer_path,
            project_root=project_root,
        )

    monkeypatch.setattr(
        rollback_execute,
        "_runtime_service_config",
        lambda *_args: {
            "database_path": str(
                project_root / "data" / "carton_erp.sqlite3"
            ),
        },
    )
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="最近正式发布证据",
    ):
        rollback_execute.resolve_active_runtime(
            pointer_path=pointer_path,
            project_root=project_root,
        )


def test_one_time_evidence_publication_never_overwrites_concurrent_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "formal"
    project_root.mkdir()
    target = tmp_path / "evidence" / "ticket.json"
    original_link = os.link

    def race_link(
        source: str | os.PathLike,
        destination: str | os.PathLike,
    ) -> None:
        Path(destination).write_text("concurrent-winner\n", encoding="utf-8")
        original_link(source, destination)

    monkeypatch.setattr(rollback_execute.os, "link", race_link)
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="拒绝覆盖",
    ):
        rollback_execute._write_signed_atomic(
            target,
            {"status": "candidate"},
            project_root=project_root,
            purpose=rollback_execute.TICKET_PURPOSE,
            replace=False,
        )
    assert target.read_text(encoding="utf-8") == "concurrent-winner\n"


def test_published_evidence_is_successful_even_if_temp_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "formal"
    project_root.mkdir()
    target = tmp_path / "evidence" / "active_runtime.json"
    original_unlink = os.unlink

    def fail_hidden_temp_cleanup(
        path: str | os.PathLike,
        *args: object,
        **kwargs: object,
    ) -> None:
        if Path(path).name.startswith(".active_runtime.json."):
            raise PermissionError("simulated antivirus hold")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(rollback_execute.os, "unlink", fail_hidden_temp_cleanup)
    written = rollback_execute._write_signed_atomic(
        target,
        {"status": "published"},
        project_root=project_root,
        purpose=rollback_execute.POINTER_PURPOSE,
        replace=False,
    )
    assert written["status"] == "published"
    assert target.is_file()
    release_state_common.verify_evidence(
        json.loads(target.read_text(encoding="utf-8")),
        project_root=project_root,
        purpose=rollback_execute.POINTER_PURPOSE,
    )


def test_active_pointer_blocks_runtime_database_escape_and_link(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    outside_runtime = tmp_path / "outside-runtime"
    project_root, pointer_path, _ = _archive_pointer_fixture(
        tmp_path / "runtime-escape",
        runtime_dir=outside_runtime,
    )
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="运行目录逃出",
    ):
        rollback_execute.resolve_active_runtime(
            pointer_path=pointer_path,
            project_root=project_root,
        )

    outside_database = _database(tmp_path / "outside.sqlite3")
    project_root, pointer_path, pointer = _archive_pointer_fixture(
        tmp_path / "database-escape",
        database=outside_database,
    )
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="数据库.*逃出|数据库路径",
    ):
        rollback_execute.resolve_active_runtime(
            pointer_path=pointer_path,
            project_root=project_root,
        )

    project_root, pointer_path, pointer = _archive_pointer_fixture(
        tmp_path / "linked-runtime",
    )
    runtime_dir = Path(pointer["runtime_dir"])
    original_check = rollback_execute._is_link_or_reparse
    monkeypatch.setattr(
        rollback_execute,
        "_is_link_or_reparse",
        lambda path: (
            True
            if path.resolve() == runtime_dir.resolve()
            else original_check(path)
        ),
    )
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="链接|reparse",
    ):
        rollback_execute.resolve_active_runtime(
            pointer_path=pointer_path,
            project_root=project_root,
        )

    project_root, pointer_path, pointer = _archive_pointer_fixture(
        tmp_path / "linked-database-parent",
    )
    database_parent = Path(pointer["database_path"]).parent
    monkeypatch.setattr(
        rollback_execute,
        "_is_link_or_reparse",
        lambda path: (
            True
            if Path(os.path.abspath(os.fspath(path))) == database_parent
            else original_check(path)
        ),
    )
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="活动数据库.*链接|活动数据库.*reparse",
    ):
        rollback_execute.resolve_active_runtime(
            pointer_path=pointer_path,
            project_root=project_root,
        )


def _activation_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, Path, dict]:
    project_root, plan_path, plan = _plan_fixture(
        tmp_path,
        monkeypatch,
        plan_id="activation",
    )
    current_database = _database(
        tmp_path / "activation-current.sqlite3"
    )
    current_snapshot = rollback_execute._database_snapshot(
        current_database,
        label="当前数据库",
    )
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    payload.pop("evidence_hmac")
    target_runtime_dir = (
        project_root.parent
        / "tm-rollback-runtimes"
        / "activation"
        / "runtime"
    ).resolve()
    target_runtime_dir.mkdir(parents=True, exist_ok=True)
    target_run_root = target_runtime_dir.parent.parent
    payload["validation_event_path"] = str(
        (target_run_root / "loopback_validation_event.json").resolve()
    )
    payload["auto_restore_permit_path"] = str(
        (target_run_root / "auto_restore_permit.json").resolve()
    )
    payload["auto_restore_permit_consumed_path"] = str(
        (
            target_run_root / "auto_restore_permit.consumed.json"
        ).resolve()
    )
    payload["production_exposure_intent_path"] = str(
        (target_run_root / "production_exposure_intent.json").resolve()
    )
    payload["current_runtime"] = {
        "runtime_id": "current-runtime",
        "database_path": str(current_database.resolve()),
        "database_activation_snapshot": current_snapshot,
    }
    payload["target_runtime"] = {
        "schema_version": 1,
        "pointer_version": 2,
        "runtime_id": "rollback-runtime",
        "runtime_kind": "archive_activation",
        "runtime_dir": str(target_runtime_dir),
        "database_path": payload["candidate_database"]["path"],
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "activation_plan_id": payload["plan_id"],
        "activation_claim_path": payload["activation_claim_path"],
        "revocation_path": payload["revocation_path"],
        "validation_event_path": payload["validation_event_path"],
        "auto_restore_permit_path": payload[
            "auto_restore_permit_path"
        ],
        "auto_restore_permit_consumed_path": payload[
            "auto_restore_permit_consumed_path"
        ],
        "production_exposure_intent_path": payload[
            "production_exposure_intent_path"
        ],
        "production_exposure_path": payload["production_exposure_path"],
    }
    plan_path.unlink()
    original_permit_path = Path(plan["auto_restore_permit_path"])
    original_permit_path.unlink()
    _write_signed(
        Path(payload["auto_restore_permit_path"]),
        {
            "schema_version": 1,
            "status": "pre_exposure_auto_restore_permitted",
            "plan_id": payload["plan_id"],
            "runtime_id": payload["target_runtime"]["runtime_id"],
            "permit_path": payload["auto_restore_permit_path"],
            "consumed_path": payload[
                "auto_restore_permit_consumed_path"
            ],
        },
        purpose=rollback_execute.AUTO_RESTORE_PERMIT_PURPOSE,
        project_root=project_root,
    )
    _write_signed(
        plan_path,
        payload,
        purpose=rollback_execute.PLAN_PURPOSE,
        project_root=project_root,
    )
    ticket_path = _authorize(project_root, plan_path)
    ticket = json.loads(ticket_path.read_text(encoding="utf-8"))
    _write_signed(
        Path(payload["validation_event_path"]),
        {
            "schema_version": 1,
            "status": "loopback_validation_completed",
            "plan_id": payload["plan_id"],
            "ticket_id": ticket["ticket_id"],
            "runtime_id": payload["target_runtime"]["runtime_id"],
            "target_runtime_template_sha256": hashlib.sha256(
                rollback_execute._canonical_bytes(payload["target_runtime"])
            ).hexdigest(),
            "validation_service": payload["validation_service"],
            "network_exposure": "loopback_only",
            "health": {"status_code": 200, "body": {"ok": True}},
        },
        purpose=rollback_execute.VALIDATION_PURPOSE,
        project_root=project_root,
    )
    monkeypatch.setattr(
        rollback_execute,
        "_assert_port_free",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        rollback_execute,
        "_verify_pointer_payload",
        lambda pointer, *_args, **_kwargs: {
            "ok": True,
            "runtime_id": pointer.get("runtime_id"),
            "database_path": pointer.get("database_path"),
            "health_url": "http://127.0.0.1:18000/api/health",
        },
    )
    return project_root, plan_path, ticket_path, payload


def _write_exposure_intent(
    *,
    project_root: Path,
    ticket_path: Path,
    plan: dict,
) -> tuple[dict, dict]:
    """Create the first durable side of the no-auto-restore boundary."""
    ticket = json.loads(ticket_path.read_text(encoding="utf-8"))
    pointer_path = Path(plan["active_pointer_path"])
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer_digest = hashlib.sha256(
        rollback_execute._canonical_bytes(
            rollback_execute._unsigned_payload(pointer)
        )
    ).hexdigest()
    rollback_execute._consume_auto_restore_permit(
        plan,
        project_root=project_root,
    )
    consumed_path = Path(plan["auto_restore_permit_consumed_path"])
    validation_path = Path(plan["validation_event_path"])
    intent_path = Path(plan["production_exposure_intent_path"])
    intent = _write_signed(
        intent_path,
        {
            "schema_version": 1,
            "status": "production_exposure_intent_recorded",
            "intent_recorded_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ),
            "plan_id": plan["plan_id"],
            "ticket_id": ticket["ticket_id"],
            "runtime_id": pointer["runtime_id"],
            "target_pointer_sha256": pointer_digest,
            "auto_restore_permit_consumed_path": str(
                consumed_path.resolve()
            ),
            "auto_restore_permit_consumed_sha256": (
                release_erp.sha256_file(consumed_path)
            ),
            "validation_event_path": str(validation_path.resolve()),
            "validation_event_sha256": release_erp.sha256_file(
                validation_path
            ),
        },
        purpose=rollback_execute.EXPOSURE_INTENT_PURPOSE,
        project_root=project_root,
    )
    return pointer, intent


def _write_durable_exposure(
    *,
    project_root: Path,
    ticket_path: Path,
    plan: dict,
) -> None:
    pointer, _intent = _write_exposure_intent(
        project_root=project_root,
        ticket_path=ticket_path,
        plan=plan,
    )
    ticket = json.loads(ticket_path.read_text(encoding="utf-8"))
    intent_path = Path(plan["production_exposure_intent_path"])
    consumed_path = Path(plan["auto_restore_permit_consumed_path"])
    validation_path = Path(plan["validation_event_path"])
    _write_signed(
        Path(plan["production_exposure_path"]),
        {
            "schema_version": 1,
            "status": "production_exposure_committed",
            "committed_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ),
            "plan_id": plan["plan_id"],
            "ticket_id": ticket["ticket_id"],
            "runtime_id": pointer["runtime_id"],
            "target_pointer_sha256": hashlib.sha256(
                rollback_execute._canonical_bytes(
                    rollback_execute._unsigned_payload(pointer)
                )
            ).hexdigest(),
            "auto_restore_permit_consumed_path": str(
                consumed_path.resolve()
            ),
            "auto_restore_permit_consumed_sha256": (
                release_erp.sha256_file(consumed_path)
            ),
            "validation_event_path": str(validation_path.resolve()),
            "validation_event_sha256": release_erp.sha256_file(
                validation_path
            ),
            "production_exposure_intent_path": str(intent_path.resolve()),
            "production_exposure_intent_sha256": release_erp.sha256_file(
                intent_path
            ),
        },
        purpose=rollback_execute.EXPOSURE_PURPOSE,
        project_root=project_root,
    )


def test_pointer_activation_is_atomic_replay_safe_and_restorable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root, _plan_path, ticket_path, plan = _activation_fixture(
        tmp_path,
        monkeypatch,
    )
    pointer_path = Path(plan["active_pointer_path"])
    destinations: list[Path] = []
    original_link = os.link

    def recording_link(
        source: str | os.PathLike,
        destination: str | os.PathLike,
    ) -> None:
        destinations.append(Path(destination).resolve())
        original_link(source, destination)

    monkeypatch.setattr(rollback_execute.os, "link", recording_link)
    activated = rollback_execute.activate_switch(
        ticket_path=ticket_path,
        project_root=project_root,
    )
    assert activated["status"] == "target_pointer_activated"
    assert pointer_path.resolve() in destinations
    assert pointer_path.is_file()
    assert not list(pointer_path.parent.glob(".*.tmp"))
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    release_state_common.verify_evidence(
        pointer,
        project_root=project_root,
        purpose=rollback_execute.POINTER_PURPOSE,
    )

    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="已经执行或撤销",
    ):
        rollback_execute.activate_switch(
            ticket_path=ticket_path,
            project_root=project_root,
        )

    restored = rollback_execute.restore_switch(
        ticket_path=ticket_path,
        project_root=project_root,
    )
    assert restored["status"] == "current_pointer_restored"
    assert not pointer_path.exists()
    restoration_path = Path(plan["restoration_event_path"])
    restoration = json.loads(restoration_path.read_text(encoding="utf-8"))
    release_state_common.verify_evidence(
        restoration,
        project_root=project_root,
        purpose=rollback_execute.EVENT_PURPOSE,
    )
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="已经执行过一次自动恢复",
    ):
        rollback_execute.restore_switch(
            ticket_path=ticket_path,
            project_root=project_root,
        )
def test_deleting_output_event_cannot_replay_consumed_activation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root, _plan_path, ticket_path, plan = _activation_fixture(
        tmp_path,
        monkeypatch,
    )
    rollback_execute.activate_switch(
        ticket_path=ticket_path,
        project_root=project_root,
    )
    Path(plan["activation_event_path"]).unlink()
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="已经执行或撤销",
    ):
        rollback_execute.activate_switch(
            ticket_path=ticket_path,
            project_root=project_root,
        )


def test_expired_authorized_ticket_still_allows_emergency_restore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root, _plan_path, ticket_path, plan = _activation_fixture(
        tmp_path,
        monkeypatch,
    )
    rollback_execute.activate_switch(
        ticket_path=ticket_path,
        project_root=project_root,
    )

    class FutureDateTime(datetime):
        @classmethod
        def now(cls, tz: timezone | None = None) -> "FutureDateTime":
            future = cls(2099, 1, 1, tzinfo=timezone.utc)
            return future if tz is None else future.astimezone(tz)

    monkeypatch.setattr(rollback_execute, "datetime", FutureDateTime)
    restored = rollback_execute.restore_switch(
        ticket_path=ticket_path,
        project_root=project_root,
    )
    assert restored["status"] == "current_pointer_restored"
    assert not Path(plan["active_pointer_path"]).exists()


def test_commit_production_exposure_writes_signed_intent_and_durable_witness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root, _plan_path, ticket_path, plan = _activation_fixture(
        tmp_path,
        monkeypatch,
    )
    rollback_execute.activate_switch(
        ticket_path=ticket_path,
        project_root=project_root,
    )

    committed = rollback_execute.commit_production_exposure(
        ticket_path=ticket_path,
        project_root=project_root,
    )
    intent_path = Path(plan["production_exposure_intent_path"])
    exposure_path = Path(plan["production_exposure_path"])
    assert committed["auto_restore_allowed"] is False
    assert intent_path.is_file()
    assert exposure_path.is_file()
    intent = _verify_signed_fixture(
        intent_path,
        purpose=rollback_execute.EXPOSURE_INTENT_PURPOSE,
        project_root=project_root,
    )
    exposure = _verify_signed_fixture(
        exposure_path,
        purpose=rollback_execute.EXPOSURE_PURPOSE,
        project_root=project_root,
    )
    assert intent["status"] == "production_exposure_intent_recorded"
    assert exposure["status"] == "production_exposure_committed"
    assert exposure["production_exposure_intent_sha256"] == (
        release_erp.sha256_file(intent_path)
    )


def test_consumed_auto_restore_permit_blocks_restore_even_without_intent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root, _plan_path, ticket_path, plan = _activation_fixture(
        tmp_path,
        monkeypatch,
    )
    rollback_execute.activate_switch(
        ticket_path=ticket_path,
        project_root=project_root,
    )
    rollback_execute._consume_auto_restore_permit(
        plan,
        project_root=project_root,
    )
    assert not Path(plan["auto_restore_permit_path"]).exists()
    assert Path(plan["auto_restore_permit_consumed_path"]).is_file()
    assert not Path(plan["production_exposure_intent_path"]).exists()

    for operation in ("restore", "revoke"):
        with pytest.raises(
            rollback_execute.RollbackExecutionError,
            match="许可.*消费|禁止自动恢复",
        ):
            if operation == "restore":
                rollback_execute.restore_switch(
                    ticket_path=ticket_path,
                    project_root=project_root,
                )
            else:
                rollback_execute.revoke_switch(
                    ticket_path=ticket_path,
                    reason="模拟许可消费后、intent 写入前断电",
                    project_root=project_root,
                )

    Path(plan["auto_restore_permit_consumed_path"]).unlink()
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="许可缺失|禁止自动恢复",
    ):
        rollback_execute.restore_switch(
            ticket_path=ticket_path,
            project_root=project_root,
        )


def _verify_signed_fixture(
    path: Path,
    *,
    purpose: str,
    project_root: Path,
) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    release_state_common.verify_evidence(
        payload,
        project_root=project_root,
        purpose=purpose,
    )
    return payload


@pytest.mark.parametrize(
    ("marker_state", "operation"),
    (
        ("intent_only", "restore"),
        ("intent_only", "revoke"),
        ("exposure_only", "restore"),
        ("exposure_only", "revoke"),
        ("damaged_intent", "restore"),
        ("damaged_intent", "revoke"),
    ),
)
def test_any_post_intent_crash_state_blocks_auto_restore_and_revoke(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    marker_state: str,
    operation: str,
) -> None:
    project_root, _plan_path, ticket_path, plan = _activation_fixture(
        tmp_path,
        monkeypatch,
    )
    rollback_execute.activate_switch(
        ticket_path=ticket_path,
        project_root=project_root,
    )
    _write_durable_exposure(
        project_root=project_root,
        ticket_path=ticket_path,
        plan=plan,
    )
    intent_path = Path(plan["production_exposure_intent_path"])
    exposure_path = Path(plan["production_exposure_path"])
    if marker_state == "intent_only":
        exposure_path.unlink()
    elif marker_state == "exposure_only":
        intent_path.unlink()
    else:
        intent_path.write_text("damaged evidence", encoding="utf-8")

    with pytest.raises(rollback_execute.RollbackExecutionError):
        if operation == "restore":
            rollback_execute.restore_switch(
                ticket_path=ticket_path,
                project_root=project_root,
            )
        else:
            rollback_execute.revoke_switch(
                ticket_path=ticket_path,
                reason="模拟正式开放边界后的异常",
                project_root=project_root,
            )
    assert Path(plan["active_pointer_path"]).is_file()
    assert not Path(plan["revocation_path"]).exists()


def test_post_exposure_business_write_is_retained_and_switch_can_finalize(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root, _plan_path, ticket_path, plan = _activation_fixture(
        tmp_path,
        monkeypatch,
    )
    candidate_database = _database(Path(plan["candidate_database"]["path"]))
    rollback_execute.activate_switch(
        ticket_path=ticket_path,
        project_root=project_root,
    )
    rollback_execute.commit_production_exposure(
        ticket_path=ticket_path,
        project_root=project_root,
    )
    with sqlite3.connect(candidate_database) as connection:
        connection.execute("INSERT INTO customers(name) VALUES ('正式开放后的业务写入')")
        connection.commit()
    monkeypatch.setattr(
        rollback_execute,
        "_read_health",
        lambda *_args, **_kwargs: {
            "status_code": 200,
            "body": {"ok": True},
            "url": "http://127.0.0.1:18000/api/health",
        },
    )
    monkeypatch.setattr(
        rollback_execute,
        "_checkout_state",
        lambda *_args: plan["formal_checkout"],
    )

    verified = rollback_execute.verify_active_switch(
        ticket_path=ticket_path,
        project_root=project_root,
    )
    finalized = rollback_execute.finalize_switch(
        ticket_path=ticket_path,
        status="switch_completed",
        project_root=project_root,
    )
    with sqlite3.connect(candidate_database) as connection:
        retained = connection.execute(
            "SELECT COUNT(*) FROM customers WHERE name = ?",
            ("正式开放后的业务写入",),
        ).fetchone()[0]
    assert verified["status"] == "target_runtime_verified"
    assert finalized["status"] == "switch_completed"
    assert retained == 1


def test_post_exposure_manual_target_retained_records_no_auto_restore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root, _plan_path, ticket_path, plan = _activation_fixture(
        tmp_path,
        monkeypatch,
    )
    _database(Path(plan["candidate_database"]["path"]))
    rollback_execute.activate_switch(
        ticket_path=ticket_path,
        project_root=project_root,
    )
    _write_durable_exposure(
        project_root=project_root,
        ticket_path=ticket_path,
        plan=plan,
    )
    monkeypatch.setattr(rollback_execute, "_port_is_free", lambda *_args: False)
    monkeypatch.setattr(
        rollback_execute,
        "_checkout_state",
        lambda *_args: plan["formal_checkout"],
    )

    result = rollback_execute.finalize_switch(
        ticket_path=ticket_path,
        status="manual_target_retained",
        error="正式监听后健康检查失败，保留目标运行目录与候选数据库",
        project_root=project_root,
    )
    assert result["ok"] is False
    assert result["status"] == "manual_target_retained"
    assert Path(plan["active_pointer_path"]).is_file()
    assert not Path(plan["revocation_path"]).exists()
    assert Path(plan["candidate_database"]["path"]).is_file()


def test_missing_pointer_with_outstanding_claim_blocks_implicit_formal_fallback(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "formal"
    project_root.mkdir()
    plan_id = "outstanding-claim"
    claim_path, _revocation_path = rollback_execute._activation_state_paths(
        plan_id,
        project_root,
    )
    _write_signed(
        claim_path,
        {
            "schema_version": 1,
            "status": "activation_claimed",
            "plan_id": plan_id,
            "ticket_id": "outstanding-ticket",
            "runtime_id": "target-runtime",
            "target_pointer_sha256": "a" * 64,
        },
        purpose=rollback_execute.ACTIVATION_CLAIM_PURPOSE,
        project_root=project_root,
    )
    pointer_path = (
        project_root / "data" / "release_state" / "active_runtime.json"
    )
    with pytest.raises(rollback_execute.RollbackExecutionError):
        rollback_execute.resolve_active_runtime(
            pointer_path=pointer_path,
            project_root=project_root,
        )


def test_formal_release_gate_rejects_pointer_entry_and_orphan_exposure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "formal"
    pointer_path = (
        project_root / "data" / "release_state" / "active_runtime.json"
    )
    pointer_path.mkdir(parents=True)
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="活动运行指针",
    ):
        rollback_execute.assert_formal_release_allowed(
            pointer_path=pointer_path,
            project_root=project_root,
        )
    pointer_path.rmdir()

    activation_root = (
        project_root / "data" / "release_state" / "rollback_activations"
    )
    activation_root.mkdir(parents=True, exist_ok=True)
    (activation_root / "orphan.exposed.json").write_text(
        "{}\n",
        encoding="utf-8",
    )
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="缺少声明",
    ):
        rollback_execute.assert_formal_release_allowed(
            pointer_path=pointer_path,
            project_root=project_root,
        )

    (activation_root / "orphan.exposed.json").unlink()
    original_check = release_state_common._is_link_or_reparse
    monkeypatch.setattr(
        release_state_common,
        "_is_link_or_reparse",
        lambda path: (
            True
            if Path(os.path.abspath(os.fspath(path)))
            == activation_root.resolve()
            else original_check(path)
        ),
    )
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="链接|reparse",
    ):
        rollback_execute.assert_formal_release_allowed(
            pointer_path=pointer_path,
            project_root=project_root,
        )


def test_missing_pointer_with_matching_revocation_can_reach_formal_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "formal"
    project_root.mkdir()
    plan_id = "revoked-claim"
    claim_path, revocation_path = rollback_execute._activation_state_paths(
        plan_id,
        project_root,
    )
    claim = _write_signed(
        claim_path,
        {
            "schema_version": 1,
            "status": "activation_claimed",
            "plan_id": plan_id,
            "ticket_id": "revoked-ticket",
            "runtime_id": "target-runtime",
            "target_pointer_sha256": "b" * 64,
        },
        purpose=rollback_execute.ACTIVATION_CLAIM_PURPOSE,
        project_root=project_root,
    )
    _write_signed(
        revocation_path,
        {
            "schema_version": 1,
            "status": "activation_revoked",
            "plan_id": plan_id,
            "ticket_id": "revoked-ticket",
            "runtime_id": "target-runtime",
            "target_pointer_sha256": "b" * 64,
            "activation_claim_sha256": release_erp.sha256_file(claim_path),
        },
        purpose=rollback_execute.REVOCATION_PURPOSE,
        project_root=project_root,
    )
    state = {
        "branch": "factory-current-baseline",
        "head": CURRENT_SHA,
        "tracked_status": "",
    }
    monkeypatch.setattr(rollback_execute, "_checkout_state", lambda *_args: state)
    monkeypatch.setattr(
        rollback_execute,
        "_assert_formal_checkout",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        rollback_execute,
        "_runtime_service_config",
        lambda *_args: {
            "database_path": str(
                project_root / "data" / "carton_erp.sqlite3"
            )
        },
    )
    pointer_path = (
        project_root / "data" / "release_state" / "active_runtime.json"
    )
    allowed = rollback_execute.assert_formal_release_allowed(
        pointer_path=pointer_path,
        project_root=project_root,
    )
    assert allowed["formal_release_allowed"] is True
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="最近正式发布证据",
    ):
        rollback_execute.resolve_active_runtime(
            pointer_path=pointer_path,
            project_root=project_root,
        )


def test_double_start_failure_writes_signed_manual_recovery_result_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root, _plan_path, ticket_path, plan = _activation_fixture(
        tmp_path,
        monkeypatch,
    )
    ticket = json.loads(ticket_path.read_text(encoding="utf-8"))
    restoration_path = Path(plan["restoration_event_path"])
    _write_signed(
        restoration_path,
        {
            "schema_version": 1,
            "event": "current_pointer_restored",
            "event_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ticket_id": ticket["ticket_id"],
            "plan_id": plan["plan_id"],
        },
        purpose=rollback_execute.EVENT_PURPOSE,
        project_root=project_root,
    )
    _write_signed(
        Path(plan["revocation_path"]),
        {
            "schema_version": 1,
            "status": "activation_revoked",
            "plan_id": plan["plan_id"],
            "ticket_id": ticket["ticket_id"],
            "runtime_id": plan["target_runtime"]["runtime_id"],
        },
        purpose=rollback_execute.REVOCATION_PURPOSE,
        project_root=project_root,
    )
    monkeypatch.setattr(
        rollback_execute,
        "_checkout_state",
        lambda *_args: plan["formal_checkout"],
    )
    result = rollback_execute.finalize_switch(
        ticket_path=ticket_path,
        status="stopped_manual_recovery_required",
        error="目标旧版本失败；当前版本自动恢复也失败",
        project_root=project_root,
    )
    assert result["ok"] is False
    assert result["status"] == "stopped_manual_recovery_required"
    result_path = Path(result["result_path"])
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    release_state_common.verify_evidence(
        payload,
        project_root=project_root,
        purpose=rollback_execute.RESULT_PURPOSE,
    )
    assert payload["verification"] == {
        "service_stopped": True,
        "manual_recovery_required": True,
    }
    assert "当前版本自动恢复也失败" in payload["error"]
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="已经生成最终结果",
    ):
        rollback_execute.finalize_switch(
            ticket_path=ticket_path,
            status="stopped_manual_recovery_required",
            project_root=project_root,
        )


def test_pre_activation_unknown_listener_writes_signed_manual_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root, _plan_path, ticket_path, plan = _activation_fixture(
        tmp_path,
        monkeypatch,
    )
    monkeypatch.setattr(
        rollback_execute,
        "_windows_process_exists",
        lambda process_id: False,
    )
    monkeypatch.setattr(
        rollback_execute,
        "_windows_listener_process_ids",
        lambda port: [99123],
    )
    monkeypatch.setattr(
        rollback_execute,
        "_checkout_state",
        lambda *_args: plan["formal_checkout"],
    )
    result = rollback_execute.finalize_switch(
        ticket_path=ticket_path,
        status="stopped_before_activation_manual_recovery_required",
        error="原 ERP PID 已退出，但正式端口被未知 PID 抢占",
        stopped_process_id=88001,
        observed_listener_process_id=99123,
        project_root=project_root,
    )
    assert result["ok"] is False
    assert result["verification"]["activation_started"] is False
    assert result["verification"]["production_port_free"] is False
    assert result["verification"]["stopped_process_id"] == 88001
    assert result["verification"]["observed_listener_process_ids"] == [99123]
    assert not Path(plan["active_pointer_path"]).exists()
    payload = _verify_signed_fixture(
        Path(result["result_path"]),
        purpose=rollback_execute.RESULT_PURPOSE,
        project_root=project_root,
    )
    assert payload["status"] == (
        "stopped_before_activation_manual_recovery_required"
    )
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="已经生成最终结果",
    ):
        rollback_execute.finalize_switch(
            ticket_path=ticket_path,
            status="stopped_before_activation_manual_recovery_required",
            stopped_process_id=88001,
            observed_listener_process_id=99123,
            project_root=project_root,
        )


def test_pre_activation_manual_result_rejects_consumed_permit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root, _plan_path, ticket_path, plan = _activation_fixture(
        tmp_path,
        monkeypatch,
    )
    rollback_execute._consume_auto_restore_permit(
        plan,
        project_root=project_root,
    )
    monkeypatch.setattr(
        rollback_execute,
        "_windows_process_exists",
        lambda process_id: False,
    )
    monkeypatch.setattr(
        rollback_execute,
        "_windows_listener_process_ids",
        lambda port: [99123],
    )
    with pytest.raises(
        rollback_execute.RollbackExecutionError,
        match="状态冲突",
    ):
        rollback_execute.finalize_switch(
            ticket_path=ticket_path,
            status="stopped_before_activation_manual_recovery_required",
            stopped_process_id=88001,
            observed_listener_process_id=99123,
            project_root=project_root,
        )


def test_public_api_cli_help_and_no_dangerous_git_or_migration_commands() -> None:
    expected = (
        "prepare_switch",
        "authorize_switch",
        "activate_switch",
        "verify_loopback_switch",
        "commit_production_exposure",
        "verify_active_switch",
        "restore_switch",
        "revoke_switch",
        "finalize_switch",
        "resolve_active_runtime",
        "resolve_validation_runtime",
        "assert_formal_release_allowed",
        "RollbackExecutionError",
        "PLAN_PURPOSE",
        "POINTER_PURPOSE",
        "TICKET_PURPOSE",
        "RESULT_PURPOSE",
    )
    assert all(hasattr(rollback_execute, name) for name in expected)

    script_path = Path(rollback_execute.__file__).resolve()
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    help_result = subprocess.run(
        [sys.executable, str(script_path), "--help"],
        cwd=script_path.parents[2],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "prepare" in help_result.stdout
    assert "authorize" in help_result.stdout
    assert "resolve-active" in help_result.stdout
    assert "verify-loopback" in help_result.stdout
    assert "commit-exposure" in help_result.stdout
    assert "assert-formal-release-allowed" in help_result.stdout
    resolver_help = subprocess.run(
        [sys.executable, str(script_path), "resolve-active", "--help"],
        cwd=script_path.parents[2],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    assert resolver_help.returncode == 0, resolver_help.stderr
    assert "allow-pre-exposure" not in resolver_help.stdout
    source = script_path.read_text(encoding="utf-8").lower()
    for forbidden in (
        "reset --hard",
        "git switch",
        "git checkout",
        "alembic upgrade",
        "alembic downgrade",
        "_run_migration(",
        "drop database",
    ):
        assert forbidden not in source


def test_windows_launcher_and_release_gate_follow_signed_active_pointer() -> None:
    project_root = Path(rollback_execute.__file__).resolve().parents[2]
    start_script = (
        project_root / "scripts" / "windows" / "start_erp.ps1"
    ).read_text(encoding="utf-8")
    wrapper = (
        project_root / "scripts" / "admin" / "rollback_execute.ps1"
    ).read_text(encoding="utf-8")
    release_script = (
        project_root / "scripts" / "admin" / "release_erp.ps1"
    ).read_text(encoding="utf-8")

    assert '"resolve-active", "--pointer", $ActivePointer' in start_script
    assert '"--app-dir", $ApplicationRoot' in start_script
    assert 'Set-ChildEnvironmentValue -Name "ERP_SECRET_KEY" -Value $null' in (
        start_script
    )
    assert "Get-ValidatedErpProcess" in wrapper
    assert "stopped_manual_recovery_required" in wrapper
    assert "Stop-Process -Id $processId" in wrapper
    assert "active_runtime.json" in release_script
    assert "assert-formal-release-allowed" in release_script
