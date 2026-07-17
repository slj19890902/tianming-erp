from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


WORKTREE = Path(__file__).resolve().parents[1]
SCRIPT = WORKTREE / "scripts" / "audit" / "mold_location_pilot.py"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _create_database(path: Path) -> dict[str, int]:
    connection = sqlite3.connect(path)
    try:
        connection.executescript("""
            CREATE TABLE mold_tools (id INTEGER PRIMARY KEY, mold_code TEXT NOT NULL,
                mold_name TEXT NOT NULL, rack_location TEXT, is_active INTEGER NOT NULL);
            CREATE TABLE warehouse_locations (id INTEGER PRIMARY KEY, location_code TEXT NOT NULL,
                warehouse_floor INTEGER NOT NULL);
            INSERT INTO warehouse_locations VALUES (1, 'F3-FG-A01', 3);
        """)
        connection.executemany("INSERT INTO mold_tools VALUES (?, ?, ?, ?, ?)", [
            (1, "M-FLAT", "平放", "3F-M-R02-L2-D03-P08", 1),
            (2, "M-VERTICAL", "重型竖放", "3F-M-R01-L1-V-P12", 1),
            (3, "M-BAD", "非法编码", "3F-M-R02-L4-D03-P08", 1),
            (4, "M-OLD", "旧位置", "二楼模具架 B-12", 1),
            (5, "M-DUP", "重复位置甲", "3F-M-R03-L1-D01-P01", 1),
            (6, "M-DUP", "重复位置乙", "3f-m-r03-l1-d01-p01", 1),
            (7, "M-INACTIVE", "停用", "3F-M-R04-L1-V-P01", 0),
            (8, "M-MISSING", "空位置", "  ", 1),
        ])
        connection.commit()
        return {"mold_tools": connection.execute("SELECT COUNT(*) FROM mold_tools").fetchone()[0],
                "warehouse_locations": connection.execute("SELECT COUNT(*) FROM warehouse_locations").fetchone()[0]}
    finally:
        connection.close()


def _run(database: Path, json_output: Path, csv_output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), "--database", str(database),
                           "--json", str(json_output), "--csv", str(csv_output)],
                          cwd=WORKTREE, check=True, text=True, capture_output=True)


def test_dry_run_is_read_only_stable_and_classifies_location_differences(tmp_path: Path) -> None:
    database = tmp_path / "isolated_n022_pilot.sqlite3"
    expected_counts = _create_database(database)
    before_hash, before_mtime = _digest(database), database.stat().st_mtime_ns
    first = _run(database, tmp_path / "first.json", tmp_path / "first.csv")
    after_first_hash, after_first_mtime = _digest(database), database.stat().st_mtime_ns
    second = _run(database, tmp_path / "second.json", tmp_path / "second.csv")
    after_second_hash, after_second_mtime = _digest(database), database.stat().st_mtime_ns
    assert before_hash == after_first_hash == after_second_hash
    assert before_mtime == after_first_mtime == after_second_mtime
    connection = sqlite3.connect(database)
    try:
        assert {"mold_tools": connection.execute("SELECT COUNT(*) FROM mold_tools").fetchone()[0],
                "warehouse_locations": connection.execute("SELECT COUNT(*) FROM warehouse_locations").fetchone()[0]} == expected_counts
        assert connection.execute("SELECT location_code FROM warehouse_locations").fetchone()[0] == "F3-FG-A01"
    finally:
        connection.close()
    first_json = (tmp_path / "first.json").read_text(encoding="utf-8")
    assert first_json == (tmp_path / "second.json").read_text(encoding="utf-8")
    assert (tmp_path / "first.csv").read_text(encoding="utf-8") == (tmp_path / "second.csv").read_text(encoding="utf-8")
    assert first.stdout == second.stdout
    report = json.loads(first_json)
    records = {row["mold_code"]: row for row in report["records"]}
    assert records["M-FLAT"]["location_kind"] == "flat"
    assert records["M-VERTICAL"]["location_kind"] == "vertical"
    assert records["M-BAD"]["location_kind"] == "invalid_3f_m"
    assert records["M-OLD"]["location_kind"] == "legacy_free_text"
    assert records["M-MISSING"]["location_kind"] == "missing"
    issues = {(row["issue"], row["mold_id"]) for row in report["issues"]}
    for issue in [("duplicate_location_occupancy", 5), ("duplicate_location_occupancy", 6),
                  ("duplicate_mold_code", 5), ("duplicate_mold_code", 6), ("inactive_mold", 7),
                  ("invalid_3f_m_code", 3), ("legacy_free_text", 4), ("missing_location", 8)]:
        assert issue in issues
    assert "warehouse_locations" not in SCRIPT.read_text(encoding="utf-8")


def test_requires_explicit_regular_database_path_and_has_no_apply_mode(tmp_path: Path) -> None:
    missing = subprocess.run([sys.executable, str(SCRIPT)], cwd=WORKTREE, text=True, capture_output=True)
    assert missing.returncode == 2
    assert "--database" in missing.stderr
    database = tmp_path / "isolated.sqlite3"
    _create_database(database)
    apply = subprocess.run([sys.executable, str(SCRIPT), "--database", str(database), "--apply"],
                           cwd=WORKTREE, text=True, capture_output=True)
    assert apply.returncode == 2
    assert "unrecognized arguments: --apply" in apply.stderr
    refused_live_output = subprocess.run(
        [sys.executable, str(SCRIPT), "--database", str(database), "--json", str(tmp_path / "live" / "report.json")],
        cwd=WORKTREE, text=True, capture_output=True,
    )
    assert refused_live_output.returncode == 2
    assert "formal/live database paths are refused" in refused_live_output.stderr
    linked = tmp_path / "linked.sqlite3"
    try:
        os.symlink(database, linked)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    refused = subprocess.run([sys.executable, str(SCRIPT), "--database", str(linked)],
                             cwd=WORKTREE, text=True, capture_output=True)
    assert refused.returncode == 2
    assert "symbolic-link database paths are refused" in refused.stderr
