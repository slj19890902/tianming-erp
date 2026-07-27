from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.admin import release_erp, release_state_common, rollback_erp
from scripts.admin.release_state_common import (
    code_revision_at,
    sqlite_logical_fingerprint,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _fixed_release_evidence_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        release_state_common,
        "_release_evidence_key",
        lambda _root: b"test-release-evidence-key".ljust(32, b"!"),
    )


def _database(path: Path, revision: str = "same_revision") -> Path:
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
        connection.execute("INSERT INTO customers(name) VALUES ('隔离客户')")
        connection.execute("INSERT INTO sales_orders VALUES (1, 1)")
        connection.commit()
    return path


def _release_evidence(
    tmp_path: Path,
    database: Path,
    *,
    previous_revision: str = "same_revision",
    release_revision: str = "same_revision",
) -> tuple[Path, Path, dict]:
    backup_path = tmp_path / "backups" / "carton_erp_before_release_test.sqlite3"
    backup = release_erp.create_sqlite_copy(database, backup_path)
    plan_path = tmp_path / "reports" / "release.json"
    plan = {
        "schema_version": 3,
        "status": "completed",
        "prepared_at": "2026-07-27T01:00:00+00:00",
        "service_started_at": "2026-07-27T01:05:00+00:00",
        "previous_code_sha": "1" * 40,
        "previous_code_revision": previous_revision,
        "code_sha": "2" * 40,
        "expected_revision": release_revision,
        "prepared_runtime_config": {"sha256": "stable-config"},
        "completed_runtime_config": {"sha256": "stable-config"},
        "source": release_erp.inspect_database(database),
        "backup": backup,
        "completed_database": {
            **release_erp.inspect_database(database),
            "logical_fingerprint": sqlite_logical_fingerprint(database),
        },
        "report_path": str(plan_path.resolve()),
    }
    plan = release_erp._write_signed_plan(
        plan_path,
        plan,
        project_root=PROJECT_ROOT,
        purpose="release-plan-v3",
    )
    pointer_path = tmp_path / "release_state" / "latest_completed_release.json"
    release_erp._write_signed_plan(
        pointer_path,
        {
            "schema_version": 2,
            "release_plan_path": str(plan_path.resolve()),
            "release_plan_sha256": release_erp.sha256_file(plan_path),
            "completed_at": plan["service_started_at"],
            "code_sha": plan["code_sha"],
            "previous_code_sha": plan["previous_code_sha"],
        },
        project_root=PROJECT_ROOT,
        purpose="release-pointer-v2",
    )
    return pointer_path, plan_path, plan


def _mock_current_checkout(
    monkeypatch: pytest.MonkeyPatch,
    plan: dict,
    *,
    config: dict | None = None,
) -> None:
    def git_output(_root: Path, *arguments: str) -> str:
        if arguments == ("branch", "--show-current"):
            return "factory-current-baseline"
        if arguments == ("rev-parse", "HEAD"):
            return str(plan["code_sha"])
        if arguments == ("status", "--porcelain"):
            return ""
        raise AssertionError(arguments)

    monkeypatch.setattr(rollback_erp, "_git_output", git_output)
    monkeypatch.setattr(
        rollback_erp,
        "code_revision_at",
        lambda *_args: plan["previous_code_revision"],
    )
    monkeypatch.setattr(
        rollback_erp,
        "runtime_config_fingerprint",
        lambda *_args: config or {"sha256": "stable-config"},
    )


def test_evidence_hmac_rejects_tampering() -> None:
    signed = release_state_common.sign_evidence(
        {"status": "completed", "code_sha": "1" * 40},
        project_root=PROJECT_ROOT,
        purpose="release-plan-v3",
    )
    release_state_common.verify_evidence(
        signed,
        project_root=PROJECT_ROOT,
        purpose="release-plan-v3",
    )
    tampered = {**signed, "code_sha": "2" * 40}
    with pytest.raises(release_state_common.ReleaseStateError, match="签名验证失败"):
        release_state_common.verify_evidence(
            tampered,
            project_root=PROJECT_ROOT,
            purpose="release-plan-v3",
        )


def test_evidence_hmac_fails_closed_after_key_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signed = release_state_common.sign_evidence(
        {"status": "completed"},
        project_root=PROJECT_ROOT,
        purpose="release-plan-v3",
    )
    monkeypatch.setattr(
        release_state_common,
        "_release_evidence_key",
        lambda _root: b"rotated-release-evidence-key".ljust(32, b"!"),
    )
    with pytest.raises(release_state_common.ReleaseStateError, match="密钥已变化"):
        release_state_common.verify_evidence(
            signed,
            project_root=PROJECT_ROOT,
            purpose="release-plan-v3",
        )


def test_runtime_fingerprint_covers_installed_distribution_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeDistribution:
        def __init__(self, version: str) -> None:
            self.metadata = {"Name": "Example_Package"}
            self.version = version

        @staticmethod
        def read_text(filename: str) -> str:
            return f"{filename}:stable"

    monkeypatch.setattr(
        release_state_common.importlib.metadata,
        "distributions",
        lambda: [FakeDistribution("1.0")],
    )
    before = release_state_common.runtime_config_fingerprint(PROJECT_ROOT)
    monkeypatch.setattr(
        release_state_common.importlib.metadata,
        "distributions",
        lambda: [FakeDistribution("2.0")],
    )
    after = release_state_common.runtime_config_fingerprint(PROJECT_ROOT)
    assert before["installed_distribution_count"] == 1
    assert before["installed_distributions_sha256"] != (
        after["installed_distributions_sha256"]
    )
    assert before["sha256"] != after["sha256"]


def test_logical_fingerprint_detects_same_count_update_delete_and_reinsert(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path / "uat.sqlite3")
    original = sqlite_logical_fingerprint(database)

    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE customers SET name = '修改后客户' WHERE id = 1")
        connection.commit()
    updated = sqlite_logical_fingerprint(database)

    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM customers WHERE id = 1")
        connection.execute("INSERT INTO customers(name) VALUES ('隔离客户')")
        connection.commit()
    replaced = sqlite_logical_fingerprint(database)

    assert original["tables"]["customers"]["rows"] == 1
    assert updated["tables"]["customers"]["rows"] == 1
    assert replaced["tables"]["customers"]["rows"] == 1
    assert updated["sha256"] != original["sha256"]
    assert replaced["sha256"] != original["sha256"]
    assert replaced["tables"]["sqlite_sequence"]["sha256"] != original["tables"]["sqlite_sequence"]["sha256"]


def test_logical_fingerprint_is_not_changed_by_wal_checkpoint(tmp_path: Path) -> None:
    database = _database(tmp_path / "uat.sqlite3")
    before = sqlite_logical_fingerprint(database)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    after = sqlite_logical_fingerprint(database)
    assert after == before


def test_read_only_assessment_returns_full_and_blocks_changed_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = _database(tmp_path / "formal.sqlite3")
    pointer, plan_path, plan = _release_evidence(tmp_path, database)
    _mock_current_checkout(monkeypatch, plan)

    full = rollback_erp.assess_rollback(
        pointer_path=pointer,
        database=database,
        project_root=PROJECT_ROOT,
    )
    assert full["mode"] == "full_rollback_allowed"
    assert full["database_changed_since_release"] is False

    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE customers SET name = '发布后业务写入' WHERE id = 1")
        connection.commit()
    compatibility_blocked = rollback_erp.assess_rollback(
        pointer_path=pointer,
        database=database,
        project_root=PROJECT_ROOT,
    )
    assert compatibility_blocked["mode"] == "blocked"
    assert "P0-5B" in compatibility_blocked["reason"]
    assert compatibility_blocked["database_changed_since_release"] is True
    assert {item["table"] for item in compatibility_blocked["changed_tables"]} == {
        "customers"
    }

    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE alembic_version SET version_num = 'new_revision'")
        connection.commit()
    blocked = rollback_erp.assess_rollback(
        pointer_path=pointer,
        database=database,
        project_root=PROJECT_ROOT,
    )
    assert blocked["mode"] == "blocked"
    assert "前向修复" in blocked["reason"]

    stored_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    stored_plan["previous_code_compatibility"] = {
        "status": "passed",
        "previous_code_sha": plan["previous_code_sha"],
        "database_revision": plan["previous_code_revision"],
        "evidence_sha256": "a" * 64,
        "tested_at": "2026-07-27T02:00:00+00:00",
    }
    release_erp._write_plan(plan_path, stored_plan)
    pointer_payload = json.loads(pointer.read_text(encoding="utf-8"))
    pointer_payload["release_plan_sha256"] = release_erp.sha256_file(plan_path)
    release_erp._write_plan(pointer, pointer_payload)
    with pytest.raises(
        rollback_erp.RollbackAssessmentError,
        match="签名验证失败",
    ):
        rollback_erp.assess_rollback(
            pointer_path=pointer,
            database=database,
            project_root=PROJECT_ROOT,
        )


def test_read_only_assessment_blocks_config_backup_and_report_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = _database(tmp_path / "formal.sqlite3")
    pointer, plan_path, plan = _release_evidence(tmp_path, database)
    _mock_current_checkout(monkeypatch, plan, config={"sha256": "changed-config"})

    config_blocked = rollback_erp.assess_rollback(
        pointer_path=pointer,
        database=database,
        project_root=PROJECT_ROOT,
    )
    assert config_blocked["mode"] == "blocked"
    assert "运行配置" in config_blocked["reason"]

    _mock_current_checkout(monkeypatch, plan)
    Path(plan["backup"]["path"]).write_bytes(b"damaged")
    backup_blocked = rollback_erp.assess_rollback(
        pointer_path=pointer,
        database=database,
        project_root=PROJECT_ROOT,
    )
    assert backup_blocked["mode"] == "blocked"
    assert "SHA-256" in backup_blocked["reason"]

    plan_path.write_text(
        plan_path.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    with pytest.raises(rollback_erp.RollbackAssessmentError, match="哈希不一致"):
        rollback_erp.assess_rollback(
            pointer_path=pointer,
            database=database,
            project_root=PROJECT_ROOT,
        )


def test_current_commit_revision_parser_matches_alembic_head() -> None:
    current_sha = rollback_erp._git_output(PROJECT_ROOT, "rev-parse", "HEAD")
    assert code_revision_at(PROJECT_ROOT, current_sha) == release_erp.code_revision(
        PROJECT_ROOT
    )


def test_fault_check_entry_is_strictly_read_only() -> None:
    powershell = (
        PROJECT_ROOT / "scripts" / "admin" / "rollback_erp.ps1"
    ).read_text(encoding="utf-8")
    batch = (
        PROJECT_ROOT / "scripts" / "windows" / "erp_fault_check.bat"
    ).read_text(encoding="utf-8")

    for forbidden in (
        "Stop-Process",
        "git switch",
        "git reset",
        "git stash",
        "alembic upgrade",
        "restore_from_backup",
        "ApprovalToken",
    ):
        assert forbidden not in powershell
    assert "不会停止 ERP" in powershell
    assert "不会修改代码或数据库" in powershell
    assert "rollback_erp.ps1" in batch

    shortcut_installer = (
        PROJECT_ROOT / "scripts" / "windows" / "install_desktop_shortcut.ps1"
    ).read_text(encoding="utf-8-sig")
    assert "天明ERP故障检查.lnk" in shortcut_installer
    assert "erp_fault_check.bat" in shortcut_installer


def test_release_and_rollback_helpers_are_directly_executable() -> None:
    python = Path(sys.executable)
    for relative_path in (
        Path("scripts/admin/release_erp.py"),
        Path("scripts/admin/rollback_erp.py"),
    ):
        result = subprocess.run(
            [str(python), "-X", "utf8", str(relative_path), "--help"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, result.stderr


def test_powershell_release_and_fault_scripts_have_valid_syntax() -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("Windows PowerShell is not available")
    for relative_path in (
        "scripts/admin/release_erp.ps1",
        "scripts/admin/rollback_erp.ps1",
        "scripts/windows/install_desktop_shortcut.ps1",
    ):
        script_path = (PROJECT_ROOT / relative_path).resolve()
        quoted = str(script_path).replace("'", "''")
        command = (
            "$tokens=$null;$errors=$null;"
            f"[System.Management.Automation.Language.Parser]::ParseFile('{quoted}',"
            "[ref]$tokens,[ref]$errors)|Out-Null;"
            "if($errors.Count -ne 0){$errors|ForEach-Object{Write-Error $_.Message};exit 1}"
        )
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
