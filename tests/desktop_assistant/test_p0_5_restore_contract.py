from __future__ import annotations

from contextlib import closing
from pathlib import Path
import hashlib
import shutil
import sqlite3
import sys
import types
import zipfile

import pytest

# The managed assistant targets Python 3.12. The web-development venv is 3.10;
# temporary fixture paths cannot be junctions, so provide the missing query.
if not hasattr(Path, "is_junction"):
    Path.is_junction = lambda self: False  # type: ignore[attr-defined]
if not hasattr(hashlib, "file_digest"):
    def _file_digest(stream, name):
        digest = hashlib.new(name)
        while block := stream.read(1024 * 1024):
            digest.update(block)
        return digest

    hashlib.file_digest = _file_digest  # type: ignore[attr-defined]

# The web-development venv intentionally omits the Windows assistant's psutil
# runtime dependency. These restore tests never control a real process.
try:
    import psutil as _psutil  # noqa: F401
except ModuleNotFoundError:
    fake_psutil = types.ModuleType("psutil")
    fake_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    fake_psutil.AccessDenied = type("AccessDenied", (Exception,), {})
    fake_psutil.TimeoutExpired = type("TimeoutExpired", (Exception,), {})
    fake_psutil.Process = lambda *_args, **_kwargs: None
    fake_psutil.process_iter = lambda *_args, **_kwargs: []
    sys.modules["psutil"] = fake_psutil

from desktop_assistant.manager import Manager
from desktop_assistant.storage import (
    database_info,
    encrypt_file,
    pack_recovery,
    pack_tree,
    sha,
)
from tests.desktop_assistant import test_recovery as recovery


PASSWORD = recovery.PASSWORD
TestManager = recovery.TestManager


@pytest.fixture()
def recovery_case():
    case = recovery.RecoveryTests()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def _package_shared(
    case: recovery.RecoveryTests,
    shared: Path,
    *,
    database_metadata: dict,
    name: str,
    release_package: Path | None = None,
) -> Path:
    release_package = release_package or case.manager.root / "packages" / f'{case.manager.state["current"]}.zip'
    release_id = sha(release_package)
    raw = case.root / f"{name}.zip"
    encrypted = case.root / f"{name}.tmbackup"
    pack_recovery(
        shared,
        {"release.zip": release_package},
        raw,
        {
            "type": "tianming.recovery.v1",
            "created": "2026-09-29T20:00:00+08:00",
            "release": release_id,
            "version": "one",
            "database": database_metadata,
            "source_shared": str(shared),
            "schema_authority": None,
        },
    )
    encrypt_file(raw, encrypted, PASSWORD)
    return encrypted


def _custom_release(case: recovery.RecoveryTests, name: str, addition: str) -> Path:
    source = case.root / f"custom-source-{name}"
    shutil.copytree(case.root / "source-one", source)
    models = source / "app/models/fixture.py"
    models.write_text(models.read_text(encoding="utf-8") + addition, encoding="utf-8")
    package = case.root / f"custom-{name}.zip"
    pack_tree(
        source,
        package,
        {"type": "tianming.release.v1", "version": name, "revision": "r1"},
        case.key,
    )
    return package


def test_incomplete_database_inside_full_package_never_activates(
    recovery_case: recovery.RecoveryTests,
) -> None:
    case = recovery_case
    shared = case.root / "incomplete-shared"
    shutil.copytree(case.manager.root / "shared", shared)
    database = shared / "data/carton_erp.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("DROP TABLE sales_order_items")
        connection.commit()
    package = _package_shared(
        case,
        shared,
        database_metadata=database_info(database),
        name="incomplete",
    )
    target = TestManager(case.root / "rejected-incomplete", case.public)

    with pytest.raises(ValueError, match="必要结构"):
        target.restore(package, PASSWORD)

    assert target.state["current"] is None
    assert not any((target.root / "shared").iterdir())


def test_missing_required_column_with_self_consistent_manifest_never_activates(
    recovery_case: recovery.RecoveryTests,
) -> None:
    case = recovery_case
    shared = case.root / "missing-column-shared"
    shutil.copytree(case.manager.root / "shared", shared)
    database = shared / "data/carton_erp.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("ALTER TABLE sales_order_items DROP COLUMN note")
        connection.commit()
    package = _package_shared(
        case,
        shared,
        database_metadata=database_info(database),
        name="missing-column",
    )
    target = TestManager(case.root / "rejected-missing-column", case.public)

    with pytest.raises(ValueError, match="必要结构（列）"):
        target.restore(package, PASSWORD)

    assert target.state["current"] is None
    assert not any((target.root / "shared").iterdir())


def test_final_schema_is_rechecked_after_pdf_rebinding(
    recovery_case: recovery.RecoveryTests,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = recovery_case
    shared = case.root / "post-rebind-shared"
    shutil.copytree(case.manager.root / "shared", shared)
    database = shared / "data/carton_erp.sqlite3"
    package = _package_shared(
        case,
        shared,
        database_metadata=database_info(database),
        name="post-rebind",
    )
    target = TestManager(case.root / "rejected-post-rebind", case.public)

    def corrupt_after_initial_check(path: Path, *_args, **_kwargs) -> int:
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("DROP TABLE sales_order_items")
            connection.commit()
        return 0

    monkeypatch.setattr("desktop_assistant.manager.rebind_pdf_sources", corrupt_after_initial_check)
    with pytest.raises(ValueError, match="最终完整性|必要结构"):
        target.restore(package, PASSWORD)

    assert target.state["current"] is None
    assert not any((target.root / "shared").iterdir())


def test_unknown_signed_model_structure_fails_without_activation(
    recovery_case: recovery.RecoveryTests,
) -> None:
    case = recovery_case
    release = _custom_release(
        case,
        "unknown-model",
        "\nclass DynamicTable(Base):\n"
        "    __tablename__ = choose_table_name()\n"
        "    id: Mapped[int] = mapped_column(primary_key=True)\n",
    )
    package = _package_shared(
        case,
        case.manager.root / "shared",
        database_metadata=database_info(case.manager.root / "shared/data/carton_erp.sqlite3"),
        name="unknown-model",
        release_package=release,
    )
    target = TestManager(case.root / "rejected-unknown-model", case.public)

    with pytest.raises(ValueError, match="静态"):
        target.restore(package, PASSWORD)

    assert target.state["current"] is None
    assert not any((target.root / "shared").iterdir())


def test_changed_signed_model_bytes_fail_without_activation(
    recovery_case: recovery.RecoveryTests,
) -> None:
    case = recovery_case
    original = _custom_release(case, "signed-source", "\n# original\n")
    tampered = case.root / "tampered-release.zip"
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(tampered, "x") as target_zip:
        for info in source.infolist():
            raw = source.read(info.filename)
            if info.filename == "app/models/fixture.py":
                raw += b"# changed after signing\n"
            target_zip.writestr(info, raw)
    package = _package_shared(
        case,
        case.manager.root / "shared",
        database_metadata=database_info(case.manager.root / "shared/data/carton_erp.sqlite3"),
        name="changed-model",
        release_package=tampered,
    )
    target = TestManager(case.root / "rejected-changed-model", case.public)

    with pytest.raises(ValueError, match="模型文件校验失败"):
        target.restore(package, PASSWORD)

    assert target.state["current"] is None
    assert not any((target.root / "shared").iterdir())


def test_model_top_level_code_is_never_executed_during_restore(
    recovery_case: recovery.RecoveryTests,
) -> None:
    case = recovery_case
    marker = case.root / "model-code-executed.txt"
    release = _custom_release(
        case,
        "non-executed-model",
        f"\nfrom pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n",
    )
    package = _package_shared(
        case,
        case.manager.root / "shared",
        database_metadata=database_info(case.manager.root / "shared/data/carton_erp.sqlite3"),
        name="non-executed-model",
        release_package=release,
    )
    target = TestManager(case.root / "accepted-non-executed-model", case.public)

    result = target.restore(package, PASSWORD)

    assert result["started"] is False
    assert not marker.exists()
    assert target.state["current"] == sha(release)


def test_unsupported_revision_never_activates_or_changes_source(
    recovery_case: recovery.RecoveryTests,
) -> None:
    case = recovery_case
    shared = case.root / "unsupported-shared"
    shutil.copytree(case.manager.root / "shared", shared)
    database = shared / "data/carton_erp.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE alembic_version SET version_num='unsupported_head'")
        connection.commit()
    before = sha(database)
    package = _package_shared(
        case,
        shared,
        database_metadata=database_info(database),
        name="unsupported",
    )
    target = TestManager(case.root / "rejected-unsupported", case.public)

    with pytest.raises(ValueError, match="程序与数据不兼容"):
        target.restore(package, PASSWORD)

    assert sha(database) == before
    assert target.state["current"] is None
    assert not any((target.root / "shared").iterdir())


def test_selected_package_is_revalidated_after_preflight_evidence(
    recovery_case: recovery.RecoveryTests,
) -> None:
    case = recovery_case
    selected = case.manager.backup(PASSWORD, case.nas)
    preflight_hash = sha(selected)
    original_database = sha(case.manager.root / "shared/data/carton_erp.sqlite3")
    raw = bytearray(selected.read_bytes())
    raw[-20] ^= 1
    selected.write_bytes(raw)
    assert sha(selected) != preflight_hash
    target = TestManager(case.root / "rejected-replaced", case.public)

    with pytest.raises(ValueError, match="口令错误或备份已损坏"):
        target.restore(selected, PASSWORD)

    assert sha(case.manager.root / "shared/data/carton_erp.sqlite3") == original_database
    assert target.state["current"] is None
    assert not any((target.root / "shared").iterdir())


def test_managed_restore_keeps_service_stopped_until_explicit_start(
    recovery_case: recovery.RecoveryTests,
) -> None:
    case = recovery_case
    source_database = case.manager.root / "shared/data/carton_erp.sqlite3"
    source_hash = sha(source_database)
    backup = case.manager.backup(PASSWORD, case.nas)
    target = TestManager(case.root / "supported-restore", case.public)

    result = target.restore(backup, PASSWORD)

    assert result == {
        "version": "one",
        "data_time": target.state["source_time"],
        "started": False,
    }
    assert sha(target.root / "shared/data/carton_erp.sqlite3") == source_hash
    assert (target.root / "shared/data/drawing.pdf").read_bytes() == b"synthetic attachment"
    assert target.state["current"] == case.manager.state["current"]
    assert Manager._process(target) is None
