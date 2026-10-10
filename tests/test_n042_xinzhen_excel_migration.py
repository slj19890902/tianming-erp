from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "n042tmpv8x9z51_n042_excel_import_ledger_uat_only.py"
)


def _load_revision():
    spec = spec_from_file_location("n042_temp_migration_under_test", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    revision = module_from_spec(spec)
    spec.loader.exec_module(revision)
    return revision


def _run_revision(connection, monkeypatch, action: str) -> None:
    revision = _load_revision()
    operations = Operations(MigrationContext.configure(connection))
    monkeypatch.setattr(revision, "op", operations)
    getattr(revision, action)()


def _batch_values(*, source_sha256: str = "a" * 64) -> dict:
    return {
        "customer_id": 42,
        "customer_code_snapshot": "XINZHEN",
        "customer_name_snapshot": "New Zhen",
        "source_type": "xinzhen_excel_carton_marking",
        "source_filename": "sample.xls",
        "source_sha256": source_sha256,
        "parser_version": "n042-xinzhen-v2.2",
        "worksheet_name": "1",
        "normalized_source_hash": "b" * 64,
        "normalized_source_json": "{}",
        "created_by": 1,
    }


def test_temporary_revision_is_explicitly_non_mergeable_and_reparented_later() -> None:
    revision = _load_revision()
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert revision.revision == "n042tmpv8x9z51"
    assert revision.down_revision == "ce61v8x9z50"
    assert "DO NOT MERGE" in source
    assert "df62v8x9z51" in source


def test_excel_import_ledger_unique_and_immutable_guards(tmp_path, monkeypatch) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'n042-ledger.sqlite3'}")
    with engine.begin() as connection:
        _run_revision(connection, monkeypatch, "upgrade")

    inspector = sa.inspect(engine)
    assert {
        "excel_order_import_batches",
        "excel_order_import_rows",
        "excel_order_import_conversions",
    }.issubset(set(inspector.get_table_names()))

    batches = sa.Table(
        "excel_order_import_batches", sa.MetaData(), autoload_with=engine
    )
    with engine.begin() as connection:
        batch_id = connection.execute(
            batches.insert().values(**_batch_values())
        ).inserted_primary_key[0]

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(batches.insert().values(**_batch_values()))

    with pytest.raises(IntegrityError, match="immutable"):
        with engine.begin() as connection:
            connection.execute(
                batches.update().where(batches.c.id == batch_id).values(
                    source_filename="changed.xls"
                )
            )

    with pytest.raises(IntegrityError, match="immutable"):
        with engine.begin() as connection:
            connection.execute(batches.delete().where(batches.c.id == batch_id))

    with engine.begin() as connection:
        with pytest.raises(RuntimeError, match="禁止破坏性降级"):
            _run_revision(connection, monkeypatch, "downgrade")


def test_empty_ledger_can_downgrade_and_upgrade_again(tmp_path, monkeypatch) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'n042-roundtrip.sqlite3'}")
    with engine.begin() as connection:
        _run_revision(connection, monkeypatch, "upgrade")
    with engine.begin() as connection:
        _run_revision(connection, monkeypatch, "downgrade")
    assert "excel_order_import_batches" not in sa.inspect(engine).get_table_names()
    with engine.begin() as connection:
        _run_revision(connection, monkeypatch, "upgrade")
    assert "excel_order_import_batches" in sa.inspect(engine).get_table_names()
