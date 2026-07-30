from __future__ import annotations

import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "cy81v8x9z70"
TARGET_REVISION = "cz82v8x9z71"
TABLE = "operation_logs"

NEW_COLUMNS = {
    "event_category",
    "result",
    "source",
    "module_code",
    "action_code",
    "actor_user_id_snapshot",
    "operator_name_snapshot",
    "object_ref",
    "customer_id_snapshot",
    "customer_name_snapshot",
    "request_id",
    "batch_id",
    "schema_version",
}
NEW_INDEXES = {
    "ix_operation_logs_category_created_id",
    "ix_operation_logs_operator_created_id",
    "ix_operation_logs_module_action_created_id",
    "ix_operation_logs_object_ref_created_id",
    "ix_operation_logs_customer_created_id",
    "ix_operation_logs_result_source_created_id",
    "ix_operation_logs_request_id",
    "ix_operation_logs_batch_id",
    "ix_operation_logs_schema_version",
}
IMMUTABLE_TRIGGERS = {
    "trg_operation_logs_immutable_update",
    "trg_operation_logs_immutable_delete",
}


def _run_alembic(
    database_path: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["ERP_DATABASE_PATH"] = str(database_path)
    environment["ERP_BACKUP_DIR"] = str(database_path.parent / "backups")
    environment["ERP_SECRET_KEY"] = "q1-02-audit-migration-test"
    environment["ERP_ENVIRONMENT"] = "test"
    environment["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _must_run(database_path: Path, *arguments: str) -> None:
    result = _run_alembic(database_path, *arguments)
    assert result.returncode == 0, result.stdout + result.stderr


def _state(database_path: Path) -> dict[str, object]:
    with sqlite3.connect(database_path) as connection:
        return {
            "revision": connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0],
            "integrity": connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0],
            "foreign_key_errors": connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall(),
            "columns": {
                str(row[1])
                for row in connection.execute(
                    f'PRAGMA table_info("{TABLE}")'
                )
            },
            "indexes": {
                str(row[1])
                for row in connection.execute(
                    f'PRAGMA index_list("{TABLE}")'
                )
            },
            "triggers": {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT name
                      FROM sqlite_master
                     WHERE type = 'trigger' AND tbl_name = ?
                    """,
                    (TABLE,),
                )
            },
            "rows": connection.execute(
                f"""
                SELECT id, user_id, action, resource, description,
                       event_category, result, source, schema_version
                  FROM {TABLE}
                 ORDER BY id
                """
            ).fetchall()
            if NEW_COLUMNS
            <= {
                str(row[1])
                for row in connection.execute(
                    f'PRAGMA table_info("{TABLE}")'
                )
            }
            else connection.execute(
                f"""
                SELECT id, user_id, action, resource, description
                  FROM {TABLE}
                 ORDER BY id
                """
            ).fetchall(),
        }


def _insert_legacy_user_and_log(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            INSERT INTO users (
                id, username, password_hash, role, real_name,
                is_active, must_change_password, customer_access_mode,
                ui_mode, auth_version
            ) VALUES (
                980001, 'q1_02_legacy_operator', 'test-only', 'admin',
                '历史操作员', 1, 0, 'all', 'standard', 1
            )
            """
        )
        connection.execute(
            """
            INSERT INTO operation_logs (
                id, user_id, action, resource, description
            ) VALUES (
                980001, 980001, 'LEGACY_TEST', 'Order', '历史日志'
            )
            """
        )
        connection.commit()


def test_migration_roundtrip_preserves_legacy_history_and_append_only_guards(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "q1-02-audit-roundtrip.sqlite3"
    _must_run(database_path, "upgrade", PREVIOUS_REVISION)
    _insert_legacy_user_and_log(database_path)

    _must_run(database_path, "upgrade", TARGET_REVISION)
    upgraded = _state(database_path)
    assert upgraded["revision"] == TARGET_REVISION
    assert upgraded["integrity"] == "ok"
    assert upgraded["foreign_key_errors"] == []
    assert NEW_COLUMNS <= upgraded["columns"]
    assert NEW_INDEXES <= upgraded["indexes"]
    assert IMMUTABLE_TRIGGERS <= upgraded["triggers"]
    assert upgraded["rows"] == [
        (
            980001,
            980001,
            "LEGACY_TEST",
            "Order",
            "历史日志",
            None,
            "legacy",
            "legacy",
            0,
        )
    ]

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            connection.execute(
                """
                UPDATE operation_logs
                   SET description = '禁止修改'
                 WHERE id = 980001
                """
            )
        except sqlite3.IntegrityError as error:
            assert "append-only" in str(error)
        else:
            raise AssertionError("operation log UPDATE unexpectedly succeeded")
        connection.rollback()

        try:
            connection.execute(
                "DELETE FROM operation_logs WHERE id = 980001"
            )
        except sqlite3.IntegrityError as error:
            assert "append-only" in str(error)
        else:
            raise AssertionError("operation log DELETE unexpectedly succeeded")
        connection.rollback()

        # Deleting an operator may only null the live FK.  Every snapshot and
        # other log field remains immutable and the history row survives.
        connection.execute("DELETE FROM users WHERE id = 980001")
        connection.commit()
        assert connection.execute(
            """
            SELECT user_id, action, resource, description
              FROM operation_logs
             WHERE id = 980001
            """
        ).fetchone() == (None, "LEGACY_TEST", "Order", "历史日志")

    _must_run(database_path, "downgrade", PREVIOUS_REVISION)
    downgraded = _state(database_path)
    assert downgraded["revision"] == PREVIOUS_REVISION
    assert downgraded["integrity"] == "ok"
    assert downgraded["foreign_key_errors"] == []
    assert NEW_COLUMNS.isdisjoint(downgraded["columns"])
    assert NEW_INDEXES.isdisjoint(downgraded["indexes"])
    assert IMMUTABLE_TRIGGERS.isdisjoint(downgraded["triggers"])
    assert downgraded["rows"] == [
        (980001, None, "LEGACY_TEST", "Order", "历史日志")
    ]

    _must_run(database_path, "upgrade", TARGET_REVISION)
    upgraded_again = _state(database_path)
    assert upgraded_again["revision"] == TARGET_REVISION
    assert upgraded_again["integrity"] == "ok"
    assert upgraded_again["foreign_key_errors"] == []
    assert NEW_COLUMNS <= upgraded_again["columns"]
    assert NEW_INDEXES <= upgraded_again["indexes"]
    assert IMMUTABLE_TRIGGERS <= upgraded_again["triggers"]
    assert upgraded_again["rows"][0][5:] == (
        None,
        "legacy",
        "legacy",
        0,
    )


def test_downgrade_with_structured_fact_fails_before_any_ddl(
    tmp_path: Path,
) -> None:
    source = tmp_path / "q1-02-audit-clean.sqlite3"
    database_path = tmp_path / "q1-02-audit-fail-closed.sqlite3"
    _must_run(source, "upgrade", TARGET_REVISION)
    shutil.copy2(source, database_path)

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO operation_logs (
                action, resource, description,
                event_category, result, source,
                module_code, action_code, request_id, schema_version
            ) VALUES (
                'CREATE', 'Order', '结构化审计事实',
                'business', 'success', 'web',
                'orders', 'CREATE', 'q1-02-request', 1
            )
            """
        )
        connection.commit()
    before = _state(database_path)

    blocked = _run_alembic(
        database_path,
        "downgrade",
        PREVIOUS_REVISION,
    )
    assert blocked.returncode != 0
    assert "禁止破坏性降级" in (blocked.stdout + blocked.stderr)

    after = _state(database_path)
    assert after == before
    assert after["revision"] == TARGET_REVISION
    assert after["integrity"] == "ok"
    assert after["foreign_key_errors"] == []
    assert NEW_COLUMNS <= after["columns"]
    assert NEW_INDEXES <= after["indexes"]
    assert IMMUTABLE_TRIGGERS <= after["triggers"]


def test_migration_is_linear_nullable_and_downgrade_guard_is_pre_ddl() -> None:
    migration = (
        PROJECT_ROOT
        / "alembic/versions/cz82v8x9z71_structured_operation_audit.py"
    ).read_text(encoding="utf-8")
    assert (
        'down_revision: Union[str, Sequence[str], None] = "cy81v8x9z70"'
        in migration
    )
    assert "schema_version = 1" in migration
    assert "server_default" not in migration.split("NEW_COLUMNS", 1)[1].split(
        "INDEXES",
        1,
    )[0]
    downgrade_body = migration.split("def downgrade() -> None:", 1)[1]
    assert downgrade_body.index("structured_fact_count") < downgrade_body.index(
        "_drop_immutability_guards()"
    )
    assert downgrade_body.index("structured_fact_count") < downgrade_body.index(
        "op.drop_index"
    )
