from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_pending_table_is_two_line_compact_without_horizontal_scroll() -> None:
    assert 'class="production-pending-panel"' in INDEX
    assert ".production-pending-panel .table-wrap" in INDEX
    assert "overflow-x: hidden" in INDEX
    assert ".production-compact-two-lines" in INDEX
    assert "-webkit-line-clamp: 2" in INDEX
    assert 'class="production-pick-detail-line"' in INDEX
    assert "productionCompactPickLine(row)" in INDEX
    assert '<table class="production-table">' in INDEX
    assert ".ui-large .production-pending-panel .production-table" in INDEX
    assert "display: table; width: 100%; table-layout: fixed !important" in INDEX
    assert "display: table-row; min-width: 0" in INDEX
    assert "display: table-cell; min-height: 0" in INDEX
    assert 'v-if="productionMaterialUsageEnabled" style="width:18%">本单领料' in INDEX
    assert 'v-if="productionMaterialUsageEnabled" class="production-pick-cell"' in INDEX


def test_material_usage_is_edited_in_a_bounded_two_line_modal() -> None:
    assert "production-material-usage-modal" in INDEX
    assert "max-width: calc(100vw - 48px)" in INDEX
    assert "overflow-y: auto; overflow-x: hidden" in INDEX
    assert 'class="production-material-usage-line meta"' in INDEX
    assert 'class="production-material-usage-line edit"' in INDEX
    for field in (
        "actual_consumed_stock_quantity",
        "returned_intact_stock_quantity",
        "damaged_stock_quantity",
        "offcut_stock_quantity",
    ):
        assert f'v-model.number="item.{field}"' in INDEX
    assert "确认完整整张已退回" in INDEX
    assert "完整退回才恢复可用库存" in INDEX


def test_material_usage_payload_carries_exact_reservation_and_lot_version() -> None:
    payload = re.search(
        r"productionMaterialUsagePayload\(row\) \{(.*?)\n\s+\},\n"
        r"\s+productionLocation",
        INDEX,
        re.DOTALL,
    )
    assert payload is not None
    body = payload.group(1)
    for field in (
        "reservation_id",
        "inventory_lot_id",
        "expected_lot_version",
        "assigned_stock_quantity",
        "actual_consumed_stock_quantity",
        "returned_intact_stock_quantity",
        "damaged_stock_quantity",
        "offcut_stock_quantity",
        "variance_reason_code",
        "return_confirmed",
    ):
        assert field in body

    request = re.search(
        r"productionCompletionRequestItems\(rows\) \{(.*?)\n\s+\},\n"
        r"\s+async batchConfirmProduction",
        INDEX,
        re.DOTALL,
    )
    assert request is not None
    assert "material_usages:materialUsages" in request.group(1)


def test_normal_plan_stays_fast_but_differences_require_saved_review() -> None:
    assert "productionMaterialUsageReadyForCompletion(row)" in INDEX
    assert "productionMaterialUsageDraftReady(row)" in INDEX
    assert "请先核对本次用料差异" in INDEX
    assert "完整整张退回需要仓库执行权限" in INDEX
    assert "四项合计必须等于指派" in INDEX
    assert "await this.confirmProductionDirectRow(row);" in INDEX


def test_material_usage_feature_is_hidden_by_default_until_raw_warehouse_exists() -> None:
    assert "productionMaterialUsageEnabled: false" in INDEX
    assert "pending.data.material_usage_enabled === true" in INDEX
    assert "if (!this.productionMaterialUsageEnabled) return true;" in INDEX
    assert "if (!this.productionMaterialUsageEnabled) return [];" in INDEX
