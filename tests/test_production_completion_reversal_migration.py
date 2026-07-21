from pathlib import Path
import sqlite3

import pytest
from alembic import command
from alembic.config import Config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REVISION = "cf62v8x9z51"
PREVIOUS_REVISION = "ce61v8x9z50"


def _config(monkeypatch: pytest.MonkeyPatch, database_path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database_path.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "production-reversal-migration-test")
    return Config(str(PROJECT_ROOT / "alembic.ini"))


def test_reversed_production_audit_is_immutable_and_blocks_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database_path = tmp_path / "production-reversal.sqlite3"
    config = _config(monkeypatch, database_path)
    command.upgrade(config, REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO production_completions (
                batch_id, task_id, order_item_id, expected_version, quantity,
                initial_disposition, status, completed_at
            ) VALUES (1, 1, 1, 1, 1, 'direct', 'posted', CURRENT_TIMESTAMP)
            """
        )
        connection.execute(
            """
            UPDATE production_completions
            SET status='reversed', reversal_reason='测试撤销', reversed_at=CURRENT_TIMESTAMP
            WHERE id=1
            """
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE production_completions SET reversal_reason='篡改' WHERE id=1"
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            connection.execute("DELETE FROM production_completions WHERE id=1")
        connection.rollback()

    with pytest.raises(RuntimeError, match="禁止破坏性降级"):
        command.downgrade(config, PREVIOUS_REVISION)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute(
            "SELECT status, reversal_reason FROM production_completions WHERE id=1"
        ).fetchone() == ("reversed", "测试撤销")
