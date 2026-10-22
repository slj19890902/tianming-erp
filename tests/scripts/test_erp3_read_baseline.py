"""The benchmark's database boundary must fail closed independently of ORM."""
import importlib.util
from pathlib import Path
import sqlite3

import pytest


MODULE = Path(__file__).resolve().parents[2] / "scripts/benchmarks/erp3_read_baseline.py"
spec = importlib.util.spec_from_file_location("erp3_read_baseline", MODULE)
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)


@pytest.fixture
def sample(tmp_path):
    path = tmp_path / "owned.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript("CREATE TABLE facts(id INTEGER PRIMARY KEY, amount INTEGER); INSERT INTO facts VALUES(1, 9);")
    return path


@pytest.mark.parametrize("statement", [
    "INSERT INTO facts VALUES(2, 20)", "UPDATE facts SET amount=1", "DELETE FROM facts",
    "CREATE TABLE unexpected(id)", "DROP TABLE facts", "ALTER TABLE facts ADD COLUMN extra",
    "PRAGMA query_only=OFF", "PRAGMA journal_mode=DELETE", "PRAGMA writable_schema=ON",
    "ATTACH DATABASE ':memory:' AS other", "SELECT load_extension('not-a-library')",
])
def test_write_attempt_rejected_and_file_unchanged(sample, statement):
    before = baseline.file_sha256(sample)
    with baseline.ReadOnlySQLiteGuard(sample) as guard:
        conn = sqlite3.connect(sample)
        assert conn.execute("SELECT amount FROM facts").fetchone() == (9,)
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(statement)
        assert guard.denied > 0
        assert conn.total_changes == 0
    assert baseline.file_sha256(sample) == before


def test_connections_cannot_escape_and_patch_is_restored(sample, tmp_path):
    original = sqlite3.connect
    with baseline.ReadOnlySQLiteGuard(sample):
        for target in (":memory:", tmp_path / "other.sqlite3", "file::memory:?cache=shared"):
            with pytest.raises(sqlite3.DatabaseError):
                sqlite3.dbapi2.connect(target)
        conn = sqlite3.connect(sample.as_uri() + "?mode=rw", uri=True)
        assert conn.execute("PRAGMA query_only").fetchone() == (1,)
        conn.execute("BEGIN")
        conn.execute("SELECT * FROM facts").fetchall()
        conn.rollback()
    assert sqlite3.connect is original


def test_summary_does_not_expose_business_payload():
    summary = baseline.payload_summary({"items": [{"customer_name": "private", "price": 12}], "total": 1})
    assert summary["counts"] == {"items_count": 1, "total": 1}
    assert "private" not in str(summary)
    assert len(summary["sha256"]) == 64
