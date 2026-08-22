from __future__ import annotations

import csv
import json
import os
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from scripts.audit import p0_15_incomplete_order_chain as cli
from test_p0_15_incomplete_order_chain import (
    REVISION,
    add_order,
    build_p0_15_database,
)


def build_cli_database(path: Path) -> Path:
    engine = build_p0_15_database(path)
    with Session(engine) as db:
        add_order(db, "CLI-PRIVATE")
        db.commit()
    engine.dispose()
    return path


def output_paths(tmp_path: Path, prefix: str = "report") -> tuple[Path, Path, Path]:
    return (
        tmp_path / f"{prefix}.json",
        tmp_path / f"{prefix}.csv",
        tmp_path / f"{prefix}.md",
    )


def cli_args(
    database: Path,
    outputs: tuple[Path, Path, Path],
    *,
    expected_sha256: str | None = None,
    expected_revision: str = REVISION,
) -> list[str]:
    json_path, csv_path, markdown_path = outputs
    return [
        "--database",
        str(database),
        "--json-output",
        str(json_path),
        "--csv-output",
        str(csv_path),
        "--markdown-output",
        str(markdown_path),
        "--source-label",
        "anonymous-isolated-copy",
        "--expected-revision",
        expected_revision,
        "--expected-sha256",
        expected_sha256 or cli._sha256(database),
    ]


def test_creator_enforces_mode_ro_query_only_and_authorizer(tmp_path: Path):
    database = build_cli_database(tmp_path / "readonly.sqlite3")
    sha_before = cli._sha256(database)
    sidecars_before = cli._sidecar_state(database)

    connection = cli._creator(database)()
    try:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM sales_orders").fetchone()[0] == 1
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("UPDATE sales_orders SET status='completed'")
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("CREATE TABLE forbidden_write(id INTEGER)")
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("ATTACH DATABASE ':memory:' AS forbidden_sidecar")
    finally:
        connection.close()

    assert cli._sha256(database) == sha_before
    assert cli._sidecar_state(database) == sidecars_before


def test_cli_writes_three_consistent_anonymous_reports_without_touching_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    database = build_cli_database(tmp_path / "source.sqlite3")
    outputs = output_paths(tmp_path)
    sha_before = cli._sha256(database)
    source_before = cli._source_state(database)
    monkeypatch.setenv("P0_15_ANONYMIZATION_KEY", "stable-test-key")

    exit_code = cli.main(cli_args(database, outputs, expected_sha256=sha_before))

    assert exit_code == 0
    stdout = json.loads(capsys.readouterr().out)
    assert stdout["status"] == "ok"
    assert stdout["scan_complete"] is True
    assert stdout["coverage"]["receipt_auto_finished"]["status"] == (
        "evaluated"
    )
    assert stdout["coverage"]["workstation_membership"]["status"] == "evaluated"
    assert cli._sha256(database) == sha_before
    assert cli._source_state(database) == source_before
    json_report = json.loads(outputs[0].read_text(encoding="utf-8"))
    with outputs[1].open("r", encoding="utf-8-sig", newline="") as handle:
        csv_rows = list(csv.DictReader(handle))
    markdown = outputs[2].read_text(encoding="utf-8")

    assert json_report["source"]["revision"] == REVISION
    assert json_report["source"]["database_sha256_before"] == sha_before
    assert json_report["source"]["database_sha256_after"] == sha_before
    assert json_report["source"]["database_unchanged"] is True
    assert json_report["source"]["quick_check"] == "ok"
    assert json_report["source"]["foreign_key_errors"] == 0
    assert len(csv_rows) == json_report["summary"]["finding_count"]
    assert {row["code"] for row in csv_rows} == set(
        json_report["summary"]["code_counts"]
    )
    assert f"数据库 revision：`{REVISION}`" in markdown
    assert f"异常/待复核/信息合计：`{len(csv_rows)}`" in markdown
    assert "扫描覆盖完整：`True`" in markdown
    assert "P1-81" in markdown
    assert "P1-84" in markdown
    assert "## 工位路由统计" in markdown
    for code in json_report["summary"]["code_counts"]:
        assert code in markdown
    combined = outputs[0].read_text(encoding="utf-8") + outputs[1].read_text(
        encoding="utf-8-sig"
    ) + markdown
    assert "ANON-ORDER-CLI-PRIVATE" not in combined
    assert "ANON-PO-CLI-PRIVATE" not in combined
    assert "ANON-PRODUCT-CLI-PRIVATE" not in combined


@pytest.mark.parametrize("sidecar_suffix", ["-journal", "-wal", "-shm"])
def test_cli_refuses_database_and_sidecar_report_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sidecar_suffix: str,
):
    database = build_cli_database(tmp_path / f"protected-{sidecar_suffix[1:]}.sqlite3")
    sha_before = cli._sha256(database)
    sidecars_before = cli._sidecar_state(database)
    monkeypatch.setenv("P0_15_ANONYMIZATION_KEY", "stable-test-key")
    invalid_sidecar = Path(str(database) + sidecar_suffix)
    valid_csv = tmp_path / f"protected-{sidecar_suffix[1:]}.csv"
    valid_markdown = tmp_path / f"protected-{sidecar_suffix[1:]}.md"

    assert cli.main(
        cli_args(
            database,
            (invalid_sidecar, valid_csv, valid_markdown),
            expected_sha256=sha_before,
        )
    ) == 2
    assert cli.main(
        cli_args(
            database,
            (database, valid_csv, valid_markdown),
            expected_sha256=sha_before,
        )
    ) == 2

    assert cli._sha256(database) == sha_before
    assert cli._sidecar_state(database) == sidecars_before
    assert not invalid_sidecar.exists()
    assert not valid_csv.exists()
    assert not valid_markdown.exists()


def test_cli_refuses_existing_hardlink_report_target_without_touching_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    database = build_cli_database(tmp_path / "hardlink-source.sqlite3")
    source_before = cli._source_state(database)
    hardlink_json = tmp_path / "hardlink-report.json"
    os.link(database, hardlink_json)
    outputs = (
        hardlink_json,
        tmp_path / "hardlink-report.csv",
        tmp_path / "hardlink-report.md",
    )
    monkeypatch.setenv("P0_15_ANONYMIZATION_KEY", "stable-test-key")

    assert cli.main(cli_args(database, outputs)) == 2

    assert cli._source_state(database) == source_before
    assert hardlink_json.exists()
    assert not outputs[1].exists()
    assert not outputs[2].exists()


def test_cli_rechecks_source_after_exclusive_report_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    database = build_cli_database(tmp_path / "post-write-source.sqlite3")
    source_before = cli._source_state(database)
    outputs = output_paths(tmp_path, "post-write")
    monkeypatch.setenv("P0_15_ANONYMIZATION_KEY", "stable-test-key")
    real_write_reports = cli._write_reports

    def write_reports_then_change_source(*args, **kwargs) -> None:
        real_write_reports(*args, **kwargs)
        stat = database.stat()
        os.utime(
            database,
            ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000),
        )

    monkeypatch.setattr(cli, "_write_reports", write_reports_then_change_source)

    assert cli.main(cli_args(database, outputs)) == 2

    assert cli._source_state(database) != source_before
    assert not any(path.exists() for path in outputs)


def test_cli_rejects_sha_or_revision_mismatch_before_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    database = build_cli_database(tmp_path / "gated.sqlite3")
    sha_before = cli._sha256(database)
    source_before = cli._source_state(database)
    monkeypatch.setenv("P0_15_ANONYMIZATION_KEY", "stable-test-key")
    bad_sha_outputs = output_paths(tmp_path, "bad-sha")
    bad_revision_outputs = output_paths(tmp_path, "bad-revision")

    assert cli.main(
        cli_args(database, bad_sha_outputs, expected_sha256="0" * 64)
    ) == 2
    assert cli.main(
        cli_args(
            database,
            bad_revision_outputs,
            expected_sha256=sha_before,
            expected_revision="wrong-revision",
        )
    ) == 2

    assert cli._source_state(database) == source_before
    assert not any(path.exists() for path in (*bad_sha_outputs, *bad_revision_outputs))


def test_cli_schema_column_gate_fails_closed_without_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    database = tmp_path / "incomplete-schema.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE alembic_version(version_num TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO alembic_version VALUES (?)", (REVISION,))
        for table in sorted(cli.REQUIRED_TABLES - {"alembic_version"}):
            connection.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY)')
    sha_before = cli._sha256(database)
    source_before = cli._source_state(database)
    outputs = output_paths(tmp_path, "schema-gate")
    monkeypatch.setenv("P0_15_ANONYMIZATION_KEY", "stable-test-key")

    assert cli.main(cli_args(database, outputs, expected_sha256=sha_before)) == 2

    assert cli._source_state(database) == source_before
    assert not any(path.exists() for path in outputs)
