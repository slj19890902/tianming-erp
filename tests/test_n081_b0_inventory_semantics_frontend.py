from pathlib import Path


WAREHOUSE_HTML = (
    Path(__file__).resolve().parents[1] / "static" / "warehouse.html"
).read_text(encoding="utf-8")


def test_warehouse_page_renders_unknown_age_without_fake_day_count() -> None:
    assert 'row.age_days==null?"日期不明"' in WAREHOUSE_HTML
    assert 'row.age_days==null?"入库日期不明"' in WAREHOUSE_HTML
    assert 'row.stock_date_accuracy==="estimated"?`约 ${row.age_days}天`' in WAREHOUSE_HTML
    assert '<option value="unknown">日期不明</option>' in WAREHOUSE_HTML


def test_finished_lot_editor_explains_technical_date_and_confirmation() -> None:
    assert "入库日期 / 技术日期" in WAREHOUSE_HTML
    assert "当前值仅为技术日期，不代表真实入库日" in WAREHOUSE_HTML
    assert "确认当前日期为真实入库日期" in WAREHOUSE_HTML
    assert "日期已主动修改；保存后将确认为精确入库日期" in WAREHOUSE_HTML
    assert "confirm_stock_date_exact:confirmStockDateExact" in WAREHOUSE_HTML


def test_interactive_manual_in_declares_exact_date_source() -> None:
    assert WAREHOUSE_HTML.count('stock_date_accuracy:"exact"') >= 2
    assert 'stock_date_original_text:$("fgDate").value' in WAREHOUSE_HTML
    assert 'stock_date_original_text:$("siDate").value' in WAREHOUSE_HTML
