from __future__ import annotations

from datetime import date
from pathlib import Path
import zipfile

from openpyxl import Workbook
from openpyxl.utils.datetime import to_excel

from app.services.historical_purchase_lookup import (
    DEFAULT_SHEET_NAME,
    load_historical_purchase_records,
    search_historical_purchase_database,
    search_historical_purchases,
)


def _write_workbook(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = DEFAULT_SHEET_NAME
    sheet.append(["佳丰", None, "№:", 2025, to_excel(date(2025, 1, 2))])
    sheet.append(["长*宽(单位cm)", "压线", "数量", "材质", "交货日期"])
    old = [None] * 24
    old[0] = "158*54"
    old[1] = "15.7*22.6*15.7"
    old[2] = 450
    old[3] = "K7C7J/AB"
    old[6] = to_excel(date(2025, 12, 20))
    old[12] = "高泰 3D90095 屏蔽袋 46*30.5*22.5"
    sheet.append(old)

    latest = [None] * 24
    latest[0] = "159*55.5"
    latest[1] = "15.8*23.9*15.8"
    latest[2] = 500
    latest[3] = "K9C7J/AB"
    latest[6] = to_excel(date(2026, 7, 8))
    latest[12] = "高泰 3D90095 屏蔽袋 46*30.5*22.5"
    latest[13] = "新版规格"
    sheet.append(latest)

    other = [None] * 24
    other[0] = "214*76.5"
    other[1] = "净"
    other[2] = 100
    other[3] = "A414B/AB"
    other[6] = to_excel(date(2026, 7, 9))
    other[12] = "22000110 天华103A外箱"
    sheet.append(other)
    workbook.save(path)


def test_parser_converts_centimetres_and_crease_to_millimetres(tmp_path: Path) -> None:
    workbook_path = tmp_path / "2025年采购单.xlsx"
    _write_workbook(workbook_path)

    records = load_historical_purchase_records(workbook_path)
    row = next(record for record in records if record.source_row == 4)

    assert row.record_date == date(2026, 7, 8)
    assert row.report_length_mm == 1590
    assert row.report_width_mm == 555
    assert row.crease_type == "压线"
    assert (row.crease_left_mm, row.crease_middle_mm, row.crease_right_mm) == (
        158,
        239,
        158,
    )
    assert row.material_code == "K9C7J/AB"
    assert row.historical_quantity == 500
    net_row = next(record for record in records if record.source_row == 5)
    assert net_row.crease_type == "净料"


def test_parser_repairs_invalid_excel_font_family_without_changing_source(
    tmp_path: Path,
) -> None:
    workbook_path = tmp_path / "2025年采购单.xlsx"
    _write_workbook(workbook_path)
    original = workbook_path.read_bytes()
    with zipfile.ZipFile(workbook_path, "r") as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    styles_name = "xl/styles.xml"
    styles = members[styles_name].decode("utf-8")
    styles = styles.replace('<family val="2"/>', '<family val="15"/>', 1)
    assert '<family val="15"/>' in styles
    members[styles_name] = styles.encode("utf-8")
    with zipfile.ZipFile(workbook_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    malformed = workbook_path.read_bytes()
    assert malformed != original

    records = load_historical_purchase_records(workbook_path)

    assert len(records) == 3
    assert workbook_path.read_bytes() == malformed


def test_search_ranks_latest_exact_product_match_first(tmp_path: Path) -> None:
    workbook_path = tmp_path / "2025年采购单.xlsx"
    _write_workbook(workbook_path)

    result = search_historical_purchases(
        "3D90095",
        workbook_path=workbook_path,
        limit=10,
    )

    assert result["source_sheet"] == DEFAULT_SHEET_NAME
    assert result["indexed_records"] == 3
    assert result["total_matches"] == 2
    assert result["items"][0]["record_date"] == "2026-07-08"
    assert result["items"][0]["report_length_mm"] == 1590
    assert result["items"][0]["source_ref"] == f"{DEFAULT_SHEET_NAME}!4"


def test_code_search_does_not_include_fuzzy_unrelated_codes(tmp_path: Path) -> None:
    workbook_path = tmp_path / "2025年采购单.xlsx"
    _write_workbook(workbook_path)

    result = search_historical_purchases(
        "3D90095",
        workbook_path=workbook_path,
        limit=100,
    )

    assert result["total_matches"] == 2
    assert all("3D90095" in row["search_text"] for row in result["items"])


def test_search_supports_product_name_and_specification_text(tmp_path: Path) -> None:
    workbook_path = tmp_path / "2025年采购单.xlsx"
    _write_workbook(workbook_path)

    by_name = search_historical_purchases("屏蔽袋", workbook_path=workbook_path)
    by_spec = search_historical_purchases(
        "46 30.5 22.5",
        workbook_path=workbook_path,
    )

    assert by_name["items"][0]["product_reference"].startswith("高泰 3D90095")
    assert by_spec["items"][0]["material_code"] == "K9C7J/AB"


def test_search_does_not_return_unrelated_rows(tmp_path: Path) -> None:
    workbook_path = tmp_path / "2025年采购单.xlsx"
    _write_workbook(workbook_path)

    result = search_historical_purchases(
        "完全不存在的款号",
        workbook_path=workbook_path,
    )

    assert result["items"] == []
    assert result["total_matches"] == 0


def test_imported_history_is_searchable_without_the_source_workbook(tmp_path: Path) -> None:
    from sqlalchemy.orm import Session

    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.historical_purchase import HistoricalPurchaseEntry

    engine = create_sqlite_engine(tmp_path / "history-search.sqlite3")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all(
            [
                HistoricalPurchaseEntry(
                    source_workbook="2025年采购单.xlsx",
                    source_sheet=DEFAULT_SHEET_NAME,
                    source_row=3,
                    source_file_sha256="a" * 64,
                    source_fingerprint="b" * 64,
                    supplier_name="佳丰",
                    record_date=date(2025, 12, 20),
                    product_reference="高泰 3D90095 屏蔽袋",
                    search_text="高泰 3D90095 屏蔽袋 46*30.5*22.5",
                    normalized_search_text="高泰3D90095屏蔽袋46305225K9C7JAB",
                    material_code="K9C7J/AB",
                    historical_quantity=500,
                    report_length_mm=1590,
                    report_width_mm=555,
                    crease_text="15.8*23.9*15.8",
                    crease_type="压线",
                    crease_left_mm=158,
                    crease_middle_mm=239,
                    crease_right_mm=158,
                )
            ]
        )
        db.commit()
        result = search_historical_purchase_database(db, "3D 90095")

    assert result["indexed_records"] == 1
    assert result["total_matches"] == 1
    assert result["items"][0]["report_width_mm"] == 555
    assert result["items"][0]["source_ref"] == f"{DEFAULT_SHEET_NAME}!3"
    engine.dispose()


def test_database_search_groups_repeated_purchase_specs_but_keeps_material_variants(
    tmp_path: Path,
) -> None:
    from sqlalchemy.orm import Session

    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.historical_purchase import HistoricalPurchaseEntry

    engine = create_sqlite_engine(tmp_path / "history-groups.sqlite3")
    Base.metadata.create_all(engine)
    common = {
        "source_workbook": "2025年采购单.xlsx",
        "source_sheet": DEFAULT_SHEET_NAME,
        "source_file_sha256": "a" * 64,
        "product_reference": "21302053美国衬板26*45",
        "search_text": "21302053美国衬板26*45",
        "normalized_search_text": "21302053美国衬板2645",
        "report_length_mm": 1120,
        "report_width_mm": 635,
        "crease_text": "净",
        "crease_type": "净料",
    }
    with Session(engine) as db:
        db.add_all(
            [
                HistoricalPurchaseEntry(
                    **common,
                    source_row=10,
                    source_fingerprint="b" * 64,
                    supplier_name="佳丰",
                    record_date=date(2026, 6, 24),
                    material_code="B4C/B",
                    historical_quantity=270,
                ),
                HistoricalPurchaseEntry(
                    **common,
                    source_row=9,
                    source_fingerprint="c" * 64,
                    supplier_name="嘉林亿",
                    record_date=date(2026, 5, 20),
                    material_code="B4C/B",
                    historical_quantity=180,
                ),
                HistoricalPurchaseEntry(
                    **common,
                    source_row=8,
                    source_fingerprint="d" * 64,
                    supplier_name="嘉林亿",
                    record_date=date(2026, 4, 18),
                    material_code="K4C/B",
                    historical_quantity=100,
                ),
            ]
        )
        db.commit()
        result = search_historical_purchase_database(db, "21302053")

    assert result["source_record_matches"] == 3
    assert result["total_matches"] == 2
    assert result["items"][0]["history_count"] == 2
    assert result["items"][0]["historical_quantity"] == 270
    assert {item["material_code"] for item in result["items"]} == {
        "B4C/B",
        "K4C/B",
    }
    engine.dispose()


def test_history_import_is_idempotent_and_preserves_all_source_rows(tmp_path: Path) -> None:
    from sqlalchemy.orm import Session

    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.customer import Customer
    from app.models.historical_purchase import HistoricalPurchaseEntry
    from app.models.product import Product
    from scripts.import_historical_purchase_entries import (
        import_historical_purchase_entries,
    )

    workbook_path = tmp_path / "2025年采购单.xlsx"
    _write_workbook(workbook_path)
    database_path = tmp_path / "history-import.sqlite3"
    engine = create_sqlite_engine(database_path)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        customer = Customer(
            customer_number=9902,
            customer_code="HIST-C",
            name="历史采购测试客户",
            payment_term_days=30,
            credit_limit=0,
        )
        db.add(customer)
        db.flush()
        db.add(
            Product(
                customer_id=customer.id,
                product_code="3D90095",
                customer_material_code="3D90095",
                product_name="屏蔽袋",
                box_category="normal",
            )
        )
        db.commit()
    engine.dispose()

    dry_run = import_historical_purchase_entries(
        workbook_path, database_path, commit=False
    )
    assert dry_run.parsed_rows == 3
    assert dry_run.inserted_rows == 3
    assert dry_run.committed is False

    first = import_historical_purchase_entries(
        workbook_path, database_path, commit=True
    )
    second = import_historical_purchase_entries(
        workbook_path, database_path, commit=True
    )
    assert first.inserted_rows == 3
    assert second.inserted_rows == 0
    assert second.unchanged_rows == 3

    engine = create_sqlite_engine(database_path)
    with Session(engine) as db:
        rows = list(db.query(HistoricalPurchaseEntry).all())
    assert len(rows) == 3
    assert sum(row.product_id is not None for row in rows) == 2
    engine.dispose()
