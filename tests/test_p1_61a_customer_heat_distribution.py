from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from scripts.audit import p1_61a_customer_heat_distribution as audit


AS_OF = date(2026, 8, 14)
ANON_KEY = b"p1-61a-anonymous-fixture-only"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _insert_order(
    connection: sqlite3.Connection,
    *,
    order_id: int,
    customer_id: int,
    order_date: str,
    status: str = "pending_production",
    lines: list[tuple[int, float, float]],
) -> None:
    connection.execute(
        "INSERT INTO sales_orders "
        "(id, order_number, customer_id, order_date, status) VALUES (?, ?, ?, ?, ?)",
        (order_id, f"SO-{order_id}", customer_id, order_date, status),
    )
    for sequence, (quantity, unit_price, subtotal) in enumerate(lines, start=1):
        connection.execute(
            "INSERT INTO sales_order_items "
            "(id, order_id, quantity, unit_price, subtotal) VALUES (?, ?, ?, ?, ?)",
            (order_id * 10 + sequence, order_id, quantity, unit_price, subtotal),
        )


@pytest.fixture()
def anonymous_database(tmp_path: Path) -> Path:
    database = tmp_path / "p1_61a_anonymous.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE alembic_version (
                version_num TEXT NOT NULL PRIMARY KEY
            );
            INSERT INTO alembic_version(version_num) VALUES ('mm21v8x9z10');

            CREATE TABLE customers (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                is_active INTEGER NOT NULL
            );
            CREATE TABLE sales_orders (
                id INTEGER PRIMARY KEY,
                order_number TEXT NOT NULL UNIQUE,
                customer_id INTEGER NOT NULL REFERENCES customers(id),
                order_date TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE INDEX ix_sales_orders_customer_id ON sales_orders(customer_id);
            CREATE INDEX ix_sales_orders_order_date ON sales_orders(order_date);
            CREATE INDEX ix_sales_orders_status ON sales_orders(status);

            CREATE TABLE sales_order_items (
                id INTEGER PRIMARY KEY,
                order_id INTEGER NOT NULL REFERENCES sales_orders(id) ON DELETE CASCADE,
                quantity INTEGER NOT NULL,
                unit_price NUMERIC,
                subtotal NUMERIC
            );
            CREATE INDEX ix_sales_order_items_order_id ON sales_order_items(order_id);
            """
        )
        connection.executemany(
            "INSERT INTO customers(id, name, status, is_active) VALUES (?, ?, ?, ?)",
            [
                (1, "苏州机密客户甲", "active", 1),
                (2, "昆山机密客户乙", "active", 1),
                (3, "常熟机密客户丙", "active", 1),
                (4, "太仓机密客户丁", "active", 1),
                (5, "无订单机密客户", "active", 1),
                (6, "已停用机密客户", "inactive", 0),
                (7, "只有无效单客户", "active", 1),
            ],
        )

        _insert_order(
            connection,
            order_id=101,
            customer_id=1,
            order_date="2026-08-14",
            lines=[(10, 2, 20), (5, 3, 15)],
        )
        _insert_order(
            connection,
            order_id=102,
            customer_id=1,
            order_date="2026-08-14",
            status="completed",
            lines=[(4, 5, 20)],
        )
        _insert_order(
            connection,
            order_id=103,
            customer_id=1,
            order_date="2026-08-01",
            status="archived",
            lines=[(3, 10, 30)],
        )
        _insert_order(
            connection,
            order_id=104,
            customer_id=1,
            order_date="2025-12-01",
            status="closed",
            lines=[(4, 10, 40)],
        )
        for order_id, status in ((105, "cancelled"), (106, "draft"), (107, "test")):
            _insert_order(
                connection,
                order_id=order_id,
                customer_id=1,
                order_date="2026-08-13",
                status=status,
                lines=[(999, 1, 999)],
            )

        _insert_order(
            connection,
            order_id=201,
            customer_id=2,
            order_date="2026-08-05",
            lines=[(2, 8, 16)],
        )
        _insert_order(
            connection,
            order_id=202,
            customer_id=2,
            order_date="2026-08-10",
            lines=[(2, 8, 16)],
        )

        _insert_order(
            connection,
            order_id=301,
            customer_id=3,
            order_date="2025-08-13",
            lines=[(2, 8, 16)],
        )

        _insert_order(
            connection,
            order_id=401,
            customer_id=4,
            order_date="2026-06-01",
            lines=[(1, 10, 10)],
        )
        _insert_order(
            connection,
            order_id=402,
            customer_id=4,
            order_date="2026-07-01",
            lines=[(1, 20, 20)],
        )
        _insert_order(
            connection,
            order_id=403,
            customer_id=4,
            order_date="2026-08-01",
            lines=[(1, 0, 0)],
        )

        _insert_order(
            connection,
            order_id=601,
            customer_id=6,
            order_date="2026-08-14",
            lines=[(1, 9999, 9999)],
        )
        for order_id, status in ((701, "dead"), (702, "voided"), (703, "test")):
            _insert_order(
                connection,
                order_id=order_id,
                customer_id=7,
                order_date="2026-08-14",
                status=status,
                lines=[(1, 9999, 9999)],
            )
        connection.commit()
    return database


def _run(database: Path, *, include_amounts: bool = True) -> dict:
    return audit.run_audit(
        database=database,
        as_of=AS_OF,
        expected_revision="mm21v8x9z10",
        expected_sha256=_sha256(database),
        include_amounts=include_amounts,
        amount_authorized=include_amounts,
        iterations=3,
        anonymization_key=ANON_KEY,
        source_label="anonymous-test-fixture",
    )


def _customer_by_token(report: dict, customer_id: int) -> dict:
    token = audit._anonymize_customer(customer_id, ANON_KEY)
    return next(row for row in report["customers"] if row["customer"] == token)


def test_master_order_frequency_deduplicates_lines_and_same_day_frequency(
    anonymous_database: Path,
) -> None:
    report = _run(anonymous_database)
    customer = _customer_by_token(report, 1)

    assert customer["valid_master_order_count_all"] == 4
    assert customer["valid_master_orders_365"] == 4
    assert customer["valid_order_days_90"] == 2
    assert customer["recency_days"] == 0
    assert customer["complete_frozen_amount_365"] == 125
    assert customer["amount_completeness_rate_365"] == 1
    assert customer["is_new_customer"] is False
    assert customer["is_dormant_over_365"] is False


def test_invalid_draft_void_cancelled_and_test_orders_do_not_count(
    anonymous_database: Path,
) -> None:
    report = _run(anonymous_database)
    invalid_only = _customer_by_token(report, 7)

    assert invalid_only["valid_master_order_count_all"] == 0
    assert invalid_only["valid_master_orders_365"] == 0
    assert invalid_only["recency_days"] is None
    assert invalid_only["is_new_customer"] is False
    assert invalid_only["is_dormant_over_365"] is True


def test_missing_price_is_null_not_zero_and_amount_permission_redacts(
    anonymous_database: Path,
) -> None:
    report = _run(anonymous_database)
    incomplete = _customer_by_token(report, 4)
    assert incomplete["amount_complete_orders_365"] == 2
    assert incomplete["amount_incomplete_orders_365"] == 1
    assert incomplete["amount_completeness_rate_365"] == pytest.approx(2 / 3, abs=1e-6)
    assert incomplete["known_frozen_amount_365"] == 30
    assert incomplete["complete_frozen_amount_365"] is None

    redacted = _run(anonymous_database, include_amounts=False)
    redacted_incomplete = _customer_by_token(redacted, 4)
    assert redacted_incomplete["known_frozen_amount_365"] is None
    assert redacted_incomplete["complete_frozen_amount_365"] is None
    assert redacted["amounts_included"] is False
    assert redacted["distribution"]["complete_frozen_amount_365"] == {
        "p20": None,
        "p40": None,
        "p50": None,
        "p60": None,
        "p80": None,
        "p90": None,
    }


def test_new_dormant_no_history_and_inactive_customer_population(
    anonymous_database: Path,
) -> None:
    report = _run(anonymous_database)
    new_customer = _customer_by_token(report, 2)
    dormant = _customer_by_token(report, 3)
    no_history = _customer_by_token(report, 5)

    assert new_customer["is_new_customer"] is True
    assert new_customer["is_dormant_over_365"] is False
    assert dormant["recency_days"] == 366
    assert dormant["is_dormant_over_365"] is True
    assert no_history["is_new_customer"] is False
    assert no_history["is_dormant_over_365"] is True
    assert len(report["customers"]) == 6
    inactive_token = audit._anonymize_customer(6, ANON_KEY)
    assert all(row["customer"] != inactive_token for row in report["customers"])


def test_core_rollup_is_one_batch_sql_and_plan_uses_order_item_index(
    anonymous_database: Path,
) -> None:
    report = _run(anonymous_database)

    assert report["performance"]["sql_statements_per_iteration"] == 1
    assert report["performance"]["iterations"] == 3
    assert report["performance"]["response_ms"]["max"] >= 0
    assert any(
        "ix_sales_order_items_order_id" in line for line in report["query_plan"]
    )
    assert "customers.name" not in audit.CORE_AGGREGATE_SQL
    assert "customer_code" not in audit.CORE_AGGREGATE_SQL


def test_readonly_connection_rejects_writes_and_source_hash_is_unchanged(
    anonymous_database: Path,
) -> None:
    before = _sha256(anonymous_database)
    with audit.connect_readonly(anonymous_database) as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("UPDATE customers SET name = '禁止写入' WHERE id = 1")

    report = _run(anonymous_database)
    assert _sha256(anonymous_database) == before
    assert report["source"]["database_sha256_before"] == before
    assert report["source"]["database_sha256_after"] == before
    assert report["source"]["sidecars_unchanged"] is True


def test_revision_and_amount_authorization_fail_closed(
    anonymous_database: Path,
) -> None:
    with pytest.raises(ValueError, match="revision mismatch"):
        audit.run_audit(
            database=anonymous_database,
            as_of=AS_OF,
            expected_revision="wrong-revision",
            expected_sha256=None,
            include_amounts=False,
            amount_authorized=False,
            iterations=1,
            anonymization_key=ANON_KEY,
            source_label="anonymous-test-fixture",
        )
    with pytest.raises(ValueError, match="requires --amount-authorized"):
        audit.run_audit(
            database=anonymous_database,
            as_of=AS_OF,
            expected_revision="mm21v8x9z10",
            expected_sha256=None,
            include_amounts=True,
            amount_authorized=False,
            iterations=1,
            anonymization_key=ANON_KEY,
            source_label="anonymous-test-fixture",
        )


def test_cli_writes_only_anonymous_reports_and_keeps_source_unchanged(
    anonymous_database: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    json_output = tmp_path / "report.json"
    markdown_output = tmp_path / "report.md"
    before = _sha256(anonymous_database)
    monkeypatch.setenv("P1_61A_ANONYMIZATION_KEY", "fixed-test-key")

    exit_code = audit.main(
        [
            "--database",
            str(anonymous_database),
            "--json-output",
            str(json_output),
            "--markdown-output",
            str(markdown_output),
            "--source-label",
            "anonymous-test-fixture",
            "--as-of",
            AS_OF.isoformat(),
            "--expected-sha256",
            before,
            "--iterations",
            "2",
            "--include-amounts",
            "--amount-authorized",
        ]
    )

    assert exit_code == 0
    assert _sha256(anonymous_database) == before
    payload = json.loads(json_output.read_text(encoding="utf-8"))
    combined = json_output.read_text(encoding="utf-8") + markdown_output.read_text(
        encoding="utf-8"
    )
    assert payload["contract_version"] == "P1-61A-v1"
    assert payload["threshold_candidates"]["status"] == (
        "diagnostic_candidates_only_not_approved_thresholds"
    )
    for secret_name in (
        "苏州机密客户甲",
        "昆山机密客户乙",
        "常熟机密客户丙",
        "太仓机密客户丁",
    ):
        assert secret_name not in combined
    assert "热1～热5阈值" in markdown_output.read_text(encoding="utf-8")


def test_metric_contract_keeps_fixed_rules_separate_from_candidate_percentiles(
    anonymous_database: Path,
) -> None:
    report = _run(anonymous_database)
    candidates = report["threshold_candidates"]

    assert candidates["fixed_rules"] == {
        "dormant_when_recency_days_gt": 365,
        "new_customer_when_valid_orders_lt": 3,
        "new_customer_when_history_span_days_lt": 30,
    }
    assert candidates["status"] == "diagnostic_candidates_only_not_approved_thresholds"
    assert "final weights or tiers" in candidates["note"]
    assert report["index_observation"]["recommendation"].startswith("No migration")


def test_cli_refuses_to_overwrite_database_with_report(
    anonymous_database: Path,
    tmp_path: Path,
) -> None:
    before = _sha256(anonymous_database)
    exit_code = audit.main(
        [
            "--database",
            str(anonymous_database),
            "--json-output",
            str(anonymous_database),
            "--markdown-output",
            str(tmp_path / "report.md"),
            "--source-label",
            "anonymous-test-fixture",
            "--as-of",
            AS_OF.isoformat(),
        ]
    )
    assert exit_code == 2
    assert _sha256(anonymous_database) == before
