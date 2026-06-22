from __future__ import annotations

import zipfile
import json
from datetime import date
from pathlib import Path
import re

from openpyxl import Workbook


def test_windows_text_reports_are_written_with_utf8_bom(tmp_path: Path) -> None:
    from scripts.import_historical_requisitions import write_windows_text

    output = tmp_path / "preview.txt"
    write_windows_text(output, "综合标识：洛普格")

    assert output.read_bytes().startswith(b"\xef\xbb\xbf")
    assert output.read_text(encoding="utf-8-sig") == "综合标识：洛普格"


def test_json_report_is_written_with_utf8_bom(tmp_path: Path) -> None:
    from scripts.import_historical_requisitions import write_json_report

    output = tmp_path / "report.log"
    write_json_report(output, {"状态": "未写入"})

    assert output.read_bytes().startswith(b"\xef\xbb\xbf")
    assert json.loads(output.read_text(encoding="utf-8-sig")) == {
        "状态": "未写入"
    }


def test_product_and_fallback_model_expose_historical_requisition_fields() -> None:
    from app.models.historical_requisition import HistoricalRequisitionMap
    from app.models.product import Product

    product_columns = Product.__table__.columns
    assert "default_cardboard_length" in product_columns
    assert "default_cardboard_width" in product_columns
    assert "default_score_lines" in product_columns
    assert "default_material_code" in product_columns
    assert HistoricalRequisitionMap.__tablename__ == "historical_requisition_maps"
    assert HistoricalRequisitionMap.__table__.columns.search_key.index is True


def test_phase13_migration_is_additive_only() -> None:
    path = Path(
        "alembic/versions/"
        "e13a6c4d2f40_phase13_historical_requisition_map.py"
    )
    content = path.read_text(encoding="utf-8").lower()

    assert "drop_table" not in content
    assert "drop_column" not in content
    assert "sales_order_items" not in content
    assert "create_table" in content
    assert "add_column" in content


def test_parse_row_extracts_combined_size_score_material_and_search_key() -> None:
    from scripts.import_historical_requisitions import parse_row

    record = parse_row(
        [
            "159*78.5",
            None,
            "33.8*10.9*33.8",
            200,
            "D212D",
            None,
            None,
            None,
            None,
            None,
            "23201028 88*67*11",
        ],
        sheet_name="2020.1-2025",
        row_number=18,
        inherited_date=date(2025, 3, 8),
    )

    assert record is not None
    assert record.cardboard_length == 159
    assert record.cardboard_width == 78.5
    assert record.score_lines == "33.8*10.9*33.8"
    assert record.quantity == 200
    assert record.material_code == "D212D"
    assert record.search_key == "23201028 88*67*11"


def test_parse_row_extracts_split_dimensions_and_chinese_search_key() -> None:
    from scripts.import_historical_requisitions import parse_row

    record = parse_row(
        [
            159,
            78.5,
            "压线",
            "33.8*10.9*33.8",
            120,
            "W7B/E",
            None,
            None,
            None,
            None,
            "洛普格 38*26*20",
        ],
        sheet_name="1-3月份",
        row_number=9,
        inherited_date=date(2025, 1, 10),
    )

    assert record is not None
    assert (record.cardboard_length, record.cardboard_width) == (159, 78.5)
    assert record.material_code == "W7B/E"
    assert record.search_key == "洛普格 38*26*20"


def test_latest_record_wins_for_same_normalized_search_key() -> None:
    from scripts.import_historical_requisitions import (
        HistoricalRecord,
        deduplicate_latest,
    )

    older = HistoricalRecord(
        search_key="23201028 88*67*11",
        cardboard_length=159,
        cardboard_width=78.5,
        score_lines=None,
        quantity=100,
        material_code="D212D",
        record_date=date(2025, 1, 1),
        sheet_name="A",
        row_number=1,
    )
    newer = HistoricalRecord(
        search_key="23201028  88×67×11",
        cardboard_length=160,
        cardboard_width=79,
        score_lines=None,
        quantity=120,
        material_code="K617A",
        record_date=date(2025, 5, 1),
        sheet_name="B",
        row_number=8,
    )

    result = deduplicate_latest([older, newer])

    assert len(result) == 1
    assert result[0].material_code == "K617A"


def test_product_match_ignores_one_character_generic_codes() -> None:
    from scripts.import_historical_requisitions import (
        HistoricalRecord,
        ProductCandidate,
        _product_match,
    )

    record = HistoricalRecord(
        search_key="XZ-0027 49*38*12",
        cardboard_length=73,
        cardboard_width=62,
        score_lines="12*38*12",
        quantity=100,
        material_code="E8K8E",
        record_date=None,
        sheet_name="历史",
        row_number=1,
    )

    generic_only = _product_match(
        record,
        [ProductCandidate(id=1, product_code="1", product_name="普通衬板")],
    )
    specific = _product_match(
        record,
        [ProductCandidate(id=2, product_code="XZ-0027", product_name="专用箱")],
    )

    assert generic_only is None
    assert specific is not None
    assert specific.id == 2


def test_repair_workbook_copy_clamps_invalid_font_family(tmp_path: Path) -> None:
    from scripts.import_historical_requisitions import repair_workbook_copy

    source = tmp_path / "broken.xlsx"
    workbook = Workbook()
    workbook.active["A1"] = "test"
    workbook.save(source)
    with zipfile.ZipFile(source, "r") as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    styles = members["xl/styles.xml"].decode("utf-8")
    styles = re.sub(
        r'<family val="2"\s*/>',
        '<family val="99" />',
        styles,
        count=1,
    )
    members["xl/styles.xml"] = styles.encode("utf-8")
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)

    repaired = repair_workbook_copy(source, tmp_path)
    with zipfile.ZipFile(repaired, "r") as archive:
        repaired_styles = archive.read("xl/styles.xml").decode("utf-8")

    assert 'family val="99"' not in repaired_styles
    assert 'family val="14"' in repaired_styles
