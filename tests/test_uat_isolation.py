from __future__ import annotations

import base64
import ctypes
import json
import hashlib
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from app.core import uat_isolation as isolation


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAYOUT_SOURCE = PROJECT_ROOT / "static" / "factory_maps" / "twin_layout_v1.json"


def make_database(path: Path, *, marker: str = "source") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "CREATE TABLE parent (id INTEGER PRIMARY KEY, marker TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL "
            "REFERENCES parent(id))"
        )
        connection.execute("INSERT INTO parent(id, marker) VALUES(1, ?)", (marker,))
        connection.execute("INSERT INTO child(id, parent_id) VALUES(1, 1)")
        connection.commit()


def prepare_layout(
    tmp_path: Path,
    *,
    port: int = 18181,
    source_database: Path | None = None,
    existing: bool = True,
) -> dict:
    root = tmp_path / "run"
    database = root / "carton_erp_home_uat.sqlite3"
    if existing:
        make_database(database)
    return isolation.prepare_uat_layout(
        root=root,
        database=database,
        port=port,
        layout_source=LAYOUT_SOURCE,
        source_database=source_database,
        protected_paths=(PROJECT_ROOT / "data" / "carton_erp.sqlite3",),
        forbidden_roots=(PROJECT_ROOT,),
    )


def apply_layout_environment(monkeypatch: pytest.MonkeyPatch, manifest: dict) -> None:
    values = {
        **manifest["environment"],
        "ERP_UAT_ROOT": manifest["root"],
        "ERP_UAT_ISOLATION_ID": manifest["isolation_id"],
        "ERP_UAT_PROTECTED_PATHS_JSON": json.dumps([]),
        "ERP_UAT_FORBIDDEN_ROOTS_JSON": json.dumps([str(PROJECT_ROOT)]),
        "ERP_ENVIRONMENT": "test",
        "ERP_BIND_HOST": "127.0.0.1",
        "ERP_PORT": manifest["cookie_name"].split("_")[2],
        "ERP_WORKERS": "1",
        "ERP_SESSION_COOKIE_NAME": manifest["cookie_name"],
        "ERP_SESSION_COOKIE_SECURE": "false",
        "ERP_SECRET_KEY": "",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_prepare_builds_complete_run_local_manifest_and_blocks_inherited_nas(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(
        "ERP_BACKUP_DIR",
        r"Z:\sata1-18015598002\BoxERP\backups",
    )
    manifest = prepare_layout(tmp_path)
    root = Path(manifest["root"])

    assert manifest["factory_twin_status"] == "missing_fail_closed"
    assert manifest["environment"]["ERP_BACKUP_DIR"] != os.environ["ERP_BACKUP_DIR"]
    assert Path(manifest["paths"]["layout_runtime_file"]).read_bytes() == (
        LAYOUT_SOURCE.read_bytes()
    )
    for name, value in manifest["paths"].items():
        path = Path(value)
        assert isolation.is_within(path, root), name
    for env_name in (
        "ERP_DATABASE_PATH",
        "ERP_LOG_DIR",
        "ERP_BACKUP_DIR",
        "ERP_FILE_STORAGE_DIR",
        "ERP_UPLOAD_TEMP_DIR",
        "ERP_INVOICE_EXPORT_DIR",
        "ERP_INVOICE_ATTACHMENT_DIR",
        "ERP_PDF_TRAINING_DIR",
        "ERP_TWIN_LAYOUT_RUNTIME_PATH",
        "ERP_TWIN_LAYOUT_DRAFT_PATH",
        "ERP_TWIN_LAYOUT_BACKUP_DIR",
        "ERP_LEGACY_UPLOAD_DIR",
        "ERP_FACTORY_TWIN_DATABASE_PATH",
        "ERP_SECRET_KEY_FILE",
        "ERP_UAT_ATTESTATION_PATH",
        "ERP_UAT_PID_PATH",
    ):
        assert env_name in manifest["environment"]


def test_cookie_and_secret_are_unique_to_database_root_and_port(tmp_path: Path) -> None:
    first = prepare_layout(tmp_path / "first", port=18181)
    second = prepare_layout(tmp_path / "second", port=18181)
    third = prepare_layout(tmp_path / "third", port=18182)

    assert len({first["cookie_name"], second["cookie_name"], third["cookie_name"]}) == 3
    secrets = {
        Path(item["paths"]["secret_file"]).read_text(encoding="utf-8")
        for item in (first, second, third)
    }
    assert len(secrets) == 3
    assert all("pytest-isolated-only" not in value for value in secrets)


def test_sqlite_backup_copy_refuses_overwrite_and_existing_copy_starts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    make_database(source, marker="authoritative")
    root = tmp_path / "run"
    destination = root / "carton_erp_home_uat.sqlite3"
    created = isolation.prepare_uat_layout(
        root=root,
        database=destination,
        port=18181,
        layout_source=LAYOUT_SOURCE,
        source_database=source,
        forbidden_roots=(PROJECT_ROOT,),
    )
    assert created["database_created"] is True
    with closing(sqlite3.connect(destination)) as connection:
        assert connection.execute("SELECT marker FROM parent").fetchone()[0] == "authoritative"

    with pytest.raises(isolation.UatIsolationError, match="overwrite"):
        isolation.prepare_uat_layout(
            root=root,
            database=destination,
            port=18181,
            layout_source=LAYOUT_SOURCE,
            source_database=source,
            forbidden_roots=(PROJECT_ROOT,),
        )

    resumed = isolation.prepare_uat_layout(
        root=root,
        database=destination,
        port=18181,
        layout_source=LAYOUT_SOURCE,
        forbidden_roots=(PROJECT_ROOT,),
    )
    assert resumed["database_created"] is False


def test_sqlite_copy_publish_race_never_deletes_racing_hardlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    make_database(source, marker="copy-source")
    destination = tmp_path / "run" / "uat.sqlite3"
    racing_owner = tmp_path / "racing-owner.sqlite3"
    make_database(racing_owner, marker="racing-owner")
    owner_sha256 = hashlib.sha256(racing_owner.read_bytes()).hexdigest()

    def publish_after_racing_actor(_temporary: Path, target: Path) -> None:
        os.link(racing_owner, target)
        raise FileExistsError(target)

    monkeypatch.setattr(isolation, "_publish_no_replace", publish_after_racing_actor)

    with pytest.raises(isolation.UatIsolationError, match="overwrite"):
        isolation.create_sqlite_copy(source, destination)

    assert destination.is_file()
    assert os.path.samefile(destination, racing_owner)
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == owner_sha256
    assert hashlib.sha256(racing_owner.read_bytes()).hexdigest() == owner_sha256
    assert list(destination.parent.glob("*.private-copy")) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows verified-file share lock")
def test_verified_sqlite_publish_lock_blocks_writers_until_rename(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "run" / "uat.sqlite3"
    make_database(source, marker="locked-source")
    original_publish = isolation._publish_no_replace
    writer_attempts: list[str] = []

    def publish_while_writer_attempts(temporary: Path, target: Path) -> None:
        for mode in ("r+b", "wb", "ab"):
            with pytest.raises(PermissionError):
                with temporary.open(mode):
                    pass
            writer_attempts.append(mode)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_void_p,
        ]
        create_file.restype = ctypes.c_void_p
        raw_writer = create_file(
            str(temporary),
            0x40000000,  # GENERIC_WRITE
            0x1 | 0x2 | 0x4,
            None,
            3,
            0,
            None,
        )
        assert raw_writer == ctypes.c_void_p(-1).value
        assert ctypes.get_last_error() in {5, 32, 33}
        writer_attempts.append("CreateFileW")
        original_publish(temporary, target)

    monkeypatch.setattr(
        isolation,
        "_publish_no_replace",
        publish_while_writer_attempts,
    )
    result = isolation.create_sqlite_copy(source, destination)

    assert writer_attempts == ["r+b", "wb", "ab", "CreateFileW"]
    assert result["integrity_check"] == "ok"
    with closing(sqlite3.connect(destination)) as connection:
        assert connection.execute("SELECT marker FROM parent").fetchone() == (
            "locked-source",
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows verified-file share lock")
def test_corruption_after_path_validation_is_rejected_under_publish_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "run" / "uat.sqlite3"
    make_database(source, marker="must-remain-valid")
    original_lock = isolation._windows_lock_verified_file
    injected = False

    def corrupt_then_lock(temporary: Path) -> tuple[int, bytes] | None:
        nonlocal injected
        with temporary.open("r+b") as handle:
            handle.write(b"NOT-SQLITE")
            handle.flush()
            os.fsync(handle.fileno())
        injected = True
        return original_lock(temporary)

    monkeypatch.setattr(isolation, "_windows_lock_verified_file", corrupt_then_lock)

    with pytest.raises(isolation.UatIsolationError, match="changed after final validation"):
        isolation.create_sqlite_copy(source, destination)

    assert injected is True
    assert not destination.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows verified-file share lock")
def test_valid_database_swap_after_validation_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    destination = tmp_path / "run" / "uat.sqlite3"
    make_database(source, marker="expected-source")
    make_database(replacement, marker="wrong-but-valid")
    original_lock = isolation._windows_lock_verified_file

    def replace_bytes_then_lock(temporary: Path) -> tuple[int, bytes] | None:
        temporary.write_bytes(replacement.read_bytes())
        return original_lock(temporary)

    monkeypatch.setattr(
        isolation,
        "_windows_lock_verified_file",
        replace_bytes_then_lock,
    )

    with pytest.raises(isolation.UatIsolationError, match="changed after final validation"):
        isolation.create_sqlite_copy(source, destination)

    assert not destination.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows file identity regression")
def test_database_hardlink_to_protected_file_is_rejected(tmp_path: Path) -> None:
    protected = tmp_path / "formal.sqlite3"
    make_database(protected)
    root = tmp_path / "run"
    root.mkdir()
    hardlink = root / "carton_erp_home_uat.sqlite3"
    os.link(protected, hardlink)

    with pytest.raises(isolation.UatIsolationError, match="hardlink|same file"):
        isolation.prepare_uat_layout(
            root=root,
            database=hardlink,
            port=18181,
            layout_source=LAYOUT_SOURCE,
            protected_paths=(protected,),
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_write_root_junction_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "run"
    root.mkdir()
    database = root / "carton_erp_home_uat.sqlite3"
    make_database(database)
    link = root / ".erp-uat"
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout

    with pytest.raises(isolation.UatIsolationError, match="symlink/junction"):
        isolation.prepare_uat_layout(
            root=root,
            database=database,
            port=18181,
            layout_source=LAYOUT_SOURCE,
        )
    assert list(outside.iterdir()) == []


def test_validate_rejects_any_write_root_outside_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = prepare_layout(tmp_path)
    apply_layout_environment(monkeypatch, manifest)
    valid = isolation.validate_uat_environment()
    assert valid["database"] == manifest["paths"]["database"]

    outside = tmp_path / "outside-backups"
    outside.mkdir()
    monkeypatch.setenv("ERP_BACKUP_DIR", str(outside))
    with pytest.raises(isolation.UatIsolationError, match="escapes UAT root"):
        isolation.validate_uat_environment()


def test_config_fails_before_creating_an_outside_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = prepare_layout(tmp_path)
    apply_layout_environment(monkeypatch, manifest)
    outside = tmp_path / "outside" / "stolen-secret.key"
    monkeypatch.setenv("ERP_SECRET_KEY_FILE", str(outside))
    monkeypatch.delenv("ERP_SECRET_KEY", raising=False)
    # Full-suite predecessors may already have imported this module. Exercise
    # the startup path itself rather than accepting the cached module object;
    # monkeypatch restores the prior cache entry after this test.
    monkeypatch.delitem(sys.modules, "app.core.config", raising=False)

    with pytest.raises(isolation.UatIsolationError, match="escapes UAT root"):
        import importlib

        importlib.import_module("app.core.config")
    assert not outside.exists()


def test_pdf_history_and_legacy_delete_fail_closed_outside_uat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = prepare_layout(tmp_path)
    apply_layout_environment(monkeypatch, manifest)
    formal_pdf = tmp_path / "formal.pdf"
    formal_pdf.write_bytes(b"%PDF-formal-sentinel")

    with pytest.raises(isolation.UatIsolationError, match="escapes isolated"):
        isolation.assert_uat_managed_path(
            formal_pdf,
            "ERP_PDF_TRAINING_DIR",
            label="PDF training sample",
        )

    from app.services.secure_uploads import remove_stored_reference

    formal_legacy = tmp_path / "worktree-legacy"
    formal_legacy.mkdir()
    sentinel = formal_legacy / "drawing.pdf"
    sentinel.write_bytes(b"formal-sentinel")
    errors = remove_stored_reference("/static/uploads/drawing.pdf")
    assert errors == []
    assert sentinel.read_bytes() == b"formal-sentinel"


def test_startup_reference_gate_rejects_copied_formal_pdf_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = prepare_layout(tmp_path)
    apply_layout_environment(monkeypatch, manifest)
    database = Path(manifest["paths"]["database"])
    formal_pdf = tmp_path / "formal-source.pdf"
    formal_pdf.write_bytes(b"%PDF-formal-sentinel")
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "CREATE TABLE pdf_order_training_samples "
            "(id INTEGER PRIMARY KEY, file_path TEXT, file_sha256 TEXT)"
        )
        connection.execute(
            "INSERT INTO pdf_order_training_samples(id, file_path, file_sha256) "
            "VALUES(1, ?, ?)",
            (str(formal_pdf), hashlib.sha256(formal_pdf.read_bytes()).hexdigest()),
        )
        connection.commit()

    with pytest.raises(isolation.UatIsolationError, match="outside its isolated copy"):
        isolation.validate_uat_database_references(database)


def test_real_app_lifespan_attests_every_isolated_consumer(
    tmp_path: Path,
) -> None:
    nonce = "0123456789abcdef0123456789abcdef"
    root = tmp_path / "lifespan-uat"
    database = root / "carton_erp_home_uat.sqlite3"
    twin_source = tmp_path / "factory-twin-source.sqlite3"
    make_database(database, marker="lifespan-uat")
    make_database(twin_source, marker="factory-twin-source")
    manifest = isolation.prepare_uat_layout(
        root=root,
        database=database,
        port=18183,
        layout_source=LAYOUT_SOURCE,
        twin_database_source=twin_source,
        protected_paths=(PROJECT_ROOT / "data" / "carton_erp.sqlite3",),
        forbidden_roots=(PROJECT_ROOT,),
        launch_nonce=nonce,
    )
    overrides = {
        **manifest["environment"],
        "ERP_UAT_ROOT": manifest["root"],
        "ERP_UAT_ISOLATION_ID": manifest["isolation_id"],
        "ERP_UAT_LAUNCH_NONCE": nonce,
        "ERP_UAT_PROTECTED_PATHS_JSON": json.dumps([]),
        "ERP_UAT_FORBIDDEN_ROOTS_JSON": json.dumps([str(PROJECT_ROOT)]),
        "ERP_ENVIRONMENT": "test",
        "ERP_BIND_HOST": "127.0.0.1",
        "ERP_PORT": "18183",
        "ERP_WORKERS": "1",
        "ERP_HEALTH_URL": "http://127.0.0.1:18183/api/health",
        "ERP_BROWSER_URL": "http://127.0.0.1:18183/",
        "ERP_ALLOWED_ORIGINS": "http://127.0.0.1:18183,http://localhost:18183",
        "ERP_TRUSTED_HOSTS": "",
        "ERP_TRUSTED_PROXY_IPS": "",
        "ERP_PRODUCTION_TRANSPORT": "development",
        "ERP_SESSION_COOKIE_NAME": manifest["cookie_name"],
        "ERP_SESSION_COOKIE_SECURE": "false",
        "ERP_SECRET_KEY": "",
    }
    child_environment = isolation.isolated_child_environment(
        overrides,
        python=sys.executable,
        temp_dir=Path(manifest["temp_dir"]),
    )
    script = """
import asyncio
import json
import os
from pathlib import Path
from app.core.uat_isolation import process_creation_token

pid_path = Path(os.environ["ERP_UAT_PID_PATH"])
owner = {
    "nonce": os.environ["ERP_UAT_LAUNCH_NONCE"],
    "pid": os.getpid(),
    "process_creation_token": process_creation_token(os.getpid()),
}
pid_path.write_text(json.dumps(owner), encoding="utf-8")
child_identity = pid_path.with_name(
    f".{pid_path.name}.{os.environ['ERP_UAT_LAUNCH_NONCE']}.child.json"
)
child_identity.write_text(json.dumps(owner), encoding="utf-8")
from app.main import phase2_lifespan

async def enter_and_exit_lifespan():
    async with phase2_lifespan(None):
        pass

asyncio.run(enter_and_exit_lifespan())
"""
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", script],
        cwd=PROJECT_ROOT,
        env=child_environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout

    attestation_path = Path(manifest["paths"]["attestation_file"])
    payload = json.loads(attestation_path.read_text(encoding="utf-8"))
    assert payload["nonce"] == nonce
    actual = payload["actual_consumers"]
    expected = {
        "database": "database",
        "sqlalchemy_engine_database": "database",
        "backup_dir": "backup_dir",
        "private_upload_dir": "private_upload_dir",
        "upload_temp_dir": "upload_temp_dir",
        "invoice_export_dir": "invoice_export_dir",
        "invoice_attachment_dir": "invoice_attachment_dir",
        "pdf_training_dir": "pdf_training_dir",
        "layout_runtime_file": "layout_runtime_file",
        "layout_editor_runtime_file": "layout_runtime_file",
        "layout_draft_file": "layout_draft_file",
        "layout_backup_dir": "layout_backup_dir",
        "legacy_upload_dir": "legacy_upload_dir",
        "delivery_print_settings_file": "delivery_print_settings_file",
        "factory_twin_database": "factory_twin_database",
        "secret_file": "secret_file",
        "stdout_log": "stdout_log",
        "stderr_log": "stderr_log",
    }
    assert set(actual) == set(expected)
    for consumer_name, logical_name in expected.items():
        expected_path = manifest["paths"][logical_name]
        assert isolation.same_file_or_path(actual[consumer_name], expected_path), (
            consumer_name,
            actual[consumer_name],
            expected_path,
        )
        assert isolation.identity_is_within(actual[consumer_name], manifest["root"])


def test_prepare_copies_pdf_samples_and_rebinds_only_uat_database(
    tmp_path: Path,
) -> None:
    source_pdf = tmp_path / "formal-sample.pdf"
    source_pdf.write_bytes(b"%PDF-isolated-copy-contract")
    digest = hashlib.sha256(source_pdf.read_bytes()).hexdigest()
    root = tmp_path / "run"
    database = root / "carton_erp_home_uat.sqlite3"
    make_database(database)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "CREATE TABLE pdf_order_training_samples "
            "(id INTEGER PRIMARY KEY, file_path TEXT, file_sha256 TEXT)"
        )
        connection.execute(
            "INSERT INTO pdf_order_training_samples(id, file_path, file_sha256) "
            "VALUES(1, ?, ?)",
            (str(source_pdf), digest),
        )
        connection.commit()

    manifest = isolation.prepare_uat_layout(
        root=root,
        database=database,
        port=18181,
        layout_source=LAYOUT_SOURCE,
        forbidden_roots=(PROJECT_ROOT,),
    )
    with closing(sqlite3.connect(database)) as connection:
        rebound = Path(
            connection.execute(
                "SELECT file_path FROM pdf_order_training_samples WHERE id=1"
            ).fetchone()[0]
        )
    assert rebound.parent == Path(manifest["paths"]["pdf_training_dir"])
    assert rebound.read_bytes() == source_pdf.read_bytes()
    assert source_pdf.read_bytes() == b"%PDF-isolated-copy-contract"
    assert manifest["pdf_training_samples"] == {"copied": 1, "rebound": 1}


def test_case_insensitive_environment_collapses_path_aliases() -> None:
    normalized = isolation.case_insensitive_environment(
        [
            ("Path", r"C:\first"),
            ("KEEP", "one"),
            ("PATH", r"C:\second"),
            ("keep", "two"),
        ]
    )
    assert [name for name in normalized if name.casefold() == "path"] == ["Path"]
    assert normalized["Path"] == r"C:\second"
    assert len([name for name in normalized if name.casefold() == "keep"]) == 1


def test_isolated_child_environment_clears_inherited_erp_aliases_without_parent_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_environment = {
        "Path": r"C:\first",
        "PATH": r"C:\second",
        "KEEP": "parent-value",
        "ERP_BACKUP_DIR": r"Z:\formal-backups",
        "erp_backup_dir": r"Z:\stale-alias",
        "ErP_UaT_LaUnCh_NoNcE": "stale-parent-nonce",
        "PYTHONPATH": r"D:\formal-project",
        "pythonhome": r"C:\formal-python",
        "TEMP": r"D:\formal-temp",
        "tmp": r"D:\stale-temp-alias",
        "TMPDIR": r"Z:\formal-tempdir",
        "SystemRoot": r"D:\untrusted-windows",
    }
    before = list(parent_environment.items())
    monkeypatch.setattr(isolation.os, "environ", parent_environment)

    child = isolation.isolated_child_environment(
        {
            "ERP_BACKUP_DIR": r"C:\uat\backups",
            "erp_database_path": r"C:\uat\uat.sqlite3",
        }
    )

    assert list(parent_environment.items()) == before
    assert child["KEEP"] == "parent-value"
    assert len([name for name in child if name.casefold() == "path"]) == 1
    assert child["ERP_BACKUP_DIR"] == r"C:\uat\backups"
    assert child["erp_database_path"] == r"C:\uat\uat.sqlite3"
    assert not any(
        name.casefold() == "erp_uat_launch_nonce" for name in child
    )
    assert not any(name.casefold() in {"pythonhome", "pythonpath"} for name in child)
    assert not any(name.casefold() in {"temp", "tmp", "tmpdir"} for name in child)
    assert child.get("SystemRoot") != r"D:\untrusted-windows"
    assert len([name for name in child if name.casefold() == "erp_backup_dir"]) == 1


def test_assert_launch_ownership_binds_pid_nonce_and_listener(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    ownership_root = tmp_path / "ownership"
    log_root = tmp_path / "logs"
    project_root.mkdir()
    ownership_root.mkdir()
    log_root.mkdir()
    lease = ownership_root / "launch.lease.json"
    pid_file = ownership_root / "launch.pid.json"
    attestation = ownership_root / "attestation.json"
    stdout = log_root / "uat.stdout.log"
    stderr = log_root / "uat.stderr.log"
    nonce = "0123456789abcdef0123456789abcdef"
    child_pid = 43210
    process_token = "0011223344556677"
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = child_pid

    def fake_popen(command: list[str], **kwargs: object) -> FakeProcess:
        captured["command"] = command
        captured["environment"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setattr(isolation.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        isolation,
        "_spawned_process_creation_token",
        lambda _process: process_token,
    )
    monkeypatch.setattr(
        isolation,
        "process_creation_token",
        lambda pid: process_token if pid == child_pid else None,
    )
    spawned = isolation.spawn_uat_process(
        python=Path(sys.executable),
        project_root=project_root,
        port=18181,
        stdout_path=stdout,
        stderr_path=stderr,
        nonce=nonce,
        lease_path=lease,
        pid_path=pid_file,
        attestation_path=attestation,
    )

    expected_owner = {
        "nonce": nonce,
        "pid": child_pid,
        "process_creation_token": process_token,
    }
    assert spawned["pid"] == child_pid
    assert spawned["nonce"] == nonce
    assert {
        key: json.loads(lease.read_text(encoding="utf-8"))[key]
        for key in expected_owner
    } == expected_owner
    assert {
        key: json.loads(pid_file.read_text(encoding="utf-8"))[key]
        for key in expected_owner
    } == expected_owner
    assert captured["environment"]["ERP_UAT_LAUNCH_NONCE"] == nonce

    verified_payload = {"pid": child_pid, "nonce": nonce, "ok": True}

    def fake_verify_attestation(
        path: Path,
        expected_pid: int,
        expected_nonce: str,
    ) -> dict[str, object]:
        assert path == attestation
        assert expected_pid == child_pid
        assert expected_nonce == nonce
        return verified_payload

    monkeypatch.setattr(isolation, "verify_uat_attestation", fake_verify_attestation)
    assert isolation.assert_launch_ownership(
        pid_path=pid_file,
        attestation_path=attestation,
        expected_pid=child_pid,
        expected_nonce=nonce,
        port=18181,
        listener_resolver=lambda port: child_pid if port == 18181 else None,
    ) == verified_payload

    with pytest.raises(isolation.UatIsolationError, match="listener owner mismatch"):
        isolation.assert_launch_ownership(
            pid_path=pid_file,
            attestation_path=attestation,
            expected_pid=child_pid,
            expected_nonce=nonce,
            port=18181,
            listener_resolver=lambda _port: child_pid + 1,
        )
    with pytest.raises(isolation.UatIsolationError, match="PID ownership"):
        isolation.assert_launch_ownership(
            pid_path=pid_file,
            attestation_path=attestation,
            expected_pid=child_pid,
            expected_nonce="fedcba9876543210fedcba9876543210",
            port=18181,
            listener_resolver=lambda _port: child_pid,
        )

    wrong_owner = isolation.cleanup_owned_launch(
        lease_path=lease,
        pid_path=pid_file,
        attestation_path=attestation,
        nonce="fedcba9876543210fedcba9876543210",
        terminate=False,
    )
    assert wrong_owner["removed"] == []
    assert lease.is_file()
    assert pid_file.is_file()


def test_default_server_command_uses_isolated_python_without_pythonpath() -> None:
    command = isolation.uat_server_command(
        Path(sys.executable),
        PROJECT_ROOT,
        18181,
    )

    assert command[:4] == [sys.executable, "-I", "-X", "utf8"]
    assert Path(command[4]).samefile(Path(isolation.__file__))
    assert command[5:] == [
        "serve",
        "--project-root",
        str(PROJECT_ROOT),
        "--port",
        "18181",
    ]
    assert "-m" not in command
    assert "--app-dir" not in command


def test_cleanup_refuses_reused_pid_and_keeps_ownership(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    nonce = "0123456789abcdef0123456789abcdef"
    pid = 43210
    lease = tmp_path / "lease.json"
    pid_file = tmp_path / "pid.json"
    attestation = tmp_path / "attestation.json"
    ownership = {
        "nonce": nonce,
        "pid": pid,
        "process_creation_token": "0011223344556677",
    }
    lease.write_text(json.dumps(ownership), encoding="utf-8")
    pid_file.write_text(json.dumps(ownership), encoding="utf-8")
    monkeypatch.setattr(
        isolation,
        "process_creation_token",
        lambda _pid: "8899aabbccddeeff",
    )

    with pytest.raises(isolation.UatIsolationError, match="PID was reused"):
        isolation.cleanup_owned_launch(
            lease_path=lease,
            pid_path=pid_file,
            attestation_path=attestation,
            nonce=nonce,
            terminate=True,
        )

    assert lease.is_file()
    assert pid_file.is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows process ownership cleanup")
def test_cleanup_stops_serving_child_even_when_wrapper_already_exited(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    nonce = "0123456789abcdef0123456789abcdef"
    wrapper_pid = 43210
    serving_pid = 43211
    wrapper_token = "0011223344556677"
    serving_token = "8899aabbccddeeff"
    lease = tmp_path / "lease.json"
    pid_file = tmp_path / "pid.json"
    attestation = tmp_path / "attestation.json"
    child_identity = pid_file.with_name(f".{pid_file.name}.{nonce}.child.json")
    wrapper = {
        "nonce": nonce,
        "pid": wrapper_pid,
        "process_creation_token": wrapper_token,
    }
    child = {
        "nonce": nonce,
        "pid": serving_pid,
        "process_creation_token": serving_token,
    }
    lease.write_text(json.dumps(wrapper), encoding="utf-8")
    pid_file.write_text(json.dumps(wrapper), encoding="utf-8")
    attestation.write_text(json.dumps(child), encoding="utf-8")
    child_identity.write_text(json.dumps(child), encoding="utf-8")
    monkeypatch.setattr(
        isolation,
        "process_creation_token",
        lambda candidate: serving_token if candidate == serving_pid else None,
    )
    terminated: list[tuple[int, str]] = []
    monkeypatch.setattr(
        isolation,
        "_terminate_owned_windows_process",
        lambda candidate, token: terminated.append((candidate, token)) or True,
    )

    result = isolation.cleanup_owned_launch(
        lease_path=lease,
        pid_path=pid_file,
        attestation_path=attestation,
        nonce=nonce,
        terminate=True,
    )

    assert terminated == [(serving_pid, serving_token)]
    assert set(result["removed"]) == {
        str(attestation),
        str(pid_file),
        str(lease),
        str(child_identity),
    }


@pytest.mark.skipif(os.name != "nt", reason="Windows process termination semantics")
def test_windows_owned_process_cleanup_accepts_access_denied_only_after_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int]] = []

    class FakeFunction:
        def __init__(self, name: str, result: int) -> None:
            self.name = name
            self.result = result
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            if self.name == "WaitForSingleObject":
                calls.append((self.name, int(args[1])))
            return self.result

    class FakeKernel32:
        OpenProcess = FakeFunction("OpenProcess", 123)
        TerminateProcess = FakeFunction("TerminateProcess", 0)
        WaitForSingleObject = FakeFunction("WaitForSingleObject", 0)
        CloseHandle = FakeFunction("CloseHandle", 1)

    monkeypatch.setattr(isolation.ctypes, "WinDLL", lambda *_args, **_kwargs: FakeKernel32())
    monkeypatch.setattr(isolation.ctypes, "get_last_error", lambda: 5)
    monkeypatch.setattr(
        isolation,
        "_creation_token_from_handle",
        lambda _handle: "0011223344556677",
    )

    assert isolation._terminate_owned_windows_process(
        43210,
        "0011223344556677",
    ) is True
    assert calls == [("WaitForSingleObject", 10_000)]


def test_spawn_ownership_failure_terminates_exact_popen_handle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    ownership_root = tmp_path / "ownership"
    logs = tmp_path / "logs"
    project_root.mkdir()
    ownership_root.mkdir()
    logs.mkdir()
    process_token = "0011223344556677"
    terminated: list[int] = []

    class FakeProcess:
        pid = 43210

    monkeypatch.setattr(isolation.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(
        isolation,
        "_spawned_process_creation_token",
        lambda _process: process_token,
    )
    monkeypatch.setattr(
        isolation,
        "_terminate_spawned_process",
        lambda process: terminated.append(process.pid),
    )
    original_write = isolation._json_write_exclusive
    calls = 0

    def fail_after_spawn(path: Path, payload: dict[str, object]) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("simulated PID ownership write failure")
        original_write(path, payload)

    monkeypatch.setattr(isolation, "_json_write_exclusive", fail_after_spawn)
    lease = ownership_root / "lease.json"
    pid_file = ownership_root / "pid.json"
    attestation = ownership_root / "attestation.json"
    nonce = "0123456789abcdef0123456789abcdef"

    with pytest.raises(OSError, match="ownership write failure"):
        isolation.spawn_uat_process(
            python=Path(sys.executable),
            project_root=project_root,
            port=18181,
            stdout_path=logs / "stdout.log",
            stderr_path=logs / "stderr.log",
            nonce=nonce,
            lease_path=lease,
            pid_path=pid_file,
            attestation_path=attestation,
        )

    assert terminated == [43210]
    assert not lease.exists()
    assert not pid_file.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows raw environment block regression")
def test_raw_windows_path_aliases_do_not_break_redirected_spawn(tmp_path: Path) -> None:
    helper = tmp_path / "raw_env_spawn.py"
    helper.write_text(
        """
import ctypes
import pathlib
import subprocess
import sys

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
CREATE_UNICODE_ENVIRONMENT = 0x00000400
WAIT_OBJECT_0 = 0
INFINITE = 0xFFFFFFFF

class STARTUPINFO(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p),
        ("dwX", ctypes.c_ulong),
        ("dwY", ctypes.c_ulong),
        ("dwXSize", ctypes.c_ulong),
        ("dwYSize", ctypes.c_ulong),
        ("dwXCountChars", ctypes.c_ulong),
        ("dwYCountChars", ctypes.c_ulong),
        ("dwFillAttribute", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("wShowWindow", ctypes.c_ushort),
        ("cbReserved2", ctypes.c_ushort),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", ctypes.c_void_p),
        ("hStdOutput", ctypes.c_void_p),
        ("hStdError", ctypes.c_void_p),
    ]

class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.c_void_p),
        ("hThread", ctypes.c_void_p),
        ("dwProcessId", ctypes.c_ulong),
        ("dwThreadId", ctypes.c_ulong),
    ]

command = subprocess.list2cmdline([
    sys.executable,
    "-X",
    "utf8",
    "-m",
    "app.core.uat_isolation",
    "run",
    "--cwd",
    sys.argv[1],
    "--python",
    sys.executable,
    "--git",
    sys.executable,
    "--temp-dir",
    sys.argv[2],
    "--environment-base64",
    sys.argv[3],
    "--command-base64",
    sys.argv[4],
])
values = [
    "Path=" + str(pathlib.Path(sys.executable).parent),
    "PATH=C:\\\\Windows\\\\System32",
    "ERP_BACKUP_DIR=C:\\\\formal-backups",
    "erp_backup_dir=C:\\\\stale-backup-alias",
    "ERP_DATABASE_PATH=C:\\\\formal.sqlite3",
    "erp_database_path=C:\\\\stale-database-alias.sqlite3",
    "PYTHONPATH=" + sys.argv[1],
    "SystemRoot=C:\\\\Windows",
    "TEMP=" + sys.argv[2],
    "TMP=" + sys.argv[2],
]
environment = ctypes.create_unicode_buffer("\\0".join(values) + "\\0\\0")
startup = STARTUPINFO()
startup.cb = ctypes.sizeof(startup)
process = PROCESS_INFORMATION()
mutable_command = ctypes.create_unicode_buffer(command)
created = kernel32.CreateProcessW(
    None,
    mutable_command,
    None,
    None,
    False,
    CREATE_UNICODE_ENVIRONMENT,
    environment,
    sys.argv[1],
    ctypes.byref(startup),
    ctypes.byref(process),
)
if not created:
    raise OSError(ctypes.get_last_error(), "CreateProcessW failed")
try:
    if kernel32.WaitForSingleObject(process.hProcess, INFINITE) != WAIT_OBJECT_0:
        raise RuntimeError("child wait failed")
    exit_code = ctypes.c_ulong()
    if not kernel32.GetExitCodeProcess(process.hProcess, ctypes.byref(exit_code)):
        raise OSError(ctypes.get_last_error(), "GetExitCodeProcess failed")
    raise SystemExit(exit_code.value)
finally:
    kernel32.CloseHandle(process.hThread)
    kernel32.CloseHandle(process.hProcess)
""",
        encoding="utf-8",
    )
    expected_backup = str(tmp_path / "uat" / "backups")
    expected_database = str(tmp_path / "uat" / "uat.sqlite3")
    overrides = base64.b64encode(
        json.dumps(
            {
                "ERP_BACKUP_DIR": expected_backup,
                "ERP_DATABASE_PATH": expected_database,
            }
        ).encode("utf-8")
    ).decode("ascii")
    dummy_code = (
        "import json,os;"
        "print(json.dumps({'environment':list(os.environ.items())},sort_keys=True))"
    )
    dummy_command = base64.b64encode(
        json.dumps([sys.executable, "-X", "utf8", "-c", dummy_code]).encode("utf-8")
    ).decode("ascii")
    result = subprocess.run(
        [
            sys.executable,
            str(helper),
            str(PROJECT_ROOT),
            str(tmp_path),
            overrides,
            dummy_command,
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    runner = json.loads(result.stdout)
    assert runner["returncode"] == 0, runner["stderr"]
    entries = json.loads(runner["stdout"])["environment"]

    def aliases(name: str) -> list[list[str]]:
        return [entry for entry in entries if entry[0].casefold() == name.casefold()]

    assert len(aliases("Path")) == 1
    assert aliases("ERP_BACKUP_DIR") == [["ERP_BACKUP_DIR", expected_backup]]
    assert aliases("ERP_DATABASE_PATH") == [["ERP_DATABASE_PATH", expected_database]]
    assert aliases("PYTHONPATH") == []
    assert not any("formal" in value or "stale-" in value for _name, value in entries)


def test_launcher_delegates_child_environment_without_mutating_parent() -> None:
    launcher = (
        PROJECT_ROOT / "scripts" / "windows" / "start_erp_uat.ps1"
    ).read_text(encoding="utf-8-sig")
    assert "app\\core\\uat_isolation.py" in launcher
    for helper_command in ('"run"', '"spawn"', '"assert-ownership"'):
        assert helper_command in launcher
    assert "SetEnvironmentVariable" not in launcher
    assert "Start-Process" not in launcher
    assert re.search(r"\$env:ERP_[A-Za-z0-9_]+\s*=", launcher) is None
    assert "ERP_BACKUP_DIR" in launcher
    assert "ERP_UAT_PROTECTED_PATHS_JSON" in launcher
    assert "ValidateOnly" in launcher
    assert "alembic upgrade" not in launcher
