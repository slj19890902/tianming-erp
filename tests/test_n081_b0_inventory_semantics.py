from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    inventory_age_warning,
    inventory_fifo_sort_key,
    normalize_stock_date_metadata,
)


def test_exact_stock_date_requires_matching_iso_source_text() -> None:
    assert normalize_stock_date_metadata(
        stock_date=date(2026, 7, 23),
        stock_date_accuracy="exact",
        stock_date_original_text=None,
    ) == ("exact", "2026-07-23")

    with pytest.raises(WarehouseInventoryError, match="原文与入库日期不一致"):
        normalize_stock_date_metadata(
            stock_date=date(2026, 7, 23),
            stock_date_accuracy="exact",
            stock_date_original_text="2026-07-22",
        )


@pytest.mark.parametrize("accuracy", ["estimated", "unknown"])
def test_non_exact_stock_date_retains_original_text(accuracy: str) -> None:
    assert normalize_stock_date_metadata(
        stock_date=date(2026, 7, 1),
        stock_date_accuracy=accuracy,
        stock_date_original_text="约 2026 年 7 月",
    ) == (accuracy, "约 2026 年 7 月")


def test_deliberate_unknown_date_gets_non_null_source_marker() -> None:
    assert normalize_stock_date_metadata(
        stock_date=date(2026, 7, 23),
        stock_date_accuracy="unknown",
        stock_date_original_text=None,
    ) == ("unknown", "未提供")


def test_unknown_stock_date_never_reports_precise_age() -> None:
    warning = inventory_age_warning(
        SimpleNamespace(
            stock_date=date(2020, 1, 1),
            stock_date_accuracy="unknown",
        ),
        today=date(2026, 7, 23),
    )

    assert warning.days is None
    assert warning.level == "unknown"
    assert warning.text == "入库日期不明，不能按精确库龄判断"


def test_estimated_stock_date_is_visibly_qualified() -> None:
    warning = inventory_age_warning(
        SimpleNamespace(
            stock_date=date(2026, 1, 1),
            stock_date_accuracy="estimated",
        ),
        today=date(2026, 7, 23),
    )

    assert warning.days == 203
    assert warning.level == "estimated"
    assert "估算" in (warning.text or "")


def test_python_fifo_places_unknown_technical_dates_after_trustworthy_dates() -> None:
    rows = [
        SimpleNamespace(
            id=1,
            stock_date=date(2020, 1, 1),
            stock_date_accuracy="unknown",
        ),
        SimpleNamespace(
            id=2,
            stock_date=date(2026, 7, 1),
            stock_date_accuracy="exact",
        ),
        SimpleNamespace(
            id=3,
            stock_date=date(2026, 6, 1),
            stock_date_accuracy="estimated",
        ),
    ]

    rows.sort(key=inventory_fifo_sort_key)

    assert [row.id for row in rows] == [3, 2, 1]


def test_pending_requisition_uses_the_shared_python_fifo_rule() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "app" / "api" / "requisition.py"
    ).read_text(encoding="utf-8")

    assert "inventory_lots.sort(key=inventory_fifo_sort_key)" in source


@pytest.mark.parametrize(
    ("days", "level"),
    [(364, None), (365, "attention"), (548, "handling"), (730, "cleanup")],
)
def test_exact_stock_date_keeps_existing_age_thresholds(
    days: int,
    level: str | None,
) -> None:
    warning = inventory_age_warning(
        SimpleNamespace(
            stock_date=date.fromordinal(date(2026, 7, 23).toordinal() - days),
            stock_date_accuracy="exact",
        ),
        today=date(2026, 7, 23),
    )

    assert warning.days == days
    assert warning.level == level
