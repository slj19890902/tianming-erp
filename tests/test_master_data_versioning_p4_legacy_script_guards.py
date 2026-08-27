from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from scripts.master_data_write_guard import (
    LegacyMasterDataWriteBlocked,
    reject_legacy_master_data_write_if_versioned,
)


@pytest.fixture()
def versioned_engine(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'versioned.sqlite3'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE master_data_object_versions (id INTEGER PRIMARY KEY)"))
    yield engine
    engine.dispose()


@pytest.fixture()
def write_statements(versioned_engine):
    statements: list[str] = []

    @event.listens_for(versioned_engine, "before_cursor_execute")
    def capture_write_statements(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")):
            statements.append(statement)

    return statements


def _assert_blocked_before_write(write_statements, action) -> None:
    with pytest.raises(LegacyMasterDataWriteBlocked, match="绕过版本服务和审计"):
        action()
    assert write_statements == []


def _stub_optional_excel_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    # This test never parses a workbook.  Stubbing avoids importing the
    # worktree's intentionally broken historical NumPy/Pandas environment.
    monkeypatch.setitem(sys.modules, "pandas", ModuleType("pandas"))
    monkeypatch.setitem(sys.modules, "openpyxl", ModuleType("openpyxl"))


def test_legacy_offline_scripts_reject_write_modes_before_first_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    versioned_engine,
    write_statements,
) -> None:
    _stub_optional_excel_dependencies(monkeypatch)
    from scripts import clean_phase12_master_data
    from scripts import import_excel_history
    from scripts import import_historical_requisitions
    from scripts import import_material_dataset
    from scripts import migrate_phase3_data
    from scripts.admin import merge_short_tianhua_customer

    monkeypatch.setattr(clean_phase12_master_data, "SessionLocal", lambda: Session(versioned_engine))
    _assert_blocked_before_write(
        write_statements,
        lambda: clean_phase12_master_data.run(commit=True),
    )

    with Session(versioned_engine) as session:
        _assert_blocked_before_write(
            write_statements,
            lambda: import_excel_history.import_csv_to_products(
                tmp_path / "not-read.csv",
                session,
                commit=True,
            ),
        )

    monkeypatch.setattr(
        import_historical_requisitions,
        "create_engine_from_settings",
        lambda _settings: versioned_engine,
    )
    _assert_blocked_before_write(
        write_statements,
        lambda: import_historical_requisitions.commit_matches(
            [],
            workbook_path=tmp_path / "history.xlsx",
            database_path=tmp_path / "versioned.sqlite3",
        ),
    )

    target_database = tmp_path / "target.sqlite3"
    source_database = tmp_path / "source.sqlite3"
    sqlite3.connect(target_database).close()
    sqlite3.connect(source_database).close()
    monkeypatch.setattr(migrate_phase3_data, "create_sqlite_engine", lambda _path: versioned_engine)
    _assert_blocked_before_write(
        write_statements,
        lambda: migrate_phase3_data.migrate_phase3_data(
            target_database=target_database,
            boxerp_database=source_database,
            report_path=tmp_path / "migration.log",
            dry_run=False,
        ),
    )

    monkeypatch.setattr(import_material_dataset, "create_sqlite_engine", lambda _path: versioned_engine)
    monkeypatch.setattr(import_material_dataset, "load_xlsx", lambda _path: [])
    _assert_blocked_before_write(
        write_statements,
        lambda: import_material_dataset.run(tmp_path / "materials.xlsx", apply=True),
    )

    legacy_merge_database = tmp_path / "legacy-merge.sqlite3"
    sqlite3.connect(legacy_merge_database).close()
    monkeypatch.setattr(
        merge_short_tianhua_customer,
        "create_engine",
        lambda _url: versioned_engine,
    )
    _assert_blocked_before_write(
        write_statements,
        lambda: merge_short_tianhua_customer.run_merge(
            legacy_merge_database,
            apply=True,
            create_backup=False,
        ),
    )


def test_guard_detects_version_columns_without_audit_table(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'version-column.sqlite3'}")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE products (id INTEGER PRIMARY KEY, version INTEGER NOT NULL)"))
        with Session(engine) as session:
            with pytest.raises(LegacyMasterDataWriteBlocked, match=r"products\.version"):
                reject_legacy_master_data_write_if_versioned(
                    session,
                    script_name="test-script.py",
                )
    finally:
        engine.dispose()
