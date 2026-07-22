from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts.admin.derive_historical_material_normalizations import build_decisions, decide


def test_decide_only_derives_format_safe_values() -> None:
    assert decide(" bc14c - ab楞 ")[:2] == ("BC14C/AB", "format_safe")
    assert decide("K618A/B楞")[1] == "pending_invalid_layer_flute"
    assert decide("K618A/AB/BE楞")[1] == "pending_ambiguous"
    assert decide("K618A")[1] == "pending_missing_flute"


def test_build_decisions_preserves_raw_archive(tmp_path: Path) -> None:
    database = tmp_path / "history.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE historical_requisition_maps (
                id INTEGER PRIMARY KEY, material_code TEXT NOT NULL,
                source_workbook TEXT NOT NULL, source_sheet TEXT NOT NULL,
                source_row INTEGER NOT NULL
            )"""
        )
        connection.execute(
            "INSERT INTO historical_requisition_maps VALUES (1, 'BC14C-AB楞', 'a.xlsx', '历史', 8)"
        )
    with sqlite3.connect(database) as connection:
        decisions = build_decisions(connection)
        raw = connection.execute("SELECT material_code FROM historical_requisition_maps WHERE id=1").fetchone()[0]
    assert raw == "BC14C-AB楞"
    assert decisions[0].derived_material_code == "BC14C/AB"
    assert decisions[0].decision_status == "format_safe"
