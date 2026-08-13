from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pytest

from scripts.admin import release_erp
from scripts.admin import weekly_home_uat as weekly


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _create_factory_shape_database(path: Path) -> None:
    revision = release_erp.code_revision(PROJECT_ROOT)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE alembic_version (version_num TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO alembic_version(version_num) VALUES (?)",
            (revision,),
        )
        for table in release_erp.REQUIRED_BUSINESS_TABLES:
            connection.execute(
                f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, note TEXT)'
            )
        connection.execute(
            "INSERT INTO customers(id, note) VALUES (1, '匿名客户')"
        )
        connection.execute(
            "INSERT INTO products(id, note) VALUES (1, '匿名产品')"
        )
        connection.commit()


@pytest.fixture()
def exported_package(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "anonymous_factory.sqlite3"
    _create_factory_shape_database(source)
    result = weekly.export_package(
        database=source,
        output_root=tmp_path / "exports",
        project_root=PROJECT_ROOT,
        now=datetime(2026, 8, 4, 15, 30, 0),
        hostname=weekly.FACTORY_HOSTNAME,
    )
    package_dir = Path(result["package_dir"])
    yield package_dir, source

    received = tmp_path / "home" / "received"
    if received.exists():
        for database in received.rglob("*.sqlite3"):
            database.chmod(stat.S_IREAD | stat.S_IWRITE)


def test_export_contains_verified_manifest_and_consistent_copy(
    exported_package: tuple[Path, Path],
) -> None:
    package_dir, source = exported_package
    verified = weekly.verify_package(package_dir)
    manifest = verified["manifest"]

    assert manifest["source"]["hostname"] == weekly.FACTORY_HOSTNAME
    assert manifest["source"]["git_sha"] == release_erp.git_sha(PROJECT_ROOT)
    assert manifest["source"]["code_alembic_head"] == release_erp.code_revision(
        PROJECT_ROOT
    )
    assert verified["snapshot"]["revision"] == release_erp.code_revision(
        PROJECT_ROOT
    )
    assert verified["snapshot"]["integrity_check"] == "ok"
    assert verified["snapshot"]["foreign_key_violations"] == 0
    assert verified["snapshot"]["core_counts"]["customers"] == 1
    assert verified["snapshot"]["core_counts"]["products"] == 1
    assert verified["snapshot"]["sha256"] == manifest["database"]["sha256"]
    assert verified["database"].resolve() != source.resolve()


def test_verify_accepts_readable_windows_nas_path_when_resolve_returns_1005(
    exported_package: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_dir, _ = exported_package
    package_absolute = package_dir.absolute()
    original_resolve = Path.resolve

    def resolve_with_nas_driver_failure(
        self: Path, strict: bool = False
    ) -> Path:
        absolute = Path(os.path.abspath(self))
        if absolute == package_absolute or package_absolute in absolute.parents:
            error = OSError("模拟 Windows NAS 映射盘 Path.resolve 失败")
            error.winerror = 1005
            raise error
        return original_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve_with_nas_driver_failure)

    verified = weekly.verify_package(package_dir)

    assert verified["database"] == package_absolute / weekly.DATABASE_FILENAME
    assert verified["snapshot"]["integrity_check"] == "ok"
    assert verified["snapshot"]["foreign_key_violations"] == 0


def test_verify_rejects_manifest_or_database_tampering(
    exported_package: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    package_dir, _ = exported_package
    manifest_tamper = tmp_path / "tamper-manifest" / package_dir.name
    shutil.copytree(package_dir, manifest_tamper)
    with (manifest_tamper / weekly.MANIFEST_FILENAME).open(
        "a", encoding="utf-8"
    ) as handle:
        handle.write(" ")
    with pytest.raises(weekly.WeeklyUatError, match="manifest 校验失败"):
        weekly.verify_package(manifest_tamper)

    database_tamper = tmp_path / "tamper-database" / package_dir.name
    shutil.copytree(package_dir, database_tamper)
    with (database_tamper / weekly.DATABASE_FILENAME).open("ab") as handle:
        handle.write(b"tampered")
    with pytest.raises(weekly.WeeklyUatError, match="manifest 不一致"):
        weekly.verify_package(database_tamper)


def test_import_keeps_received_copy_immutable_and_reset_discards_only_uat_changes(
    exported_package: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    package_dir, _ = exported_package
    uat_root = tmp_path / "home"
    imported = weekly.import_package(
        package_dir=package_dir,
        uat_root=uat_root,
        project_root=PROJECT_ROOT,
        hostname="HOME-UAT",
    )
    received = Path(imported["received_database"])
    working = Path(imported["working_database"])
    runtime = Path(imported["runtime_file"])
    original_sha = weekly.sha256_file(received)

    assert original_sha == weekly.sha256_file(working)
    assert not (received.stat().st_mode & stat.S_IWRITE)
    with closing(sqlite3.connect(working)) as connection:
        connection.execute(
            "INSERT INTO customers(id, note) VALUES (2, '家庭模拟客户')"
        )
        connection.commit()
    assert weekly.sha256_file(working) != original_sha
    assert weekly.sha256_file(received) == original_sha
    imported_again = weekly.import_package(
        package_dir=package_dir,
        uat_root=uat_root,
        project_root=PROJECT_ROOT,
        hostname="HOME-UAT",
    )
    assert imported_again["existing_working_copy"] is True
    with closing(sqlite3.connect(working)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 2

    sidecars = [Path(str(working) + suffix) for suffix in ("-wal", "-shm", "-journal")]
    for sidecar in sidecars:
        sidecar.write_text("仅家庭 UAT 重置测试", encoding="utf-8")

    reset = weekly.reset_working_copy(
        runtime_file=runtime,
        uat_root=uat_root,
        confirmed=True,
    )
    assert reset["sha256"] == original_sha
    assert all(not sidecar.exists() for sidecar in sidecars)
    with closing(sqlite3.connect(working)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 1
    assert weekly.sha256_file(received) == original_sha


def test_prepare_candidate_run_requires_descendant_and_never_overwrites(
    exported_package: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_dir, _ = exported_package
    uat_root = tmp_path / "home"
    imported = weekly.import_package(
        package_dir=package_dir,
        uat_root=uat_root,
        project_root=PROJECT_ROOT,
        hostname="HOME-UAT",
    )
    received = Path(imported["received_database"])
    received_sha = weekly.sha256_file(received)
    source_git_sha = imported["git_sha"]
    candidate_git_sha = "f" * 40

    monkeypatch.setattr(weekly, "git_sha", lambda _root: candidate_git_sha)
    monkeypatch.setattr(weekly, "code_revision", lambda _root: "candidate-head")
    monkeypatch.setattr(weekly, "_git_is_ancestor", lambda *_args: True)

    prepared = weekly.prepare_candidate_run(
        source_runtime_file=Path(imported["runtime_file"]),
        uat_root=uat_root,
        project_root=PROJECT_ROOT,
        hostname="HOME-UAT",
    )
    working = Path(prepared["working_database"])
    runtime = json.loads(Path(prepared["runtime_file"]).read_text(encoding="utf-8"))

    assert prepared["source_git_sha"] == source_git_sha
    assert prepared["git_sha"] == candidate_git_sha
    assert prepared["target_revision"] == "candidate-head"
    assert prepared["migration_required"] is True
    assert weekly.sha256_file(working) == received_sha
    assert weekly.sha256_file(received) == received_sha
    assert working.stat().st_mode & stat.S_IWRITE
    assert not (received.stat().st_mode & stat.S_IWRITE)
    assert runtime["candidate"] is True
    assert runtime["working_database"] == str(working)
    with pytest.raises(weekly.WeeklyUatError, match="拒绝覆盖既有候选"):
        weekly.prepare_candidate_run(
            source_runtime_file=Path(imported["runtime_file"]),
            uat_root=uat_root,
            project_root=PROJECT_ROOT,
            hostname="HOME-UAT",
        )


def test_prepare_candidate_run_rejects_non_descendant(
    exported_package: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_dir, _ = exported_package
    uat_root = tmp_path / "home"
    imported = weekly.import_package(
        package_dir=package_dir,
        uat_root=uat_root,
        project_root=PROJECT_ROOT,
        hostname="HOME-UAT",
    )
    monkeypatch.setattr(weekly, "git_sha", lambda _root: "e" * 40)
    monkeypatch.setattr(weekly, "_git_is_ancestor", lambda *_args: False)

    with pytest.raises(weekly.WeeklyUatError, match="不是工厂数据包代码的 Git 后代"):
        weekly.prepare_candidate_run(
            source_runtime_file=Path(imported["runtime_file"]),
            uat_root=uat_root,
            project_root=PROJECT_ROOT,
            hostname="HOME-UAT",
        )


def test_import_and_start_gates_reject_wrong_host_sha_revision_path_and_port(
    exported_package: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    package_dir, _ = exported_package
    uat_root = tmp_path / "home"
    with pytest.raises(weekly.WeeklyUatError, match="禁止在工厂正式主机"):
        weekly.import_package(
            package_dir=package_dir,
            uat_root=uat_root,
            project_root=PROJECT_ROOT,
            hostname=weekly.FACTORY_HOSTNAME,
        )

    wrong_sha_package = tmp_path / "wrong-sha" / package_dir.name
    shutil.copytree(package_dir, wrong_sha_package)
    manifest_path = wrong_sha_package / weekly.MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source"]["git_sha"] = "0" * 40
    content = weekly._json_bytes(manifest)
    manifest_path.write_bytes(content)
    (wrong_sha_package / weekly.MANIFEST_CHECKSUM_FILENAME).write_text(
        weekly._sha256_bytes(content) + "\n",
        encoding="ascii",
    )
    with pytest.raises(weekly.WeeklyUatError, match="代码 SHA"):
        weekly.import_package(
            package_dir=wrong_sha_package,
            uat_root=uat_root,
            project_root=PROJECT_ROOT,
            hostname="HOME-UAT",
        )

    imported = weekly.import_package(
        package_dir=package_dir,
        uat_root=uat_root,
        project_root=PROJECT_ROOT,
        hostname="HOME-UAT",
    )
    database = Path(imported["working_database"])
    valid = weekly.validate_home_runtime(
        database=database,
        uat_root=uat_root,
        project_root=PROJECT_ROOT,
        port=18200,
        hostname="HOME-UAT",
    )
    assert valid["bind_host"] == "127.0.0.1"
    assert valid["environment"] == "test"
    assert valid["workers"] == 1

    with pytest.raises(weekly.WeeklyUatError, match="端口"):
        weekly.validate_home_runtime(
            database=database,
            uat_root=uat_root,
            project_root=PROJECT_ROOT,
            port=8000,
            hostname="HOME-UAT",
        )
    with pytest.raises(weekly.WeeklyUatError, match="根目录内"):
        weekly.validate_home_runtime(
            database=tmp_path / "outside.sqlite3",
            uat_root=uat_root,
            project_root=PROJECT_ROOT,
            port=18200,
            hostname="HOME-UAT",
        )
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE alembic_version SET version_num='wrong-head'")
        connection.commit()
    with pytest.raises(release_erp.ReleaseGateError, match="revision 不匹配"):
        weekly.validate_home_runtime(
            database=database,
            uat_root=uat_root,
            project_root=PROJECT_ROOT,
            port=18200,
            hostname="HOME-UAT",
        )


def test_reset_requires_explicit_confirmation(
    exported_package: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    package_dir, _ = exported_package
    uat_root = tmp_path / "home"
    imported = weekly.import_package(
        package_dir=package_dir,
        uat_root=uat_root,
        project_root=PROJECT_ROOT,
        hostname="HOME-UAT",
    )
    with pytest.raises(weekly.WeeklyUatError, match="必须显式确认"):
        weekly.reset_working_copy(
            runtime_file=Path(imported["runtime_file"]),
            uat_root=uat_root,
            confirmed=False,
        )


def test_powershell_entries_keep_factory_and_home_boundaries() -> None:
    export_source = (
        PROJECT_ROOT / "scripts" / "admin" / "export_weekly_home_uat.ps1"
    ).read_text(encoding="utf-8")
    import_source = (
        PROJECT_ROOT
        / "scripts"
        / "windows"
        / "import_start_weekly_home_uat.ps1"
    ).read_text(encoding="utf-8")
    reset_source = (
        PROJECT_ROOT / "scripts" / "windows" / "reset_weekly_home_uat.ps1"
    ).read_text(encoding="utf-8")
    launcher_source = (
        PROJECT_ROOT / "scripts" / "windows" / "start_erp_uat.ps1"
    ).read_text(encoding="utf-8")

    assert "PC-20250926DZYH" in export_source
    assert "factory-current-baseline" in export_source
    for marker in (
        "manifest.sha256",
        "fetch --prune origin",
        "merge-base --is-ancestor",
        "worktree add --detach",
        "weekly_home_uat.py",
        "start_erp_uat.ps1",
        "WinError 1005",
        ".nas-package-",
        "Copy-Item -LiteralPath $package",
        "Remove-Item -LiteralPath $stagingRoot",
    ):
        assert marker in import_source
    for marker in (
            "erp_uat_",
            "Win32_Process",
            "uvicorn",
            "from\\s+uvicorn\\.main\\s+import\\s+main",
        "--confirm-reset",
        "ConvertFrom-Json",
        '"nonce", "pid", "process_creation_token", "port", "isolation_id"',
        "Test-UatProcessCommandLine",
        "Assert-OwnedRecordNonce",
        "[Environment]::MachineName",
        '$pidFile = Join-Path $runRoot ("erp_uat_{0}.pid" -f $Port)',
        "UAT 所有权记录指向隔离目录之外",
    ):
        assert marker in reset_source
    assert "cleanup-owned" in reset_source
    for marker in (
        'ERP_ENVIRONMENT = "test"',
        'ERP_BIND_HOST = "127.0.0.1"',
        "ERP_SESSION_COOKIE_NAME",
        "weekly_home_uat.py",
        "PC-20250926DZYH",
        "UAT 数据库必须位于",
    ):
        assert marker in launcher_source
    assert "alembic upgrade" not in launcher_source
    for relative in (
        Path("scripts/admin/export_weekly_home_uat.bat"),
        Path("scripts/windows/import_start_weekly_home_uat.bat"),
        Path("scripts/windows/reset_weekly_home_uat.bat"),
    ):
        source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        assert "powershell.exe" in source
        assert "-ExecutionPolicy Bypass" in source


def test_reset_entry_supports_nonce_bound_pid_records_and_legacy_pid() -> None:
    source = (
        PROJECT_ROOT / "scripts" / "windows" / "reset_weekly_home_uat.ps1"
    ).read_text(encoding="utf-8-sig")

    for marker in (
        '$runRoot = Get-FullPath ([System.IO.Path]::GetDirectoryName($database))',
        '$pidFile = Join-Path $runRoot ("erp_uat_{0}.pid" -f $Port)',
        '$pidText -match "^\\d+$"',
        '"nonce", "pid", "process_creation_token", "port", "isolation_id"',
        "cleanup-owned",
        "Test-UatProcessCommandLine",
        "Win32_Process",
        "Assert-NoReparseComponents",
        "Assert-OwnedRecordNonce",
        "$expectedLeasePath",
        "$expectedAttestationPath",
        '[Environment]::MachineName -ieq "PC-20250926DZYH"',
    ):
        assert marker in source
    assert "$structuredPidFile" not in source
    assert "$legacyPidFile" not in source
    assert "$env:COMPUTERNAME" not in source

    command_check = source.index("Test-UatProcessCommandLine `")
    stop_process = source.index("Stop-Process -Id $pidValue -Force")
    stopped_check = source.index(
        'Get-CimInstance Win32_Process -Filter ("ProcessId=" + $pidValue) '
        "-ErrorAction Stop) {"
    )
    cleanup_owned = source.index("cleanup-owned")
    assert command_check < stop_process < stopped_check
    assert cleanup_owned < stop_process
    assert "Remove-Item -Path" not in source
    assert "Remove-Item *" not in source
