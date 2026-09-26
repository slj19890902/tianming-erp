from pathlib import Path
import sqlite3

from scripts.diagnostics.preview_box_style_mapping import build_preview


def test_preview_is_aggregate_read_only_and_separates_uncertain_names(tmp_path: Path) -> None:
    database = tmp_path / "products.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            box_style TEXT,
            is_active INTEGER NOT NULL,
            deleted_at TEXT,
            purged_at TEXT,
            length_mm INTEGER,
            width_mm INTEGER,
            height_mm INTEGER,
            splice_mode TEXT,
            pieces_per_box INTEGER,
            mold_tool_id INTEGER,
            die_cut_path TEXT,
            is_composite INTEGER NOT NULL DEFAULT 0,
            is_virtual_composite_parent INTEGER NOT NULL DEFAULT 0,
            production_process TEXT,
            unit TEXT,
            supply_mode TEXT
        );
        INSERT INTO products VALUES
            (1, 'WC 五层钉箱', 1, NULL, NULL, 400, 300, 200, 'single', 1, NULL, NULL, 0, 0, '打钉', '只', 'corrugated_production'),
            (2, 'WC 五层钉箱', 1, NULL, NULL, 500, 400, 300, 'double', 2, NULL, NULL, 0, 0, '双拼打钉', '只', 'corrugated_production'),
            (3, 'NH 天华内盒1', 1, NULL, NULL, 300, 200, 100, 'single', 1, NULL, NULL, 0, 0, NULL, '只', 'corrugated_production'),
            (4, 'ZHJ 纸护角', 1, NULL, NULL, 300, 30, NULL, NULL, NULL, NULL, NULL, 0, 0, NULL, '根', 'corrugated_production'),
            (5, 'BOM组合', 1, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1, 1, NULL, '套', 'corrugated_production'),
            (6, 'A1', 0, NULL, NULL, 1, 1, 1, NULL, NULL, NULL, NULL, 0, 0, NULL, '只', 'corrugated_production');
        """
    )
    connection.commit()
    connection.close()

    payload = build_preview(database)
    by_name = {item["legacy_name"]: item for item in payload["items"]}

    assert payload["summary"]["active_products"] == 5
    assert by_name["WC 五层钉箱"]["status"] == "confirmed_legacy"
    assert by_name["WC 五层钉箱"]["target_code"] == "a1_0201"
    assert by_name["WC 五层钉箱"]["double_splice_count"] == 1
    assert by_name["NH 天华内盒1"]["status"] == "pending_confirmation"
    assert by_name["ZHJ 纸护角"]["status"] == "not_box"
    assert by_name["BOM组合"]["status"] == "system_special"

    verify = sqlite3.connect(database)
    assert verify.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 6
    assert verify.execute("SELECT box_style FROM products WHERE id = 1").fetchone()[0] == "WC 五层钉箱"
    verify.close()
