from types import SimpleNamespace

import pytest

from app.services.sheet_cutting_settings import (
    SheetCuttingSettings, normalize_settings, component_settings,
    product_yield_mode, resolve_order_sheet_contract,
)
from app.services.sheet_cutting_contract import SheetCuttingContractError


def test_settings_distinguish_cutting_label_from_legacy_yield_projection():
    part = SheetCuttingSettings(2, 2, 3, True)
    assert part.cutting_mode == "一开四"
    assert part.legacy_yield_mode == "一开12"
    assert part.contract(340, 200).supplier_size_mm == (680, 400)
    assert product_yield_mode(SimpleNamespace(default_cutting_mode="一开四", sheet_cutting_settings={"schema_version": 2, "whole": part.to_dict()})) == "一开12"


def test_cover_base_separate_and_legacy_null_never_inferred():
    value = {"schema_version": 2, "cover": SheetCuttingSettings(2, 1).to_dict(),
             "base": SheetCuttingSettings(1, 3, 4, True).to_dict()}
    assert component_settings(value).output_per_sheet == 2
    assert component_settings(value, "base").output_per_sheet == 12
    assert component_settings(None) is None
    assert product_yield_mode(SimpleNamespace(default_cutting_mode="一开三")) == "一开三"


@pytest.mark.parametrize("value", [{}, {"schema_version": True, "whole": {}},
    {"schema_version": 2, "cover": {}}, {"schema_version": 2, "whole": {"length_parts": 2}},
    {"schema_version": 2, "whole": SheetCuttingSettings().to_dict(), "extra": {}}])
def test_incomplete_or_unknown_settings_rejected(value):
    with pytest.raises(SheetCuttingContractError):
        normalize_settings(value)


def test_override_can_change_only_cutting_not_mold_or_theory():
    item = SimpleNamespace(sheet_cutting_settings_snapshot={"schema_version": 2,
        "whole": SheetCuttingSettings(1, 1, 2, True).to_dict()},
        snapshot_report_length_mm=340, snapshot_report_width_mm=200)
    selected = SheetCuttingSettings(2, 2, 2, True).contract(340, 200)
    assert resolve_order_sheet_contract(item, proposal=selected.to_snapshot()) == selected
    for changed in (SheetCuttingSettings(2, 2, 3, True).contract(340, 200),
                    SheetCuttingSettings(2, 2, 2, True).contract(341, 200)):
        with pytest.raises(SheetCuttingContractError):
            resolve_order_sheet_contract(item, proposal=changed.to_snapshot())
