from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from sqlalchemy.orm import Session


ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE_HTML = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")
TWIN_SOURCE = (
    ROOT / "factory_twin" / "frontend" / "src" / "WarehouseTwinApp.tsx"
).read_text(encoding="utf-8")
TWIN_CSS = (
    ROOT / "factory_twin" / "frontend" / "src" / "warehouseTwin.css"
).read_text(encoding="utf-8")


def test_full_warehouse_search_filters_before_the_result_cap(tmp_path) -> None:
    from app.api.warehouse import _inventory_search_page, _lot_query
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.warehouse_inventory import InventoryLot, WarehouseLocation

    engine = create_sqlite_engine(tmp_path / "p1-71-search.sqlite3")
    Base.metadata.create_all(engine)
    now = datetime(2026, 8, 18, 8, 0, 0)
    with Session(engine) as db:
        location = WarehouseLocation(
            location_code="P1-71-A1",
            location_name="联合审计测试库位",
            warehouse_type="finished",
            warehouse_floor=3,
            area_code="A1",
            placement_status="placed",
        )
        db.add(location)
        db.flush()
        db.bulk_save_objects(
            [
                InventoryLot(
                    lot_number=f"P1-71-NONMATCH-{index:04d}",
                    inventory_type="finished",
                    warehouse_location_id=location.id,
                    quantity_available=1,
                    quantity_reserved=0,
                    quantity_consumed=0,
                    quantity_damaged=0,
                    quantity_scrapped=0,
                    unit="boxes",
                    status="active",
                    source_type="stocktake",
                    stock_date=date(2026, 8, 18),
                    stock_date_accuracy="exact",
                    last_movement_at=now,
                )
                for index in range(2505)
            ]
        )
        db.add(
            InventoryLot(
                lot_number="P1-71-AFTER-CAP-TARGET",
                inventory_type="finished",
                warehouse_location_id=location.id,
                quantity_available=9,
                quantity_reserved=2,
                quantity_consumed=0,
                quantity_damaged=1,
                quantity_scrapped=0,
                unit="boxes",
                status="active",
                source_type="stocktake",
                stock_date=date(2026, 8, 18),
                stock_date_accuracy="exact",
                last_movement_at=now,
            )
        )
        db.commit()

        rows, total = _inventory_search_page(
            db,
            _lot_query().where(InventoryLot.status == "active"),
            keyword="AFTER-CAP-TARGET",
            limit=500,
        )

        assert total == 1
        assert [row.lot_number for row in rows] == ["P1-71-AFTER-CAP-TARGET"]
    engine.dispose()


def test_map_and_ledger_preserve_stable_deep_link_context() -> None:
    assert "const ledgerLinkUrl = (() =>" in TWIN_SOURCE
    assert 'params.set("location_id", String(selectedLocation.location_id))' in TWIN_SOURCE
    assert 'params.set("lot_id", String(focusedSearchItem.lot_id))' in TWIN_SOURCE
    assert 'href={ledgerLinkUrl} target="_top"' in TWIN_SOURCE
    assert "function currentLedgerMapParams()" in WAREHOUSE_HTML
    assert "function syncWarehouseLedgerUrl()" in WAREHOUSE_HTML
    assert "function syncWarehouseTabUrl(tab)" in WAREHOUSE_HTML
    assert "state.tab=tab;syncWarehouseTabUrl(tab)" in WAREHOUSE_HTML
    assert '["movements","insights","molds","printing_plates","stocktake_review","inventory_onboarding"].includes(requestedTab)' in WAREHOUSE_HTML
    assert '$("keywordFilter").value=keyword||lot?.lot_number||""' in WAREHOUSE_HTML
    assert 'window.history.replaceState(window.history.state,""' in WAREHOUSE_HTML


def test_quantity_bucket_semantics_are_visible_in_both_surfaces() -> None:
    for text in ("实物", "可用", "预占", "报损待处置"):
        assert text in TWIN_SOURCE
    for text in ("实物在库", "可用数量", "已预占", "报损待处置", "累计消耗", "累计报废"):
        assert text in WAREHOUSE_HTML
    assert "实物在库 = 可用 + 已预占 + 报损待处置" in WAREHOUSE_HTML


def test_movement_ledger_covers_every_bucket_and_is_paginated() -> None:
    for movement_type in (
        "manual_in",
        "reserve",
        "release_reserve",
        "consume",
        "reverse_consume",
        "damage",
        "scrap",
        "return_in",
        "return_reconsume",
        "location_transfer",
    ):
        assert f'<option value="{movement_type}">' in WAREHOUSE_HTML
    assert "function movementBalanceHtml(row)" in WAREHOUSE_HTML
    assert "before_reserved" in WAREHOUSE_HTML
    assert "before_consumed" in WAREHOUSE_HTML
    assert "before_damaged" in WAREHOUSE_HTML
    assert "before_scrapped" in WAREHOUSE_HTML
    assert 'movementPageSize:50' in WAREHOUSE_HTML
    assert 'id="movementPrevPage"' in WAREHOUSE_HTML
    assert 'id="movementNextPage"' in WAREHOUSE_HTML


def test_mode_switches_do_not_silently_mix_move_and_planning_state() -> None:
    assert "hasUnsubmittedWarehouseMoveState" in TWIN_SOURCE
    assert "尚未提交的移货 / 合并页面草稿" in TWIN_SOURCE
    assert "resetWarehouseMoveWorkbench" in TWIN_SOURCE
    assert 'setWarehouseOperationMessage("")' in TWIN_SOURCE
    assert "先加入移货草稿" in TWIN_SOURCE


def test_detail_dialog_has_one_modal_scroll_and_keyboard_lifecycle() -> None:
    assert "body.asset-detail-open{overflow:hidden}" in WAREHOUSE_HTML
    assert 'role="dialog" aria-modal="true"' in WAREHOUSE_HTML
    assert 'aria-describedby="assetDetailSubtitle" tabindex="-1"' in WAREHOUSE_HTML
    assert "function trapAssetDetailFocus(event)" in WAREHOUSE_HTML
    assert "assetDetailOpener" in WAREHOUSE_HTML
    assert "trapAssetDetailFocus(event)" in WAREHOUSE_HTML


def test_high_frequency_controls_have_labels_targets_and_keyboard_focus() -> None:
    assert "min-height: 44px" in TWIN_CSS
    assert ":focus-visible" in TWIN_CSS
    assert 'class="filter-control" aria-label="库存流水批次号"' in WAREHOUSE_HTML
    assert 'class="filter-control" aria-label="库存流水类型"' in WAREHOUSE_HTML
    assert ".filter-control{min-height:44px}" in WAREHOUSE_HTML
    assert ".btn:focus-visible" in WAREHOUSE_HTML


def test_map_explains_disabled_view_and_empty_inspector_next_step() -> None:
    assert 'aria-describedby={(locationEditMode || mapMode === "move") ? "twin-view-mode-help"' in TWIN_SOURCE
    assert 'id="twin-view-mode-help"' in TWIN_SOURCE
    assert "从查货开始，或直接点地图" in TWIN_SOURCE
    assert "地图只是库存台账的空间投影" in TWIN_SOURCE


def test_critical_1366_metrics_remain_visible_until_the_mobile_breakpoint() -> None:
    assert "@media (max-width: 1180px)" in TWIN_CSS
    assert ".twin-toolbar-summary span:nth-child(2)" in TWIN_CSS
    assert ".twin-toolbar-summary span:nth-child(4)" in TWIN_CSS
    assert "display: inline-flex" in TWIN_CSS


def test_navigation_prioritizes_daily_ledger_work_over_secondary_admin_tools() -> None:
    assert '<details id="warehouseMoreTabs" class="warehouse-more-tabs">' in WAREHOUSE_HTML
    assert "更多仓库功能" in WAREHOUSE_HTML
    assert "带当前条件看地图" in WAREHOUSE_HTML
    assert 'role="tablist" aria-label="仓库地图操作模式"' in TWIN_SOURCE
    assert 'role="tab" aria-selected={mapMode === "lookup"}' in TWIN_SOURCE
