from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import create_engine

from app.models import Base


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "backfill_inventory_cost_snapshots.py"


def load_script_module():
    spec = importlib.util.spec_from_file_location(
        "backfill_inventory_cost_snapshots", SCRIPT_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load script: {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def module():
    return load_script_module()


def make_database(path: Path, revision: str = "ai36v7w8x9e26") -> None:
    engine = create_engine(f"sqlite+pysqlite:///{path.as_posix()}")
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32))")
            connection.exec_driver_sql(
                "INSERT INTO alembic_version (version_num) VALUES (?)", (revision,)
            )
    finally:
        engine.dispose()


def fingerprint(path: Path) -> tuple[str, int]:
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def journal_mode(path: Path) -> str:
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        return str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()


def test_dry_run_keeps_file_hash_mtime_and_journal_mode_unchanged(
    module, tmp_path: Path
) -> None:
    database = tmp_path / "copies" / "inventory-copy.sqlite3"
    database.parent.mkdir()
    make_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode = WAL")

    before = fingerprint(database)
    before_journal_mode = journal_mode(database)

    report = module.build_report(database, apply=False)

    assert report["mode"] == "dry-run"
    assert report["database_fingerprint_unchanged"] is True
    assert fingerprint(database) == before
    assert journal_mode(database) == before_journal_mode == "wal"


def test_direct_script_dry_run_needs_no_pythonpath_and_keeps_file_unchanged(
    tmp_path: Path,
) -> None:
    database = tmp_path / "copies" / "inventory-copy.sqlite3"
    database.parent.mkdir()
    make_database(database)
    before = fingerprint(database)
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(SCRIPT_PATH),
            "--database",
            str(database),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert '"mode": "dry-run"' in result.stdout
    assert fingerprint(database) == before


@pytest.mark.parametrize("apply", [False, True])
def test_formal_database_name_is_rejected_in_every_mode(
    module, tmp_path: Path, apply: bool
) -> None:
    database = tmp_path / "carton_erp.sqlite3"
    make_database(database)

    with pytest.raises(module.SafetyError, match="carton_erp.sqlite3"):
        module.validate_database_safety(
            database=database,
            apply=apply,
            confirm_copy=module.COPY_CONFIRMATION,
            copy_root=tmp_path,
        )


def test_apply_rejects_missing_or_outside_copy_root(module, tmp_path: Path) -> None:
    copy_root = tmp_path / "approved-copies"
    copy_root.mkdir()
    outside = tmp_path / "outside.sqlite3"
    make_database(outside)

    with pytest.raises(module.SafetyError, match="copy-root"):
        module.validate_database_safety(
            database=outside,
            apply=True,
            confirm_copy=module.COPY_CONFIRMATION,
            copy_root=None,
        )
    with pytest.raises(module.SafetyError, match="inside --copy-root"):
        module.validate_database_safety(
            database=outside,
            apply=True,
            confirm_copy=module.COPY_CONFIRMATION,
            copy_root=copy_root,
        )


def test_apply_rejects_symlink_target_when_supported(module, tmp_path: Path) -> None:
    copy_root = tmp_path / "approved-copies"
    copy_root.mkdir()
    source = tmp_path / "outside.sqlite3"
    make_database(source)
    target = copy_root / "linked-copy.sqlite3"
    try:
        target.symlink_to(source)
    except OSError as error:
        pytest.skip(f"symlinks are unavailable on this platform: {error}")

    with pytest.raises(module.SafetyError, match="symlink"):
        module.validate_database_safety(
            database=target,
            apply=True,
            confirm_copy=module.COPY_CONFIRMATION,
            copy_root=copy_root,
        )


def test_apply_rejects_multiple_hard_links(module, tmp_path: Path) -> None:
    copy_root = tmp_path / "approved-copies"
    copy_root.mkdir()
    source = tmp_path / "source.sqlite3"
    make_database(source)
    target = copy_root / "copy-link.sqlite3"
    try:
        os.link(source, target)
    except OSError as error:
        pytest.skip(f"hard links are unavailable on this platform: {error}")

    with pytest.raises(module.SafetyError, match="multiple hard links"):
        module.validate_database_safety(
            database=target,
            apply=True,
            confirm_copy=module.COPY_CONFIRMATION,
            copy_root=copy_root,
        )


def test_apply_rejects_known_live_database_by_file_identity(
    module, monkeypatch, tmp_path: Path
) -> None:
    copy_root = tmp_path / "approved-copies"
    copy_root.mkdir()
    live_database = tmp_path / "live.sqlite3"
    make_database(live_database)
    target = copy_root / "copy-link.sqlite3"
    try:
        os.link(live_database, target)
    except OSError as error:
        pytest.skip(f"hard links are unavailable on this platform: {error}")
    monkeypatch.setattr(module, "_known_live_databases", lambda: (live_database,))

    with pytest.raises(module.SafetyError, match="known live"):
        module.validate_database_safety(
            database=target,
            apply=True,
            confirm_copy=module.COPY_CONFIRMATION,
            copy_root=copy_root,
        )


def test_apply_accepts_a_regular_database_copy(module, monkeypatch, tmp_path: Path) -> None:
    copy_root = tmp_path / "approved-copies"
    copy_root.mkdir()
    database = copy_root / "inventory-cost-snapshot-copy.sqlite3"
    make_database(database)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT_PATH),
            "--database",
            str(database),
            "--apply",
            "--confirm-copy",
            module.COPY_CONFIRMATION,
            "--copy-root",
            str(copy_root),
        ],
    )

    assert module.main() == 0
