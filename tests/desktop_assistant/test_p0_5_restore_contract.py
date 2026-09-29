from __future__ import annotations

from contextlib import closing
from pathlib import Path
import hashlib
import shutil
import sqlite3
import sys
import types

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
) -> Path:
    release_id = case.manager.state["current"]
    raw = case.root / f"{name}.zip"
    encrypted = case.root / f"{name}.tmbackup"
    pack_recovery(
        shared,
        {"release.zip": case.manager.root / "packages" / f"{release_id}.zip"},
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


def test_incomplete_database_inside_full_package_never_activates(
    recovery_case: recovery.RecoveryTests,
) -> None:
    case = recovery_case
    shared = case.root / "incomplete-shared"
    shutil.copytree(case.manager.root / "shared", shared)
    database = shared / "data/carton_erp.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("DROP TABLE sales_orders")
        connection.commit()
    package = _package_shared(
        case,
        shared,
        database_metadata={"revision": "r1", "counts": {}},
        name="incomplete",
    )
    target = TestManager(case.root / "rejected-incomplete", case.public)

    with pytest.raises(ValueError, match="缺少ERP业务表"):
        target.restore(package, PASSWORD)

    assert target.state["current"] is None
    assert not any((target.root / "shared").iterdir())


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
