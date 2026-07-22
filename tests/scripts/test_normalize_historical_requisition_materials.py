from __future__ import annotations

import sqlite3
import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Iterator

import pytest
import scripts.admin.normalize_historical_requisition_materials as normalizer

from scripts.admin.normalize_historical_requisition_materials import (
    APPLY_CONFIRMATION,
    STOP_CONFIRMATION,
    NormalizationError,
    build_parser,
    build_plan,
    execute,
    load_approved_mappings,
    normalize_lookup_text,
    sha256_file,
)


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _create_database(path: Path) -> None:
    with _connect(path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE products (
                id INTEGER PRIMARY KEY,
                layer_count INTEGER,
                flute_type TEXT,
                material_id INTEGER,
                default_material_code TEXT
            );
            CREATE TABLE materials (id INTEGER PRIMARY KEY, code TEXT NOT NULL);
            CREATE TABLE historical_purchase_entries (
                id INTEGER PRIMARY KEY,
                source_workbook TEXT NOT NULL,
                source_sheet TEXT NOT NULL,
                source_row INTEGER NOT NULL,
                source_file_sha256 TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL,
                supplier_name TEXT,
                record_date TEXT,
                product_reference TEXT NOT NULL,
                search_text TEXT NOT NULL,
                normalized_search_text TEXT NOT NULL,
                material_code TEXT NOT NULL,
                product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
                customer_id INTEGER,
                notes TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT
            );
            CREATE TABLE historical_requisition_maps (
                id INTEGER PRIMARY KEY,
                product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
                search_key TEXT NOT NULL,
                normalized_search_key TEXT NOT NULL,
                material_code TEXT NOT NULL,
                raw_data TEXT,
                source_workbook TEXT,
                source_sheet TEXT,
                source_row INTEGER,
                updated_at TEXT
            );
            INSERT INTO materials(id, code) VALUES
                (1, 'K618A-OLD'), (2, 'A6A'), (3, 'J41818X');
            INSERT INTO products(id, layer_count, flute_type, material_id, default_material_code) VALUES
                (1, 5, 'AB', 1, 'K618A'),
                (2, 3, 'B', 2, 'A6A'),
                (3, 7, 'ABC', 3, 'J41818X');
            """
        )


def _insert_purchase(
    path: Path,
    *,
    row_id: int,
    material: str,
    product_id: int | None,
) -> None:
    search_text = f"产品-{row_id}"
    product_reference = f"REF-{row_id}"
    supplier = "测试纸板厂"
    searchable = " | ".join((search_text, product_reference, material, supplier))
    with _connect(path) as connection:
        connection.execute(
            """
            INSERT INTO historical_purchase_entries(
                id, source_workbook, source_sheet, source_row,
                source_file_sha256, source_fingerprint, supplier_name,
                product_reference, search_text, normalized_search_text,
                material_code, product_id, customer_id, notes
            ) VALUES (?, '采购原表.xlsx', '历史', ?, ?, ?, ?, ?, ?, ?, ?, ?, 9, '原始备注')
            """,
            (
                row_id,
                row_id + 100,
                f"sha-{row_id}",
                f"finger-{row_id}",
                supplier,
                product_reference,
                search_text,
                normalize_lookup_text(searchable),
                material,
                product_id,
            ),
        )


def _insert_legacy(
    path: Path,
    *,
    row_id: int,
    material: str,
    product_id: int | None,
) -> None:
    with _connect(path) as connection:
        connection.execute(
            """
            INSERT INTO historical_requisition_maps(
                id, product_id, search_key, normalized_search_key,
                material_code, raw_data, source_workbook, source_sheet, source_row
            ) VALUES (?, ?, ?, ?, ?, ?, '旧报料.xlsx', '旧表', ?)
            """,
            (
                row_id,
                product_id,
                f"SEARCH-{row_id}",
                f"SEARCH{row_id}",
                material,
                f'{{"raw":"{material}"}}',
                row_id + 200,
            ),
        )


def _args(*values: str):
    return build_parser().parse_args(list(values))


def _apply_args(database: Path, reports: Path, backups: Path):
    return _args(
        "--database",
        str(database),
        "--output-dir",
        str(reports),
        "--table",
        "historical_purchase_entries",
        "--apply",
        "--confirm-apply",
        APPLY_CONFIRMATION,
        "--confirm-service-stopped",
        STOP_CONFIRMATION,
        "--expected-sha256",
        sha256_file(database),
        "--backup-dir",
        str(backups),
    )


def _install_report_staging_failure(
    *,
    monkeypatch: pytest.MonkeyPatch,
    reports: Path,
    report_kind: str,
) -> list[Path]:
    failed_paths: list[Path] = []

    def is_target(path: Path) -> bool:
        if path.parent != reports:
            return False
        if report_kind == "json":
            return path.name.endswith(".pending.json")
        if report_kind == "csv":
            return (
                path.name.endswith(".pending.csv")
                and not path.name.endswith("_REVIEW.pending.csv")
            )
        if report_kind == "review_csv":
            return path.name.endswith("_REVIEW.pending.csv")
        if report_kind == "markdown":
            return path.name.endswith(".pending.md")
        raise AssertionError(f"unsupported report kind: {report_kind}")

    if report_kind in {"json", "markdown"}:
        real_write_text = Path.write_text

        def partial_write_text_then_fail(self: Path, *args, **kwargs):
            if is_target(self):
                real_write_text(self, *args, **kwargs)
                failed_paths.append(self)
                raise OSError(f"simulated {report_kind} staging write failure")
            return real_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", partial_write_text_then_fail)
    else:
        real_open = Path.open

        def partial_open_then_fail(self: Path, *args, **kwargs):
            if is_target(self):
                handle = real_open(self, *args, **kwargs)
                try:
                    handle.write("partial report staging data")
                    handle.flush()
                finally:
                    handle.close()
                failed_paths.append(self)
                raise OSError(f"simulated {report_kind} staging write failure")
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", partial_open_then_fail)
    return failed_paths


def test_dry_run_classifies_without_touching_database(tmp_path: Path) -> None:
    database = tmp_path / "history.sqlite3"
    reports = tmp_path / "reports"
    _create_database(database)
    _insert_purchase(database, row_id=1, material="K618A / AB楞", product_id=1)
    _insert_purchase(database, row_id=2, material="K618A/B楞", product_id=1)
    _insert_purchase(database, row_id=3, material="K618A/BE", product_id=1)
    _insert_purchase(database, row_id=4, material="K618A/AB/BE楞", product_id=None)
    _insert_purchase(database, row_id=5, material="J41818X/ABC", product_id=3)
    _insert_purchase(database, row_id=6, material="J41818X/AB", product_id=3)
    _insert_purchase(database, row_id=7, material="  k618a/ab  ", product_id=1)
    _insert_legacy(database, row_id=1, material="A6A / B楞", product_id=2)

    before_hash = sha256_file(database)
    before_stat = database.stat()
    result = execute(
        _args("--database", str(database), "--output-dir", str(reports))
    )

    assert sha256_file(database) == before_hash
    assert database.stat().st_size == before_stat.st_size
    assert database.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert result["summary"]["planned_updates"] == 4
    assert result["summary"]["needs_review"] == 2
    assert result["summary"]["statuses"]["unchanged_canonical"] == 1
    assert result["summary"]["statuses"]["warning_canonical_vs_product"] == 1
    assert result["summary"]["statuses"]["review_ambiguous"] == 1
    assert result["summary"]["statuses"]["review_seven_layer_noncanonical"] == 1
    assert all(Path(path).is_file() for path in result["reports"].values())
    review_header = Path(result["reports"]["review_csv"]).read_text(
        encoding="utf-8-sig"
    ).splitlines()[0]
    for field in (
        "source_workbook",
        "source_sheet",
        "source_row",
        "supplier_name",
        "product_reference",
        "search_text",
        "product_material_base",
    ):
        assert field in review_header

    with _connect(database) as connection:
        lowercase = next(row for row in build_plan(connection) if row.row_id == 7)
    assert lowercase.status == "planned_format_normalization"
    assert lowercase.new_material_code == "K618A/AB"

    with _connect(database) as connection:
        assert connection.execute(
            "SELECT material_code FROM historical_purchase_entries WHERE id=2"
        ).fetchone()[0] == "K618A/B楞"


def test_approved_mapping_is_exact_per_row_and_cannot_conflict_with_product(
    tmp_path: Path,
) -> None:
    database = tmp_path / "history.sqlite3"
    mapping = tmp_path / "approved.csv"
    _create_database(database)
    _insert_purchase(database, row_id=1, material="K618A/AB/BE楞", product_id=None)
    _insert_purchase(database, row_id=2, material="K618A/BE", product_id=1)
    _insert_purchase(database, row_id=3, material="K618A/AB/BE楞", product_id=1)
    mapping.write_text(
        "table_name,row_id,expected_material_code,approved_material_code,review_status,reviewer,reviewed_at,authority_override\n"
        "historical_purchase_entries,1,K618A/AB/BE楞,K618A/BE,approved,张三,2026-07-22 10:00:00,\n"
        "historical_purchase_entries,3,K618A/AB/BE楞,K618A/BE,approved,李四,2026-07-22 10:05:00,approved/manual\n",
        encoding="utf-8-sig",
    )
    approved = load_approved_mappings(mapping)
    with _connect(database) as connection:
        decisions = build_plan(connection, approved)
    decision = next(row for row in decisions if row.row_id == 1)
    assert decision.status == "planned_approved_mapping"
    assert decision.new_material_code == "K618A/BE"
    assert next(row for row in decisions if row.row_id == 2).status == "warning_canonical_vs_product"
    override = next(row for row in decisions if row.row_id == 3)
    assert override.status == "planned_approved_mapping"
    assert "覆盖当前常用箱" in override.reason
    result = execute(
        _args(
            "--database",
            str(database),
            "--approved-mapping",
            str(mapping),
            "--output-dir",
            str(tmp_path / "reports"),
        )
    )
    payload = json.loads(Path(result["reports"]["json"]).read_text(encoding="utf-8"))
    assert payload["approval"]["path"] == str(mapping.resolve())
    assert payload["approval"]["sha256"] == sha256_file(mapping)
    assert payload["approval"]["approved_count"] == 2
    assert payload["approval"]["approvals"][1]["authority_override"] == "approved/manual"


def test_product_authority_requires_matching_material_base(tmp_path: Path) -> None:
    database = tmp_path / "history.sqlite3"
    _create_database(database)
    _insert_purchase(database, row_id=1, material="N717K/B楞", product_id=1)
    with _connect(database) as connection:
        decision = next(
            row for row in build_plan(connection) if row.table_name == "historical_purchase_entries"
        )
    assert decision.status == "review_product_material_base_mismatch"
    assert decision.new_material_code is None


def test_five_layer_single_flute_accident_uses_product_authority_only_with_base_evidence(
    tmp_path: Path,
) -> None:
    database = tmp_path / "history.sqlite3"
    _create_database(database)
    with _connect(database) as connection:
        connection.execute(
            """
            INSERT INTO products(
                id, layer_count, flute_type, material_id, default_material_code
            ) VALUES (4, 5, 'AB', NULL, 'BC14C')
            """
        )
    _insert_purchase(database, row_id=1, material="BC14C/A", product_id=4)
    _insert_purchase(database, row_id=2, material="BC14C/A", product_id=None)

    with _connect(database) as connection:
        decisions = build_plan(connection, {}, ("historical_purchase_entries",))

    with_product = next(decision for decision in decisions if decision.row_id == 1)
    without_evidence = next(decision for decision in decisions if decision.row_id == 2)
    assert with_product.status == "planned_product_authority"
    assert with_product.new_material_code == "BC14C/AB"
    assert without_evidence.status == "review_ambiguous"
    assert without_evidence.new_material_code is None


def test_decision_fingerprint_covers_authority_and_source_evidence(tmp_path: Path) -> None:
    database = tmp_path / "history.sqlite3"
    _create_database(database)
    _insert_purchase(database, row_id=1, material="K618A/B楞", product_id=1)
    with _connect(database) as connection:
        decision = build_plan(connection, {}, ("historical_purchase_entries",))[0]

    changed_authority = replace(decision, product_flute_type="BE")
    changed_source = replace(decision, source_sheet="被替换的来源表")
    original_fingerprint = normalizer._decision_fingerprint([decision])
    assert normalizer._decision_fingerprint([changed_authority]) != original_fingerprint
    assert normalizer._decision_fingerprint([changed_source]) != original_fingerprint


def test_partial_report_publish_is_removed_and_json_is_not_exposed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "history.sqlite3"
    reports = tmp_path / "reports"
    _create_database(database)
    _insert_purchase(database, row_id=1, material="K618A/B楞", product_id=1)
    with _connect(database) as connection:
        decision = build_plan(connection, {}, ("historical_purchase_entries",))[0]
    real_replace = normalizer.os.replace
    publish_calls = 0

    def fail_second_publish(source, destination):
        nonlocal publish_calls
        publish_calls += 1
        if publish_calls == 2:
            raise OSError("simulated partial report publication")
        return real_replace(source, destination)

    monkeypatch.setattr(normalizer.os, "replace", fail_second_publish)
    with pytest.raises(OSError, match="partial report publication"):
        normalizer.write_reports(
            output_dir=reports,
            mode="DRY_RUN",
            database=database,
            database_sha256_before=sha256_file(database),
            database_sha256_after=sha256_file(database),
            decisions=[decision],
            verification={"integrity_check": "ok", "foreign_key_errors": 0},
            backup=None,
            approval=None,
        )
    assert list(reports.iterdir()) == []


@pytest.mark.parametrize("report_kind", ["json", "csv", "review_csv", "markdown"])
def test_report_staging_write_failure_removes_every_pending_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    report_kind: str,
) -> None:
    database = tmp_path / "history.sqlite3"
    reports = tmp_path / "reports"
    _create_database(database)
    _insert_purchase(database, row_id=1, material="K618A/B楞", product_id=1)
    with _connect(database) as connection:
        decision = build_plan(connection, {}, ("historical_purchase_entries",))[0]
    failed_paths = _install_report_staging_failure(
        monkeypatch=monkeypatch,
        reports=reports,
        report_kind=report_kind,
    )

    with pytest.raises(OSError, match=f"{report_kind} staging write failure"):
        normalizer.write_reports(
            output_dir=reports,
            mode="DRY_RUN",
            database=database,
            database_sha256_before=sha256_file(database),
            database_sha256_after=sha256_file(database),
            decisions=[decision],
            verification={"integrity_check": "ok", "foreign_key_errors": 0},
            backup=None,
            approval=None,
        )

    assert len(failed_paths) == 1
    assert failed_paths[0].exists() is False
    assert list(reports.iterdir()) == []


def test_apply_uses_online_backup_cas_and_preserves_source_fields(tmp_path: Path) -> None:
    database = tmp_path / "history.sqlite3"
    reports = tmp_path / "reports"
    backups = tmp_path / "backups"
    _create_database(database)
    _insert_purchase(database, row_id=1, material="K618A/B楞", product_id=1)
    _insert_purchase(database, row_id=2, material="A6A / B楞", product_id=2)
    _insert_legacy(database, row_id=1, material="A6A / B楞", product_id=2)
    with _connect(database) as connection:
        source_before = connection.execute(
            """
            SELECT source_workbook, source_sheet, source_row, source_file_sha256,
                   source_fingerprint, search_text, product_reference, notes
            FROM historical_purchase_entries WHERE id=1
            """
        ).fetchone()
        legacy_before = connection.execute(
            """
            SELECT search_key, normalized_search_key, raw_data, source_workbook,
                   source_sheet, source_row
            FROM historical_requisition_maps WHERE id=1
            """
        ).fetchone()
    expected_hash = sha256_file(database)

    result = execute(
        _args(
            "--database",
            str(database),
            "--output-dir",
            str(reports),
            "--apply",
            "--confirm-apply",
            APPLY_CONFIRMATION,
            "--confirm-service-stopped",
            STOP_CONFIRMATION,
            "--expected-sha256",
            expected_hash,
            "--backup-dir",
            str(backups),
        )
    )

    assert result["summary"]["planned_updates"] == 3
    backup = Path(result["backup"]["path"])
    assert backup.is_file()
    assert not list(backups.glob("*.pending"))
    assert result["backup"]["published_sha256"] == result["backup"]["sha256"]
    assert result["backup"]["published_size"] == result["backup"]["size"]
    assert result["backup"]["published_verification"]["integrity_check"] == "ok"
    assert result["backup"]["published_verification"]["foreign_key_errors"] == 0
    with _connect(backup) as connection:
        assert connection.execute(
            "SELECT material_code FROM historical_purchase_entries WHERE id=1"
        ).fetchone()[0] == "K618A/B楞"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    with _connect(database) as connection:
        connection.row_factory = sqlite3.Row
        first = connection.execute(
            "SELECT * FROM historical_purchase_entries WHERE id=1"
        ).fetchone()
        assert first["material_code"] == "K618A/AB"
        expected_search = normalize_lookup_text(
            " | ".join(
                (
                    first["search_text"],
                    first["product_reference"],
                    "K618A/AB",
                    first["supplier_name"],
                )
            )
        )
        assert first["normalized_search_text"] == expected_search
        assert connection.execute(
            "SELECT material_code FROM historical_purchase_entries WHERE id=2"
        ).fetchone()[0] == "A6A/B"
        assert connection.execute(
            "SELECT material_code FROM historical_requisition_maps WHERE id=1"
        ).fetchone()[0] == "A6A/B"
        source_after = connection.execute(
            """
            SELECT source_workbook, source_sheet, source_row, source_file_sha256,
                   source_fingerprint, search_text, product_reference, notes
            FROM historical_purchase_entries WHERE id=1
            """
        ).fetchone()
        legacy_after = connection.execute(
            """
            SELECT search_key, normalized_search_key, raw_data, source_workbook,
                   source_sheet, source_row
            FROM historical_requisition_maps WHERE id=1
            """
        ).fetchone()
        assert tuple(source_after) == tuple(source_before)
        assert tuple(legacy_after) == tuple(legacy_before)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_apply_refuses_partial_cleanup_when_review_rows_remain(tmp_path: Path) -> None:
    database = tmp_path / "history.sqlite3"
    _create_database(database)
    _insert_purchase(database, row_id=1, material="K618A/AB/BE楞", product_id=None)
    expected_hash = sha256_file(database)

    with pytest.raises(NormalizationError, match="禁止部分清洗"):
        execute(
            _args(
                "--database",
                str(database),
                "--output-dir",
                str(tmp_path / "reports"),
                "--apply",
                "--confirm-apply",
                APPLY_CONFIRMATION,
                "--confirm-service-stopped",
                STOP_CONFIRMATION,
                "--expected-sha256",
                expected_hash,
                "--backup-dir",
                str(tmp_path / "backups"),
            )
        )
    assert sha256_file(database) == expected_hash
    assert not (tmp_path / "backups").exists()


def test_apply_refuses_nonempty_wal_or_journal_sidecar(tmp_path: Path) -> None:
    database = tmp_path / "history.sqlite3"
    _create_database(database)
    _insert_purchase(database, row_id=1, material="K618A/B楞", product_id=1)
    expected_hash = sha256_file(database)
    Path(f"{database}-wal").write_bytes(b"uncheckpointed")

    with pytest.raises(NormalizationError, match="WAL/journal"):
        execute(
            _args(
                "--database",
                str(database),
                "--output-dir",
                str(tmp_path / "reports"),
                "--apply",
                "--confirm-apply",
                APPLY_CONFIRMATION,
                "--confirm-service-stopped",
                STOP_CONFIRMATION,
                "--expected-sha256",
                expected_hash,
                "--backup-dir",
                str(tmp_path / "backups"),
            )
        )
    assert sha256_file(database) == expected_hash


def test_apply_requires_delete_journal_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "history.sqlite3"
    _create_database(database)
    _insert_purchase(database, row_id=1, material="K618A/B楞", product_id=1)
    expected_hash = sha256_file(database)
    real_verify = normalizer._verify_database
    verification_calls = 0

    def report_non_delete_once(connection: sqlite3.Connection):
        nonlocal verification_calls
        result = real_verify(connection)
        verification_calls += 1
        if verification_calls == 1:
            result["journal_mode"] = "truncate"
        return result

    monkeypatch.setattr(normalizer, "_verify_database", report_non_delete_once)
    with pytest.raises(NormalizationError, match="精确要求 journal_mode=delete"):
        execute(
            _args(
                "--database",
                str(database),
                "--output-dir",
                str(tmp_path / "reports"),
                "--apply",
                "--confirm-apply",
                APPLY_CONFIRMATION,
                "--confirm-service-stopped",
                STOP_CONFIRMATION,
                "--expected-sha256",
                expected_hash,
                "--backup-dir",
                str(tmp_path / "backups"),
            )
        )
    assert sha256_file(database) == expected_hash
    assert not (tmp_path / "backups").exists()


def test_table_scope_allows_current_history_to_be_cleaned_before_legacy_review(
    tmp_path: Path,
) -> None:
    database = tmp_path / "history.sqlite3"
    _create_database(database)
    _insert_purchase(database, row_id=1, material="K618A/B楞", product_id=1)
    _insert_legacy(database, row_id=1, material="K618A/AB/BE楞", product_id=None)
    expected_hash = sha256_file(database)

    result = execute(
        _args(
            "--database",
            str(database),
            "--output-dir",
            str(tmp_path / "reports"),
            "--table",
            "historical_purchase_entries",
            "--apply",
            "--confirm-apply",
            APPLY_CONFIRMATION,
            "--confirm-service-stopped",
            STOP_CONFIRMATION,
            "--expected-sha256",
            expected_hash,
            "--backup-dir",
            str(tmp_path / "backups"),
        )
    )
    assert result["summary"]["needs_review"] == 0
    with _connect(database) as connection:
        assert connection.execute(
            "SELECT material_code FROM historical_purchase_entries WHERE id=1"
        ).fetchone()[0] == "K618A/AB"
        assert connection.execute(
            "SELECT material_code FROM historical_requisition_maps WHERE id=1"
        ).fetchone()[0] == "K618A/AB/BE楞"


def test_post_commit_report_failure_preserves_failed_state_and_restores_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "history.sqlite3"
    reports = tmp_path / "reports"
    backups = tmp_path / "backups"
    _create_database(database)
    _insert_purchase(database, row_id=1, material="K618A/B楞", product_id=1)
    expected_hash = sha256_file(database)

    def fail_report(**_kwargs):
        raise OSError("simulated report storage failure")

    real_replace = normalizer.os.replace
    replace_calls: list[tuple[Path, Path]] = []

    def observed_replace(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if destination_path == database:
            assert database.exists(), "正式数据库路径在原子恢复前不得消失"
        replace_calls.append((source_path, destination_path))
        return real_replace(source, destination)

    monkeypatch.setattr(normalizer, "write_reports", fail_report)
    monkeypatch.setattr(normalizer.os, "replace", observed_replace)
    with pytest.raises(NormalizationError, match="已自动恢复验证备份"):
        execute(
            _args(
                "--database",
                str(database),
                "--output-dir",
                str(reports),
                "--table",
                "historical_purchase_entries",
                "--apply",
                "--confirm-apply",
                APPLY_CONFIRMATION,
                "--confirm-service-stopped",
                STOP_CONFIRMATION,
                "--expected-sha256",
                expected_hash,
                "--backup-dir",
                str(backups),
            )
        )
    with _connect(database) as connection:
        assert connection.execute(
            "SELECT material_code FROM historical_purchase_entries WHERE id=1"
        ).fetchone()[0] == "K618A/B楞"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert list(backups.glob("*_FAILED_RAW_*.sqlite3"))
    assert list(backups.glob("*_FAILED_COMMITTED_HISTORY_MATERIAL_*.sqlite3"))
    assert any(destination == database for _, destination in replace_calls)
    assert not any(source == database and destination != database for source, destination in replace_calls)
    failure_reports = list(reports.glob("HISTORICAL_MATERIAL_NORMALIZATION_FAILED_*.json"))
    assert len(failure_reports) == 1
    failure_payload = json.loads(failure_reports[0].read_text(encoding="utf-8"))
    assert failure_payload["status"] == "apply_failed_and_backup_restored"


def test_failed_state_snapshot_is_best_effort_and_does_not_block_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "history.sqlite3"
    reports = tmp_path / "reports"
    backups = tmp_path / "backups"
    _create_database(database)
    _insert_purchase(database, row_id=1, material="K618A/B楞", product_id=1)
    expected_hash = sha256_file(database)
    real_online_backup = normalizer._online_backup

    def fail_report(**_kwargs):
        raise OSError("simulated report storage failure")

    def flaky_online_backup(database_path, backup_dir, *, label="before_historical_material_cleanup"):
        if label == "FAILED_COMMITTED_HISTORY_MATERIAL":
            raise OSError("simulated failed-state snapshot failure")
        return real_online_backup(database_path, backup_dir, label=label)

    monkeypatch.setattr(normalizer, "write_reports", fail_report)
    monkeypatch.setattr(normalizer, "_online_backup", flaky_online_backup)
    with pytest.raises(NormalizationError, match="已自动恢复验证备份"):
        execute(
            _args(
                "--database",
                str(database),
                "--output-dir",
                str(reports),
                "--table",
                "historical_purchase_entries",
                "--apply",
                "--confirm-apply",
                APPLY_CONFIRMATION,
                "--confirm-service-stopped",
                STOP_CONFIRMATION,
                "--expected-sha256",
                expected_hash,
                "--backup-dir",
                str(backups),
            )
        )
    with _connect(database) as connection:
        assert connection.execute(
            "SELECT material_code FROM historical_purchase_entries WHERE id=1"
        ).fetchone()[0] == "K618A/B楞"
    failure_report = next(reports.glob("HISTORICAL_MATERIAL_NORMALIZATION_FAILED_*.json"))
    payload = json.loads(failure_report.read_text(encoding="utf-8"))
    assert "simulated failed-state snapshot failure" in payload["failed_consistent_snapshot_error"]
    assert payload["failed_consistent_snapshot_path"] is None


def test_durable_commit_exception_is_detected_and_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "history.sqlite3"
    reports = tmp_path / "reports"
    backups = tmp_path / "backups"
    _create_database(database)
    _insert_purchase(database, row_id=1, material="K618A/B楞", product_id=1)
    expected_hash = sha256_file(database)

    def commit_then_fail(connection: sqlite3.Connection) -> None:
        connection.commit()
        raise OSError("simulated exception after durable commit")

    monkeypatch.setattr(normalizer, "_commit_transaction", commit_then_fail)
    with pytest.raises(
        NormalizationError,
        match="写入/提交异常且发现持久变化，已自动恢复验证备份",
    ):
        execute(
            _args(
                "--database",
                str(database),
                "--output-dir",
                str(reports),
                "--table",
                "historical_purchase_entries",
                "--apply",
                "--confirm-apply",
                APPLY_CONFIRMATION,
                "--confirm-service-stopped",
                STOP_CONFIRMATION,
                "--expected-sha256",
                expected_hash,
                "--backup-dir",
                str(backups),
            )
        )
    with _connect(database) as connection:
        assert connection.execute(
            "SELECT material_code FROM historical_purchase_entries WHERE id=1"
        ).fetchone()[0] == "K618A/B楞"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert list(backups.glob("*_FAILED_RAW_*.sqlite3"))
    failure_report = next(reports.glob("HISTORICAL_MATERIAL_NORMALIZATION_FAILED_*.json"))
    payload = json.loads(failure_report.read_text(encoding="utf-8"))
    assert sha256_file(database) == payload["pre_apply_backup"]["sha256"]


def test_online_backup_publish_failure_removes_pending_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "history.sqlite3"
    backups = tmp_path / "backups"
    _create_database(database)
    real_replace = normalizer.os.replace

    def fail_backup_publish(source, destination):
        if Path(source).name.endswith(".pending"):
            raise OSError("simulated backup atomic publish failure")
        return real_replace(source, destination)

    monkeypatch.setattr(normalizer.os, "replace", fail_backup_publish)
    with pytest.raises(OSError, match="backup atomic publish failure"):
        normalizer._online_backup(database, backups)
    assert backups.is_dir()
    assert list(backups.iterdir()) == []


def test_online_backup_reverifies_published_path_and_removes_invalid_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "history.sqlite3"
    backups = tmp_path / "backups"
    _create_database(database)
    real_verify = normalizer._verify_database
    published_path_was_checked = False

    def fail_only_published_backup(connection: sqlite3.Connection):
        nonlocal published_path_was_checked
        opened_path = Path(connection.execute("PRAGMA database_list").fetchone()[2])
        if opened_path.parent == backups and not opened_path.name.startswith("."):
            published_path_was_checked = True
            raise OSError("simulated published backup verification failure")
        return real_verify(connection)

    monkeypatch.setattr(normalizer, "_verify_database", fail_only_published_backup)
    with pytest.raises(OSError, match="published backup verification failure"):
        normalizer._online_backup(database, backups)
    assert published_path_was_checked is True
    assert backups.is_dir()
    assert list(backups.iterdir()) == []


@pytest.mark.parametrize("fault_kind", ["sha256", "stat"])
def test_post_commit_hash_or_stat_failure_restores_backup_and_writes_failure_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_kind: str,
) -> None:
    database = tmp_path / "history.sqlite3"
    reports = tmp_path / "reports"
    backups = tmp_path / "backups"
    _create_database(database)
    _insert_purchase(database, row_id=1, material="K618A/B楞", product_id=1)
    expected_hash = sha256_file(database)
    real_commit = normalizer._commit_transaction
    committed = False
    injected = False

    def mark_commit(connection: sqlite3.Connection) -> None:
        nonlocal committed
        real_commit(connection)
        committed = True

    monkeypatch.setattr(normalizer, "_commit_transaction", mark_commit)
    if fault_kind == "sha256":
        real_sha256 = normalizer.sha256_file

        def fail_sha256_once(path: Path) -> str:
            nonlocal injected
            if committed and Path(path) == database and not injected:
                injected = True
                raise OSError("simulated post-commit sha256 failure")
            return real_sha256(path)

        monkeypatch.setattr(normalizer, "sha256_file", fail_sha256_once)
    else:
        real_stat = Path.stat

        def fail_stat_once(self: Path, *args, **kwargs):
            nonlocal injected
            if committed and self == database and not injected:
                injected = True
                raise OSError("simulated post-commit Path.stat failure")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", fail_stat_once)

    with pytest.raises(NormalizationError, match="提交后收尾审计失败，已自动恢复验证备份"):
        execute(
            _args(
                "--database",
                str(database),
                "--output-dir",
                str(reports),
                "--table",
                "historical_purchase_entries",
                "--apply",
                "--confirm-apply",
                APPLY_CONFIRMATION,
                "--confirm-service-stopped",
                STOP_CONFIRMATION,
                "--expected-sha256",
                expected_hash,
                "--backup-dir",
                str(backups),
            )
        )
    assert injected is True
    with _connect(database) as connection:
        assert connection.execute(
            "SELECT material_code FROM historical_purchase_entries WHERE id=1"
        ).fetchone()[0] == "K618A/B楞"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    failure_report = next(reports.glob("HISTORICAL_MATERIAL_NORMALIZATION_FAILED_*.json"))
    payload = json.loads(failure_report.read_text(encoding="utf-8"))
    assert payload["status"] == "apply_failed_and_backup_restored"
    assert not [
        path
        for path in reports.glob("HISTORICAL_MATERIAL_NORMALIZATION_APPLY_*.json")
        if not path.name.endswith(".INVALIDATED.json")
    ]


@pytest.mark.parametrize("compensation_fails", [False, True])
def test_restore_replace_failure_compensates_sidecar_or_records_emergency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    compensation_fails: bool,
) -> None:
    database = tmp_path / "history.sqlite3"
    reports = tmp_path / "reports"
    backups = tmp_path / "backups"
    sidecar = Path(f"{database}-shm")
    _create_database(database)
    _insert_purchase(database, row_id=1, material="K618A/B楞", product_id=1)
    expected_hash = sha256_file(database)
    real_replace = normalizer.os.replace
    main_replace_failed = False
    compensation_failed = False

    def create_sidecar_then_fail_report(**_kwargs):
        sidecar.write_bytes(b"post-commit-sidecar")
        raise OSError("simulated report failure before emergency restore")

    def fail_restore_replace(source, destination):
        nonlocal main_replace_failed, compensation_failed
        source_path = Path(source)
        destination_path = Path(destination)
        if destination_path == database and ".restore-" in source_path.name:
            main_replace_failed = True
            raise OSError("simulated main database restore replace failure")
        if (
            compensation_fails
            and destination_path == sidecar
            and source_path.name.endswith("-shm")
        ):
            compensation_failed = True
            raise OSError("simulated sidecar compensation failure")
        return real_replace(source, destination)

    monkeypatch.setattr(normalizer, "write_reports", create_sidecar_then_fail_report)
    monkeypatch.setattr(normalizer.os, "replace", fail_restore_replace)
    with pytest.raises(NormalizationError, match="自动恢复失败，禁止启动 ERP"):
        execute(
            _args(
                "--database",
                str(database),
                "--output-dir",
                str(reports),
                "--table",
                "historical_purchase_entries",
                "--apply",
                "--confirm-apply",
                APPLY_CONFIRMATION,
                "--confirm-service-stopped",
                STOP_CONFIRMATION,
                "--expected-sha256",
                expected_hash,
                "--backup-dir",
                str(backups),
            )
        )
    assert main_replace_failed is True
    with _connect(database) as connection:
        assert connection.execute(
            "SELECT material_code FROM historical_purchase_entries WHERE id=1"
        ).fetchone()[0] == "K618A/AB"
    emergency_report = next(
        reports.glob("EMERGENCY_HISTORICAL_MATERIAL_NORMALIZATION_*.json")
    )
    payload = json.loads(emergency_report.read_text(encoding="utf-8"))
    assert payload["status"] == "EMERGENCY_RESTORE_FAILED"
    assert payload["prohibit_erp_start"] is True
    assert payload["database"] == str(database)
    assert Path(payload["restore_temp"]).is_file()
    assert Path(payload["pre_apply_backup_path"]).is_file()
    shm_record = next(record for record in payload["sidecars"] if record["suffix"] == "-shm")
    if compensation_fails:
        assert compensation_failed is True
        assert sidecar.exists() is False
        assert Path(shm_record["quarantine"]).is_file()
        assert shm_record["compensation_restored"] is False
        assert "sidecar compensation failure" in shm_record["compensation_error"]
    else:
        assert compensation_failed is False
        assert sidecar.read_bytes() == b"post-commit-sidecar"
        assert Path(shm_record["quarantine"]).exists() is False
        assert shm_record["compensation_restored"] is True


@pytest.mark.parametrize(
    ("fault_kind", "expected_stage"),
    [
        ("copy", "restore_temp_copy"),
        ("hash", "restore_temp_hash_verification"),
        ("integrity", "restore_temp_integrity_verification"),
    ],
)
def test_restore_pre_replace_stage_failures_are_explicit_emergencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_kind: str,
    expected_stage: str,
) -> None:
    database = tmp_path / "history.sqlite3"
    reports = tmp_path / "reports"
    backups = tmp_path / "backups"
    _create_database(database)
    _insert_purchase(database, row_id=1, material="K618A/B楞", product_id=1)
    args = _apply_args(database, reports, backups)

    def fail_report(**_kwargs):
        raise OSError("simulated post-commit report failure")

    monkeypatch.setattr(normalizer, "write_reports", fail_report)
    if fault_kind == "copy":
        real_copy2 = normalizer.shutil.copy2

        def fail_restore_copy(source, destination, *args, **kwargs):
            if ".restore-" in Path(destination).name:
                raise OSError("simulated restore temporary copy failure")
            return real_copy2(source, destination, *args, **kwargs)

        monkeypatch.setattr(normalizer.shutil, "copy2", fail_restore_copy)
    elif fault_kind == "hash":
        real_sha256 = normalizer.sha256_file

        def fail_restore_hash(path: Path) -> str:
            if ".restore-" in Path(path).name:
                raise OSError("simulated restore temporary hash failure")
            return real_sha256(path)

        monkeypatch.setattr(normalizer, "sha256_file", fail_restore_hash)
    else:
        real_verify = normalizer._verify_database

        def fail_restore_integrity(connection: sqlite3.Connection):
            opened_path = connection.execute("PRAGMA database_list").fetchone()[2]
            if ".restore-" in Path(opened_path).name:
                raise OSError("simulated restore temporary integrity failure")
            return real_verify(connection)

        monkeypatch.setattr(normalizer, "_verify_database", fail_restore_integrity)

    with pytest.raises(NormalizationError, match="自动恢复失败，禁止启动 ERP"):
        execute(args)

    with _connect(database) as connection:
        # Restore never reached main-file replacement; the committed cleanup is
        # intentionally left in an explicit stop-the-world emergency state.
        assert connection.execute(
            "SELECT material_code FROM historical_purchase_entries WHERE id=1"
        ).fetchone()[0] == "K618A/AB"
    emergency_report = next(
        reports.glob("EMERGENCY_HISTORICAL_MATERIAL_NORMALIZATION_*.json")
    )
    payload = json.loads(emergency_report.read_text(encoding="utf-8"))
    assert payload["status"] == "EMERGENCY_RESTORE_FAILED"
    assert payload["restore_stage"] == expected_stage
    assert payload["main_replace_state"] == "not_started"
    assert payload["prohibit_erp_start"] is True
    assert Path(payload["pre_apply_backup_path"]).is_file()


def test_post_replace_verification_failure_never_reattaches_old_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "history.sqlite3"
    reports = tmp_path / "reports"
    backups = tmp_path / "backups"
    sidecar = Path(f"{database}-shm")
    _create_database(database)
    _insert_purchase(database, row_id=1, material="K618A/B楞", product_id=1)
    args = _apply_args(database, reports, backups)
    real_replace = normalizer.os.replace
    real_verify = normalizer._verify_database
    main_replaced = False

    def create_sidecar_then_fail_report(**_kwargs):
        sidecar.write_bytes(b"old-post-commit-sidecar")
        raise OSError("simulated report failure")

    def observe_restore_replace(source, destination):
        nonlocal main_replaced
        result = real_replace(source, destination)
        if Path(destination) == database and ".restore-" in Path(source).name:
            main_replaced = True
        return result

    def fail_post_replace_verification(connection: sqlite3.Connection):
        opened_path = Path(connection.execute("PRAGMA database_list").fetchone()[2])
        if main_replaced and opened_path == database:
            raise OSError("simulated post-replace integrity failure")
        return real_verify(connection)

    monkeypatch.setattr(normalizer, "write_reports", create_sidecar_then_fail_report)
    monkeypatch.setattr(normalizer.os, "replace", observe_restore_replace)
    monkeypatch.setattr(normalizer, "_verify_database", fail_post_replace_verification)
    with pytest.raises(NormalizationError, match="自动恢复失败，禁止启动 ERP"):
        execute(args)

    assert main_replaced is True
    with _connect(database) as connection:
        assert connection.execute(
            "SELECT material_code FROM historical_purchase_entries WHERE id=1"
        ).fetchone()[0] == "K618A/B楞"
    assert sidecar.exists() is False
    emergency_report = next(
        reports.glob("EMERGENCY_HISTORICAL_MATERIAL_NORMALIZATION_*.json")
    )
    payload = json.loads(emergency_report.read_text(encoding="utf-8"))
    assert payload["restore_stage"] == "post_replace_integrity_verification"
    assert payload["main_replace_state"] == "replaced_unverified"
    assert payload["sidecar_compensation_attempted"] is False
    shm_record = next(record for record in payload["sidecars"] if record["suffix"] == "-shm")
    assert Path(shm_record["quarantine"]).read_bytes() == b"old-post-commit-sidecar"
    assert shm_record["compensation_restored"] is False
    assert "继续隔离" in shm_record["compensation_skipped_reason"]


def test_restore_replace_that_completes_then_raises_keeps_sidecar_quarantined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "history.sqlite3"
    reports = tmp_path / "reports"
    backups = tmp_path / "backups"
    sidecar = Path(f"{database}-shm")
    _create_database(database)
    _insert_purchase(database, row_id=1, material="K618A/B楞", product_id=1)
    args = _apply_args(database, reports, backups)
    real_replace = normalizer.os.replace
    replacement_completed = False

    def create_sidecar_then_fail_report(**_kwargs):
        sidecar.write_bytes(b"old-sidecar-after-ambiguous-replace")
        raise OSError("simulated report failure")

    def replace_then_raise(source, destination):
        nonlocal replacement_completed
        if Path(destination) == database and ".restore-" in Path(source).name:
            real_replace(source, destination)
            replacement_completed = True
            raise OSError("simulated error after successful main replace")
        return real_replace(source, destination)

    monkeypatch.setattr(normalizer, "write_reports", create_sidecar_then_fail_report)
    monkeypatch.setattr(normalizer.os, "replace", replace_then_raise)
    with pytest.raises(NormalizationError, match="自动恢复失败，禁止启动 ERP"):
        execute(args)

    assert replacement_completed is True
    with _connect(database) as connection:
        assert connection.execute(
            "SELECT material_code FROM historical_purchase_entries WHERE id=1"
        ).fetchone()[0] == "K618A/B楞"
    assert sidecar.exists() is False
    emergency_report = next(
        reports.glob("EMERGENCY_HISTORICAL_MATERIAL_NORMALIZATION_*.json")
    )
    payload = json.loads(emergency_report.read_text(encoding="utf-8"))
    assert payload["restore_stage"] == "main_database_atomic_replace"
    assert payload["main_replace_state"] == "replace_outcome_unknown_or_replaced"
    assert payload["sidecar_compensation_attempted"] is False
    shm_record = next(record for record in payload["sidecars"] if record["suffix"] == "-shm")
    assert Path(shm_record["quarantine"]).read_bytes() == b"old-sidecar-after-ambiguous-replace"


def test_partial_apply_report_cleanup_failure_gets_tombstone_after_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "history.sqlite3"
    reports = tmp_path / "reports"
    backups = tmp_path / "backups"
    _create_database(database)
    _insert_purchase(database, row_id=1, material="K618A/B楞", product_id=1)
    args = _apply_args(database, reports, backups)
    real_replace = normalizer.os.replace
    real_unlink = Path.unlink
    report_publish_calls = 0

    def fail_second_report_publish(source, destination):
        nonlocal report_publish_calls
        source_path = Path(source)
        if source_path.parent == reports and ".pending." in source_path.name:
            report_publish_calls += 1
            if report_publish_calls == 2:
                raise OSError("simulated partial APPLY report publication")
        return real_replace(source, destination)

    def refuse_published_csv_cleanup(self: Path, *args, **kwargs):
        if (
            self.parent == reports
            and "_APPLY_" in self.name
            and self.suffix.lower() == ".csv"
            and not self.name.endswith("_REVIEW.csv")
            and ".pending." not in self.name
        ):
            raise OSError("simulated published APPLY CSV cleanup failure")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(normalizer.os, "replace", fail_second_report_publish)
    monkeypatch.setattr(Path, "unlink", refuse_published_csv_cleanup)
    with pytest.raises(
        NormalizationError,
        match="报告失效标记或清理失败，禁止启动 ERP",
    ):
        execute(args)

    assert report_publish_calls == 2
    with _connect(database) as connection:
        assert connection.execute(
            "SELECT material_code FROM historical_purchase_entries WHERE id=1"
        ).fetchone()[0] == "K618A/B楞"
    residual_csv = next(reports.glob("HISTORICAL_MATERIAL_NORMALIZATION_APPLY_*.csv"))
    tombstone = Path(f"{residual_csv}.INVALIDATED.json")
    tombstone_payload = json.loads(tombstone.read_text(encoding="utf-8"))
    assert tombstone_payload["status"] == "INVALIDATED_APPLY_REPORT"
    assert tombstone_payload["valid"] is False
    assert tombstone_payload["prohibit_erp_start"] is True
    assert not [
        path
        for path in reports.glob("HISTORICAL_MATERIAL_NORMALIZATION_APPLY_*.json")
        if not path.name.endswith(".INVALIDATED.json")
    ]
    emergency_report = next(
        reports.glob("EMERGENCY_HISTORICAL_MATERIAL_NORMALIZATION_*.json")
    )
    emergency = json.loads(emergency_report.read_text(encoding="utf-8"))
    assert emergency["status"] == "EMERGENCY_REPORT_CLEANUP_FAILED"
    assert emergency["extra_evidence"]["database_restore_verified"] is True


def test_published_apply_json_cleanup_failure_is_replaced_by_invalid_tombstone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "history.sqlite3"
    reports = tmp_path / "reports"
    backups = tmp_path / "backups"
    _create_database(database)
    _insert_purchase(database, row_id=1, material="K618A/B楞", product_id=1)
    args = _apply_args(database, reports, backups)
    real_unlink = Path.unlink

    def fail_return_audit(**_kwargs):
        raise OSError("simulated return audit failure after report publication")

    def refuse_success_json_cleanup(self: Path, *args, **kwargs):
        if (
            self.parent == reports
            and "_APPLY_" in self.name
            and self.suffix.lower() == ".json"
            and not self.name.startswith("EMERGENCY_")
        ):
            raise OSError("simulated authoritative APPLY JSON cleanup failure")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(normalizer, "_audit_before_return", fail_return_audit)
    monkeypatch.setattr(Path, "unlink", refuse_success_json_cleanup)
    with pytest.raises(
        NormalizationError,
        match="报告失效标记或清理失败，禁止启动 ERP",
    ):
        execute(args)

    with _connect(database) as connection:
        assert connection.execute(
            "SELECT material_code FROM historical_purchase_entries WHERE id=1"
        ).fetchone()[0] == "K618A/B楞"
    apply_json = next(reports.glob("HISTORICAL_MATERIAL_NORMALIZATION_APPLY_*.json"))
    tombstone_payload = json.loads(apply_json.read_text(encoding="utf-8"))
    assert tombstone_payload["status"] == "INVALIDATED_APPLY_REPORT"
    assert tombstone_payload["valid"] is False
    assert tombstone_payload["prohibit_erp_start"] is True
    emergency_report = next(
        reports.glob("EMERGENCY_HISTORICAL_MATERIAL_NORMALIZATION_*.json")
    )
    emergency = json.loads(emergency_report.read_text(encoding="utf-8"))
    assert emergency["status"] == "EMERGENCY_REPORT_CLEANUP_FAILED"
    artifact = next(
        item
        for item in emergency["extra_evidence"]["report_artifacts"]
        if item["path"] == str(apply_json)
    )
    assert artifact["authoritative_json_replaced"] is True
    assert artifact["tombstone_error"] is None


@pytest.mark.parametrize("report_kind", ["json", "csv", "review_csv", "markdown"])
def test_apply_staging_failure_with_unlink_failure_restores_and_invalidates_residual(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    report_kind: str,
) -> None:
    database = tmp_path / "history.sqlite3"
    reports = tmp_path / "reports"
    backups = tmp_path / "backups"
    _create_database(database)
    _insert_purchase(database, row_id=1, material="K618A/B楞", product_id=1)
    args = _apply_args(database, reports, backups)
    failed_paths = _install_report_staging_failure(
        monkeypatch=monkeypatch,
        reports=reports,
        report_kind=report_kind,
    )
    real_unlink = Path.unlink

    def refuse_failed_staging_cleanup(self: Path, *args, **kwargs):
        if failed_paths and self == failed_paths[0]:
            raise OSError(f"simulated {report_kind} pending unlink failure")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse_failed_staging_cleanup)
    with pytest.raises(
        NormalizationError,
        match="报告失效标记或清理失败，禁止启动 ERP",
    ):
        execute(args)

    assert len(failed_paths) == 1
    failed_path = failed_paths[0]
    with _connect(database) as connection:
        assert connection.execute(
            "SELECT material_code FROM historical_purchase_entries WHERE id=1"
        ).fetchone()[0] == "K618A/B楞"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    if report_kind == "json":
        tombstone = failed_path
    else:
        tombstone = Path(f"{failed_path}.INVALIDATED.json")
        assert failed_path.is_file()
    tombstone_payload = json.loads(tombstone.read_text(encoding="utf-8"))
    assert tombstone_payload["status"] == "INVALIDATED_APPLY_REPORT"
    assert tombstone_payload["valid"] is False
    assert tombstone_payload["prohibit_erp_start"] is True

    emergency_report = next(
        reports.glob("EMERGENCY_HISTORICAL_MATERIAL_NORMALIZATION_*.json")
    )
    emergency = json.loads(emergency_report.read_text(encoding="utf-8"))
    assert emergency["status"] == "EMERGENCY_REPORT_CLEANUP_FAILED"
    assert emergency["restore_stage"] == "report_artifact_cleanup_after_restore"
    assert emergency["extra_evidence"]["database_restore_verified"] is True
    artifact = next(
        item
        for item in emergency["extra_evidence"]["report_artifacts"]
        if item["path"] == str(failed_path)
    )
    assert "pending unlink failure" in artifact["delete_error"]
    assert artifact["tombstone_error"] is None
    assert not [
        path
        for path in reports.iterdir()
        if path.name.startswith("HISTORICAL_MATERIAL_NORMALIZATION_APPLY_")
        and not path.name.endswith(".INVALIDATED.json")
    ]
