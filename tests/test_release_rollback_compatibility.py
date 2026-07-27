from __future__ import annotations

import io
import json
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

from scripts.admin import release_erp, release_state_common, rollback_runtime


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_SHA = "1" * 40
RELEASE_SHA = "2" * 40
REVISION = "same_revision"


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
        connection.execute("INSERT INTO customers(name) VALUES ('隔离客户')")
        connection.execute("INSERT INTO sales_orders VALUES (1, 1)")
        connection.commit()
    return path


def _output_root(project_root: Path, suffix: str) -> Path:
    return (
        project_root.parent
        / "tm-rollback-runtimes"
        / f"compatibility-{suffix}"
    )


def _fake_release_evidence(
    tmp_path: Path,
    database: Path,
) -> tuple[Path, dict, Path]:
    backup_path = tmp_path / "backup" / "before.sqlite3"
    backup = release_erp.create_sqlite_copy(database, backup_path)
    plan_path = tmp_path / "release_plan.json"
    plan = {
        "schema_version": 3,
        "status": "completed",
        "previous_code_sha": PREVIOUS_SHA,
        "previous_code_revision": REVISION,
        "code_sha": RELEASE_SHA,
        "source": release_erp.inspect_database(database),
        "backup": backup,
        "completed_database": {
            **release_erp.inspect_database(database),
            "logical_fingerprint": (
                release_state_common.sqlite_logical_fingerprint(database)
            ),
        },
    }
    pointer_path = tmp_path / "pointer.json"
    pointer = {
        "schema_version": 2,
        "release_plan_path": str(plan_path),
        "previous_code_sha": PREVIOUS_SHA,
        "code_sha": RELEASE_SHA,
    }
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
    return pointer_path, plan, plan_path


def _install_safe_mocks(
    monkeypatch: pytest.MonkeyPatch,
    pointer_path: Path,
    plan: dict,
    plan_path: Path,
) -> None:
    def fake_git(
        _project_root: Path,
        *arguments: str,
        text: bool = True,
    ) -> str | bytes:
        assert text is True
        if arguments == ("branch", "--show-current"):
            return "factory-current-baseline\n"
        if arguments == ("rev-parse", "HEAD"):
            return RELEASE_SHA + "\n"
        if arguments == ("status", "--porcelain"):
            return ""
        raise AssertionError(arguments)

    def fake_archive(
        _project_root: Path,
        previous_sha: str,
        runtime_dir: Path,
    ) -> None:
        assert previous_sha == PREVIOUS_SHA
        runtime_dir.mkdir(parents=True)
        (runtime_dir / "app.py").write_text(
            "APP_MARKER = 'isolated-old-version'\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(
        rollback_runtime,
        "_verify_release_evidence",
        lambda actual, _root: (
            {"previous_code_sha": PREVIOUS_SHA, "code_sha": RELEASE_SHA},
            plan,
            plan_path,
        )
        if actual == pointer_path
        else pytest.fail("unexpected pointer"),
    )
    monkeypatch.setattr(rollback_runtime, "_git", fake_git)
    monkeypatch.setattr(
        rollback_runtime,
        "code_revision_at",
        lambda *_args: REVISION,
    )
    monkeypatch.setattr(rollback_runtime, "assert_ancestor", lambda *_args: None)
    monkeypatch.setattr(
        rollback_runtime,
        "_dependency_manifest_hashes",
        lambda *_args: {},
    )
    monkeypatch.setattr(rollback_runtime, "_archive_runtime", fake_archive)
    monkeypatch.setattr(
        rollback_runtime,
        "_run_old_alembic_current",
        lambda *_args: f"{REVISION} (head)",
    )

    def fake_health(
        _python: Path,
        runtime_dir: Path,
        _database: Path,
        host: str,
        port: int,
        _timeout: int,
        _secret: str,
    ) -> dict:
        log_path = runtime_dir.parent / "old_runtime.log"
        log_path.write_text("isolated health ok\n", encoding="utf-8")
        return {
            "health_url": f"http://{host}:{port}/api/health",
            "status_code": 200,
            "body": {"ok": True},
            "launcher_pid": 100,
            "listener_pid": 101,
            "listener_ancestry": [101, 100],
            "verification": "unit-test",
            "process_log_path": str(log_path),
            "process_log_sha256": release_erp.sha256_file(log_path),
        }

    monkeypatch.setattr(
        rollback_runtime,
        "_start_health_check",
        fake_health,
    )
    monkeypatch.setattr(rollback_runtime, "_assert_port_available", lambda *_: None)
    monkeypatch.setattr(
        rollback_runtime,
        "_controller_manifest",
        lambda *_args: {
            "algorithm": "unit-controller-v1",
            "sha256": "a" * 64,
            "file_count": 1,
            "files": [{"path": "unit.py", "sha256": "a" * 64, "size": 1}],
        },
    )


def _prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database: Path,
    suffix: str,
) -> dict:
    pointer_path, plan, plan_path = _fake_release_evidence(tmp_path, database)
    _install_safe_mocks(monkeypatch, pointer_path, plan, plan_path)
    output_root = _output_root(tmp_path / "formal-checkout", suffix)
    return rollback_runtime.prepare_compatibility(
        pointer_path=pointer_path,
        database=database,
        output_root=output_root,
        report_path=output_root / "compatibility.json",
        port=18120,
        python_path=Path(sys.executable),
        project_root=tmp_path / "formal-checkout",
    )


def test_prepare_full_rollback_uses_backup_and_signs_both_evidence_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "formal-checkout"
    project_root.mkdir()
    database = _database(project_root / "data" / "formal.sqlite3")
    formal_hash_before = release_erp.sha256_file(database)

    result = _prepare(
        tmp_path,
        monkeypatch,
        database=database,
        suffix="full",
    )

    assert result["mode"] == "full_rollback"
    assert release_erp.sha256_file(database) == formal_hash_before
    report = json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))
    manifest = json.loads(
        Path(result["runtime_manifest_path"]).read_text(encoding="utf-8")
    )
    release_state_common.verify_evidence(
        report,
        project_root=project_root,
        purpose=rollback_runtime.COMPATIBILITY_REPORT_PURPOSE,
    )
    release_state_common.verify_evidence(
        manifest,
        project_root=project_root,
        purpose=rollback_runtime.RUNTIME_MANIFEST_PURPOSE,
    )
    assert report["runtime_environment"]["bind_host"] == "127.0.0.1"
    assert report["runtime_environment"]["database_path"] == (
        result["rehearsal_database"]
    )
    assert report["rehearsal_logical_fingerprint_before"] == (
        report["rehearsal_logical_fingerprint_after"]
    )
    assert report["execution_eligible"] is False
    verified = rollback_runtime.verify_compatibility(
        report_path=Path(result["report_path"]),
        project_root=project_root,
    )
    assert verified["status"] == "compatibility_verified"

    runtime_file = Path(result["runtime_dir"]) / "app.py"
    runtime_file.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(
        rollback_runtime.CompatibilityError,
        match="运行文件已变化",
    ):
        rollback_runtime.verify_compatibility(
            report_path=Path(result["report_path"]),
            project_root=project_root,
        )


def test_prepare_code_only_uses_current_copy_without_changing_formal_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "formal-checkout"
    project_root.mkdir()
    database = _database(project_root / "data" / "formal.sqlite3")
    pointer_path, plan, plan_path = _fake_release_evidence(tmp_path, database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE customers SET name = '发布后的真实业务数据' WHERE id = 1"
        )
        connection.commit()
    formal_logical = release_state_common.sqlite_logical_fingerprint(database)
    formal_hash_before = release_erp.sha256_file(database)
    _install_safe_mocks(monkeypatch, pointer_path, plan, plan_path)
    output_root = _output_root(project_root, "code-only")

    result = rollback_runtime.prepare_compatibility(
        pointer_path=pointer_path,
        database=database,
        output_root=output_root,
        report_path=output_root / "compatibility.json",
        port=18121,
        python_path=Path(sys.executable),
        project_root=project_root,
    )

    assert result["mode"] == "code_only"
    assert release_erp.sha256_file(database) == formal_hash_before
    report = json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))
    assert report["source_database"] == str(database.resolve())
    assert report["formal_database_logical_fingerprint"] == formal_logical
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE customers SET name = '兼容证据之后的新业务写入' WHERE id = 1"
        )
        connection.commit()
    with pytest.raises(
        rollback_runtime.CompatibilityError,
        match="当前数据库内容已变化",
    ):
        rollback_runtime.verify_compatibility(
            report_path=Path(result["report_path"]),
            project_root=project_root,
        )


def test_changed_data_with_incompatible_revision_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "formal-checkout"
    project_root.mkdir()
    database = _database(project_root / "data" / "formal.sqlite3")
    pointer_path, plan, plan_path = _fake_release_evidence(tmp_path, database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE alembic_version SET version_num = 'new_revision'"
        )
        connection.commit()
    _install_safe_mocks(monkeypatch, pointer_path, plan, plan_path)
    output_root = _output_root(project_root, "incompatible")

    with pytest.raises(rollback_runtime.CompatibilityError, match="schema 不兼容"):
        rollback_runtime.prepare_compatibility(
            pointer_path=pointer_path,
            database=database,
            output_root=output_root,
            report_path=output_root / "compatibility.json",
            port=18122,
            python_path=Path(sys.executable),
            project_root=project_root,
        )
    assert not (output_root / "compatibility.json").exists()


def test_checkout_must_match_signed_release_sha(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "formal-checkout"
    project_root.mkdir()
    database = _database(project_root / "data" / "formal.sqlite3")
    pointer_path, plan, plan_path = _fake_release_evidence(tmp_path, database)
    _install_safe_mocks(monkeypatch, pointer_path, plan, plan_path)

    def mismatched_git(
        _project_root: Path,
        *arguments: str,
        text: bool = True,
    ) -> str | bytes:
        assert text is True
        if arguments == ("branch", "--show-current"):
            return "factory-current-baseline\n"
        if arguments == ("rev-parse", "HEAD"):
            return "3" * 40 + "\n"
        if arguments == ("status", "--porcelain"):
            return ""
        raise AssertionError(arguments)

    monkeypatch.setattr(rollback_runtime, "_git", mismatched_git)
    output_root = _output_root(project_root, "wrong-head")
    with pytest.raises(
        rollback_runtime.CompatibilityError,
        match="不是最近签名发布版本",
    ):
        rollback_runtime.prepare_compatibility(
            pointer_path=pointer_path,
            database=database,
            output_root=output_root,
            report_path=output_root / "compatibility.json",
            port=18125,
            python_path=Path(sys.executable),
            project_root=project_root,
        )


@pytest.mark.parametrize(
    ("host", "port", "message"),
    [
        ("0.0.0.0", 18120, "loopback"),
        ("127.0.0.1", 8000, "正式端口 8000"),
    ],
)
def test_network_boundary_rejects_nonloopback_and_formal_port(
    tmp_path: Path,
    host: str,
    port: int,
    message: str,
) -> None:
    project_root = tmp_path / "formal-checkout"
    project_root.mkdir()
    database = _database(project_root / "data" / "formal.sqlite3")
    output_root = tmp_path / "isolated"

    with pytest.raises(rollback_runtime.CompatibilityError, match=message):
        rollback_runtime.prepare_compatibility(
            pointer_path=tmp_path / "missing-pointer.json",
            database=database,
            output_root=output_root,
            report_path=output_root / "compatibility.json",
            port=port,
            bind_host=host,
            python_path=Path(sys.executable),
            project_root=project_root,
        )


def test_output_root_must_be_new_and_outside_formal_checkout(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "formal-checkout"
    project_root.mkdir()
    database = _database(project_root / "data" / "formal.sqlite3")

    with pytest.raises(rollback_runtime.CompatibilityError, match="不得位于"):
        rollback_runtime.prepare_compatibility(
            pointer_path=tmp_path / "missing-pointer.json",
            database=database,
            output_root=project_root / "runtime",
            report_path=project_root / "runtime" / "compatibility.json",
            port=18120,
            python_path=Path(sys.executable),
            project_root=project_root,
        )

    arbitrary_root = tmp_path / "arbitrary-outside"
    with pytest.raises(
        rollback_runtime.CompatibilityError,
        match="固定目录",
    ):
        rollback_runtime.prepare_compatibility(
            pointer_path=tmp_path / "missing-pointer.json",
            database=database,
            output_root=arbitrary_root,
            report_path=arbitrary_root / "compatibility.json",
            port=18120,
            python_path=Path(sys.executable),
            project_root=project_root,
        )

    output_root = _output_root(project_root, "nonempty")
    output_root.mkdir(parents=True)
    (output_root / "keep.txt").write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(rollback_runtime.CompatibilityError, match="不存在或为空"):
        rollback_runtime.prepare_compatibility(
            pointer_path=tmp_path / "missing-pointer.json",
            database=database,
            output_root=output_root,
            report_path=output_root / "compatibility.json",
            port=18120,
            python_path=Path(sys.executable),
            project_root=project_root,
        )


def test_occupied_loopback_port_is_rejected() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = int(listener.getsockname()[1])
        with pytest.raises(rollback_runtime.CompatibilityError, match="已被占用"):
            rollback_runtime._assert_port_available("127.0.0.1", port)


def test_isolated_environment_does_not_inherit_formal_erp_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ERP_DATABASE_PATH", r"D:\formal\carton_erp.sqlite3")
    monkeypatch.setenv("ERP_BACKUP_DIR", r"D:\formal\backups")
    monkeypatch.setenv("ERP_SECRET_KEY_FILE", r"D:\formal\session_secret.key")
    monkeypatch.setenv("PYTHONPATH", r"D:\untrusted-python")
    monkeypatch.setenv("ALEMBIC_CONFIG", r"D:\untrusted-alembic.ini")
    monkeypatch.setenv("UVICORN_PORT", "8000")
    database = tmp_path / "copy.sqlite3"

    environment = rollback_runtime._isolated_environment(
        database,
        "127.0.0.1",
        18120,
        "isolated-secret-" + "x" * 48,
    )

    assert environment["ERP_DATABASE_PATH"] == str(database)
    assert "ERP_BACKUP_DIR" not in environment
    assert "ERP_SECRET_KEY_FILE" not in environment
    assert "PYTHONPATH" not in environment
    assert "ALEMBIC_CONFIG" not in environment
    assert "UVICORN_PORT" not in environment
    assert environment["ERP_ENVIRONMENT"] == "test"
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"


def test_dependency_gate_ignores_only_line_endings_and_blocks_real_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "requirements.txt").write_bytes(b"one==1\r\ntwo==2\r\n")

    def fake_cat_file(arguments: list[str], **_kwargs) -> subprocess.CompletedProcess:
        reference = arguments[-1]
        return subprocess.CompletedProcess(
            arguments,
            0 if reference.endswith(":requirements.txt") else 1,
        )

    monkeypatch.setattr(rollback_runtime.subprocess, "run", fake_cat_file)
    monkeypatch.setattr(
        rollback_runtime,
        "_git",
        lambda *_args, **_kwargs: b"one==1\ntwo==2\n",
    )
    result = rollback_runtime._dependency_manifest_hashes(
        tmp_path,
        PREVIOUS_SHA,
    )
    assert result["requirements.txt"]["exists"] is True

    (tmp_path / "requirements.txt").write_text(
        "one==1\nchanged==3\n",
        encoding="utf-8",
    )
    with pytest.raises(rollback_runtime.CompatibilityError, match="不一致"):
        rollback_runtime._dependency_manifest_hashes(
            tmp_path,
            PREVIOUS_SHA,
        )


def test_archive_rejects_path_traversal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode="w") as bundle:
        member = tarfile.TarInfo("../escaped.txt")
        payload = b"escape"
        member.size = len(payload)
        bundle.addfile(member, io.BytesIO(payload))
    monkeypatch.setattr(
        rollback_runtime,
        "_git",
        lambda *_args, **_kwargs: archive_bytes.getvalue(),
    )

    with pytest.raises(rollback_runtime.CompatibilityError, match="不安全路径"):
        rollback_runtime._archive_runtime(
            tmp_path / "checkout",
            PREVIOUS_SHA,
            tmp_path / "isolated" / "runtime",
        )
    assert not (tmp_path / "escaped.txt").exists()


def test_runtime_mutation_blocks_signed_compatibility_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "formal-checkout"
    project_root.mkdir()
    database = _database(project_root / "data" / "formal.sqlite3")
    pointer_path, plan, plan_path = _fake_release_evidence(tmp_path, database)
    _install_safe_mocks(monkeypatch, pointer_path, plan, plan_path)

    def mutate_runtime(
        _python: Path,
        runtime_dir: Path,
        _database_path: Path,
        host: str,
        port: int,
        _timeout: int,
        _secret: str,
    ) -> dict:
        (runtime_dir / "unexpected.log").write_text("mutation", encoding="utf-8")
        return {
            "health_url": f"http://{host}:{port}/api/health",
            "status_code": 200,
            "body": {"ok": True},
        }

    monkeypatch.setattr(
        rollback_runtime,
        "_start_health_check",
        mutate_runtime,
    )
    output_root = _output_root(project_root, "mutated-runtime")
    with pytest.raises(
        rollback_runtime.CompatibilityError,
        match="改变了运行目录",
    ):
        rollback_runtime.prepare_compatibility(
            pointer_path=pointer_path,
            database=database,
            output_root=output_root,
            report_path=output_root / "compatibility.json",
            port=18123,
            python_path=Path(sys.executable),
            project_root=project_root,
        )
    assert not (output_root / "compatibility.json").exists()


def test_phase1_entry_has_no_formal_switch_restore_or_service_stop() -> None:
    powershell = (
        PROJECT_ROOT / "scripts" / "admin" / "rollback_runtime.ps1"
    ).read_text(encoding="utf-8")
    python = (
        PROJECT_ROOT / "scripts" / "admin" / "rollback_runtime.py"
    ).read_text(encoding="utf-8")
    combined = powershell + "\n" + python

    for forbidden in (
        "Stop-Process",
        "git switch",
        "git reset",
        "git stash",
        "alembic upgrade",
        "restore_from_backup",
        "ApprovalToken",
    ):
        assert forbidden not in combined
    assert "--output-root" in powershell
    assert "--bind-host" in powershell
    assert "-I -B -X utf8" in powershell
    assert "PYTHONDONTWRITEBYTECODE" in powershell
    assert "尚未执行任何正式回退" in powershell
    assert "--previous-code-sha" not in combined
    assert "--revision" not in combined
    assert "--reason" not in combined


def test_rollback_runtime_cli_and_powershell_syntax() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "scripts/admin/rollback_runtime.py",
            "--help",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    prepare_help = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-X",
            "utf8",
            "scripts/admin/rollback_runtime.py",
            "prepare-compatibility",
            "--help",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert prepare_help.returncode == 0, prepare_help.stderr
    assert "--project-root" not in prepare_help.stdout
    assert "--python" not in prepare_help.stdout

    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("Windows PowerShell is not available")
    script_path = (
        PROJECT_ROOT / "scripts" / "admin" / "rollback_runtime.ps1"
    ).resolve()
    quoted = str(script_path).replace("'", "''")
    command = (
        "$tokens=$null;$errors=$null;"
        f"[System.Management.Automation.Language.Parser]::ParseFile('{quoted}',"
        "[ref]$tokens,[ref]$errors)|Out-Null;"
        "if($errors.Count -ne 0){$errors|ForEach-Object{Write-Error $_.Message};"
        "exit 1}"
    )
    parsed = subprocess.run(
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
    assert parsed.returncode == 0, parsed.stderr


def test_real_previous_commit_starts_only_on_fresh_isolated_database_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Exercise the real Git archive, Alembic CLI and loopback health check."""
    monkeypatch.setenv("ERP_SECRET_KEY", "isolated-uat-secret-" + "x" * 48)
    previous_sha = str(
        rollback_runtime._git(PROJECT_ROOT, "rev-parse", "HEAD^")
    ).strip()
    release_sha = str(
        rollback_runtime._git(PROJECT_ROOT, "rev-parse", "HEAD")
    ).strip()
    previous_revision = release_state_common.code_revision_at(
        PROJECT_ROOT,
        previous_sha,
    )
    current_revision = release_erp.code_revision(PROJECT_ROOT)
    assert previous_revision == current_revision

    database = tmp_path / "uat-source" / "carton_erp.sqlite3"
    database.parent.mkdir(parents=True)
    release_erp._run_migration(database, current_revision, PROJECT_ROOT)
    source = release_erp.inspect_database(database)
    release_erp.assert_healthy(source, label="隔离 UAT 来源")
    release_erp.assert_business_smoke(source, label="隔离 UAT 来源")
    backup_path = tmp_path / "uat-backup" / "before.sqlite3"
    backup = release_erp.create_sqlite_copy(database, backup_path)
    completed_logical = (
        release_state_common.sqlite_logical_fingerprint_via_copy(database)
    )
    plan_path = tmp_path / "uat-evidence" / "release_plan.json"
    release_erp._write_signed_plan(
        plan_path,
        {
            "schema_version": 3,
            "status": "completed",
            "previous_code_sha": previous_sha,
            "previous_code_revision": previous_revision,
            "code_sha": release_sha,
            "source": source,
            "backup": backup,
            "completed_database": {
                **source,
                "logical_fingerprint": completed_logical,
            },
        },
        project_root=PROJECT_ROOT,
        purpose=rollback_runtime.PLAN_PURPOSE,
    )
    pointer_path = tmp_path / "uat-evidence" / "latest_completed_release.json"
    release_erp._write_signed_plan(
        pointer_path,
        {
            "schema_version": 2,
            "release_plan_path": str(plan_path.resolve()),
            "release_plan_sha256": release_erp.sha256_file(plan_path),
            "previous_code_sha": previous_sha,
            "code_sha": release_sha,
        },
        project_root=PROJECT_ROOT,
        purpose=rollback_runtime.POINTER_PURPOSE,
    )
    formal_hash_before = release_erp.sha256_file(database)
    allowed_base = PROJECT_ROOT.parent / "tm-rollback-runtimes"
    allowed_base.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="pytest-p05b-",
        dir=allowed_base,
    ) as output_name:
        output_root = Path(output_name)
        result = rollback_runtime.prepare_compatibility(
            pointer_path=pointer_path,
            database=database,
            output_root=output_root,
            report_path=output_root / "compatibility.json",
            port=18139,
            python_path=Path(sys.executable),
            project_root=PROJECT_ROOT,
            timeout_seconds=30,
        )

        assert result["mode"] == "full_rollback"
        assert result["previous_code_sha"] == previous_sha
        assert release_erp.sha256_file(database) == formal_hash_before
        report = json.loads(
            Path(result["report_path"]).read_text(encoding="utf-8")
        )
        assert report["health"]["health_url"] == (
            "http://127.0.0.1:18139/api/health"
        )
        assert report["health"]["status_code"] == 200
        assert report["health"]["body"] == {"ok": True}
        assert report["health"]["launcher_pid"] > 0
        assert report["health"]["listener_pid"] > 0
